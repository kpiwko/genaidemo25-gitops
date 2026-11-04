# Headless OpenWebUI — Task 1 Compatibility Matrix (freeze)

- Date: 2026-11-03
- Status: Task 1 executed (read-only preflight). Runtime validation is
  **PENDING** (needs authenticated fixtures / approved runtime probes).
  No API writes, no login, no DB config dump, no auth credential reads
  were performed.
- Source of truth: running pods in `hsworkshop` on cluster
  `https://api.kpicl2-pool-rft8p.aws.rh-ods.com:6443` (verified exact
  server, no context switch), inspected via read-only `oc exec` of
  installed package files and FastAPI `app.openapi()`; plus public
  unauthenticated `GET /api/config` and GitHub API for immutable
  upstream commit resolution.

## 1. Deployed versions / image freeze

| Component | Manifest tag | Running imageID (digest) | Version evidence | Immutable upstream |
|---|---|---|---|---|
| OpenWebUI | `ghcr.io/open-webui/open-webui:latest` | `ghcr.io/open-webui/open-webui@sha256:0b399a8ba34558e181d316750455f2a95376a38cd60a61b9c942e138898944bd` | `/app/package.json` → `0.12.5`; `WEBUI_BUILD_HASH=f7ac7ee10962ddec0017f5c29b6740d9b9c5faca` | Commit `f7ac7ee10962ddec0017f5c29b6740d9b9c5faca` = release commit "0.12.5 (#33371)"; GitHub API `git/ref/tags/v0.12.5` resolves to the **same SHA** (annotated-tag object → commit `f7ac7ee…`). Tag: `v0.12.5`. |
| Pipelines | `ghcr.io/open-webui/pipelines:main` | `ghcr.io/open-webui/pipelines@sha256:b48e9bc338ce2be0acfbeff01810db72408a12f07739f9e3879c1f2b00952d6e` | `:main` floating tag (no version in package) | **UNRESOLVED**: container digest is not a git SHA; GitHub commit-API lookup for that SHA as a commit returned HTTP 500/404 (it is not a repo commit). Latest upstream `main` commit at check time was `c7121bd904c15954b458eb2184ba77c379936284` (2026-10-29) — cannot confirm it matches the digest. Freeze must be by **digest** (as recorded), not by tag/commit. |
| Langfuse Python SDK (in Pipelines pod) | n/a (filter frontmatter `requirements: langfuse>=5.0.0`) | n/a | `pip show langfuse` in pod → **5.0.4** (matches prior expectation) | Freeze SDK 5.0.4. Note: filter declares `>=5.0.0` (unbounded); Task 3's `requirements.lock` must pin exactly 5.0.4. |

OpenWebUI pod itself has **no** `langfuse` module (tracing lives only in
Pipelines filter) — confirmed.

## 2. API contracts (from installed 0.12.5 `app.openapi()`)

All routes verified present in the running version's OpenAPI schema:

| Route | Verdict | Evidence |
|---|---|---|
| `POST /api/v1/auths/signin` | **SUPPORTED** | Body `SigninForm {email: string, password: string}` (both required). 200 → `SessionUserResponse \| MfaChallengeResponse \| PendingUserResponse`. `SessionUserResponse` fields include `id, name, role (string), email, profile_image_url, token, token_type, expires_at`. Admin verification: check `role == "admin"` in `SessionUserResponse`; `MfaChallengeResponse`/`PendingUserResponse` variants must be treated as fail-closed non-admin outcomes. |
| `GET /api/v1/models/all` | **SUPPORTED** | Returns `ModelResponse[]` (persisted records incl. private/unrelated). |
| `POST /api/v1/models/import` | **SUPPORTED** | Body `ModelsImportForm {models: [object]}` (`additionalProperties: true` — additive changed-only list). Returns boolean. **Architect amendment (2026-11-04, review finding):** import UPDATES never replace grants — only INSERTs (creates) carry their grants; existing records' `access_grants` are untouched by an import update. Grant/ACL convergence for existing records therefore uses the architect-approved pinned exception below. |
| `POST /api/v1/models/model/access/update` | **SUPPORTED (architect-approved addition)** | The ONLY extra mutating route beyond the planned set — a pinned, grandfathered exception scoped to exactly the managed model ids `career-guide`/`free-chat`, NOT a new auth policy. Body `{id, access_grants}`; 200 returns `ModelModel \| null`; `null` is NOT treated as success — a re-read of `models/all` is decisive. |
| `GET /api/models` | **SUPPORTED** | Discovery; 200 schema opaque `{}` (runtime shape varies by caller role). |
| `POST /api/chat/completed` | **PRESENT but DEPRECATED** | Source (installed `main.py:2142`): "Deprecated: outlet filters now run inline during chat completion. Kept for backward compatibility with external integrations." Requires `get_verified_user` + chat ownership. **Finalization for the browser path is inline outlet execution, not this route** — Task 4/6 finalization evidence must target the inline outlet path; external `POST /api/chat/completed` remains as fallback. |

Note: public unauthenticated `/openapi.json`, `/api/openapi.json`, `/docs`,
`/api/docs` all serve the SPA HTML shell (200 `text/html`) — the schema is
**not** exposed unauthenticated on this deployment; extraction required
in-pod `app.openapi()` (performed read-only).

## 3. ACL representation — `AccessGrantModel` (exact, 0.12.5)

`ModelResponse` has **no `access_control` field**; it carries
`access_grants: AccessGrantModel[]`. Exact schema:

```
AccessGrantModel (all required):
  id: string
  resource_type: string        # e.g. 'model'
  resource_id: string
  principal_type: string       # 'user' | 'group' | 'anyone'  (access_grants.py:14-16)
  principal_id: string         # WILDCARD_PRINCIPAL_ID = '*'  (access_grants.py:17)
  permission: string           # 'read' | 'write'            (access_grants.py:173)
  created_at: integer
```

Public-read grant (exact, from installed source):

```
{principal_type: 'user', principal_id: '*', permission: 'read'}
```

Semantics (installed `access_grants.py`):
- `anyone` principal requires `principal_id == '*'` AND `permission == 'read'`
  (no-auth anyone-read, line 177).
- "Public" for authenticated users = `user:*:read` (line 301), which
  legacy-compat maps to `access_control: null` while preserving write grants.
- Empty grant list `[]` = private/owner-only.
- Import forms (`ModelAccessGrantsForm`) take
  `{id, name?, access_grants: [object]}` — grant dicts are
  `additionalProperties: true` objects shaped as above.
- **Architect amendment (2026-11-04):** grant comparison and convergence
  are SEMANTIC — `{principal_type, principal_id, permission}` tuples only;
  server-assigned `id`/`resource_type`/`resource_id`/`created_at` are
  volatile and ignored. ACL drift is converged via
  `POST /api/v1/models/model/access/update` with exactly
  `{id, access_grants: [public-read grant]}` (no anonymous `anyone`, no
  write permission, no legacy `access_control`).

**Plan's original `anyone/*/read` example was WRONG; corrected 2026-11-03
(architect-verified):** the exact public-read grant is
`{principal_type:'user', principal_id:'*', permission:'read'}` —
authenticated user wildcard read. It is NOT `anyone` (which is the no-auth
anyone-read semantic and must not be used here), NOT legacy
`access_control`, and NOT wildcard write. Imports must use the
grants-list representation only. Tests must cover: ordinary authenticated
read succeeds, no edit, anonymous denied.

## 4. `models/import` ownership semantics (installed 0.12.5 source)

`/app/backend/open_webui/routers/models.py` `import_models` (line 509+):
- Requires `admin` role **or** `workspace.models_import` permission; else 401.
- Non-admin overwrite requires `existing_model.user_id == user.id` **or**
  write access grant; else 401 `MODEL_ID_TAKEN` (fail closed).
- Admin may overwrite any existing model (writable set = all existing).
- Models with invalid `id` are skipped; `base_model_id == id` is healed to
  `null`; models referencing inaccessible knowledge files are **skipped**
  with a warning (partial-import skip semantics confirmed).
- Additive changed-only import is supported as designed: pass only
  changed/missing models; second no-op run passes `[]`-equivalent (no writes).
- **Architect amendment (2026-11-04, review finding):** import UPDATEs do
  NOT replace grants — only INSERTs (creates) carry their grants. Existing
  records' `access_grants` pass through untouched on import updates; ACL
  drift on existing records is converged via the architect-approved
  `POST /api/v1/models/model/access/update` (§2), never via import.
