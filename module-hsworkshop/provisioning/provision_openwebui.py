#!/usr/bin/env python3
"""Additive OpenWebUI workshop preset provisioner (Task 4, ADR-0002).

Python stdlib only — runs inside the pinned OpenWebUI image with no extra
dependencies. Provisions exactly the two managed workshop presets
(``career-guide``, ``free-chat`` on ``qwen36-service``) through the
OpenWebUI REST API and nothing else:

  1. ``POST /api/v1/auths/signin``      — existing admin session (fail
     closed unless the response is a ``SessionUserResponse`` with
     ``role == "admin"``; MFA-challenge / pending-user variants fail).
  2. ``GET /api/v1/models/all``         — persisted records (read-only).
  3. ``POST /api/v1/models/import``     — additive changed-only import of
     the managed presets; unrelated/private models are never included.
     For CREATES the public-read grant is inserted with the model; for
     UPDATES of existing records the server's grants are preserved
     VERBATIM — OpenWebUI 0.12.5 import updates never replace grants.
  4. ``POST /api/v1/models/model/access/update`` — architect-approved
     addition (the ONLY extra mutating route, pinned exception, not a new
     auth policy), scoped to exactly the managed model ids
     ``career-guide``/``free-chat``: body ``{id, access_grants}``
     converges ACL drift to the exact public-read grant. The 200 response
     is ``ModelModel | null``; ``null`` is NOT treated as success — the
     mandatory re-read is decisive.

No full-sync endpoint, no DELETE, no DB access, no account reset or
promotion, no alternate APIs or account changes on failure. Unknown
schemas fail closed before any model write. A re-read happens after every
mutation attempt and before any retry; uncertain outcomes (network
errors, HTTP 5xx — the write may have committed) are never blindly
duplicated. Definite failures (HTTP 4xx, invalid request/schema) fail
closed immediately. A converged second run performs zero import and zero
access-update POSTs.

Environment (read-only inputs; credentials never come from argv and are
never logged — only counters are reported):

  OPENWEBUI_URL            base URL of OpenWebUI. HTTPS, or HTTP only for
                           documented in-cluster targets (``localhost``,
                           ``*.svc.cluster.local``, or a bare in-namespace
                           service name such as the Job's internal
                           ``http://openwebui:8080``). Userinfo, query, and
                           fragment are rejected.
  OPENWEBUI_ADMIN_EMAIL    admin email. Falls back to WEBUI_ADMIN_EMAIL
                           (the Task 2 bootstrap Secret key).
  OPENWEBUI_ADMIN_PASSWORD admin password. Falls back to
                           WEBUI_ADMIN_PASSWORD (Task 2 Secret key).
  PRESETS_PATH             path to the sanitized presets file (default:
                           ``configuration-models.json`` adjacent to this
                           script, cwd-independent).

Exit codes: 0 = provisioned (or already converged), 1 = failure,
2 = bad usage/environment.

Task 5 consumes exactly these env names.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# --- Managed scope (exact, per plan Task 4) -------------------------------

MANAGED_MODEL_IDS = frozenset({"career-guide", "free-chat"})
MANAGED_BASE_MODEL_ID = "qwen36-service"

# The exact architect-verified public-read grant (0.12.5 AccessGrantModel
# semantics): authenticated user wildcard READ. Not `anyone` (no-auth), not
# legacy `access_control`, not wildcard write.
PUBLIC_READ_GRANT = {
    "principal_type": "user",
    "principal_id": "*",
    "permission": "read",
}

# --- Schema allowlists (fail closed on anything else) ---------------------

PRESET_FIELDS = frozenset(
    {"id", "base_model_id", "name", "params", "meta", "is_active", "access_grants"}
)
META_FIELDS = frozenset(
    {
        "description",
        "capabilities",
        "defaultFeatureIds",
        "suggestion_prompts",
        "tags",
        "builtinTools",
    }
)
CAPABILITY_FIELDS = frozenset(
    {
        "file_context",
        "vision",
        "file_upload",
        "web_search",
        "image_generation",
        "code_interpreter",
        "citations",
        "status_updates",
        "builtin_tools",
    }
)
BUILTIN_TOOL_FIELDS = frozenset(
    {
        "time",
        "memory",
        "chats",
        "notes",
        "knowledge",
        "channels",
        "web_search",
        "image_generation",
        "code_interpreter",
    }
)
PARAMS_FIELDS = frozenset(
    {
        "system",
        "temperature",
        "top_p",
        "top_k",
        "num_ctx",
        "max_tokens",
        "frequency_penalty",
        "presence_penalty",
        "repeat_penalty",
        "seed",
        "stop",
        "reasoning_effort",
    }
)

# Fields of a server ModelResponse that the managed comparison looks at.
MANAGED_COMPARE_FIELDS = ("base_model_id", "name", "params", "is_active")

# Server-owned ModelResponse fields preserved verbatim in update forms
# (ownership, timestamps, effective write access, server revision). The
# open ``meta`` object is merged: managed preset keys are replaced,
# other server meta keys (e.g. a profile image) are preserved untouched.
RECORD_PASSTHROUGH_FIELDS = (
    "user_id",
    "user",
    "created_at",
    "updated_at",
    "write_access",
    "version_id",  # 0.12.5 ModelResponse: string | null, server-owned
)

# A server record may carry exactly the known ModelResponse fields; anything
# else is an unknown schema and fails closed BEFORE any model write.
RECORD_KNOWN_FIELDS = frozenset(PRESET_FIELDS) | frozenset(RECORD_PASSTHROUGH_FIELDS)

# --- HTTP bounds ------------------------------------------------------------

HTTP_TIMEOUT_SECONDS = 10
READ_ATTEMPTS = 3        # reads are side-effect free
WRITE_ATTEMPTS = 3       # writes: re-read + re-diff before every retry

SIGNIN_PATH = "/api/v1/auths/signin"
MODELS_ALL_PATH = "/api/v1/models/all"
MODELS_IMPORT_PATH = "/api/v1/models/import"
# Architect-approved addition — the ONLY extra mutating route (pinned
# exception, not a new auth policy), scoped to the managed model ids.
MODEL_ACCESS_UPDATE_PATH = "/api/v1/models/model/access/update"


class ProvisioningError(Exception):
    """Sanitized failure (no credentials, tokens, or response bodies)."""


class _UncertainWriteError(ProvisioningError):
    """A write outcome that cannot be confirmed failed (network error,
    timeout, or HTTP 5xx): the mutation MAY have committed, so callers
    must re-read state before any retry and never blindly duplicate."""


class _DefiniteFailureError(ProvisioningError):
    """A definitively rejected request (HTTP 4xx): retrying or switching
    endpoints cannot help — fail closed immediately."""


# --- Redirects: never followed (credential cross-host protection) ----------


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise ProvisioningError(f"redirect refused (HTTP {code})")


_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _request_json(method: str, url: str, payload, token: str | None):
    """One bounded HTTP call. Returns the parsed JSON body.

    Raises ProvisioningError with sanitized messages only (status codes and
    exception type names — never bodies, headers, tokens, or payloads).
    """
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        exc.close()
        message = f"HTTP {exc.code} from {method} {urlparse_path(url)}"
        if 500 <= exc.code <= 599:
            # Server-side failure: the write may have committed — uncertain.
            raise _UncertainWriteError(message) from None
        raise _DefiniteFailureError(message) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        # Connection-level failure: the write may have been delivered and
        # committed before the response was lost — treat as uncertain.
        raise _UncertainWriteError(f"network error ({type(reason).__name__})") from None
    try:
        return json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ProvisioningError(f"non-JSON response from {method} {urlparse_path(url)}") from None


def urlparse_path(url: str) -> str:
    return urllib.parse.urlsplit(url).path or "/"


# --- Base URL validation -----------------------------------------------------


def validate_base_url(base_url: str) -> str:
    """Validate and normalize the base URL (fail closed).

    HTTPS anywhere; HTTP only for documented in-cluster targets. No
    userinfo, query, or fragment; no path beyond the root.
    """
    if not base_url:
        raise ProvisioningError("base URL is empty")
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme not in ("https", "http"):
        raise ProvisioningError("base URL scheme must be http or https")
    if not parts.netloc:
        raise ProvisioningError("base URL has no host")
    if parts.username or parts.password:
        raise ProvisioningError("base URL must not contain userinfo")
    if parts.query or parts.fragment:
        raise ProvisioningError("base URL must not contain a query or fragment")
    if parts.path not in ("", "/"):
        raise ProvisioningError("base URL must not contain a path")
    hostname = (parts.hostname or "").lower()
    internal_http = (
        hostname == "localhost"
        or hostname.endswith(".svc.cluster.local")
        or hostname.endswith(".svc")
        or "." not in hostname
    )
    if parts.scheme == "http" and not internal_http:
        raise ProvisioningError("plain-HTTP base URL allowed only for in-cluster targets")
    return base_url.rstrip("/")


# --- Preset loading and normalization ----------------------------------------


def _fail_schema(detail: str):
    raise ProvisioningError(f"preset schema rejected: {detail}")


def _validate_tags(tags):
    """Managed ``meta.tags`` must be the canonical OpenWebUI form: a list
    of ``{name: str}`` dicts. A bare string tag (operator shorthand the
    server would normalize to ``{name: ...}``) fails closed rather than
    being silently transformed — the preset input must converge exactly."""
    if not isinstance(tags, list):
        _fail_schema("meta.tags must be a list")
    for tag in tags:
        if (
            not isinstance(tag, dict)
            or set(tag) != {"name"}
            or not isinstance(tag.get("name"), str)
            or not tag["name"]
        ):
            _fail_schema("meta.tags entries must be {name: str} objects")


def _validate_meta(meta) -> dict:
    if not isinstance(meta, dict):
        _fail_schema("meta must be an object")
    unknown = set(meta) - set(META_FIELDS)
    if unknown:
        _fail_schema(f"unknown meta field(s): {sorted(unknown)}")
    if "capabilities" in meta:
        caps = meta["capabilities"]
        if not isinstance(caps, dict):
            _fail_schema("meta.capabilities must be an object")
        unknown = set(caps) - set(CAPABILITY_FIELDS)
        if unknown:
            _fail_schema(f"unknown capability field(s): {sorted(unknown)}")
    if "builtinTools" in meta and not isinstance(meta["builtinTools"], dict):
        _fail_schema("meta.builtinTools must be an object")
    if "builtinTools" in meta:
        unknown = set(meta["builtinTools"]) - set(BUILTIN_TOOL_FIELDS)
        if unknown:
            _fail_schema(f"unknown builtinTools field(s): {sorted(unknown)}")
    for key in ("defaultFeatureIds", "suggestion_prompts"):
        # suggestion_prompts may be a list or explicit null (clearing).
        if key in meta and meta[key] is not None and not isinstance(meta[key], list):
            _fail_schema(f"meta.{key} must be a list or null")
    if "tags" in meta:
        _validate_tags(meta["tags"])
    if "description" in meta and not isinstance(meta["description"], str):
        _fail_schema("meta.description must be a string")
    return meta


def _validate_params(params) -> dict:
    if not isinstance(params, dict):
        _fail_schema("params must be an object")
    unknown = set(params) - set(PARAMS_FIELDS)
    if unknown:
        _fail_schema(f"unknown params field(s): {sorted(unknown)}")
    return params


def normalize_managed(model) -> dict:
    """Validate one preset and return its canonical managed form.

    Fails closed (ProvisioningError) on unknown fields, wrong types,
    non-managed ids, wrong base model, or any grant other than the exact
    public-read grant.
    """
    if not isinstance(model, dict):
        _fail_schema("preset must be an object")
    unknown = set(model) - set(PRESET_FIELDS)
    if unknown:
        _fail_schema(f"unknown field(s): {sorted(unknown)}")
    for required in ("id", "base_model_id", "name", "params", "meta", "access_grants"):
        if required not in model:
            _fail_schema(f"missing required field: {required}")
    if model["id"] not in MANAGED_MODEL_IDS:
        _fail_schema(f"model id is not managed: {model['id']!r}")
    if model["base_model_id"] != MANAGED_BASE_MODEL_ID:
        _fail_schema(f"base_model_id must be {MANAGED_BASE_MODEL_ID!r}")
    if not isinstance(model["name"], str) or not model["name"]:
        _fail_schema("name must be a non-empty string")
    is_active = model.get("is_active", True)
    if not isinstance(is_active, bool):
        _fail_schema("is_active must be a boolean")
    grants = model["access_grants"]
    if not isinstance(grants, list) or len(grants) != 1:
        _fail_schema("access_grants must be exactly the public-read grant")
    grant = grants[0]
    if not isinstance(grant, dict) or set(grant) != set(PUBLIC_READ_GRANT):
        _fail_schema("access_grants must be exactly the public-read grant")
    if any(grant.get(key) != value for key, value in PUBLIC_READ_GRANT.items()):
        _fail_schema("access_grants must be exactly the public-read grant")
    return {
        "id": model["id"],
        "base_model_id": model["base_model_id"],
        "name": model["name"],
        "params": _validate_params(model["params"]),
        "meta": _validate_meta(model["meta"]),
        "is_active": is_active,
        "access_grants": [dict(PUBLIC_READ_GRANT)],
    }


def load_presets(path) -> list:
    """Load and validate the sanitized presets file (sole input, read-only)."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except OSError:
        raise ProvisioningError("presets file could not be read") from None
    except ValueError:
        raise ProvisioningError("presets file is not valid JSON") from None
    if not isinstance(data, list) or not data:
        raise ProvisioningError("presets file must be a non-empty list")
    presets = [normalize_managed(model) for model in data]
    ids = [preset["id"] for preset in presets]
    if len(set(ids)) != len(ids):
        raise ProvisioningError("duplicate preset ids")
    if set(ids) != set(MANAGED_MODEL_IDS):
        raise ProvisioningError(
            f"presets must cover exactly the managed ids {sorted(MANAGED_MODEL_IDS)}"
        )
    return presets


