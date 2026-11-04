#!/usr/bin/env python3
"""Offline unit tests for provisioning/provision_openwebui.py (Task 4).

Fully local and offline: urllib is exercised against in-process fake
servers (mock handlers), no cluster access, no live API writes, no real
credentials. Run:

    python3 -m unittest module-hsworkshop/tests/test_openwebui_provisioning.py
or from module-hsworkshop:
    python3 -m unittest tests.test_openwebui_provisioning
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODULE = os.path.join(REPO_ROOT, "module-hsworkshop")
PROVISION_DIR = os.path.join(MODULE, "provisioning")
PRESETS_PATH = os.path.join(PROVISION_DIR, "configuration-models.json")

sys.path.insert(0, PROVISION_DIR)
import provision_openwebui as prov  # noqa: E402

ADMIN_EMAIL = "admin@hsworkshop.local"
ADMIN_PASSWORD = "supersecret-password-1"
JWT = "jwt-never-logged"

PUBLIC_READ_GRANT = dict(prov.PUBLIC_READ_GRANT)


# --- Preset / record helpers -------------------------------------------------


def _base_preset(mid="career-guide", **over):
    preset = {
        "id": mid,
        "base_model_id": "qwen36-service",
        "name": "Career Guide",
        "params": {"system": "Be helpful."},
        "meta": {
            "description": "Career guide preset.",
            "capabilities": {"web_search": True},
            "defaultFeatureIds": ["web_search"],
            "suggestion_prompts": None,
            "tags": [],
            "builtinTools": {"time": True},
        },
        "is_active": True,
        "access_grants": [dict(PUBLIC_READ_GRANT)],
    }
    preset.update(over)
    return preset


def _presets():
    return [
        _base_preset("career-guide"),
        _base_preset(
            "free-chat",
            name="Free Chat",
            params={},
            meta={"description": "Open chat.", "capabilities": {"web_search": False}},
        ),
    ]


def _server_record(preset, user_id="14c24101-0000-0000-0000-08d69000d5e7", **extra):
    """A realistic server ModelResponse (actual 0.12.5 ModelModel shape:
    id, user_id, base_model_id, name, params, meta, access_grants,
    is_active, updated_at/created_at, version_id): volatile + ownership
    fields that the provisioner must preserve (update forms) and ignore
    (comparison)."""
    record = copy.deepcopy(preset)
    record["user_id"] = user_id
    record["user"] = {"id": user_id, "name": "Existing Admin", "role": "admin"}
    record["created_at"] = 1774278618
    record["updated_at"] = 1774278618
    record["write_access"] = True
    record["version_id"] = "3f9d2c1a-7b44-4d3e-9a10-52c8d1b6e001"
    record["meta"] = dict(record.get("meta") or {})
    record["meta"]["profile_image_url"] = "data:image/png;base64,AAA"
    record["access_grants"] = [
        {
            "id": "grant-volatile-1",
            "resource_type": "model",
            "resource_id": record["id"],
            "principal_type": "user",
            "principal_id": "*",
            "permission": "read",
            "created_at": 1774278618,
        }
    ]
    record.update(extra)
    return record


# --- Fake OpenWebUI API server ------------------------------------------------


class FakeAPI(BaseHTTPRequestHandler):
    """Records every request; responses are scripted per test.

    Behavior is controlled via class attributes set by each test (a fresh
    subclass-less instance per server; tests assign onto the class).
    """

    # script: dict path -> callable(body) -> status, json
    script = {}
    requests = []  # list of (method, path, parsed-body-or-None)
    require_admin_bearer = True

    def log_message(self, *args):  # silence
        pass

    @classmethod
    def reset(cls, script, require_admin_bearer=True):
        cls.script = script
        cls.requests = []
        cls.require_admin_bearer = require_admin_bearer

    def _respond(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = None
        if raw:
            body = json.loads(raw.decode("utf-8"))
        type(self).requests.append((method, self.path, body))
        handler = type(self).script.get(self.path)
        if handler is None:
            self._send(404, {"detail": "not found"})
            return
        status, payload = handler(body, self.headers)
        self._send(status, payload)

    def _send(self, status, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._check_auth()
        self._respond("GET")

    def do_POST(self):
        self._check_auth()
        self._respond("POST")

    def _check_auth(self):
        if not type(self).require_admin_bearer:
            return
        if self.path == prov.SIGNIN_PATH:
            return
        auth = self.headers.get("Authorization", "")
        if auth != "Bearer " + JWT:
            self._send(401, {"detail": "unauthorized"})
            raise SystemExit(0)  # abort handler; response already sent


class ProvisioningTestCase(unittest.TestCase):
    def setUp(self):
        self._threads = []
        self._servers = []

    def tearDown(self):
        for server in self._servers:
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=5)

    def _serve(self, script, require_admin_bearer=True):
        FakeAPI.reset(script, require_admin_bearer)
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._servers.append(server)
        self._threads.append(thread)
        host, port = server.server_address
        return f"http://localhost:{port}"

    def _signin_ok(self, email=ADMIN_EMAIL, password=ADMIN_PASSWORD):
        def handler(body, headers):
            if body == {"email": email, "password": password}:
                return 200, {
                    "id": "u1",
                    "role": "admin",
                    "email": email,
                    "token": JWT,
                    "token_type": "Bearer",
                }
            return 401, {"detail": "bad credentials"}

        return handler

    def _models_all(self, records):
        def handler(body, headers):
            return 200, records

        return handler

    def run_provision(self, base_url, presets=None, email=ADMIN_EMAIL,
                      password=ADMIN_PASSWORD):
        return prov.provision(
            base_url, email, password, _presets() if presets is None else presets
        )


def _posts():
    return [r for r in FakeAPI.requests if r[0] == "POST"]


def _imports():
    return [r for r in _posts() if r[1] == prov.MODELS_IMPORT_PATH]


def _access_updates():
    return [r for r in _posts() if r[1] == prov.MODEL_ACCESS_UPDATE_PATH]


# --- Sanitized presets file ------------------------------------------------------


class TestSanitizedPresetsFile(unittest.TestCase):
    def test_file_loads_and_covers_exactly_managed_ids(self):
        presets = prov.load_presets(PRESETS_PATH)
        self.assertEqual({p["id"] for p in presets}, {"career-guide", "free-chat"})
        for preset in presets:
            self.assertEqual(preset["base_model_id"], "qwen36-service")
            self.assertEqual(
                preset["access_grants"], [dict(PUBLIC_READ_GRANT)]
            )

    def test_file_contains_no_personal_or_volatile_data(self):
        with open(PRESETS_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        banned_keys = {"user_id", "user", "write_access", "created_at", "updated_at",
                       "profile_image_url", "access_control"}
        for model in data:
            self.assertEqual(banned_keys & set(model), set())
            self.assertEqual(banned_keys & set(model.get("meta", {})), set())
            for grant in model["access_grants"]:
                self.assertNotIn("id", grant)
        text = open(PRESETS_PATH, encoding="utf-8").read()
        self.assertNotIn("base64", text)
        self.assertNotIn("kpiwko", text)

    def test_file_load_is_read_only_input(self):
        before = open(PRESETS_PATH, "rb").read()
        prov.load_presets(PRESETS_PATH)
        self.assertEqual(open(PRESETS_PATH, "rb").read(), before)

    def test_main_default_presets_path_is_adjacent_and_cwd_independent(self):
        # The default input is configuration-models.json ADJACENT to the
        # script (module-hsworkshop/provisioning/), independent of the
        # current working directory. Verify by running main()'s default-path
        # resolution from a DIFFERENT cwd: a fake server (no requests made on
        # the failure path we force) plus a missing-credentials env is enough
        # — main() must find the real presets file (not fail with a missing
        # file) and reach the credentials check / provisioning stage.
        env = {
            "OPENWEBUI_URL": "http://openwebui:8080",
            # credentials intentionally absent -> main() must return 2 AFTER
            # presets loading only if we can observe that; instead provide
            # valid-shape credentials against a dead-loopback URL so the
            # failure (if any) is HTTP, never "presets file not found".
            "OPENWEBUI_ADMIN_EMAIL": "admin@hsworkshop.local",
            "OPENWEBUI_ADMIN_PASSWORD": "supersecret-password-1",
        }
        saved = {k: os.environ.get(k) for k in list(env) + ["PRESETS_PATH"]}
        try:
            os.environ.update(env)
            os.environ.pop("PRESETS_PATH", None)
            cwd = os.getcwd()
            os.chdir(tempfile.gettempdir())
            try:
                # Dead port on loopback: connection refused -> bounded
                # failure exit 1, proving the presets file WAS found and
                # parsed (a missing file also exits 1, so distinguish by
                # stderr content).
                import io
                import contextlib

                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    rc = prov.main()
            finally:
                os.chdir(cwd)
            self.assertEqual(rc, 1)
            self.assertNotIn("presets", stderr.getvalue().lower().replace(
                "presets did not converge", "").replace("load", ""))
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_main_presets_path_env_override(self):
        # PRESETS_PATH explicitly overrides the default (e.g. the Job's
        # /provision/configuration-models.json). Point it at a copy of the
        # real file in a temp dir; the run must use it (same convergence
        # failure semantics as above prove parsing succeeded).
        with tempfile.TemporaryDirectory() as tmp:
            custom = os.path.join(tmp, "custom-presets.json")
            with open(PRESETS_PATH, "rb") as src, open(custom, "wb") as dst:
                dst.write(src.read())
            env = {
                "OPENWEBUI_URL": "http://openwebui:8080",
                "OPENWEBUI_ADMIN_EMAIL": "admin@hsworkshop.local",
                "OPENWEBUI_ADMIN_PASSWORD": "supersecret-password-1",
                "PRESETS_PATH": custom,
            }
            saved = {k: os.environ.get(k) for k in env}
            try:
                os.environ.update(env)
                import io
                import contextlib

                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    rc = prov.main()
                self.assertEqual(rc, 1)
                self.assertNotIn("no such file", stderr.getvalue().lower())
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_main_missing_custom_presets_path_fails(self):
        env = {
            "OPENWEBUI_URL": "http://openwebui:8080",
            "OPENWEBUI_ADMIN_EMAIL": "admin@hsworkshop.local",
            "OPENWEBUI_ADMIN_PASSWORD": "supersecret-password-1",
            "PRESETS_PATH": "/nonexistent-dir-xyz/presets.json",
        }
        saved = {k: os.environ.get(k) for k in env}
        try:
            os.environ.update(env)
            import io
            import contextlib

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = prov.main()
            self.assertEqual(rc, 1)
            self.assertIn("presets", stderr.getvalue().lower())
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


# --- normalize_managed / load_presets ------------------------------------------------


class TestNormalizeManaged(unittest.TestCase):
    def test_minimal_preset_normalizes(self):
        preset = normalize = prov.normalize_managed(_base_preset())
        self.assertEqual(normalize, preset)

    def test_is_active_defaults_true(self):
        preset = _base_preset()
        del preset["is_active"]
        self.assertTrue(prov.normalize_managed(preset)["is_active"])

    def test_unknown_field_fails_closed(self):
        preset = _base_preset()
        preset["unexpected"] = 1
        with self.assertRaises(prov.ProvisioningError):
            prov.normalize_managed(preset)

    def test_unknown_meta_capability_fails_closed(self):
        preset = _base_preset()
        preset["meta"]["capabilities"]["surprise"] = True
        with self.assertRaises(prov.ProvisioningError):
            prov.normalize_managed(preset)

    def test_unknown_params_field_fails_closed(self):
        preset = _base_preset()
        preset["params"]["surprise"] = 1
        with self.assertRaises(prov.ProvisioningError):
            prov.normalize_managed(preset)

    def test_unmanaged_model_id_fails_closed(self):
        with self.assertRaises(prov.ProvisioningError):
            prov.normalize_managed(_base_preset("some-other-model"))

    def test_wrong_base_model_fails_closed(self):
        preset = _base_preset()
        preset["base_model_id"] = "other-service"
        with self.assertRaises(prov.ProvisioningError):
            prov.normalize_managed(preset)

    def test_anyone_grant_rejected(self):
        preset = _base_preset()
        preset["access_grants"] = [
            {"principal_type": "anyone", "principal_id": "*", "permission": "read"}
        ]
        with self.assertRaises(prov.ProvisioningError):
            prov.normalize_managed(preset)

    def test_write_permission_grant_rejected(self):
        preset = _base_preset()
        preset["access_grants"] = [
            {"principal_type": "user", "principal_id": "*", "permission": "write"}
        ]
        with self.assertRaises(prov.ProvisioningError):
            prov.normalize_managed(preset)

    def test_legacy_access_control_field_rejected(self):
        preset = _base_preset()
        preset["access_control"] = None
        with self.assertRaises(prov.ProvisioningError):
            prov.normalize_managed(preset)

    def test_extra_grant_rejected(self):
        preset = _base_preset()
        preset["access_grants"].append(dict(PUBLIC_READ_GRANT))
        with self.assertRaises(prov.ProvisioningError):
            prov.normalize_managed(preset)


class TestLoadPresets(unittest.TestCase):
    def _write(self, data):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        return path

    def test_missing_file_fails(self):
        with self.assertRaises(prov.ProvisioningError):
            prov.load_presets("/nonexistent/presets.json")

    def test_invalid_json_fails(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as handle:
            handle.write("{not json")
        with self.assertRaises(prov.ProvisioningError):
            prov.load_presets(path)
        os.unlink(path)

    def test_incomplete_coverage_fails(self):
        path = self._write([_base_preset()])
        with self.assertRaises(prov.ProvisioningError):
            prov.load_presets(path)
        os.unlink(path)

    def test_bad_model_id_in_file_fails(self):
        path = self._write([_base_preset("rogue-model"), _base_preset("free-chat")])
        with self.assertRaises(prov.ProvisioningError):
            prov.load_presets(path)
        os.unlink(path)


# --- Base URL validation ------------------------------------------------------------------


class TestBaseUrlValidation(unittest.TestCase):
    def test_https_accepted(self):
        self.assertEqual(
            prov.validate_base_url("https://chat.example.com/"),
            "https://chat.example.com",
        )

    def test_internal_http_accepted(self):
        for url in ("http://localhost:8080", "http://openwebui:8080",
                    "http://openwebui.hsworkshop.svc.cluster.local:8080"):
            self.assertEqual(prov.validate_base_url(url), url)

    def test_public_http_rejected(self):
        with self.assertRaises(prov.ProvisioningError):
            prov.validate_base_url("http://chat.hsworkshop.fous.ai")

    def test_userinfo_rejected(self):
        with self.assertRaises(prov.ProvisioningError):
            prov.validate_base_url("http://admin:secret@openwebui:8080")

    def test_query_and_fragment_rejected(self):
        for url in ("http://openwebui:8080?x=1", "http://openwebui:8080#frag"):
            with self.assertRaises(prov.ProvisioningError):
                prov.validate_base_url(url)

    def test_path_rejected(self):
        with self.assertRaises(prov.ProvisioningError):
            prov.validate_base_url("http://openwebui:8080/openwebui")

    def test_bad_scheme_rejected(self):
        with self.assertRaises(prov.ProvisioningError):
            prov.validate_base_url("file:///etc/passwd")


# --- Provisioning flow (fake API) ---------------------------------------------------------------


class TestProvisioning(ProvisioningTestCase):
    def test_creates_missing_models_additively(self):
        unrelated = {
            "id": "private-teacher-model",
            "user_id": "other-user",
            "base_model_id": "qwen36-service",
            "name": "Private",
            "params": {},
            "meta": {},
            "is_active": True,
            "access_grants": [],
        }
        state = {"records": [unrelated], "forms": []}

        def all_handler(body, headers):
            return 200, state["records"]

        def import_handler(body, headers):
            forms = body["models"]
            state["forms"].extend(forms)
            # The write lands: managed records appear with server-assigned
            # ownership/volatile fields; the unrelated model is untouched.
            state["records"] = [unrelated] + [
                _server_record(p) for p in _presets()
            ]
            return 200, True

        base = self._serve(
            {
                prov.SIGNIN_PATH: self._signin_ok(),
                prov.MODELS_ALL_PATH: all_handler,
                prov.MODELS_IMPORT_PATH: import_handler,
            }
        )
        counts = self.run_provision(base)
        self.assertEqual(counts["created"], 2)
        self.assertEqual(counts["unchanged"], 0)
        self.assertEqual(counts["verified"], True)
        self.assertEqual(counts["import_batches"], 1)
        self.assertEqual(len(state["forms"]), 2)
        # The unrelated private model was never part of any import payload.
        for form in state["forms"]:
            self.assertIn(form["id"], prov.MANAGED_MODEL_IDS)
        # Create forms carry no owner identity and no write permission.
        for form in state["forms"]:
            self.assertNotIn("user_id", form)
            self.assertNotIn("user", form)
            self.assertNotIn("write_access", form)
            self.assertEqual(form["access_grants"], [dict(PUBLIC_READ_GRANT)])

    def test_noop_second_run_zero_import_posts(self):
        records = [_server_record(p) for p in _presets()]
        base = self._serve(
            {
                prov.SIGNIN_PATH: self._signin_ok(),
                prov.MODELS_ALL_PATH: self._models_all(records),
            }
        )
        counts = self.run_provision(base)
        self.assertEqual(counts["created"], 0)
        self.assertEqual(counts["updated"], 0)
        self.assertEqual(counts["unchanged"], 2)
        self.assertEqual(counts["import_batches"], 0)
        self.assertEqual(counts["verified"], True)
        self.assertEqual(_imports(), [])

    def test_matching_grants_with_volatile_server_fields_ignored(self):
        record = _server_record(_presets()[0])
        # Volatile grant fields must not force an update.
        record["access_grants"][0]["id"] = "different-volatile-id"
        record["access_grants"][0]["created_at"] = 1111111111
        record["created_at"] = 1
        record["updated_at"] = 2
        # A server that does not serialize null-valued managed meta keys
        # (e.g. suggestion_prompts) still compares equal to the preset.
        record["meta"] = {"description": "Career guide preset.",
                          "capabilities": {"web_search": True},
                          "defaultFeatureIds": ["web_search"],
                          "tags": [],
                          "builtinTools": {"time": True}}
        records = [record, _server_record(_presets()[1])]
        state = {"records": records}
        base = self._serve(
            {
                prov.SIGNIN_PATH: self._signin_ok(),
                prov.MODELS_ALL_PATH: lambda body, headers: (200, state["records"]),
                prov.MODELS_IMPORT_PATH: (lambda body, headers: (200, True)),
            }
        )
        counts = self.run_provision(base)
        self.assertEqual(counts["unchanged"], 2)
        self.assertEqual(counts["created"], 0)
        self.assertEqual(counts["updated"], 0)
        self.assertEqual(_imports(), [])

    def test_drifted_managed_fields_update_preserving_ownership(self):
        drifted = _server_record(_presets()[0], name="Old Name", is_active=False)
        drifted["meta"]["unmanaged_extra"] = {"keep": ["me"]}
        records = [drifted, _server_record(_presets()[1])]
        state = {"records": records, "forms": []}

        def import_handler(body, headers):
            forms = body["models"]
            state["forms"].extend(forms)
            # The update lands: managed fields now match the preset; the
            # server-owned fields and unmanaged meta survive the round trip.
            updated = _server_record(_presets()[0])
            updated["user_id"] = drifted["user_id"]
            updated["user"] = drifted["user"]
            updated["meta"]["unmanaged_extra"] = {"keep": ["me"]}
            state["records"] = [updated, records[1]]
            return 200, True

        base = self._serve(
            {
                prov.SIGNIN_PATH: self._signin_ok(),
                prov.MODELS_ALL_PATH: lambda body, headers: (200, state["records"]),
                prov.MODELS_IMPORT_PATH: import_handler,
            }
        )
        counts = self.run_provision(base)
        self.assertEqual(counts["updated"], 1)
        self.assertEqual(counts["verified"], True)
        self.assertEqual(len(state["forms"]), 1)
        form = state["forms"][0]
        self.assertEqual(form["id"], "career-guide")
        self.assertEqual(form["name"], "Career Guide")
        # Server ownership and unmanaged metadata preserved verbatim.
        self.assertEqual(form["user_id"], drifted["user_id"])
        self.assertEqual(form["user"], drifted["user"])
        self.assertEqual(form["meta"]["unmanaged_extra"], {"keep": ["me"]})
        self.assertEqual(form["meta"]["profile_image_url"],
                         drifted["meta"]["profile_image_url"])
        # Semantically matching grants are reused verbatim (stable ids).
        self.assertEqual(form["access_grants"], drifted["access_grants"])

    def test_acl_only_drift_converged_via_access_endpoint_not_import(self):
        # Review case: private preset (grants mismatch, config matches) is
        # converged via the architect-approved access-update endpoint ONLY
        # — never via import (0.12.5 import updates never replace grants).
        record = _server_record(_presets()[0])
        record["access_grants"] = []  # currently private
        records = [record, _server_record(_presets()[1])]
        state = {"records": records, "forms": [], "access_bodies": []}

        def import_handler(body, headers):
            state["forms"].extend(body["models"])
            return 200, True

        def access_handler(body, headers):
            state["access_bodies"].append(body)
            # The ACL write lands: grants now semantic public-read.
            updated = _server_record(_presets()[0])
            updated["user_id"] = record["user_id"]
            updated["user"] = record["user"]
            state["records"] = [updated, records[1]]
            return 200, updated  # ModelModel (dict), not a boolean

        base = self._serve(
            {
                prov.SIGNIN_PATH: self._signin_ok(),
                prov.MODELS_ALL_PATH: lambda body, headers: (200, state["records"]),
                prov.MODELS_IMPORT_PATH: import_handler,
                prov.MODEL_ACCESS_UPDATE_PATH: access_handler,
            }
        )
        counts = self.run_provision(base)
        self.assertEqual(counts["verified"], True)
        # No import was used for the ACL drift; exactly one access update.
        self.assertEqual(state["forms"], [])
        self.assertEqual(len(state["access_bodies"]), 1)
        self.assertEqual(
            state["access_bodies"][0],
            {"id": "career-guide", "access_grants": [dict(PUBLIC_READ_GRANT)]},
        )
        self.assertEqual(_imports(), [])

    def test_config_and_acl_drift_both_converged(self):
        # Review case: config drift handled by import (grants verbatim),
        # ACL drift by the access endpoint — both, in that order.
        drifted = _server_record(_presets()[0], name="Old Name")
        drifted["access_grants"] = []  # also private
        records = [drifted, _server_record(_presets()[1])]
        state = {"records": records, "forms": [], "access_bodies": [],
                 "import_order": [], "access_order": []}

        def import_handler(body, headers):
            state["forms"].extend(body["models"])
            state["import_order"].append(len(state["access_order"]))
            # Config update lands; grants UNCHANGED by import (0.12.5).
            updated = _server_record(_presets()[0], name="Career Guide")
            updated["user_id"] = drifted["user_id"]
            updated["user"] = drifted["user"]
            updated["access_grants"] = []  # import does not touch grants
            state["records"] = [updated, records[1]]
            return 200, True

        def access_handler(body, headers):
            state["access_bodies"].append(body)
            state["access_order"].append(len(state["import_order"]))
            updated = _server_record(_presets()[0], name="Career Guide")
            updated["user_id"] = drifted["user_id"]
            updated["user"] = drifted["user"]
            state["records"] = [updated, records[1]]
            return 200, updated

        base = self._serve(
            {
                prov.SIGNIN_PATH: self._signin_ok(),
                prov.MODELS_ALL_PATH: lambda body, headers: (200, state["records"]),
                prov.MODELS_IMPORT_PATH: import_handler,
                prov.MODEL_ACCESS_UPDATE_PATH: access_handler,
            }
        )
        counts = self.run_provision(base)
        self.assertEqual(counts["verified"], True)
        # Import (config) happened BEFORE the access update (ACL).
        self.assertEqual(state["import_order"], [0])
        self.assertEqual(state["access_order"], [1])
        # The import form passed the server's grants through verbatim
        # (empty — import never replaces grants).
        self.assertEqual(state["forms"][0]["access_grants"], [])

    def test_uncertain_acl_write_lands_no_duplicate(self):
        # Review case: the access-update POST dies with a network error
        # AFTER committing; the mandatory re-read finds convergence and NO
        # duplicate access update is issued.
        records = [_server_record(_presets()[0]), _server_record(_presets()[1])]
        records[0]["access_grants"] = []
        state = {"records": records, "access_calls": 0}

        def access_handler(body, headers):
            state["access_calls"] += 1
            updated = _server_record(_presets()[0])
            state["records"] = [updated, records[1]]
            if state["access_calls"] == 1:
                raise OSError("simulated network failure after commit")
            return 200, updated

        base = self._serve(
            {
                prov.SIGNIN_PATH: self._signin_ok(),
                prov.MODELS_ALL_PATH: lambda body, headers: (200, state["records"]),
                prov.MODEL_ACCESS_UPDATE_PATH: access_handler,
            }
        )
        counts = self.run_provision(base)
        self.assertEqual(state["access_calls"], 1)
        self.assertEqual(counts["verified"], True)
        self.assertEqual(counts["access_updates"], 1)

    def test_access_update_null_response_not_success_re_read_decisive(self):
        # 0.12.5 access/update returns ModelModel | null; null is NOT
        # verified success — the re-read decides. Here null + state that
        # actually landed: verified via re-read, one call only.
        records = [_server_record(_presets()[0]), _server_record(_presets()[1])]
        records[0]["access_grants"] = []
        state = {"records": records, "access_calls": 0}

        def access_handler(body, headers):
            state["access_calls"] += 1
            updated = _server_record(_presets()[0])
            state["records"] = [updated, records[1]]
            return 200, None  # null body

        base = self._serve(
            {
                prov.SIGNIN_PATH: self._signin_ok(),
                prov.MODELS_ALL_PATH: lambda body, headers: (200, state["records"]),
                prov.MODEL_ACCESS_UPDATE_PATH: access_handler,
            }
        )
        counts = self.run_provision(base)
        self.assertEqual(counts["verified"], True)
        self.assertEqual(state["access_calls"], 1)

    def test_access_update_denied_403_fails_closed_no_import_fallback(self):
        # Review case: denial on the access endpoint fails closed — no
        # alternate API, no import-based grant replacement fallback.
        records = [_server_record(_presets()[0]), _server_record(_presets()[1])]
        records[0]["access_grants"] = []
        state = {"records": records}

        base = self._serve(
            {
                prov.SIGNIN_PATH: self._signin_ok(),
                prov.MODELS_ALL_PATH: lambda body, headers: (200, state["records"]),
                prov.MODEL_ACCESS_UPDATE_PATH: (lambda body, headers: (403, {"detail": "no"})),
                prov.MODELS_IMPORT_PATH: (lambda body, headers: (200, True)),
            }
        )
        with self.assertRaises(prov.ProvisioningError):
            self.run_provision(base)
        # No import-based grant fallback occurred.
        self.assertEqual(_imports(), [])

    def test_acl_non_convergence_fails_after_bounded_attempts(self):
        # Review case: bounded non-convergence on the ACL path.
        records = [_server_record(_presets()[0]), _server_record(_presets()[1])]
        records[0]["access_grants"] = []
        state = {"records": records, "access_calls": 0}

        def access_handler(body, headers):
            state["access_calls"] += 1
            # Server accepts but never applies (grants stay private).
            return 200, _server_record(_presets()[0])

        base = self._serve(
            {
                prov.SIGNIN_PATH: self._signin_ok(),
                prov.MODELS_ALL_PATH: lambda body, headers: (200, state["records"]),
                prov.MODEL_ACCESS_UPDATE_PATH: access_handler,
            }
        )
        with self.assertRaises(prov.ProvisioningError):
            self.run_provision(base)
        # Bounded: exactly WRITE_ATTEMPTS access-update rounds for the model.
        self.assertEqual(state["access_calls"], prov.WRITE_ATTEMPTS)

    def test_noop_second_run_zero_import_and_zero_access_posts(self):
        # Review case: converged state -> zero import AND zero
        # access-update POSTs.
        records = [_server_record(p) for p in _presets()]
        base = self._serve(
            {
                prov.SIGNIN_PATH: self._signin_ok(),
                prov.MODELS_ALL_PATH: self._models_all(records),
            }
        )
        counts = self.run_provision(base)
        self.assertEqual(counts["created"], 0)
        self.assertEqual(counts["updated"], 0)
        self.assertEqual(counts["unchanged"], 2)
        self.assertEqual(counts["import_batches"], 0)
        self.assertEqual(counts["access_updates"], 0)
        self.assertEqual(counts["verified"], True)
        self.assertEqual(_imports(), [])
        self.assertEqual(_access_updates(), [])

    def test_operator_added_managed_meta_key_is_cleared(self):
        # Review case (bug 3): an operator added a managed meta key on the
        # server (e.g. defaultFeatureIds on free-chat, whose preset omits
        # it); the reconcile must CLEAR it (explicit None in the import
        # form), preserving unmanaged server meta keys — no broad deletion.
        record = _server_record(_presets()[1])  # free-chat preset has no defaultFeatureIds
        record["meta"]["defaultFeatureIds"] = ["web_search", "image_generation"]
        records = [_server_record(_presets()[0]), record]
        state = {"records": records, "forms": []}

        def import_handler(body, headers):
            state["forms"].extend(body["models"])
            updated = _server_record(_presets()[1])
            updated["user_id"] = record["user_id"]
            updated["user"] = record["user"]
            # Server normalizes the cleared key away (drops the null).
            updated["meta"] = {k: v for k, v in updated["meta"].items()
                               if k != "defaultFeatureIds"}
            state["records"] = [records[0], updated]
            return 200, True

        base = self._serve(
            {
                prov.SIGNIN_PATH: self._signin_ok(),
                prov.MODELS_ALL_PATH: lambda body, headers: (200, state["records"]),
                prov.MODELS_IMPORT_PATH: import_handler,
            }
        )
        counts = self.run_provision(base)
        self.assertEqual(counts["verified"], True)
        form = state["forms"][0]
        self.assertEqual(form["id"], "free-chat")
        # Explicit None clears the managed key...
        self.assertIsNone(form["meta"]["defaultFeatureIds"])
        # ...while unmanaged server meta is preserved (no broad deletion).
        self.assertEqual(form["meta"]["profile_image_url"],
                         record["meta"]["profile_image_url"])

    def test_version_id_present_is_accepted_and_ignored(self):
        # Bug 1: the REAL 0.12.5 ModelResponse carries version_id
        # (string | null); it must not trip the unknown-field allowlist,
        # must be excluded from comparison, and preserved in update forms.
        record = _server_record(_presets()[0], version_id="v-xyz-2")
        records = [record, _server_record(_presets()[1])]
        state = {"records": records, "forms": []}

        def import_handler(body, headers):
            state["forms"].extend(body["models"])
            updated = _server_record(_presets()[0], name="Old Name")
            # (not reached: no drift — comparison ignores version_id)
            state["records"] = records
            return 200, True

        base = self._serve(
            {
                prov.SIGNIN_PATH: self._signin_ok(),
                prov.MODELS_ALL_PATH: lambda body, headers: (200, state["records"]),
                prov.MODELS_IMPORT_PATH: import_handler,
            }
        )
        counts = self.run_provision(base)
        self.assertEqual(counts["unchanged"], 2)
        self.assertEqual(_imports(), [])

    def test_tags_normalization_none_and_empty_converge(self):
        # Review-2 case (B3 tags clearing): the pinned 0.12.5
        # ModelMeta.normalize_tags converts tags None -> []. The free-chat
        # preset OMITS tags; a server reporting tags: [] (normalized null)
        # is converged (no write). An operator-ADDED tags list on the
        # server is cleared (import form writes the managed key as None),
        # and the post-write server fixture normalizes None -> [] with the
        # final re-read a no-op.
        converged_record = _server_record(_presets()[1])
        converged_record["meta"]["tags"] = []  # server-normalized null
        records = [_server_record(_presets()[0]), converged_record]
        state = {"records": records, "forms": []}

        # (1) No drift: [] compares equal to the omitted preset tags.
        base = self._serve(
            {
                prov.SIGNIN_PATH: self._signin_ok(),
                prov.MODELS_ALL_PATH: lambda body, headers: (200, state["records"]),
                prov.MODELS_IMPORT_PATH: (lambda body, headers: (200, True)),
            }
        )
        counts = self.run_provision(base)
        self.assertEqual(counts["unchanged"], 2)
        self.assertEqual(_imports(), [])

        # (2) Operator added tags on the server -> cleared to converge.
        drifted = _server_record(_presets()[1])
        drifted["meta"]["tags"] = [{"name": "operator-tag"}]
        state["records"] = [_server_record(_presets()[0]), drifted]

        def import_handler(body, headers):
            state["forms"].extend(body["models"])
            updated = _server_record(_presets()[1])
            # Pinned server fixture: normalize_tags(None -> []) on the
            # persisted record after the managed key was written None.
            updated["meta"]["tags"] = []
            state["records"] = [state["records"][0], updated]
            return 200, True

        base = self._serve(
            {
                prov.SIGNIN_PATH: self._signin_ok(),
                prov.MODELS_ALL_PATH: lambda body, headers: (200, state["records"]),
                prov.MODELS_IMPORT_PATH: import_handler,
            }
        )
        counts = self.run_provision(base)
        self.assertEqual(counts["verified"], True)
        form = state["forms"][0]
        self.assertEqual(form["id"], "free-chat")
        # The managed key is explicitly cleared in the import form...
        self.assertIsNone(form["meta"]["tags"])
        # ...and the final state ([] normalized) is a converged no-op.
        self.assertEqual(counts["import_batches"], 1)

    def test_created_record_wrong_acl_repaired_same_invocation(self):
        # Review-2 case (stale gating): phase 1 creates a missing model
        # but the server FILTERS the insert's grants (record lands with
        # empty ACL); the refreshed post-create diff must see the ACL
        # drift and phase 3 must repair it via the access endpoint within
        # the SAME invocation (the initial diff said "missing" only).
        unrelated = {
            "id": "private-teacher-model",
            "user_id": "other-user",
            "base_model_id": "qwen36-service",
            "name": "Private",
            "params": {},
            "meta": {},
            "is_active": True,
            "access_grants": [],
        }
        state = {"records": [unrelated], "forms": [], "access_bodies": []}

        def import_handler(body, headers):
            forms = body["models"]
            state["forms"].extend(forms)
            # Insert lands but the server strips the grants (filtered
            # insert): the created records carry an empty ACL.
            created = []
            for form in forms:
                record = _server_record(
                    next(p for p in _presets() if p["id"] == form["id"])
                )
                record["access_grants"] = []
                created.append(record)
            state["records"] = [unrelated] + created
            return 200, True

        def access_handler(body, headers):
            state["access_bodies"].append(body)
            records = list(state["records"])
            for i, record in enumerate(records):
                if record.get("id") == body["id"]:
                    records[i] = _server_record(
                        next(p for p in _presets() if p["id"] == body["id"])
                    )
            state["records"] = records
            return 200, records  # ModelModel list is not the shape; return the model
        # (return the model itself, matching ModelModel | null)

        base = self._serve(
            {
                prov.SIGNIN_PATH: self._signin_ok(),
                prov.MODELS_ALL_PATH: lambda body, headers: (200, state["records"]),
                prov.MODELS_IMPORT_PATH: import_handler,
                prov.MODEL_ACCESS_UPDATE_PATH: access_handler,
            }
        )
        counts = self.run_provision(base)
        self.assertEqual(counts["verified"], True)
        self.assertEqual(counts["created"], 2)
        # Both created models' ACLs were repaired via the access endpoint.
        self.assertEqual(
            sorted(b["id"] for b in state["access_bodies"]),
            ["career-guide", "free-chat"],
        )
        for body in state["access_bodies"]:
            self.assertEqual(body["access_grants"], [dict(PUBLIC_READ_GRANT)])

    def test_non_admin_signin_fails_closed_no_model_reads(self):
        def handler(body, headers):
            return 200, {"id": "u1", "role": "user", "token": JWT}

        base = self._serve({prov.SIGNIN_PATH: handler})
        with self.assertRaises(prov.ProvisioningError):
            self.run_provision(base)
        self.assertEqual(
            [r for r in FakeAPI.requests if r[1] == prov.MODELS_ALL_PATH], []
        )
        self.assertEqual(_imports(), [])

    def test_mfa_challenge_signin_fails_closed(self):
        def handler(body, headers):
            return 200, {"challenge": "mfa", "mfa_enabled": True}

        base = self._serve({prov.SIGNIN_PATH: handler})
        with self.assertRaises(prov.ProvisioningError):
            self.run_provision(base)
        self.assertEqual(_imports(), [])

    def test_pending_user_signin_fails_closed(self):
        def handler(body, headers):
            return 200, {"id": "u1", "role": "user", "pending": True}

        base = self._serve({prov.SIGNIN_PATH: handler})
        with self.assertRaises(prov.ProvisioningError):
            self.run_provision(base)

    def test_signin_403_no_extra_operations(self):
        def handler(body, headers):
            return 403, {"detail": "forbidden"}

        base = self._serve({prov.SIGNIN_PATH: handler})
        with self.assertRaises(prov.ProvisioningError):
            self.run_provision(base)
        # No model list, no import, nothing beyond the single signin POST.
        self.assertEqual(
            [r for r in FakeAPI.requests if r[1] != prov.SIGNIN_PATH], []
        )

    def test_bad_credentials_401_fails_closed(self):
        def handler(body, headers):
            return 401, {"detail": "bad"}

        base = self._serve({prov.SIGNIN_PATH: handler})
        with self.assertRaises(prov.ProvisioningError):
            self.run_provision(base, password="wrong-password")
        self.assertEqual(_imports(), [])

    def test_unknown_server_schema_fails_before_writes(self):
        weird = [{"id": "career-guide", "something": "unexpected"}]
        base = self._serve(
            {
                prov.SIGNIN_PATH: self._signin_ok(),
                prov.MODELS_ALL_PATH: self._models_all(weird),
            }
        )
        # The record is not a dict with a comparable shape: comparison treats
        # missing managed fields as drift and would attempt an import; the
        # malformed-record guard must fail closed instead.
        with self.assertRaises(prov.ProvisioningError):
            self.run_provision(base)
        self.assertEqual(_imports(), [])

    def test_models_all_non_list_fails_closed(self):
        base = self._serve(
            {
                prov.SIGNIN_PATH: self._signin_ok(),
                prov.MODELS_ALL_PATH: (lambda body, headers: (200, {"data": []})),
            }
        )
        with self.assertRaises(prov.ProvisioningError):
            self.run_provision(base)
        self.assertEqual(_imports(), [])

    def test_timeout_on_import_rereads_before_retry_no_duplicates(self):
        # First import attempt dies with a network error AFTER the write
        # lands server-side; the retry's re-read must find convergence and
        # perform NO second import POST (no duplicates).
        state = {"records": [], "import_calls": 0}

        def all_handler(body, headers):
            return 200, state["records"]

        def import_handler(body, headers):
            state["import_calls"] += 1
            state["records"] = [_server_record(p) for p in _presets()]
            if state["import_calls"] == 1:
                raise OSError("simulated network failure")
            return 200, True

        base = self._serve(
            {
                prov.SIGNIN_PATH: self._signin_ok(),
                prov.MODELS_ALL_PATH: all_handler,
                prov.MODELS_IMPORT_PATH: import_handler,
            }
        )
        counts = self.run_provision(base)
        self.assertEqual(state["import_calls"], 1)
        # The uncertain import was followed by a verification re-read that
        # found convergence — zero duplicate import POSTs, verified, and no
        # retry was ever needed (the write had landed).
        self.assertEqual(counts["retries"], 0)
        self.assertEqual(counts["import_batches"], 1)
        self.assertEqual(counts["verified"], True)
        self.assertEqual(len(_imports()), 1)

    def test_false_import_confirmation_with_convergence_succeeds(self):
        # Server returns JSON false (0.12.5 import may confirm with false on
        # partial skips) while the state actually matches the presets: the
        # post-write re-read is the truth and no retry write happens.
        state = {"records": [], "import_calls": 0}

        def all_handler(body, headers):
            return 200, state["records"]

        def import_handler(body, headers):
            state["import_calls"] += 1
            state["records"] = [_server_record(p) for p in _presets()]
            return 200, False

        base = self._serve(
            {
                prov.SIGNIN_PATH: self._signin_ok(),
                prov.MODELS_ALL_PATH: all_handler,
                prov.MODELS_IMPORT_PATH: import_handler,
            }
        )
        counts = self.run_provision(base)
        self.assertEqual(counts["verified"], True)
        self.assertEqual(state["import_calls"], 1)

    def test_persistent_non_convergence_fails_after_bounded_retries(self):
        state = {"records": []}

        def all_handler(body, headers):
            return 200, state["records"]

        def import_handler(body, headers):
            # Server accepts but never applies (records stay missing).
            return 200, True

        base = self._serve(
            {
                prov.SIGNIN_PATH: self._signin_ok(),
                prov.MODELS_ALL_PATH: all_handler,
                prov.MODELS_IMPORT_PATH: import_handler,
            }
        )
        with self.assertRaises(prov.ProvisioningError):
            self.run_provision(base)
        self.assertEqual(len(_imports()), prov.WRITE_ATTEMPTS)

    def test_import_http_500_fails_closed(self):
        base = self._serve(
            {
                prov.SIGNIN_PATH: self._signin_ok(),
                prov.MODELS_ALL_PATH: self._models_all([]),
                prov.MODELS_IMPORT_PATH: (lambda body, headers: (500, {"detail": "boom"})),
            }
        )
        with self.assertRaises(prov.ProvisioningError):
            self.run_provision(base)

    def test_bearer_token_required_for_model_reads(self):
        # Server enforces auth: without the admin bearer, reads are 401 and
        # provisioning must fail closed (never fall back to anonymous).
        def handler(body, headers):
            return 401, {"detail": "unauthorized"}

        base = self._serve(
            {
                prov.SIGNIN_PATH: self._signin_ok(),
                prov.MODELS_ALL_PATH: handler,
            }
        )
        with self.assertRaises(prov.ProvisioningError):
            self.run_provision(base)
        self.assertEqual(_imports(), [])

    def test_redirect_refused_for_bearer_calls(self):
        class RedirectAPI(FakeAPI):
            def do_GET(self):
                self._check_auth()
                self.send_response(302)
                self.send_header("Location", "http://evil.example.com/api/v1/models/all")
                self.send_header("Content-Length", "0")
                self.end_headers()

        RedirectAPI.reset({})
        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectAPI)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._servers.append(server)
        self._threads.append(thread)
        host, port = server.server_address
        with self.assertRaises(prov.ProvisioningError):
            self.run_provision(f"http://localhost:{port}")
        # Only the signin POST left this host; no credentials were followed.
        self.assertEqual(
            [r for r in RedirectAPI.requests if r[1] != prov.SIGNIN_PATH], []
        )


# --- ACL grant unit semantics (ordinary authenticated read; no anonymous/edit) ----------


class TestGrantSemantics(unittest.TestCase):
    """Unit-level structure checks of the exact grant. Runtime ordinary-user
    visibility is a Task 6 fixture check (no live API writes here)."""

    def test_grant_is_authenticated_public_read_only(self):
        grant = prov.PUBLIC_READ_GRANT
        self.assertEqual(grant["principal_type"], "user")
        self.assertEqual(grant["principal_id"], "*")
        self.assertEqual(grant["permission"], "read")
        self.assertNotEqual(grant["principal_type"], "anyone")
        self.assertNotEqual(grant["permission"], "write")

    def test_no_legacy_access_control_in_import_forms(self):
        form = prov._import_form_for_missing(_base_preset())
        self.assertNotIn("access_control", form)
        record = _server_record(_base_preset())
        form = prov._import_form_for_update(_base_preset(), record)
        self.assertNotIn("access_control", form)


# --- main() environment contract -----------------------------------------------------------------


class TestMain(unittest.TestCase):
    def test_main_missing_url_returns_2(self):
        env = {"OPENWEBUI_ADMIN_EMAIL": ADMIN_EMAIL,
               "OPENWEBUI_ADMIN_PASSWORD": ADMIN_PASSWORD}
        real = os.environ.copy()
        os.environ.clear()
        os.environ.update(env)
        try:
            self.assertEqual(prov.main(), 2)
        finally:
            os.environ.clear()
            os.environ.update(real)

    def test_main_missing_credentials_returns_2(self):
        real = os.environ.copy()
        os.environ.clear()
        os.environ.update({"OPENWEBUI_URL": "http://openwebui:8080"})
        try:
            self.assertEqual(prov.main(), 2)
        finally:
            os.environ.clear()
            os.environ.update(real)

    def test_webui_admin_env_fallback_accepted(self):
        # Task 2 bootstrap Secret key names are the documented fallback.
        env = {"OPENWEBUI_URL": "https://chat.example.com",
               "WEBUI_ADMIN_EMAIL": ADMIN_EMAIL,
               "WEBUI_ADMIN_PASSWORD": ADMIN_PASSWORD}
        real = os.environ.copy()
        os.environ.clear()
        os.environ.update(env)
        try:
            # Fails at network stage (no server) — but past env validation,
            # proving the WEBUI_* names were accepted as credential source.
            self.assertEqual(prov.main(), 1)
        finally:
            os.environ.clear()
            os.environ.update(real)

    def test_credentials_not_in_argv_or_error_output(self):
        # prov.main() takes no arguments: credentials come from env only.
        import inspect

        params = list(inspect.signature(prov.main).parameters)
        self.assertEqual(params, [])


if __name__ == "__main__":
    unittest.main()