- **ModelResponse shape (review finding, 2026-11-04):** real 0.12.5
  records carry `version_id` (string \| null) alongside id/user_id/
  base_model_id/name/params/meta/access_grants/is_active/updated_at/
  created_at. `version_id` is server-owned: excluded from desired-state
  comparison, preserved verbatim in update forms.

**Verdict: SUPPORTED** for the ADR-0002 additive pattern (with the
grant-amendment caveat above).

## 5. Connection env / persistence (installed 0.12.5 `config.py`)

- `OPENAI_API_KEYS` (line 341): `;`-separated plural; falls back to singular
  `OPENAI_API_KEY` when empty/unset. Keys are split on `;` and stripped.
- `OPENAI_API_BASE_URLS` (line 347): `;`-separated plural; falls back to
  singular `OPENAI_API_BASE_URL`; empty entry defaults
  `https://api.openai.com/v1`.
- `OPENAI_API_CONFIGS` (line 355): JSON **object** env; invalid JSON or
  non-object is ignored with a warning. **Supported** in 0.12.5.
- **Ordering matches by index**: `OPENAI_API_KEYS[i]` pairs with
  `OPENAI_API_BASE_URLS[i]` (line 370 uses list index).
- Persisted config keys: `openai.api_keys`, `openai.api_base_urls`
  (DEFAULT_CONFIG, lines 2937-2938) — with persistent config enabled, DB
  values override env on subsequent starts.
- `ENABLE_PERSISTENT_CONFIG` (line 3365): default **True**; env →
  `Config.configure(enable_persistent=...)`.

## 6. Signup / admin bootstrap semantics (installed 0.12.5 source)

- `WEBUI_ADMIN_EMAIL`/`WEBUI_ADMIN_PASSWORD` (`env.py:832-833`) drive
  `create_admin_user` at startup (`main.py:401-404`): creates the admin
  **only if no users exist**, then — **side effect** —
  `await Config.upsert({'ui.enable_signup': False})` (signup disabled and
  **persisted to DB**).
- Signup route (`auths.py:860+`): when users exist, signup is gated by
  persisted `ui.enable_signup` (DB), not env `ENABLE_SIGNUP`. First admin
  is explicitly NOT gated on ENABLE_SIGNUP.
- **Live confirmation**: public `GET /api/config` shows
  `"enable_signup": false` although env `ENABLE_SIGNUP=true` — the DB
  persisted value wins (consistent with the bootstrap side effect having
  run on first start).
- **Correction (2026-11-03, architect-verified, supersedes the earlier
  claim that env cannot re-enable signup):** with
  `ENABLE_PERSISTENT_CONFIG=false`, `models/config.py` 136–150 and
  201–212 ignore persisted/in-memory config, so declared env DOES govern.
  First bootstrap (`main.py` 400–404) sets `enable_signup:false`; a second
  start with existing users skips bootstrap (`utils/auth.py` 641–643), so
  a declared `ENABLE_SIGNUP=true` CAN restore signup in that mode.
  Fixture proof (fresh isolated, second start): **PASS 2026-11-04, §13.3**.
  Because the live effective value is `false`, the deployment declares
  explicit `ENABLE_SIGNUP=false` alongside persistence-off to keep the
  manifest honest — no auth hooks, no signup-policy reopening in Task 2.
- Signup config key mapping: `'ENABLE_SIGNUP' → 'ui.enable_signup'`
  (`auths.py:114`).

## 7. Effective non-secret workshop settings (env, sanitized)

From the running OpenWebUI pod (non-secret values only):
`ENABLE_SIGNUP=true` (env; DB overrides — see §6),
`DEFAULT_USER_ROLE=user`, `ENABLE_OPENAI_API=true`,
`ENABLE_OLLAMA_API=false`, `ENABLE_EVALUATION_ARENA_MODELS=false`,
`ENABLE_RAG_WEB_SEARCH=true`, `RAG_WEB_SEARCH_ENGINE=searxng`,
`SEARXNG_QUERY_URL=http://searxng:8080/search?q=<query>&format=json`,
`RAG_EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2`,
`WEBUI_NAME=HS Workshop AI`,
`WEBUI_URL=https://chat.hsworkshop.fous.ai`,
`CORS_ALLOW_ORIGIN=<both ADR-0001 origins>`,
`WEBUI_SESSION_COOKIE_SECURE=true`,
`WEBUI_SESSION_COOKIE_SAME_SITE=lax`,
`LANGFUSE_HOST=http://langfuse:3000`,
`OPENAI_API_BASE_URL=http://guardrails-proxy.hsworkshop.svc.cluster.local:8080/v1`.
No plural `OPENAI_API_BASE_URLS`/`OPENAI_API_KEYS`/`OPENAI_API_CONFIGS`
env set today; connections beyond the singular guardrails URL are
Admin-Panel/DB persisted. **Resolved 2026-11-04 by the owner-admin inventory
(§13):** there is exactly **one** persisted OpenAI connection (the guardrails
proxy) holding a non-secret (`unused`) placeholder key, with
`api_configs` empty — no hidden extra providers.

**Credentials availability metadata (no values read/recorded):**
`openwebui-secret` envFrom supplies `WEBUI_ADMIN_EMAIL`,
`WEBUI_ADMIN_PASSWORD`, `WEBUI_SECRET_KEY`, `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY` into the pod env (presence observed in sanitized
key listing only). Bootstrap credentials for Task 2's Secret therefore
have an existing source to mirror; values were not read into any artifact.

## 8. Pipelines startup / valves / readiness (installed source)

- Startup: `PIPELINES_URLS` triggers module load from URL; per-pipeline
  frontmatter `requirements:` are installed via
  `pip install` (`install_frontmatter_requirements`, `main.py:123-150`) —
  the verified startup mechanism for the filter's `langfuse>=5.0.0`.
- Valves overlay: on load, `valves.json` next to the module is merged as
  `{**pipeline.valves.model_dump(), **valves_json}` — persisted valves
  override code defaults, missing keys keep defaults (`main.py:196-210`).
  `valves.json` is created empty if absent (line 190-195).
- Filter routes: `POST /{pipeline_id}/filter/inlet` and
  `/outlet` (also `/v1/...` variants, `main.py:595-647`).
- **`/api/chat/completed` is NOT a Pipelines route** — it is OpenWebUI's
  deprecated compatibility route (§2); OpenWebUI 0.12.5 runs outlet filters
  inline during chat completion.
- Readiness: `GET /` on 9099 (manifest probe). Authenticated filter discovery
  (`/api/pipelines` or equivalent) is **post-deploy acceptance** (§13.4) —
  it can only be exercised against the deployed revision, and it is not
  claimed here.

## 9. Git / cluster state (read-only)

- Branch `zijeme-it` (active live Argo tracking branch), up to date with
  `origin/zijeme-it`; `origin = git@github.com:kpiwko/genaidemo25-gitops.git`
  (fork, as expected). No fetch/switch performed.
- Cluster server verified exact:
  `https://api.kpicl2-pool-rft8p.aws.rh-ods.com:6443`, user `kpiwko`
  (credentials valid).
- Local untracked user files preserved untouched:
  `docs/adr/0002-...md`, `docs/plans/2026-11-03-headless-openwebui.md`.

## 10. PASS / STOP / PENDING summary