# --- Server-record comparison (volatile fields ignored) ----------------------


def _grant_signature(grant) -> tuple | None:
    if not isinstance(grant, dict):
        return None
    return (
        grant.get("principal_type"),
        grant.get("principal_id"),
        grant.get("permission"),
    )


def _meta_view(meta) -> dict:
    """Comparable meta view: managed keys only, null-valued keys treated
    as absent on BOTH sides. A desired explicit ``None`` (clearing, e.g.
    defaultFeatureIds/suggestion_prompts) is written as ``None`` in the
    import form; once cleared the server reports the key absent or null,
    so both compare equal here. The pinned 0.12.5 ``ModelMeta.normalize_tags``
    converts ``tags: None -> []``, so an EMPTY tags list is also normalized
    to absent on both sides (a preset omitting tags converges with a server
    reporting ``[]`` or ``None``)."""
    if not isinstance(meta, dict):
        meta = {}
    view = {}
    for key, value in meta.items():
        if key not in META_FIELDS:
            continue
        if value is None:
            continue
        if key == "tags" and value == []:
            continue
        view[key] = value
    return view


def _server_managed_view(record) -> dict:
    """Extract the comparable managed view of a server ModelResponse.

    Volatile server-assigned fields (grant ``id``/``resource_type``/
    ``resource_id``/``created_at``, model timestamps, ``version_id``) are
    ignored.
    """
    grants = record.get("access_grants") or []
    if not isinstance(grants, list):
        grants = []
    return {
        "base_model_id": record.get("base_model_id"),
        "name": record.get("name"),
        "params": record.get("params") or {},
        "meta": _meta_view(record.get("meta")),
        "is_active": bool(record.get("is_active", False)),
        "grants": {_grant_signature(g) for g in grants},
    }


def _preset_view(preset: dict) -> dict:
    return {
        "base_model_id": preset["base_model_id"],
        "name": preset["name"],
        "params": preset["params"],
        "meta": _meta_view(preset["meta"]),
        "is_active": preset["is_active"],
        "grants": {_grant_signature(g) for g in preset["access_grants"]},
    }


def _config_matches(preset: dict, record: dict) -> bool:
    """Managed CONFIG fields match (base_model_id, name, params, meta,
    is_active)? Grants are compared separately."""
    view_preset = _preset_view(preset)
    view_server = _server_managed_view(record)
    return all(
        view_preset[key] == view_server[key]
        for key in ("base_model_id", "name", "params", "meta", "is_active")
    )


def _grants_match(preset: dict, record: dict) -> bool:
    """Grants semantically equal to the exact public-read grant?
    Volatile server fields (ids, resource ids, timestamps) are ignored."""
    return _preset_view(preset)["grants"] == _server_managed_view(record)["grants"]


def _validate_server_record(record) -> dict:
    """Fail closed on unknown server record schemas BEFORE any model write."""
    if not isinstance(record, dict):
        raise ProvisioningError("managed server record is not an object (fail closed)")
    unknown = set(record) - set(RECORD_KNOWN_FIELDS)
    if unknown:
        raise ProvisioningError(
            f"managed server record has unknown field(s): {sorted(unknown)} (fail closed)"
        )
    return record