> **Architect-verified corrections (2026-11-03, planner authorized, compact).**
> > The earlier-version false statements in this document (§3 `anyone`
> > representation, §6 "env cannot restore signup", former STOP items 1–3)
> > have been corrected IN PLACE above; this block records only items with no
> > other home:
> > - (a) Source compatibility is COMPLETE. The fresh-isolated signup
> >   precedence fixture **PASSED 8/8 checks** (three starts, §13.3). The
> >   remaining runtime validation — ACL/public-read, Pipelines startup and
> >   filter discovery, retained provisioning Job, Langfuse tracing,
> >   no-op second sync, existing user/chat data, both TLS origins — is
> >   controlled **POST-DEPLOY acceptance** (§13.4) and is NOT proven here.
> > - (b) Freeze values are final — OpenWebUI 0.12.5, commit
> >   `f7ac7ee10962ddec0017f5c29b6740d9b9c5faca`, digest
> >   `sha256:0b399a8ba34558e181d316750455f2a95376a38cd60a61b9c942e138898944bd`;
> >   Pipelines digest
> >   `sha256:b48e9bc338ce2be0acfbeff01810db72408a12f07739f9e3879c1f2b00952d6e`
> >   (no further digest→commit lookup); Langfuse SDK exactly 5.0.4.
> > - (e) Other stored effective config requires an owner-sanitized inventory
> >   before deploy — env list alone is insufficient (**hard deploy gate**
> >   for `ENABLE_PERSISTENT_CONFIG=false`; runbook records it as an
> >   owner-admin input). **Inventory DONE 2026-11-04 (§13); both named
> >   decisions closed locally (§13.1), whole-config comparison PASS
> >   (§13.2: 434/434 keys, 0 undeclared customizations).** What remains
> >   before a sync is the admin recovery point / sanitized-baseline
> >   prerequisite, which is **UNCONFIRMED** as of §13.4, plus post-deploy
> >   acceptance.
> > - (h) SDK 5.0.4 is the actual installed version; the Task 3 hash lock is
> >   tested against the pinned image — if an exact lock requires an
> >   unrelated gRPC dep or a new image build, STOP (Task 3 gate).
> > - (i) The new `openwebui-admin-bootstrap-secret` is local creds metadata
> >   of the existing admin only — it is NOT permission to read, copy, or
> >   reset anything; the existing `openwebui-secret` keys are not read.

**PASS (source-verified, ready for Task 2):**
- Version freeze: OpenWebUI 0.12.5 (digest + tag `v0.12.5` + commit
  `f7ac7ee…` all consistent), Pipelines digest
  `sha256:b48e9bc…` (freeze by digest), Langfuse SDK 5.0.4.
- `signin` / `models/all` / `models/import` / `GET /api/models` all
  supported with documented shapes; admin-role check via
  `SessionUserResponse.role`.
- Import ownership/write-access enforcement and partial-import skip
  semantics support the additive changed-only pattern.
- ACL: exact `AccessGrantModel` shape; public-read grant =
  `{principal_type:'user', principal_id:'*', permission:'read'}` —
  authenticated read only; NOT `anyone` (no-auth), NOT legacy
  `access_control`, NOT wildcard write. Tests: ordinary authenticated
  read, no edit, anonymous denied. Use the grants-list representation only.
- Plural connection env (`OPENAI_API_BASE_URLS`/`OPENAI_API_KEYS`,
  index-paired; `OPENAI_API_CONFIGS` JSON object) supported.
- `ENABLE_PERSISTENT_CONFIG` semantics confirmed; admin-env bootstrap
  signup-disabling side effect confirmed in source **and** live
  (`enable_signup:false` persisted).

**STOP / architect decisions — ALL RESOLVED 2026-11-03 (architect-verified
corrections; planner authorized):**
1. ~~Signup restore mechanism (Task 2)~~ RESOLVED: the earlier claim that
   env `ENABLE_SIGNUP` cannot re-enable signup once the DB has
   `ui.enable_signup=false` was FALSE under `ENABLE_PERSISTENT_CONFIG=false`
   — env governs in that mode (models/config 136–150, 201–212), and a
   second start with existing users skips the bootstrap side effect
   (utils/auth 641–643), so declared env CAN restore. Task 2 therefore
   declares explicit `ENABLE_SIGNUP=false` (matching the live effective
   policy — no auth hooks, no reopening) together with persistence-off.
   Fixture proof of the second-start restore path is **CLOSED — PASS
   2026-11-04 (§13.3, 8/8 checks over three starts of the pinned digest)**.
2. ~~Pipelines upstream commit~~ RESOLVED as not-needed: freeze by digest
   `sha256:b48e9bc…` (no further digest→commit lookup).
3. ~~`/api/chat/completed` deprecation~~ RESOLVED: outlet filters run
   inline in 0.12.5; finalization evidence targets the inline outlet path
   (browser-observed completed traces). Never add a routine extra
   finalizer on the deprecated route (duplicate-finalization risk).

**PENDING (separately approved runtime work, Task 6):**
- **Post-deploy acceptance only — not yet run, not proven (§13.4):**
  authenticated signin/role on the deployed revision, `models/all` + import
  behavior + discovery shape, ACL public-read / no-write / anonymous-denied,
  persisted valves overlay on the live PVC, Pipelines startup and
  authenticated filter discovery, retained `openwebui-workshop-provision` Job
  success, Langfuse tracing (delayed-readiness invalid-key fixture), no-op
  second sync, existing designated user/chat data unchanged, both TLS
  origins / cookies / Route. These need the deployment, so they cannot be
  closed pre-sync.
- Persisted connections/config inventory beyond env — **DONE 2026-11-04,
  see §13** (owner-admin authenticated, sanitized). Result: inventory
  complete; the configuration comparison is **PASS** (§13.2, 434/434 keys,
  0 undeclared customizations). The search-flip and code-exec-default items
  that this bullet previously described as an owner decision are closed
  (§13.1) and no owner decision was required.
- **Admin recovery point / sanitized baseline — UNCONFIRMED (§13.4).** The
  plan's Task 6 prerequisite is not yet evidenced: no OpenWebUI
  `VolumeSnapshot` exists in `hsworkshop` and no project backup document is
  named. Absence of that one snapshot kind does not prove no backup exists.
  This is an owner input (confirm externally managed backup) or an approved
  bounded backup step — not something this unit may waive or perform.
- Check basis (unchanged): OpenWebUI 0.12.5 was checked against **installed
  source of the running image only** (no Context7/upstream-docs cross-check
  was performed within budget; installed source is the stronger evidence and
  matches tag `v0.12.5`).

No API writes, no sign-in, no DB dumps, no secret value reads, no
cluster mutations, and no Git writes were performed during Task 1.

## 11. Task 3 implementation record (local, 2026-11-03)

- **Dependency lock (feasible, verified):** the pinned Pipelines image's
  `start.sh` supports `PIPELINES_REQUIREMENTS_PATH` → `pip install -r` before
  module load; pip 24.0 enforces `--hash=sha256:` entries in that mode even
  without `--require-hashes` (tampered-hash dry-run fails hard). Lock =
  langfuse 5.0.4 + 8 OpenTelemetry packages (api/sdk/proto/
  exporter-otlp-proto-http/exporter-otlp-common/exporter-otlp-proto-common/
  exporter-http-transport/semantic-conventions), all sha256-pinned in
  `module-hsworkshop/pipelines/requirements.lock`. The pinned image
  preinstalls langfuse 3.2.8; the delta installs exactly the 9 packages
  above — grpcio and all other deps are already satisfied, NOT upgraded, and
  no image build is required (Task 3 STOP gate cleared).
- **Managed valves:** after every persisted-valves overlay (load and update),
  the filter re-asserts `pipelines=["*"]`, `priority=0`, `debug=false` AND
  host/secret_key/public_key from the pod environment
  (Secret-backed). Missing env credentials fail startup closed
  (`MissingEnvCredentialsError`); there is NO personal-cloud fallback host.