def _import_form_for_missing(preset: dict) -> dict:
    """CREATE form (import INSERT): preset fields + the public-read grant
    (an insert carries its grants). No user_id is written — the server
    assigns the importing admin as owner; we never invent or write owner
    ids."""
    form = dict(preset)
    form["access_grants"] = [dict(PUBLIC_READ_GRANT)]
    return form


def _import_form_for_update(preset: dict, record: dict) -> dict:
    """CONFIG-update form (import UPDATE): the SERVER record verbatim
    (ownership ``user_id``/``user``, timestamps, ``write_access``,
    ``version_id``, unmanaged metadata preserved) with only the managed
    CONFIG fields replaced. The open ``meta`` object is MERGED: managed
    preset keys are replaced — an explicitly-absent managed key in the
    preset is written as ``None`` (clearing it, e.g. defaultFeatureIds or
    suggestion_prompts on free-chat) while other server meta keys (e.g. a
    profile image) stay untouched. Grants are NOT managed here: OpenWebUI
    0.12.5 import updates never replace grants, so the server's grant
    dicts are passed through VERBATIM (they cannot be changed by this
    route); ACL drift is converged separately via the architect-approved
    access-update endpoint.
    """
    form = dict(record)
    for field in MANAGED_COMPARE_FIELDS:
        form[field] = preset[field]
    server_meta = record.get("meta") if isinstance(record.get("meta"), dict) else {}
    merged_meta = dict(server_meta)
    for key in META_FIELDS:
        if key in preset["meta"]:
            # Explicit managed value, including None (clear the key).
            merged_meta[key] = preset["meta"][key]
        else:
            # Managed key ABSENT from the desired preset: write None so an
            # operator-added value is cleared (e.g. defaultFeatureIds or
            # suggestion_prompts on free-chat). Unmanaged server meta keys
            # are never touched (no broad deletion).
            merged_meta[key] = None
    form["meta"] = merged_meta
    # Grants pass through verbatim (import cannot replace them).
    form["access_grants"] = record.get("access_grants") or []
    return form


def _access_update_form(preset: dict) -> dict:
    """Architect-approved access-update body: exactly ``{id, access_grants}``
    for one managed model, converging its ACL to the public-read grant."""
    return {
        "id": preset["id"],
        "access_grants": [dict(PUBLIC_READ_GRANT)],
    }


# --- API helpers --------------------------------------------------------------


def _signin(base_url: str, email: str, password: str) -> str:
    """Sign in as the existing admin; return the session JWT (memory only).

    Fail closed unless the response is a SessionUserResponse whose ``role``
    is exactly ``admin`` (MFA-challenge and pending-user variants carry no
    usable admin session). The role is taken from the response body, never
    trusted from the token itself. Redirects are refused (a redirect must
    never carry credentials or a Bearer token to another host).
    """
    if not email or not password:
        raise ProvisioningError("admin credentials are missing")
    response = _request_json(
        "POST", base_url + SIGNIN_PATH, {"email": email, "password": password}, None
    )
    if not isinstance(response, dict):
        raise ProvisioningError("signin response was not a session object (fail closed)")
    if response.get("role") != "admin":
        raise ProvisioningError("signin did not return an admin session (fail closed)")
    token = response.get("token")
    if not isinstance(token, str) or not token:
        raise ProvisioningError("signin returned no session token (fail closed)")
    return token