- **Startup auth:** hard 300s total deadline; the request uses the pinned
  SDK's own mechanism with EXPLICIT bounds — `client.api.projects.get(
  request_options={"timeout_in_seconds": min(30, remaining),
  "max_retries": 0})` (verified supported by the 5.0.4 wheel's
  `RequestOptions` TypedDict and `projects.get` signature; `auth_check()`
  itself passes no request options and would inherit Fern's internal
  retries + Retry-After honoring, which could overshoot the deadline).
  auth_check-equivalent semantics per the 5.0.4 source: success = the
  projects call returned with a non-empty project list. Empty list fails
  closed immediately (same meaning as auth_check's "no project found").
  Invalid-credential classification is EXACT and TYPED: only Fern ApiError
  `status_code` 401/403 (verified attribute on the 5.0.4 ApiError); no
  substring matching, so a 503 whose body/headers contain "401"/"403"
  retries instead of failing. Transient errors retried; failed attempt
  clients are released (`shutdown()`), the successful client is kept.
  Invalid credentials raise immediately and fail process startup (Pipelines
  lifespan propagates module `on_startup` exceptions). Runtime
  (valves-update) reinit remains fail-open.
- **Operational note:** the Pipelines pod requires real Langfuse project
  keys (Secret-backed env `LANGFUSE_HOST`/`LANGFUSE_SECRET_KEY`/
  `LANGFUSE_PUBLIC_KEY`). Missing or placeholder credentials fail startup
  BY DESIGN (fail-closed) — the pod crash-loops until valid keys are
  provided; it never silently runs untraced at startup nor falls back to
  the personal Langfuse cloud.
- **Manifest:** digest-pinned image (init + main), `PIPELINES_URLS` removed,
  init container copies ONLY the managed `.py` to the existing PVC,
  `PIPELINES_API_KEY` from `openwebui-connections-secret` (matches Task 2),
  startupProbe (6 min) + readinessProbe (authenticated `check_ready.py`),
  no liveness probe.
- **ConfigMap generation:** nested `pipelines/kustomization.yaml` holds the
  `configMapGenerator` with LOCAL file sources; the base includes
  `../pipelines` as a directory resource. Standard kustomize load
  restrictions remain enabled (no flags, no builder-default assumptions).
  Content-hash name observed in render:
  `pipelines-langfuse-filter-c2fgc85527`.
- **Validation (local, offline):** `tests/test_langfuse_pipeline.py` 42/42
  PASS (typed SDK-like ApiError fakes; pinned-SDK request-options proof:
  max_retries=0 and timeout≤remaining asserted on the actual call; 503 with
  misleading 401/403 substrings proven transient); standard `kustomize
  build` renders: base 40 docs (CM
  `pipelines-langfuse-filter-ffhm9kh76d`), overlay 45 docs,
  certificate-only 2 docs; `tests/run-tests.sh` 50 PASS / 3 FAIL (the 3 are
  the pre-existing Task 2 baseline stale assertions owned by Task 5).
- **Remaining gates (not performed by scope):** live cluster mutation/deploy;
  runtime fixture proofs (in-pod version lock, PVC valves startup) are
  Task 6.

## 12. Task 5 implementation record (local, 2026-11-03 — COMPLETE)

**Architect + planner amendment (executed):** the sole canonical presets
input was MOVED, content unchanged, from
`module-hsworkshop/configuration-models.json` to
`module-hsworkshop/provisioning/configuration-models.json` — no duplicate,
no symlink. This made both generator file sources local to a nested
`provisioning/` kustomization root, resolving the earlier generator-layout
blocker with standard kustomize load restrictions fully enabled (no flags,
no builder-default assumptions; the containment-cycle/symlink/file-include
rejections recorded during the blocked investigation are superseded by this
amendment and are no longer live constraints).

**Implemented:**

- `provisioning/kustomization.yaml` (new): generator-only nested
  kustomization (no resources — cycle-free); `configMapGenerator` name
  `openwebui-workshop-provision` (frozen name; the Job sharing the same
  base name is legal — different kinds), namespace `hsworkshop`, local
  files `provision_openwebui.py` + `configuration-models.json`. The
  generated name is content-hashed; observed hash suffix in the local
  render: `cd2gcmtk89` (rolls on any input change).
- `install/openwebui-provisioning.yaml` (new): PostSync Job
  `openwebui-workshop-provision` — `argocd.argoproj.io/hook: PostSync` +
  `hook-delete-policy: BeforeHookCreation` ONLY (last result retained for
  audit; no HookSucceeded/HookFailed deletion); existing `hsworkshop-anyuid`
  SA with `automountServiceAccountToken: false` (no RBAC added — the Job
  talks only to the OpenWebUI HTTP API); the SAME pinned OpenWebUI 0.12.5
  digest image as the Deployment with explicit
  `python /provision/provision_openwebui.py`; env exactly
  `OPENWEBUI_URL=http://openwebui:8080` (internal, allowed by the Task 4
  base-URL validator), `WEBUI_ADMIN_EMAIL`/`WEBUI_ADMIN_PASSWORD` via
  secretKeyRef to the Task 2 `openwebui-admin-bootstrap-secret` (matching
  the provisioner's fallback key names), `PRESETS_PATH=
  /provision/configuration-models.json`; single readOnly ConfigMap volume
  mount; no PVC mount; `restartPolicy: Never`, `backoffLimit: 3`,
  `activeDeadlineSeconds: 600`; resources requests=limits=cpu 100m /
  memory 128Mi (non-root container, read-only ConfigMap files only).
- `install/kustomization.yaml`: lists `openwebui-provisioning.yaml` and
  includes `../provisioning` as a directory resource — the same standard
  pattern as the Task 3 `../pipelines` include. Kustomize rewrites the Job's
  volume reference to the generated hash name.
- `provisioning/provision_openwebui.py`: default `PRESETS_PATH` is now the
  JSON adjacent to the script (cwd-independent); the `PRESETS_PATH` env
  override is preserved.
- `tests/test_headless_manifests.py` (new): 24 offline tests covering the
  Job spec (hook policy, SA/automount, no extra RBAC, bounded spec, pinned
  image + explicit python, exact env contract, readOnly CM mount, no PVC,
  resources), the hash CM (both sole inputs present, content equals the
  Git-tracked files, Job reference hash-rewritten, input change rolls the
  hash and the Job reference in a sandbox copy), preservation (PVC set and
  Deployment wiring, plural URLs, ENABLE_SIGNUP=false, overlay adds exactly
  its 5 custom-domain objects, provisioning Job/CM identical in base and
  overlay, certificate-only exactly 2 docs, semantic base independence, no
  plaintext secrets), and wiring (install resource list, generator
  local-files-only, sole-input uniqueness across the module).
- `tests/test_openwebui_provisioning.py`: presets path constant repointed
  to `provisioning/`; 3 new tests (default path is adjacent and
  cwd-independent; `PRESETS_PATH` env override; missing custom path fails).
- `tests/run-tests.sh`: mirror extended with a `provisioning/` copy;
  Section 1 replaced the whole-base-equals-HEAD assertion with semantic
  base independence (plural URLs, no singular competitor env,
  `ENABLE_SIGNUP=false`, `ENABLE_PERSISTENT_CONFIG=false`, provisioning
  Job + hash CM wired and hash-rewritten, and the exact parsed PVC set
  {openwebui-data 5Gi, pipelines-data 2Gi, postgres-data, clickhouse-data,
  minio-data} with Deployment claimName wiring); Section 12 asserts the
  plural connection env and the desired `ENABLE_SIGNUP=false` state;
  Section 14 asserts the ACTUAL active Argo first-source path
  (`module-hsworkshop/overlays/custom-domain`; the Qwen model source
  assertion is unchanged). A shell-level PVC grep that failed under
  `set -o pipefail` (SIGPIPE from `grep -q` on the large ConfigMap render)
  was replaced by the parsed-YAML assertions above — no check weakened.

**Validation (local, offline, all newly executed 2026-11-03):**

- Python unittest discovery over `tests/test_*.py`: **146/146 OK**
  (28 secrets + 42 langfuse + 52 provisioning incl. 3 new + 24 headless
  manifests).
- `bash module-hsworkshop/tests/run-tests.sh`: **53 PASS / 0 FAIL**
  (previously 50 PASS / 3 FAIL — the three stale assertions owned by Task 5
  are repaired; the suite is fully green for the first time since Task 2).
- Standard renders (no flags, kustomize v5.8.1): base **42 docs** (was 40:
  + provisioning Job + hash CM `openwebui-workshop-provision-cd2gcmtk89`),
  overlay **47 docs** (was 45: same +2), certificate-only **2 docs**.

**Live gates (NOT performed — by scope; Task 6 / separately approved):**
no Secret provisioning (the bootstrap/connections Secrets must exist before
any deploy), no cluster mutation or deployment/full-sync, no live API
writes, no runtime fixture proofs. The Task 2 deploy gate still applies:
the owner-admin sanitized inventory of stored effective config is a hard
prerequisite before `ENABLE_PERSISTENT_CONFIG=false` is deployed. **Status
update 2026-11-04: that inventory is done and the comparison is PASS
(§13.2, 434/434 keys), the signup fixture is PASS 8/8 (§13.3); what remains
is the UNCONFIRMED admin recovery point (§13.4) plus post-deploy acceptance.**
Runtime behavior (hook execution, preset visibility for ordinary
authenticated users, anonymous denial, no edit) is unproven until the
deployment is exercised.

## 13. Owner-admin sanitized inventory — deploy-gate evidence (2026-11-04)

Read-only, credential-scoped inventory executed after the owner applied the
two Task 2 Secrets. Purpose: produce the owner-sanitized inventory of stored
effective configuration that is the hard prerequisite for deploying
`ENABLE_PERSISTENT_CONFIG=false`, and confirm credential readiness.

**Transport and bounds (all observed):**

- Cluster server exactly `https://api.kpicl2-pool-rft8p.aws.rh-ods.com:6443`,
  user `kpiwko`, current context
  `hsworkshop/api-kpicl2-pool-rft8p-aws-rh-ods-com:6443/kpiwko`; no context
  switch, no cluster mutation.
- Only the new admin Secret's credentials were consumed, in process memory,
  for one `POST /api/v1/auths/signin` against
  `https://chat.hsworkshop.fous.ai` with TLS certificate validation enabled
  (`ssl_verify_result=0`). Response was `SessionUserResponse` with
  `role == "admin"` (no MFA/pending variant). The returned JWT was held in
  memory only for the inventory GETs and discarded; no credential, JWT,
  base64 blob, or raw settings payload was written to a file, argv, log, or
  this document. Responses were filtered in process.
- Secrets inspected as **metadata/key names only** (never values of the
  connections Secret, never the legacy `openwebui-secret`):
  `openwebui-connections-secret` (Opaque, created 2026-11-04T10:30:39Z, keys
  `GUARDRAILS_API_KEY`, `PIPELINES_API_KEY`) and
  `openwebui-admin-bootstrap-secret` (Opaque, created 2026-11-04T10:30:39Z,
  keys `WEBUI_ADMIN_EMAIL`, `WEBUI_ADMIN_PASSWORD`). Readiness: PASS — key
  names match the Task 2/4 manifest refs and the admin credentials
  authenticate successfully.
- Admin GETs used: `GET /api/v1/configs/export` (flat map, **434 keys**),
  `GET /api/config`, `GET /api/v1/models/all`. `GET /api/v1/configs/models`
  and `GET /api/v1/configs/connections` also returned 200 JSON but were not
  needed for the assigned comparison. Live health: `GET /health` 200,
  `/api/config` reports `version 0.12.5`.
- Limitation recorded: `/openapi.json` is served by the SPA fallback (HTML),
  so the supported-route list was confirmed by JSON 200 responses plus the
  pinned 0.12.5 contracts in §3, not by a live schema.

**Comparison of stored effective configuration vs proposed manifests**
(`install/openwebui.yaml` + `overlays/custom-domain/openwebui-patch.yaml`
+ current live Deployment env, all non-secret):

| Concern | Stored/live effective value | Proposed | Verdict |
| --- | --- | --- | --- |
| Signup | persisted `ui.enable_signup=false`; public `features.enable_signup=false`; env `ENABLE_SIGNUP=true` is a no-op today | `ENABLE_SIGNUP=false` + persistence off | **MATCH** — preserved |
| Default role | persisted `ui.default_user_role="user"` | `DEFAULT_USER_ROLE=user` | **MATCH** |
| Branding | no `ui.name` key persisted; `WEBUI_NAME=HS Workshop AI` env; `/api/config` name `HS Workshop AI (Open WebUI)` | same `WEBUI_NAME` | **MATCH** (env-only, unaffected by persistence-off) |
| Web search | persisted `web.search.enable=false`, `web.search.engine=""`, `web.search.searxng_query_url` = the SearXNG URL (env-sourced), `result_count=3`, `confirmation.enable=false`; `user.permissions.features.web_search=true`; public `features.enable_web_search=false`; env `ENABLE_RAG_WEB_SEARCH=true`, `RAG_WEB_SEARCH_ENGINE=searxng` are **not read by 0.12.5** and were inert (§13.1) | `ENABLE_WEB_SEARCH=false`, `WEB_SEARCH_ENGINE=searxng`, unchanged `SEARXNG_QUERY_URL` | **MATCH (effective)** — search stays OFF; the engine value is inert while search is off; the dead legacy names are removed |
| Model providers | `openai.enable=true`; `api_base_urls` count **1** (`http://guardrails-proxy.hsworkshop.svc.cluster.local:8080`); `api_keys` count 1 holding the non-secret placeholder (`unused`); `api_configs={}`; `ollama.enable=false` with `ollama.base_urls` count 1 at the image default `host.docker.internal:11434`; `direct.enable=false`; `evaluation.arena.enable=false` | plural 2 URLs (guardrails + pipelines) with Secret-backed keys, singular env removed | **EXPECTED additive change** (Task 2/3); guardrails connection preserved, Pipelines connection added intentionally, Ollama stays disabled |
| Code execution / interpreter | `code_execution.enable=true` (`engine=pyodide`), `code_interpreter.enable=true` (`engine=pyodide`); public `features.enable_code_execution=true`, `enable_code_interpreter=true` | explicit `ENABLE_CODE_EXECUTION=true`, `CODE_EXECUTION_ENGINE=pyodide`, `ENABLE_CODE_INTERPRETER=true`, `CODE_INTERPRETER_ENGINE=pyodide` | **MATCH** — preserved ON with pyodide and now explicit; the pinned defaults are also `True`/`pyodide` (config.py:415-439), so this declaration removes reliance on an implicit default rather than changing behavior |
| OAuth | `oauth.enable=true` but `client_id`, `client_secret`, `provider_url`, `redirect_uri` all empty (`provider_name="SSO"`) | not declared | benign flip to disabled; no provider is configured, so no functional effect |
| Model records | `GET /api/v1/models/all` returns **0** records | Task 4 presets import is create-only | consistent; nothing persisted to preserve |
| HTTPS / cookies / CORS | env `WEBUI_URL=https://chat.hsworkshop.fous.ai`, `WEBUI_SESSION_COOKIE_SECURE=true`, `WEBUI_SESSION_COOKIE_SAME_SITE=lax`, `CORS_ALLOW_ORIGIN` = the two ADR-0001 origins; live signin `Set-Cookie` carries `Secure`, `HttpOnly`, `SameSite`, `Max-Age`; CORS echoes exactly those two origins with `allow-credentials=true` and rejects an unknown origin; no `Strict-Transport-Security` header | overlay sets the same four env values | **MATCH** (env-only: the four values are supplied by env; `webui.url` exists in the export with an empty persisted value, so comparison is by env + live headers, not persisted JSON — see §13.2) |
| Other persisted settings | 434 keys total, including 0.12.5 toggles such as `folders/notes/memories/calendar/automations/channels/skills/subagents/apps.enable`, `users.enable_status`, `chat.tool_search`, `chat.context_compaction`, `auth.jwt_expiry="4w"`, `auth.enable_api_keys=false` | most not declared | **COMPARED — §13.2**: all 434 keys diffed against env-derived defaults; these are equal to the pinned defaults, 0 undeclared customizations |

**Gate verdict (updated 2026-11-04 after §13.1 + §13.2 + §13.3): NAMED
PREFLIGHT UNIT PASS; whole-config configuration gate PASS — 434/434 keys
accounted, 0 undeclared customizations (§13.2); fresh-isolated signup
fixture PASS — 8/8 checks (§13.3).** Predeployment readiness is therefore
**CONDITIONAL**: one Task 6 prerequisite, the admin recovery point /
sanitized baseline, is UNCONFIRMED (§13.4), and everything that needs the
deployed revision is post-deploy acceptance, not proven. The two items that
blocked this gate are closed locally, without any owner decision:

1. **Web search stays OFF.** The manifest now declares the env name 0.12.5
   actually reads, `ENABLE_WEB_SEARCH=false`. The old
   `ENABLE_RAG_WEB_SEARCH=true` / `RAG_WEB_SEARCH_ENGINE=searxng` entries were
   inert on the pinned image, so the earlier "would flip to enabled"
   prediction was a dead-name defect, not an effective behavior change;
   0.12.5's own default is also `False`.
2. **Code execution / interpreter stay ON** with the pinned `pyodide`
   engine, now declared explicitly (`ENABLE_CODE_EXECUTION`,
   `CODE_EXECUTION_ENGINE`, `ENABLE_CODE_INTERPRETER`,
   `CODE_INTERPRETER_ENGINE`). Their 0.12.5 defaults are already
   `True`/`pyodide`, so the declaration removes reliance on implicit
   defaults rather than changing behavior.

What was open until §13.2, and its resolution: only the named settings were
compared in this section. The remaining persisted keys from the 434-key export
have since been diffed against the env-derived defaults of the pinned source —
see **§13.2: 434/434 accounted, 0 undeclared customizations, configuration
gate PASS**. The fresh-isolated second-start signup restore is no longer
pending either — **§13.3 PASS, 8/8 checks**. Preset visibility for ordinary
users, ACL convergence, retained Job success, tracing, no-op sync, user/chat
data and TLS are post-deploy acceptance (§13.4), and the admin recovery
point / sanitized baseline is UNCONFIRMED (§13.4).

Everything else in the assigned comparison is preserved or is an intentional
Task 2/3 change. No settings/model/account writes, no DB access, no broad
user/chat inspection, no legacy Secret access, and no deployment/restart
occurred.

### 13.1 Predeployment unit — web search / code tools preservation (2026-11-04, local)

Scope: close the two named gate items by declaring their effective values with
the env names the pinned image reads, and record a sanitized comparison
against the pinned defaults. The gate status recorded in §10 predates this
unit (it still describes the running deployment env and the pre-unit
decision); this subsection is the current preflight status. **No cluster or live API access was performed in
this unit**: every live value below is reused from the §13 inventory record,
not re-observed. No credential was consumed, no Secret was read, no
deployment/publication happened.

**Frozen source verified** (commit
`f7ac7ee10962ddec0017f5c29b6740d9b9c5faca`, image digest
`sha256:0b399a8ba34558e181d316750455f2a95376a38cd60a61b9c942e138898944bd`,
`backend/open_webui/config.py` unless noted):

- `ENABLE_WEB_SEARCH` default `False` (1178); `WEB_SEARCH_ENGINE` default
  `''` (1187); `SEARXNG_QUERY_URL` default `''` (1234);
  `WEB_SEARCH_RESULT_COUNT` default `3` (1196). `ENABLE_RAG_WEB_SEARCH` /
  `RAG_WEB_SEARCH_ENGINE` do not appear anywhere in 0.12.5 config.
- `ENABLE_CODE_EXECUTION` default `True` (415); `CODE_EXECUTION_ENGINE`
  default `pyodide` (417); `ENABLE_CODE_INTERPRETER` default `True` (430);
  `CODE_INTERPRETER_ENGINE` default `pyodide` (439).
- Key mapping into persisted config: `code_execution.enable/engine` (2944-45),
  `code_interpreter.enable/engine` (2951, 2958), `web.search.enable` (3052),
  `web.search.engine` (3055), `web.search.result_count` (3058),
  `web.search.searxng_query_url` (3068).
- Precedence: `ENABLE_PERSISTENT_CONFIG` defaults `True` and is passed to
  `Config.configure` (3365-3371); with persistence off,
  `Config.get`/`get_all` return the env-derived `DEFAULTS` and never read the
  DB (`backend/open_webui/models/config.py:136-150, 189-198`). This is the
  exact mechanism by which env governs after the flip.

**Sanitized comparison (reused §13 inventory; no secrets involved):**

| Persisted key | Live effective | Pinned 0.12.5 default | Proposed env | Verdict |
| --- | --- | --- | --- | --- |
| `web.search.enable` | `false` | `false` (`ENABLE_WEB_SEARCH`) | `ENABLE_WEB_SEARCH=false` | MATCH — explicit, preserved OFF |
| `web.search.engine` | `""` | `""` | `WEB_SEARCH_ENGINE=searxng` | INERT DIFF — search off, no behavior change; records intended backend and matches the SearXNG URL |
| `web.search.searxng_query_url` | env-sourced SearXNG URL (§13 records `http://searxng:8080/...`) | `""` | unchanged `SEARXNG_QUERY_URL=http://searxng:8080/search?q=<query>&format=json` | MATCH — inherited env, unchanged |
| `web.search.result_count` | `3` | `3` | not declared | MATCH — default, no declaration needed |
| `web.search.confirmation.enable` | `false` | `false` | not declared | MATCH — default |
| `code_execution.enable` | `true` | `true` | `ENABLE_CODE_EXECUTION=true` | MATCH — explicit |
| `code_execution.engine` | `pyodide` | `pyodide` | `CODE_EXECUTION_ENGINE=pyodide` | MATCH — explicit |
| `code_interpreter.enable` | `true` | `true` | `ENABLE_CODE_INTERPRETER=true` | MATCH — explicit |
| `code_interpreter.engine` | `pyodide` | `pyodide` | `CODE_INTERPRETER_ENGINE=pyodide` | MATCH — explicit |

Consequence: because every inspected value already equals its pinned default
(except the env-sourced SearXNG URL, which is unchanged), persistence-off
would not have changed these behaviors even before this unit. The manifest
declarations make the intent auditable and remove the misleading legacy env
names; they do not introduce a behavior change.

**Remaining scope — CLOSED by §13.2 (2026-11-04):** the full 434-key export
has now been diffed against the env-derived defaults of the pinned source:
434/434 accounted, 430 equal, 3 declared differences, 1 Secret-backed value,
**0 undeclared customizations**. The candidate keys named here
(`folders/notes/memories/calendar/automations/channels/skills/subagents/apps.enable`,
`users.enable_status`, `chat.tool_search`, `chat.context_compaction`,
`auth.jwt_expiry`, `auth.enable_api_keys`) were all confirmed equal to the
pinned defaults in that run.

**Validation performed for this unit (2026-11-04, local):**

- Focused suites: `tests/test_headless_manifests.py` 26/26 OK;
  `tests/test_openwebui_secrets.py` 30/30 OK (both execute the standard
  `kustomize build` renders of base, overlay, and certificate-only).
- Full Python discovery (`python3 -m unittest discover -s
  module-hsworkshop/tests`): 163/163 OK (26 manifests + 30 secrets + 45
  langfuse + 62 provisioning).
- Bash suite `module-hsworkshop/tests/run-tests.sh`: 53 PASS / 0 FAIL.
- Standard `kustomize build` (no relaxed load flags): base 42 objects,
  overlay 47, certificate-only 2 — unchanged counts, TLS/Route/PVC/model
  sources untouched.

### 13.2 Whole-config predeployment diff — persist-off configuration gate PASS (2026-11-04, read-only)

Scope: diff **all 434** persisted keys from `GET /api/v1/configs/export`
against the **env-derived** defaults of the exact pinned image — the same
`DEFAULT_CONFIG` that `Config.configure` receives once
`ENABLE_PERSISTENT_CONFIG=false` (models/config.py:136-150,189-198). The env
model used for "derived" is the image-baked `ENV` block of the pinned
Dockerfile (`ENV=prod`, `OLLAMA_BASE_URL=/ollama`, cache/model defaults) plus
the **proposed** manifest env from `install/openwebui.yaml` and
`overlays/custom-domain/openwebui-patch.yaml`. This is not an empty
`DEFAULT_CONFIG` skeleton comparison.

**Method and provenance (no new source work).** The pod's
`/app/backend/open_webui/config.py` is byte-identical to upstream commit
`f7ac7ee10962ddec0017f5c29b6740d9b9c5faca`
(`sha256 9c9cdd93…01ca45`, `env.py` likewise), so defaults were computed by
static evaluation of the module-level assignments in that exact source, in an
ephemeral in-memory process: no application import, no DB access, no pod or
application writes, and no file written into this repository. Config-relevant
env names present in the running pod were enumerated as **names only**
(184 names; 11 intersect the `config.py`/`env.py` env names) and are fully
explained by the pinned Dockerfile `ENV` block plus the proposed manifest —
therefore the legacy Secret `envFrom` contributes **no** config-affecting env
name and no Secret value had to be read on the defaults side.

**Live access (same narrow scope as §13).** Cluster server verified exactly
`https://api.kpicl2-pool-rft8p.aws.rh-ods.com:6443`, context user `kpiwko`,
no context switch. One `POST /api/v1/auths/signin` with the admin bootstrap
Secret, consumed in process memory only (`role=admin`, TLS verified), and one
`GET /api/v1/configs/export` (434 keys). Credentials, JWT and all raw values
stayed in memory; only the sanitized summary below was emitted. No other
Secret was read, and there were no writes, restarts or deployments.

**Coverage — all 434 keys accounted**

| Bucket | Keys |
| --- | --- |
| Persisted keys compared | 434 |
| Equal to proposed env-derived default | 430 |
| Intentional, manifest-declared difference | 3 |
| Secret/opaque value (presence + shape only) | 1 |
| Behaviorally relevant **undeclared** customization | **0** |
| In export but not in pinned defaults | 0 |
| In pinned defaults but not in export | 0 |

Key-name sets matched exactly (434 vs 434, nothing missing or extra), an
independent corroboration that the static extraction enumerated the same
config surface the live server exports.

**Intentional, declared differences — every one traced to manifest env**

| Key | Persisted (live) | Post-change env-derived | Classification |
| --- | --- | --- | --- |
| `openai.api_base_urls` | 1 URL (guardrails proxy) | 2 URLs (guardrails + pipelines) | Task 2/3 additive, index-paired with keys |
| `web.search.engine` | `""` | `searxng` | inert — `web.search.enable` stays `false` (§13.1) |
| `webui.url` | `""` | the `WEBUI_URL` custom-domain value | env-only; never persisted, so nothing to drop |

**Opaque (Secret-backed, in-process only)**

| Key | Persisted shape | Post-change shape |
| --- | --- | --- |
| `openai.api_keys` | 1 element (non-secret placeholder) | 2 elements from `openwebui-connections-secret` |

Only counts/shapes are recorded here; no key material was read out, compared
by hash, or emitted.

**Env-governed preservation (4 keys) — why the declarations are load-bearing.**
These are keys where the image default differs from the manifest env *and* the
manifest env reproduces the persisted value: `ui.enable_signup` (image default
`true` → `false`, matching persisted), `ui.default_user_role` (→ `user`),
`ollama.enable` (image default `true` → `false`), and
`web.search.searxng_query_url` (unchanged env URL). Without the
`ENABLE_SIGNUP=false` / `DEFAULT_USER_ROLE=user` / `ENABLE_OLLAMA_API=false`
declarations the persistence-off restart would have flipped two of them.

**Spot-verified persisted values, equal to pinned defaults** (read from the
same export; these are the items §13 flagged as unaudited): `auth.jwt_expiry`
`4w`, `auth.enable_api_keys` `false`, `users.enable_status` `true`,
`code_execution.enable` `true` / `code_execution.engine` `pyodide`,
`code_interpreter.enable` `true`, `web.search.enable` `false`,
`web.search.result_count` `3`, `web.search.confirmation.enable` `false`,
`openai.enable` `true`, `ollama.enable` `false`, `direct.enable` `false`,
`evaluation.arena.enable` `false`,
`folders/notes/memories/calendar/automations.enable` `true`,
`channels/subagents/apps.enable` `false`, `chat.tool_search.enable` `false`,
`chat.context_compaction.enable` `false`.

**Auth / OAuth / permissions / security classification.** Every `oauth.*`,
`auth.*`, `user.permissions*`, `users.*` and `ldap.*` key equals its pinned
default; no permission, group, signup, API-key or webhook setting is a
persisted customization. **Correction to §13:** `oauth.enable` is `true` by
*default* (`ENABLE_OAUTH`, config.py) and stays `true` after the flip — there
is no "benign flip to disabled". The posture is unchanged either way, because
`oauth.client_id`, `oauth.client_secret`, `oauth.provider_url`,
`oauth.redirect_uri` are empty and `oauth.enable_signup=false`, so no provider
can be exercised. `ldap.enable` `false` with empty `ldap.server.*`.

**Five `OPENAI_API_KEY`-derived keys resolved by source chain, not assumed
missing-means-default.** `OPENAI_API_KEY` defaults to `""` (config.py:328) and
is re-derived only at config.py:367-371 when `OPENAI_API_BASE_URLS` contains
`https://api.openai.com/v1`, which the proposal does not. Hence
`rag.openai.api_key`, `image_generation.openai.api_key`,
`images.edit.openai.api_key`, `audio.stt.openai.api_key` and
`audio.tts.openai.api_key` derive `""` and equal the persisted values.

**Coverage confidence and honest limits.** Static evaluation reproduces
434/434 keys with exact key-set agreement, and every spot-checked value
matches the §13 inventory. Limits: (a) it evaluates the module-level paths
taken under the two env maps, so runtime-only branches are approximated — the
Ollama port probe is treated as identity, and the observed equality of
`ollama.base_urls` shows that matches the running pod; (b) a mis-derived
default would surface as an unexplained `undeclared_diff`, of which there are
none; (c) DB-only rows outside `DEFAULT_CONFIG` (models, prompts, tools,
users, chats) are out of scope by design and unaffected by the flip.

**Gate verdict: persistence-off configuration gate PASS — 434/434 accounted,
0 undeclared customizations.** Deploying `ENABLE_PERSISTENT_CONFIG=false` with
the current manifests drops no stored behavior beyond the three declared
differences; the Secret-backed `openai.api_keys` change is the intended
Task 2/3 addition.

**Smallest preservation fixes required: none.** No further manifest env
declaration is needed for configuration preservation. Remaining non-blocking
follow-ups: keep the §13 `oauth.enable` wording corrected as above. The
fresh-isolated second-start signup restore is **CLOSED (PASS, §13.3, 8/8
checks)**; preset visibility for ordinary users, the retained provisioning
Job, ACL and tracing remain **post-deploy acceptance (§13.4)**, not
pre-sync work — unchanged by this read-only unit, which ran no tests because
it changed documentation only.

### 13.3 Predeployment runtime proof — fresh-isolated second-start signup precedence (2026-11-04, local)

Scope: close the Task 6 "fresh isolated: native admin and second-start signup
restore, user role" runtime item **locally**, without a cluster, deployment or
production write. No Secret, PVC, DB, model or config write was performed and
`secrets.env` was not read. Nothing was published.

**Fixture (executed, not mocked).**
`module-hsworkshop/tests/runtime_openwebui_signup_precedence.py` drives three
starts of the EXACT digest pinned in `install/openwebui.yaml`
(`ghcr.io/open-webui/open-webui@sha256:0b399a…944bd`, linux/arm64, pulled by
digest on demand; image `/app/package.json` → `0.12.5`, container Python
3.12.16). Local runtime: podman 6.0.2 (podman-machine-default, libkrun). The
script parses the digest out of the manifest and refuses a mismatch; it is not
part of unittest discovery and changes no manifest, preset or provisioner.

Isolation: no published ports — HTTP is probed with `podman exec` against the
container's own loopback only; disposable data directory under the approved
TMPDIR; one container (`--cpus 2 --memory 2g`, matching the manifest limits)
recreated per scenario over the same data; fixture admin plus a synthetic
signup user with credentials generated in-process by `secrets` and passed as
`-e NAME` values inherited from the client environment (never argv, stdout or
logs); only booleans/role/status/version emitted; container and data removed in
`finally` (verified afterwards: 0 fixture containers, 0 temp directories).

**Observed results (executed 2026-11-04; 8/8 checks PASS — 3 in A, 3 in B,
2 in C; reviewer re-run also PASS):**

| Scenario | Container env | Observed | Verdict |
| --- | --- | --- | --- |
| A fresh bootstrap, empty data | `ENABLE_PERSISTENT_CONFIG=false`, `ENABLE_SIGNUP=true` | `/api/config` `enable_signup=false`; admin signin `role=admin`; `POST /api/v1/auths/signup` → 403 | **PASS** — bootstrap side effect reproduces |
| B second start, same data, existing users | same env | `enable_signup=true`; existing admin signin `role=admin`; signup → 200 `role=user` | **PASS** — declared env restores signup; default role `user` |
| C third start, same data, production value | `ENABLE_SIGNUP=false` | `enable_signup=false`; signup → 403 | **PASS** — policy preserved on existing data |

**Source corroboration from the same image** (read-only, no application
import): `main.py` 400–404 calls
`Config.upsert({'ui.enable_signup': False})` after admin creation; with
persistence disabled, `models/config.py` `upsert` updates the per-process
`Config.DEFAULTS` instead of the DB, and `get`/`get_all` return `DEFAULTS`;
`utils/auth.py` skips admin creation when users already exist. That is exactly
the first-start-false / second-start-env-true precedence observed above.

**Verdict: PASS for the fresh-isolated signup-restore proof.** The pending
second-start item in §6 is closed. Production posture is unchanged: the deploy
manifest still declares `ENABLE_SIGNUP=false`; the fixture's `true` value
existed only inside the disposable container and was never applied to any
deployment.

**Fixture limits (explicit — do not over-read this PASS):**

1. **Persistence-off only.** Every start ran with
   `ENABLE_PERSISTENT_CONFIG=false`. The fixture never seeded a *pre-existing
   persisted* `ui.enable_signup` row and then flipped the flag, so it does not
   exercise the "old DB row present, persistence disabled" path. The claim
   that a stale persisted row is ignored under persistence-off is therefore
   **source corroboration** (`models/config.py` `get`/`get_all` returning
   `DEFAULTS`, `main.py` 400–404, `utils/auth.py` 641–643), **not a runtime
   observation**. The live deployment is the only place that path exists.
2. **Architecture differs from production.** The fixture ran the pinned
   *digest* on `linux/arm64` (podman-machine, libkrun) because the digest is
   an index digest; the production nodes are `x86_64`. The same index digest
   resolves to a different per-arch manifest there. Image *identity* is
   pinned; per-arch binary equivalence is assumed, not proven on this host.
3. **Minimal env only.** The container carried only the signup/persistence
   variables plus generated credentials. It did **not** test model providers
   (guardrails/Pipelines connection pairs, `OPENAI_API_CONFIGS`), web search,
   code execution, OAuth, CORS/cookies/Route, Langfuse, or the valves/PVC
   path — none of those are evidenced by this fixture.
4. **Not a deployment fidelity test.** No PVC, Secret, cluster object or
   Argo CD revision was involved; startup on the real volume with the real
   manifest env is post-deploy acceptance.

**Still postdeployment or separately approved (not run here, not proven —
see §13.4):** full sync plus retained `openwebui-workshop-provision` Job
success, unchanged designated existing user/chats and signing secrets,
public-read/no-write and unrelated private-model preservation, no-op second
sync, startup settings across the real restart, both TLS origins/cookies/
Route, Qwen and guardrails; delayed-Langfuse invalid-key startup fixture and
browser streaming/non-stream finalization (Pipelines/Langfuse fixture,
separate unit); preset visibility for ordinary users on a provisioned
container and the additive access-update no-op (provisioner/ACL fixture,
separate unit).

### 13.4 Bounded readiness packet — recovery prerequisite, conditional status, publish intent (2026-11-04, read-only)

Purpose: state precisely what is ready, what is not, and the single owner
decision still outstanding before a sync. No deployment, backup, snapshot,
export or API write was performed in this unit; no volume content, DB row,
Secret value or account/chat data was read.

**Plan prerequisite (exact).** Plan `Task 6` first bullet reads: "Reconfirm
correct cluster/health/revisions; existing admin recovery point; sanitized
baseline; no broad user/chat scan." The *recovery point* is therefore a
**pre-sync** prerequisite. The *sanitized baseline* half is satisfied by
§13/§13.2 (owner-admin sanitized configuration inventory) — that is a
configuration baseline, **not** a DB/volume recovery point, and the two must
not be conflated.

**Verification performed — metadata only, read-only:**

- Cluster/API exactly `https://api.kpicl2-pool-rft8p.aws.rh-ods.com:6443`,
  context `hsworkshop/api-kpicl2-pool-rft8p-aws-rh-ods-com:6443/kpiwko`,
  user `kpiwko`; server 4.21.40. **No context switch, no mutation.**
- Known volume `PersistentVolumeClaim/hsworkshop/openwebui-data`: `Bound`,
  request/capacity 5Gi, storageClass `gp3-csi`, PV
  `pvc-08a94400-d049-46c8-93b4-9ef2250cbd06`, created `2026-10-29T15:07:31Z`,
  `status.conditions` empty; annotations are only the Argo CD tracking id,
  `last-applied-configuration` and the standard CSI bind/provisioner keys /
  selected-node. **No backup, snapshot or recovery annotation or label.**
- Snapshot API **is** served by this cluster (`snapshot.storage.k8s.io/v1`:
  `VolumeSnapshot`, `VolumeSnapshotContent`, `VolumeSnapshotClass`), so the
  query below is meaningful rather than an "API unavailable" result.
- `VolumeSnapshot` objects in namespace `hsworkshop`: **none**.
- `VolumeSnapshotContent` objects whose `volumeSnapshotRef.namespace` is
  `hsworkshop`: **none**.
- Project backup documentation: **none named** — no backup/restore/snapshot
  document exists in this repository (`docs/`, ADRs, plan, READMEs,
  `module-hsworkshop/README.md`).

**Status: admin recovery point — UNCONFIRMED.** Not evidenced and not
disproved. Limits of this check: it covers exactly one PVC's metadata and one
snapshot resource kind on one cluster. It deliberately does **not** query AWS
EBS snapshots, AWS Backup vaults/plans, cross-region replication, Velero/OADP
objects, or owner-side out-of-band backups — that would be a broad
backup-system search with credential expansion outside this unit's scope.
**Absence of a `VolumeSnapshot` for one PVC is not proof that no backup
exists.**

**Consequence — readiness is CONDITIONAL, not unconditional PASS.**

- Predeployment evidence closed: configuration gate PASS (§13.2, 434/434
  keys, 0 undeclared customizations) and fresh-isolated signup fixture PASS
  (§13.3, 8/8 checks).
- Not closed: the recovery point above. A sync must not be treated as
  approved until the owner either (a) **confirms an externally managed
  recovery point** covering `hsworkshop/openwebui-data` and its DB, or
  (b) **approves a bounded backup step** (e.g. a single `VolumeSnapshot` of
  that PVC) to be executed under separate approval. This unit performed
  neither and does not waive the prerequisite unilaterally, invents no
  timestamp, and claims no recovery capability.
- **Git revert does not undo API/DB writes.** Reverting the Git change
  restores manifests/revisions only; the provisioner's preset/ACL API writes,
  DB-persisted configuration and any data written by a sync survive it. That
  asymmetry is exactly why the recovery point is a precondition, not a
  rollback story.

**Intended publication (asked, not performed).** Read-only Git state:
branch `zijeme-it`, HEAD `4deb37f`, up to date with `origin/zijeme-it`,
`origin = git@github.com:kpiwko/genaidemo25-gitops.git`. On the user's
explicit go: commit the reviewed work on `zijeme-it`, push to that origin, and
let Argo CD auto-sync the tracked first source
(`module-hsworkshop/overlays/custom-domain`) activate the new revision. This
unit made **comment/documentation changes only**, ran no commit/push/PR/
branch/merge, and changed no manifest semantics.

**Post-deploy acceptance — explicit, controlled, NOT proven. Task 6 is not
complete and must not be recorded as complete:** signin/role on the deployed
revision; ACL public read / no write / anonymous denied / unrelated private
model preserved; Pipelines startup with the locked requirements plus
authenticated filter discovery; retained `openwebui-workshop-provision` Job
success; Langfuse tracing (delayed readiness + invalid-key startup fixture);
browser streaming and non-stream finalization; no-op second sync; designated
existing user/chat data and signing secrets unchanged; both TLS origins,
canonical cookies, original Route, Qwen and guardrails.

**Single owner decision still required (as of the 13.4 packet):** the pre-sync
recovery point — confirm an externally managed backup, or approve the bounded
backup step. All other pre-sync evidence is PASS locally; nothing else blocked
the sync.

#### 13.4.1 Owner decision — explicit backup waiver (2026-11-04, publish unit)

The owner was asked to resolve the recovery-point prerequisite above and
answered explicitly: **"I don't need a backup."** That is recorded here as a
knowing, owner-supplied **waiver** of the predeployment backup/recovery
prerequisite for this sync.

- **No backup artefact was created.** This unit ran no `VolumeSnapshot`, no
  export, no `kubectl` backup/copy, and no volume/DB read. There is still no
  project recovery point, and this document claims none.
- The waiver is a risk acceptance by the owner, **not** evidence that a
  recovery point exists. The UNCONFIRMED status above stands as a factual
  record; it is no longer a blocking gate because the owner waived it.
- **Git revert still does not undo API/DB writes** (see the asymmetry note
  above); the waiver does not change that. Post-deploy acceptance below remains
  open and is owned by the next unit.
- Predeployment evidence is unaffected: configuration gate PASS (§13.2) and
  fresh-isolated signup fixture PASS (§13.3) remain the basis for publication.

**Publication (this unit).** On the owner's explicit go, the reviewed headless
change (Tasks 2–5 plus preflight/runtime-fixture docs) is committed on
`zijeme-it` and pushed to `origin` (`git@github.com:kpiwko/genaidemo25-gitops.git`).
Argo CD tracks `zijeme-it` through `module-hsworkshop/overlays/custom-domain`
and may auto-sync the new revision. No PR, merge, force-push or branch rewrite
is part of this publication.