def _read_models(base_url: str, token: str) -> list:
    """GET /api/v1/models/all with bounded retries (read-only, safe)."""
    last_error: ProvisioningError | None = None
    for _ in range(READ_ATTEMPTS):
        try:
            response = _request_json("GET", base_url + MODELS_ALL_PATH, None, token)
        except _DefiniteFailureError:
            raise
        except ProvisioningError as exc:
            last_error = exc
            continue
        if not isinstance(response, list):
            raise ProvisioningError("models/all did not return a list (fail closed)")
        return response
    raise last_error or ProvisioningError("models/all failed")


def _import_models(base_url: str, token: str, forms: list) -> None:
    """POST /api/v1/models/import with only the changed/missing forms.

    OpenWebUI 0.12.5 returns a boolean; a JSON ``false`` or a non-boolean
    body is treated as an unconfirmed outcome — the mandatory post-write
    re-read is decisive either way. Raises _UncertainWriteError /
    _DefiniteFailureError per the failure class.
    """
    response = _request_json(
        "POST", base_url + MODELS_IMPORT_PATH, {"models": forms}, token
    )
    if response is not True:
        # Not confirmed; the re-read after the call decides what happened.
        raise _UncertainWriteError("import was not confirmed (boolean false)")


def _update_model_access(base_url: str, token: str, preset: dict) -> None:
    """POST /api/v1/models/model/access/update — the architect-approved
    pinned exception, scoped to exactly the managed model ids.

    Body ``{id, access_grants}``; 200 response is ``ModelModel | null``.
    ``null`` is NOT treated as success — the re-read is decisive.
    """
    response = _request_json(
        "POST", base_url + MODEL_ACCESS_UPDATE_PATH, _access_update_form(preset), token
    )
    if not isinstance(response, dict) or response is None:
        # null or unexpected body: not verified — re-read decides.
        raise _UncertainWriteError("access update returned null/unexpected body")


# --- Provisioning ---------------------------------------------------------------


def provision(base_url: str, email: str, password: str, presets) -> dict:
    """Additively converge the managed presets. Returns counters only.

    Config drift and ACL drift are converged SEPARATELY:

    - Missing models are created via import (INSERT, grants included);
      after every import the state is re-read and validated BEFORE any
      access update (the access endpoint can create incomplete records,
      so it is never used for creation).
    - Config drift on existing records is converged via import UPDATE
      forms (grants pass through verbatim — 0.12.5 import never replaces
      them; ownership/user_id/user/timestamps/version_id preserved).
    - ACL drift (grants not semantically equal to the exact public-read
      grant — ids/timestamps ignored) is converged via the
      architect-approved access-update endpoint ONLY, never via import.

    A re-read happens after every mutation attempt and before any retry;
    uncertain write outcomes (network error, HTTP 5xx — the write may
    have committed) are never blindly duplicated. Definite failures
    (HTTP 4xx, invalid request/schema) fail closed immediately with no
    alternate API or account changes. Writes are bounded so the total
    request count stays well inside the Job's 600 s activeDeadline. A
    converged second run performs zero import and zero access POSTs.
    """
    base_url = validate_base_url(base_url)
    presets = [normalize_managed(preset) for preset in presets]
    if not presets:
        raise ProvisioningError("no presets to provision")

    token = _signin(base_url, email, password)

    counts = {
        "presets": len(presets),
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "import_batches": 0,
        "access_updates": 0,
        "retries": 0,
    }

    # Counter semantics: created/updated/unchanged describe the actions of
    # THIS run (the initial diff) and are frozen across verification
    # re-reads. A converged second run reports created=0, updated=0,
    # unchanged=all, import_batches=0, access_updates=0.
    def _existing(records) -> dict:
        existing = {}
        for record in records:
            if isinstance(record, dict) and record.get("id") in MANAGED_MODEL_IDS:
                _validate_server_record(record)
                existing[record["id"]] = record
        return existing

    def _diff(records):
        """Split into per-preset drift: (missing, config_forms, acl_ids)."""
        existing = _existing(records)
        missing = []
        config_forms = []
        acl_ids = []
        for preset in presets:
            record = existing.get(preset["id"])
            if record is None:
                missing.append(preset)
                continue
            if not _config_matches(preset, record):
                config_forms.append(_import_form_for_update(preset, record))
            if not _grants_match(preset, record):
                acl_ids.append(preset)
        return missing, config_forms, acl_ids

    def _converged(missing, config_forms, acl_ids):
        return not missing and not config_forms and not acl_ids

    def _write_round(action, done):
        """Perform one bounded mutation round. ``action`` is a callable
        returning the current (missing, config_forms, acl_ids) drift;
        ``done`` selects the component this phase is responsible for
        converging (phases run in order, so later phases' drift does not
        loop an earlier phase). Every path re-reads after the mutation and
        before any retry."""
        for attempt in range(WRITE_ATTEMPTS):
            if attempt > 0:
                counts["retries"] += 1
            missing, config_forms, acl_ids = action()
            if done(missing, config_forms, acl_ids):
                return True
        return False

    # --- Phase 1: create missing models (import INSERT) ------------------
    def _create_action():
        records = _read_models(base_url, token)
        missing, config_forms, acl_ids = _diff(records)
        if missing:
            counts["import_batches"] += 1
            # Uncertain outcomes (network/5xx — may have committed) fall
            # through to the re-read below; the loop re-diffs before any
            # retry so a write that landed is never blindly repeated.
            try:
                _import_models(base_url, token, [_import_form_for_missing(p) for p in missing])
            except (_UncertainWriteError,):
                pass
            except _DefiniteFailureError:
                raise
            # Mandatory re-read: validate the created records before any
            # access update (never trust the import confirmation alone).
            records = _read_models(base_url, token)
            missing, config_forms, acl_ids = _diff(records)
            return missing, config_forms, acl_ids
        return missing, config_forms, acl_ids

    missing, config_forms, acl_ids = _diff(_read_models(base_url, token))
    counts.update(
        created=len(missing),
        updated=len(config_forms) + len(acl_ids),
        unchanged=len(presets) - len(missing) - len(config_forms) - len(acl_ids),
    )
    if _converged(missing, config_forms, acl_ids):
        counts["verified"] = True
        return counts

    # Missing models first: create + validate via re-read (the access
    # endpoint can create incomplete records, so it must never be used
    # for creation).
    if missing:
        if not _write_round(_create_action, lambda m, c, a: not m):
            raise ProvisioningError("presets did not converge after bounded import retries")
        # Refresh the drift from a fresh validated re-read: the initial
        # diff (computed before the creates) is stale now — a created
        # record may still carry config or ACL drift (e.g. the server
        # filtered the insert's grants), which the next phases must see.
        missing, config_forms, acl_ids = _diff(_read_models(base_url, token))

    # --- Phase 2: config drift via import UPDATE (grants verbatim) -------
    def _config_action():
        records = _read_models(base_url, token)
        missing, config_forms, acl_ids = _diff(records)
        if missing:
            return missing, config_forms, acl_ids
        if config_forms:
            counts["import_batches"] += 1
            try:
                _import_models(base_url, token, config_forms)
            except _UncertainWriteError:
                pass
            except _DefiniteFailureError:
                raise
            records = _read_models(base_url, token)
            missing, config_forms, acl_ids = _diff(records)
        return missing, config_forms, acl_ids

    if config_forms:
        if not _write_round(_config_action, lambda m, c, a: not m and not c):
            raise ProvisioningError("presets did not converge after bounded import retries")
        # Refresh the drift after the config phase for the same reason:
        # ACL drift may have been revealed (or introduced) by the import
        # updates and must gate phase 3 against CURRENT state.
        missing, config_forms, acl_ids = _diff(_read_models(base_url, token))

    # --- Phase 3: ACL drift via the architect-approved access endpoint ---
    def _acl_action():
        records = _read_models(base_url, token)
        missing, config_forms, acl_ids = _diff(records)
        if missing or config_forms:
            return missing, config_forms, acl_ids
        for preset in acl_ids:
            counts["access_updates"] += 1
            try:
                _update_model_access(base_url, token, preset)
            except _UncertainWriteError:
                pass
            except _DefiniteFailureError:
                raise
            # Re-read after every mutation attempt.
            records = _read_models(base_url, token)
            missing, config_forms, acl_ids = _diff(records)
            if missing or config_forms:
                return missing, config_forms, acl_ids
        return missing, config_forms, acl_ids

    if acl_ids:
        if not _write_round(_acl_action, _converged):
            raise ProvisioningError("presets did not converge after bounded access-update retries")

    # Final verification re-read.
    missing, config_forms, acl_ids = _diff(_read_models(base_url, token))
    if _converged(missing, config_forms, acl_ids):
        counts["verified"] = True
        return counts
    raise ProvisioningError("presets did not converge")


# --- Entry point ------------------------------------------------------------------


def main() -> int:
    base_url = os.environ.get("OPENWEBUI_URL", "")
    email = os.environ.get("OPENWEBUI_ADMIN_EMAIL") or os.environ.get("WEBUI_ADMIN_EMAIL", "")
    password = os.environ.get("OPENWEBUI_ADMIN_PASSWORD") or os.environ.get(
        "WEBUI_ADMIN_PASSWORD", ""
    )
    presets_path = os.environ.get("PRESETS_PATH") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "configuration-models.json",
    )

    if not base_url:
        print("provision_openwebui: OPENWEBUI_URL is not set", file=sys.stderr)
        return 2
    if not email or not password:
        print(
            "provision_openwebui: admin credentials are not set "
            "(OPENWEBUI_ADMIN_EMAIL/PASSWORD or WEBUI_ADMIN_EMAIL/PASSWORD)",
            file=sys.stderr,
        )
        return 2

    try:
        presets = load_presets(presets_path)
        counts = provision(base_url, email, password, presets)
    except ProvisioningError as exc:
        # Counters and sanitized messages only — never credentials, JWTs,
        # payloads, or API error bodies.
        print(f"provision_openwebui: failed: {exc}", file=sys.stderr)
        return 1

    summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    print(f"provision_openwebui: ok ({summary})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
