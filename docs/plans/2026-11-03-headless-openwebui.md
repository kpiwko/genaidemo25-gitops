# Headless OpenWebUI Implementation Plan

- Date: 2026-11-03
- Status: approved plan (planner-owned); Task 1 executed 2026-11-03 —
  compatibility matrix in `docs/headless-openwebui.md`; Task 2 local
  implementation executed 2026-11-03 (live deployment still gated); Tasks 3+ not
  started. Architect-verified corrections applied 2026-11-03 (planner-authorized,
  compact): see Task 1/2/3/4/6 notes and Unresolved.
- Spec: ADR-0002 (accepted direction; implementation conditional on version-specific
  preflight), ADR-0001 (binding)
- Goal: headless browser-workshop configuration — no manual Admin Panel connections,
  model JSON, or tracing setup.

## Architecture summary

- Existing Kustomize base + custom-domain overlay + Argo CD multi-source Application.
- Git owns startup/filter artifacts and a bounded, authenticated, additive PostSync
  preset Job — **not** continuous database self-healing.
- Tech: OpenShift, Argo CD, Kustomize, Python stdlib, pinned current OpenWebUI /
  Pipelines SDK5, Langfuse 4.71.

## Git / cluster constraints

- Root known; actual remote expected to be a fork `github.com/kpiwko/genaidemo25-gitops`
  at `origin` (SSH or HTTPS) — read the actual value during preflight, do not change it.
- Expected base `origin/zijeme-it`, the active live Argo branch — not the old `main`.
- Read-only preflight first: no fetch, no switch. Current cluster is exactly
  `https://api.kpicl2-pool-rft8p.aws.rh-ods.com:6443`; do not switch clusters; if
  credentials are expired, stop.
- Future work is a separate orchestrator-authorized unit; explicit workdir per shell;
  checkpoint every 6–8 tool calls.
- No commit/push/PR/merge is authorized in planning. Any publish to the tracked
  `zijeme-it` branch may auto-deploy — a significant boundary; feature branch and
  review target must be verified before publication; user approval required for
  force/merge/delete.

## Global preservation constraints

Preserve: PVCs (users/chats DB), signing secrets, auth, the live effective
signup policy (currently `false` — declared explicitly as
`ENABLE_SIGNUP=false` in Task 2; no reopening), user branding, SearXNG
intended flags, ADR-0001 canonical chat with both origins,
cookies on the original Route with TLS, Qwen/guardrails/Langfuse 4.71, model Helm source.

Forbidden: frontend Insights, Goose, upgrades, auth-policy expansion, image upgrades,
infrastructure rewrite, direct DB access/deletion of `/models` sync, arbitrary hooks.
No Secret/env dumps, no secrets in files/credentials in argv/logs/docs.

The exact deployed version is unverified — **no implementation until Task 1 freezes
source contracts**; unsupported findings return to the architect, never guessed
API/upgrade.

## Review focus

Unknown schemas/ownership fail closed; partial imports and timeouts re-read before
retry; no-op second run preserves private models; valves override fails startup;
auth bounded; fresh native bootstrap signup side effects without account reset;
actual stream + non-stream browser finalization to prove completed traces.

## Task 1 — Read-only compatibility freeze / preservation (deps: none)

**Status: EXECUTED 2026-11-03 (read-only). Matrix delivered in
`docs/headless-openwebui.md`.** Freeze: OpenWebUI 0.12.5
(digest `sha256:0b399a8b…944bd`, tag `v0.12.5`, commit `f7ac7ee…`),
Pipelines digest `sha256:b48e9bc3…52d6e` (`:main`), Langfuse SDK 5.0.4.
All planned API routes supported; ACL = `AccessGrantModel` grants list
(public-read `user:*:read`); import ownership/partial-skip semantics
confirmed. Runtime validation PENDING (fixtures). Two architect
decisions — both RESOLVED 2026-11-03 by verified correction: (1) the
earlier conclusion that signup restore requires an authenticated
persisted-config write (env alone cannot re-enable) was FALSE under
`ENABLE_PERSISTENT_CONFIG=false` — declared env governs and CAN restore
(fixture proof pending); Task 2 declares `ENABLE_SIGNUP=false` matching
the live effective policy; (2) Pipelines digest→commit unmapped (500/404),
freeze by digest (no further lookup). `/api/chat/completed` is deprecated in 0.12.5 —
outlet filters run inline; finalization evidence targets the inline
path. Cluster/server verified exact; credentials valid; no writes
performed.

**Architect-verified corrections (2026-11-03, planner authorized, compact):**
source compatibility is COMPLETE; runtime validation remains PENDING. No
further digest/commit lookups: OpenWebUI 0.12.5 commit
`f7ac7ee10962ddec0017f5c29b6740d9b9c5faca` digest
`sha256:0b399a8ba34558e181d316750455f2a95376a38cd60a61b9c942e138898944bd`;
Pipelines `sha256:b48e9bc338ce2be0acfbeff01810db72408a12f07739f9e3879c1f2b00952d6e`
(freeze by digest, no commit mapping); SDK exactly 5.0.4. Correction to
decision (1): the claim "env cannot restore signup" is FALSE when
`ENABLE_PERSISTENT_CONFIG=false` (models/config 136–150, 201–212 ignore
persisted/in-memory config; first bootstrap main 400–404 sets
`enable_signup:false`; second start with existing users skips bootstrap
utils/auth 641–643, so declared env CAN restore) — declared restore is
true, fixture proof pending. Live effective signup is `false`: the
deployment MUST declare explicit `ENABLE_SIGNUP=false` before turning
persistence off (no auth hooks, no signup-policy reopening). Other stored
effective config needs an owner-sanitized inventory before deploy — env
list alone is insufficient.

Files: ADR-0002 and new `docs/headless-openwebui.md` (created during
execution).

- Read-only Git status/refs (`origin` actual, no fetch), active Argo source.
- Current cluster exactly `https://api.kpicl2-pool-rft8p.aws.rh-ods.com:6443`; no
  switch; expired credentials stop work.
- Collect running image IDs/versions matching tagged source, and sanitized effective
  non-secret settings. No Secrets dump.
- Freeze API contracts:
  - `POST /api/v1/auths/signin` — admin verify.
  - `GET /api/v1/models/all` — persisted records; import `POST /api/v1/models/import`
    `{models:[changed]}` — ownership / unmanaged preservation.
  - Discovery `GET /api/models`.
  - One public ACL grant representation — current `anyone/*/read` vs legacy
    `access_control:null`: ONLY the actual source representation, never both.
    Architect correction (2026-11-03): exact grant is
    `{principal_type:'user', principal_id:'*', permission:'read'}` —
    authenticated user wildcard read. NOT `anyone` (no-auth), NOT legacy
    `access_control`, NOT wildcard write. Tests must cover ordinary
    authenticated read, no edit, anonymous denied.
- Plural connections env/index, API configs persistence precedence, pinned
  bootstrap/signup on second start, deps install/persisted valves overlay/probe/filter
  lifecycle incl `/api/chat/completed` older browser finalization (streaming and
  non-stream). Architect correction (2026-11-03): `/api/chat/completed` is
  deprecated in 0.12.5; outlet filters run inline — never add a routine extra
  finalizer on that route (duplicate-finalization risk). Evidence targets the
  inline outlet path.
- Exact SDK5 version, diagnostics only, no data.
- Output: sourced compatibility matrix, accepted-architect gate. Current upstream is
  not deployed-proof; live write proof pending fixtures.
- Stop-and-return-to-architect conditions: unsupported safe imports/ACL/signup/
  dependency locking/finalization.

## Task 2 — Git-owned startup and credentials (deps: 1)

**Status: COMPLETE (LOCAL) 2026-11-03. Implemented and locally validated:
`install/openwebui.yaml` (digest pin, plural ordered connections, explicit
secretKeyRefs before composed OPENAI_API_KEYS, singular env removed,
ENABLE_PERSISTENT_CONFIG=false + ENABLE_SIGNUP=false, admin-bootstrap
explicit refs overriding legacy envFrom), `apply-openwebui-secrets.sh`
(guarded, hidden-input, server-side, 2 Secrets only), and
`tests/test_openwebui_secrets.py` — 25/25 unittest PASS offline with
mocked oc; `bash -n` clean; base + overlay + certificate-only kustomize
renders succeed with TLS/Route/PVC/model sources unchanged and the overlay
adding exactly its 5 custom-domain objects with no env drift on Task 2
keys. LIVE GATES (not performed, by scope): no Secret provision, no
deployment/sync — deployment remains blocked on the owner-admin
sanitized inventory of stored effective config (env list alone is
insufficient); the Pipelines key must match the Task 3 manifest before any
deploy. Existing `tests/run-tests.sh`: 50 PASS / 3 FAIL — all 3 are
known, non-Task-2-regression: two are the predicted old-singular-URL /
base-differs-from-HEAD assertions owned by Task 5's test adaptation; the
Argo-app source-path assertion FAILS IDENTICALLY AT HEAD (pre-existing,
untouched files).**

**Deploy-gate update (2026-11-04, read-only):** the owner applied
`openwebui-connections-secret` (`GUARDRAILS_API_KEY`, `PIPELINES_API_KEY`)
and `openwebui-admin-bootstrap-secret` (`WEBUI_ADMIN_EMAIL`,
`WEBUI_ADMIN_PASSWORD`); both exist with exactly those key names and the
admin credentials authenticate (signin `SessionUserResponse`, role admin) —
credential readiness PASS. The owner-admin sanitized inventory is DONE and
recorded in `docs/headless-openwebui.md` §13 (single persisted OpenAI
connection, no hidden providers; effective signup `false`, role `user`,
branding and HTTPS/cookie/CORS behavior preserved).

**Whole-config preflight unit (2026-11-04, read-only, no deployment):**
**configuration gate PASS — 434/434 keys accounted, 0 undeclared
customizations** (docs §13.2). Every persisted key from the owner-admin export
was diffed against the env-derived defaults of the exact pinned source (image
`ENV` block + proposed manifest env): 430 equal, 3 manifest-declared
differences (`openai.api_base_urls` 1→2 URLs, inert `web.search.engine`,
env-only `webui.url`), 1 Secret-backed shape change (`openai.api_keys` 1→2),
and **no** behaviorally relevant stored customization outside the declared
env. Four keys are env-governed rather than default-governed
(`ui.enable_signup`, `ui.default_user_role`, `ollama.enable`,
`web.search.searxng_query_url`), which is why those declarations are
load-bearing. **No preservation fix is required.** Remaining before deploy:
the pre-sync **admin recovery point is UNCONFIRMED** (docs §13.4), and the
post-deploy acceptance items (ACL, Pipelines startup, retained provisioning
Job, tracing, no-op sync, user/chat data, TLS) are proven only by exercising
the deployment — the fresh-isolated signup restore is already **PASS 8/8**
(docs §13.3). The owner's explicit deployment approval is still required; no
publication happened in this unit.

**Earlier named preflight unit (2026-11-04, local, no deployment):** both
previously open items are closed in the manifest; no owner decision was
required.
`install/openwebui.yaml` now declares `ENABLE_WEB_SEARCH=false` (web search
preserved OFF) and removes the legacy `ENABLE_RAG_WEB_SEARCH` /
`RAG_WEB_SEARCH_ENGINE` names, which the pinned 0.12.5 image does not read
(its defaults are `ENABLE_WEB_SEARCH=False`, `WEB_SEARCH_ENGINE=''`); the
SearXNG URL is unchanged and `WEB_SEARCH_ENGINE=searxng` is inert while
search is off. Code execution/interpreter are declared explicitly ON with
`pyodide` (`ENABLE_CODE_EXECUTION`/`CODE_EXECUTION_ENGINE`/
`ENABLE_CODE_INTERPRETER`/`CODE_INTERPRETER_ENGINE`), matching both the
inventory and the pinned defaults. Verdict for that unit: **named preflight PASS** (docs §13.1), superseded by
**§13.2 whole-config configuration gate PASS** (434/434 accounted, 0
undeclared customizations, no preservation fix required). Remaining before
deploy: the UNCONFIRMED pre-sync admin recovery point (docs §13.4), the
post-deploy acceptance items, and the owner's explicit deployment approval;
the fresh-isolated signup restore is PASS 8/8 (docs §13.3).

Files: `install/openwebui.yaml`; new `apply-openwebui-secrets.sh`,
`tests/test_openwebui_secrets.py`; `docs/headless-openwebui.md` (all module-hsworkshop
prefixed except docs).

- Tests first: stub `oc` wrong context/server refusal before input; missing input
  fails; semicolon in key rejects; no credentials in argv/output; only 2 Secrets
  mutated.
- Match existing custom-domain guard: hidden input/stdin/file, secure tmp cleanup,
  server-side apply, no `--force-conflicts`.
- Secret `openwebui-connections-secret` with `GUARDRAILS_API_KEY` /
  `PIPELINES_API_KEY`. Guardrails placeholder is unused/explicit and is NOT real
  protection (documented); the pipelines key is random and new — provisioning it
  affects the matching Pipelines manifest in Task 3; do not deploy in between.
- Explicit `secretKeyRef`s precede `OPENAI_API_KEYS=$(GUARDRAILS_API_KEY);$(PIPELINES_API_KEY)`.
- Ordered URLs
  `http://guardrails-proxy.hsworkshop.svc.cluster.local:8080/v1;http://pipelines.hsworkshop.svc.cluster.local:9099`
  (no whitespace); remove competing singular; verified index `OPENAI_API_CONFIGS` only
  if actually needed (current freeze: not needed — plural lists are index-paired and
  sufficient); do not whitelist filter; guardrails placeholder is not auth.
- Pin current OpenWebUI digest
  `ghcr.io/open-webui/open-webui@sha256:0b399a8ba34558e181d316750455f2a95376a38cd60a61b9c942e138898944bd`
  (0.12.5, commit `f7ac7ee10962ddec0017f5c29b6740d9b9c5faca`), no upgrade; preserve
  effective workshop settings before setting `ENABLE_PERSISTENT_CONFIG=false`.
- Secret `openwebui-admin-bootstrap-secret` with `WEBUI_ADMIN_EMAIL`/`PASSWORD`;
  existing admin reuse via explicit refs overrides legacy envFrom (keep
  `openwebui-secret` envFrom for signing keys/Langfuse; do not copy or read values);
  no signing secret changes; missing credentials block — never password
  reset/promote/disable auth. New Secret is for local creds metadata of the
  existing admin only — not permission to read/copy/reset.
- Startup env: `ENABLE_PERSISTENT_CONFIG=false` AND explicit `ENABLE_SIGNUP=false`
  (architect correction: the live DB already has effective `enable_signup:false`;
  env `ENABLE_SIGNUP=true` is currently a no-op lie — declaring `false` preserves
  the effective policy honestly under persistence-off). No auth hooks; no signup
  policy reopening in this task.
- **Deploy gate (blocker, recorded in runbook/docs):** the owner must first
  provide an owner-sanitized inventory of other stored effective config
  (connections beyond env, etc. — env list alone is insufficient) before
  `ENABLE_PERSISTENT_CONFIG=false` is deployed; otherwise persisted-but-undeclared
  settings would silently drop.
- Empty-DB native bootstrap only if verified; architect correction
  (2026-11-03): the claim "env cannot restore signup" is FALSE when
  `ENABLE_PERSISTENT_CONFIG=false` — models/config 136–150, 201–212 ignore
  persisted/in-memory config; first bootstrap (main 400–404) sets
  `enable_signup:false`; second start with existing users skips bootstrap
  (utils/auth 641–643), so declared `ENABLE_SIGNUP` can restore. Fixture proof
  pending (fresh isolated, after second start — Task 6).
- The provisioning Job itself is Task 4/5, not Task 2.
- Validation: run unittest module for the test file; deployment render asserts in
  Task 5.

## Task 3 — Reproducible Pipelines / tracing readiness (deps: 1, 2)

**Status: COMPLETE (LOCAL) 2026-11-03. Implemented and locally validated.
Gate assessment cleared: the exact hash-checked lock is installable through
the pinned image's own start.sh mechanism (`PIPELINES_REQUIREMENTS_PATH` →
`pip install -r`, hash-enforcing even without `--require-hashes` — verified
by dry-run + tampered-hash test against the pinned image). The lock's delta
closure is exactly langfuse 5.0.4 + its 8 OpenTelemetry dependencies;
grpcio and every other pinned-image dependency are already satisfied and
NOT upgraded (no new image build). Implemented: `pipelines/requirements.lock`
(9 packages, sha256-pinned), `pipelines/check_ready.py` (authenticated
discovery readiness: env key, bearer header, filter id/name + type=filter +
wildcard check, bounded timeout), `pipelines/langfuse_v4_filter_pipeline.py`
v1.1.0 (frontmatter requirements removed — lock is the single dep source;
managed valves `pipelines=["*"]`/`priority=0`/`debug=false` AND
host/secret_key/public_key re-asserted FROM ENV after every persisted-valves
overlay, before auth/use — stale persisted values never survive; missing env
creds raise MissingEnvCredentialsError fail-closed with NO personal-cloud
fallback; startup auth requires auth_check() True within a hard 300s total
deadline with per-call SDK timeouts capped to the REMAINING budget, transient
retry, invalid-credential immediate raise that fails process startup via the
lifespan handler; runtime valves-update path stays fail-open; idempotent
shutdown; review fixes: startup auth uses the pinned SDK's bounded request
mechanism `api.projects.get(request_options={timeout_in_seconds:
min(30, remaining), max_retries: 0})` — verified against the 5.0.4 wheel —
so Fern internal retries/Retry-After cannot overshoot the 300s deadline,
with auth_check-equivalent non-empty-projects semantics and failed-attempt
client cleanup; invalid-credential classification is exact+typed
(ApiError.status_code 401/403 only, no substring matching — 503s with
misleading 401/403 bodies retry), `install/pipelines.yaml` (digest-pinned image, PIPELINES_URLS
removed, init container on the SAME pinned image copying ONLY the managed
.py to the existing PVC preserving others/valves, PIPELINES_API_KEY explicit
ref to openwebui-connections-secret matching Task 2, startupProbe 6min
covering pip install + auth budget, readinessProbe via check_ready.py, NO
liveness probe so external Langfuse outages don't restart the container),
`install/kustomization.yaml` + new nested `pipelines/kustomization.yaml`
(configMapGenerator `pipelines-langfuse-filter` with LOCAL file sources in
the nested root — standard kustomize load restrictions stay fully enabled,
no flags, no Argo-default assumptions; base includes ../pipelines as a
directory resource; content-hash rolls the Deployment on any artifact
change), `tests/test_langfuse_pipeline.py` — 39/39 →
42/42 → 45/45 (latest, review worker 2026-11-04) unittest PASS offline
(mocked SDK/server, no secrets). Renders with standard kustomize (no flags):
base 40 docs (CM `pipelines-langfuse-filter-c2fgc85527`, volume ref
auto-rewritten), overlay 45 docs, certificate-only 2 docs.
`tests/run-tests.sh`: 50 PASS / 3 FAIL — the 3 are exactly the Task 2
baseline stale assertions owned by Task 5 (base-differs-from-HEAD, overlay
build assertion, Argo app assertion); the earlier 5-fail regression from
out-of-root generator paths is fixed via the nested kustomization + a
mirror-dependency copy of the pipelines directory in run-tests.sh (test
harness dependency only, not a Task 5 assertion change). LIVE GATES (not
performed, by scope): no cluster mutation, no deploy; runtime fixture
proofs (PVC valves startup, version lock in-pod) remain Task 6.**

Files: `pipelines/langfuse_v4_filter_pipeline.py`,
`pipelines/requirements.lock` (new), `pipelines/check_ready.py` (new),
`pipelines/kustomization.yaml` (new), `install/pipelines.yaml`,
`install/kustomization.yaml`, `tests/test_langfuse_pipeline.py` (new),
`tests/run-tests.sh` (mirror copy only), `docs/headless-openwebui.md`.

- Tests first: after persisted valves loaded, managed host keys targeted `*`,
  priority debug off — re-asserted before auth/valve updates; false auth fails;
  transients bounded ≤300 s per call; timeouts / invalid-credential prompt fail
  without leak; runtime fail-open preserved.
- Pin current digest; API key matching the Secret. Architect note: SDK 5.0.4
  is the actual installed version; the hash lock is tested against the pinned
  image. If an exact lock requires an unrelated gRPC dep or a new image build,
  STOP and return to the architect — that is Task 3 scope, not Task 2.
- Generated content-hash CM `pipelines-langfuse-filter` (filter + lock + readiness
  script). Same pinned image init copies ONLY the managed `.py` file to the existing
  PVC, preserving others/valves — no reset.
- Remove `PIPELINES_URLS` mutable exec.
- Exact compatible SDK5+ hash-checked dep lock through verified startup mechanism;
  requires new image build / dep unrelated upgrade → stop, architect.
- Startup/readiness: authenticated, expected filter discovery, keys via env not argv.
- Validation: unittest + render and approved fixture (version lock / PVC valves
  startup).

## Task 4 — Additive presets API convergence (deps: 1, 2)

**Status: COMPLETE (LOCAL) 2026-11-03. Implemented and locally validated.
`configuration-models.json` sanitized as the sole presets input (allowlist
exactly `career-guide`/`free-chat` on `qwen36-service`; pedagogy
prompts/names/intended capabilities preserved verbatim; removed real
user_id/user identity, created/updated timestamps, `write_access`, and the
~62 KB embedded profile PNG from each preset; canonical public-read grant
`{principal_type:'user', principal_id:'*', permission:'read'}` declared;
readable pretty-printed, 4.5 KB). New
`provisioning/provision_openwebui.py` (Python stdlib only, no dependency
changes): `load_presets(path)`, `normalize_managed(model)`,
`provision(base_url, email, password, presets) -> counts`, `main() -> int`
exactly as planned. Fail-closed unknown-schema allowlists on BOTH the
preset file (fields, meta, capabilities, params, builtinTools; grants must
be exactly the public-read grant — `anyone`/write/legacy
`access_control`/extra grants all rejected) and the server records
(unknown ModelResponse fields fail closed BEFORE any model write).
Signin: `POST /api/v1/auths/signin`; admin role verified from the
SessionUserResponse body (never trusted from the token); MFA-challenge and
pending-user variants fail closed; JWT kept in memory only. Writes:
additive changed-only `POST /api/v1/models/import {models:[forms]}` —
create forms carry no owner identity (server assigns the admin as owner;
no invented user_id is ever written); update forms reuse the SERVER record
verbatim (user_id/user/timestamps/write_access/version_id preserved; meta
merged so unmanaged server keys like a profile image survive; managed meta
keys absent from the preset are written as explicit `None` so
operator-added values — e.g. defaultFeatureIds/suggestion_prompts on
free-chat — are cleared without broad deletion; null-valued managed meta
keys compare equal to absent ones; tags validated as canonical
`[{name: str}]` dicts, string shorthand fails closed).
**Architect amendment (2026-11-04, review finding — grants):** OpenWebUI
0.12.5 import UPDATES never replace grants (only INSERTs carry them), so
import update forms pass the server's grants through VERBATIM and ACL
drift is converged separately via the architect-approved pinned exception
`POST /api/v1/models/model/access/update` body `{id, access_grants}`
(200 `ModelModel | null`; null is NOT success — the re-read is decisive),
scoped to exactly the managed ids career-guide/free-chat — the ONLY extra
mutating route, a grandfathered pinned exception, not a new auth policy.
Config and ACL drift are converged in ordered phases: create-missing
(import INSERT, re-read-validated BEFORE any access update — the access
endpoint can create incomplete records, so it is never used for creation)
→ config drift (import UPDATE, grants verbatim) → ACL drift (access
endpoint only). Grant comparison is semantic
(principal_type/principal_id/permission only; volatile server
id/resource_type/resource_id/created_at ignored). Server records: real
0.12.5 ModelResponse fields validated incl. server-owned `version_id`
(string | null) — excluded from comparison, preserved in update forms;
unknown fields still fail closed BEFORE any model write.
B3 tags (review-2): the pinned 0.12.5 ``ModelMeta.normalize_tags``
converts ``tags: None -> []``, so the comparable meta view normalizes an
EMPTY tags list to absent on both sides — a preset omitting tags
converges with a server reporting ``[]`` or ``None``, and an
operator-added tags list is cleared (managed key written as ``None``).
Phase gating (review-2): the drift is refreshed from a validated
re-read + diff after phase 1 (creates) and phase 2 (config) before
gating the next phase — a created record whose insert grants were
server-filtered gets its ACL drift seen and repaired by phase 3 within
the same invocation.
Bounded retries (3 reads, 3 write attempts per phase) with MANDATORY
re-read after every mutation attempt and re-read + re-diff before any
retry — uncertain write outcomes (network error, HTTP 5xx — the write may
have committed, e.g. a connection lost after delivery) are never blindly
duplicated; definite failures (HTTP 4xx, invalid request/schema) fail
closed immediately with no alternate API or account changes; persistent
non-convergence fails after the bounded retries (total request count
stays well inside the Job's 600 s activeDeadline). No full-sync endpoint,
no DELETE, no DB access, no account reset/promotion. HTTP: stdlib urllib
with a redirect-refusing opener (credentials/Bearer never cross hosts),
10 s timeouts, counters-only output (no JWT/credential/payload/API-error
bodies in any log or exception message). Base-URL validation: HTTPS
anywhere, plain HTTP only for documented in-cluster targets (localhost,
*.svc, *.svc.cluster.local, or a bare in-namespace service name — the
Task 5 Job's internal `http://openwebui:8080`); userinfo/query/fragment/
path rejected. Env contract (documented for Task 5, credentials from env
never argv): `OPENWEBUI_URL`, `OPENWEBUI_ADMIN_EMAIL`/`OPENWEBUI_ADMIN_PASSWORD`
with fallback to the Task 2 bootstrap Secret key names
`WEBUI_ADMIN_EMAIL`/`WEBUI_ADMIN_PASSWORD`, read-only input
`PRESETS_PATH` (default: `configuration-models.json` adjacent to the
script in `provisioning/`).
Counter semantics: `created`/`updated`/`unchanged` describe THIS run's
actions from the initial diff (a converged second run reports
created=0/updated=0/unchanged=all/import_batches=0/access_updates=0 —
zero import AND zero access POSTs); `verified` is set only when a final
re-read confirms the server state matches the presets.
`tests/test_openwebui_provisioning.py`:
62/62 unittest PASS offline against an in-process fake OpenWebUI API
(mocked HTTP, no network, no cluster, no live API writes, no real
credentials), covering the review cases: ACL-only drift converged via the
access endpoint only (no import), config+ACL both-drift ordering, an
uncertain ACL write that lands with zero duplicates, null-response
re-read-decisive, 403 denial fail-closed with no import fallback, bounded
ACL non-convergence, no-op second run (zero import and access posts),
operator-added managed meta key cleared, version_id accepted/ignored,
tags normalization (pinned ModelMeta.normalize_tags None→[]; operator-
added tags cleared, final state a converged no-op), and stale-gating
repair (a created record whose insert grants were server-filtered gets
its ACL repaired via the access endpoint within the SAME invocation).
Validation runs (2026-11-04, final after review-2): full Python
discovery across all four suites 159/159 OK (62 provisioning + 28 secrets
+ 45 langfuse + 24 manifests); provisioning module alone 62/62 OK; bash
`tests/run-tests.sh` 53 PASS / 0 FAIL with standard renders (no flags). LIVE GATES
(not performed, by scope): no cluster
login, no Secret reads, no API writes against real users, no deploy;
runtime fixture proofs (ordinary authenticated user read visibility,
anonymous denied, no edit) are Task 6.**

Files: `provisioning/configuration-models.json` (sanitized in place; moved
from the module root to `provisioning/` by the Task 5 amendment — content
unchanged, sole canonical copy),
`provisioning/provision_openwebui.py` (new),
`tests/test_openwebui_provisioning.py` (new).

Local validation checksums (sha256, current after the 2026-11-04 review
amendments — see Task 4 status for the full change list):
`provisioning/configuration-models.json`
`bf7e8a2870fd95760160d4e2137a8e804f20df7d989453477a37c209fb30c1de`
(unchanged by the amendments).

- Sanitize `configuration-models.json` as sole input: allowlist
  `career-guide`/`free-chat` on `qwen36-service`; preserve prompts/pedagogy/approved
  capabilities; remove user info, IDs/timestamps, write fields, embedded PNG.
- New `provisioning/provision_openwebui.py` (stdlib); `tests/test_openwebui_provisioning.py`.
- Interfaces: `load_presets(path) -> list[dict]`, `normalize_managed(model) -> dict`,
  `provision(base_url, email, password, presets) -> dict counts`, `main() -> int`.
- Credentials/base URL from input env; no logs of JWT/payloads; counts only.
- Tests first: unknown schema fails closed; admin 403 → no reset/promotion;
  missing/create changed-only; no-op second run performs no writes; unrelated/private
  presets preserved (ownership, unmanaged); partial import skips/timeouts re-read
  before retry (bounded API).
- Supported: auth/list/import/re-read/discovery ONLY. Single actual public-read
  representation — architect correction (2026-11-03): exact ACL grant is
  `{principal_type:'user', principal_id:'*', permission:'read'}` (authenticated
  read), NOT `anyone` (no-auth), NOT legacy `access_control`, NOT wildcard
  write. No public write / anonymous auth bypass. Tests: ordinary
  authenticated read works, no edit, anonymous denied.
- Validation: unittest with mocks; later, pinned fixture ordinary-user visibility.

## Task 5 — Hook integration / offline regression (deps: 2–4)

**Status: COMPLETE (LOCAL) 2026-11-03.** Architect+planner amendment
executed: the sole canonical presets JSON was MOVED (content unchanged)
to `provisioning/configuration-models.json` — no duplicate, no symlink —
making both generator sources local to a nested `provisioning/`
kustomization root and resolving the earlier generator-layout blocker
under standard kustomize load restrictions (no flags). Implemented:
`provisioning/kustomization.yaml` (generator-only; content-hash CM
`openwebui-workshop-provision`, ns hsworkshop, observed hash suffix
`cd2gcmtk89`), `install/openwebui-provisioning.yaml` (PostSync Job:
BeforeHookCreation ONLY, existing hsworkshop-anyuid SA with automount
false and no added RBAC, same pinned 0.12.5 image with explicit python,
env exactly the Task 4 contract with Task 2 bootstrap secretKeyRefs,
internal `http://openwebui:8080`, readOnly CM mount, no PVC, Never /
backoffLimit 3 / activeDeadlineSeconds 600, 100m/128Mi), wiring in
`install/kustomization.yaml` (`openwebui-provisioning.yaml` +
`../provisioning`), provisioner default `PRESETS_PATH` now adjacent to
the script (cwd-independent; env override preserved),
`tests/test_headless_manifests.py` (new, 24 tests), Task 4 test updates
(+3 default/override tests, 52 total), and `tests/run-tests.sh` (mirror
+ provisioning copy; the three stale assertions repaired: semantic base
independence with parsed PVC set, plural URLs + `ENABLE_SIGNUP=false`
desired state, actual overlay Argo path; a pipefail/SIGPIPE shell grep
replaced by parsed-YAML asserts — no weakening). Validation:
**146/146 Python unittest** (28+42+52+24), **`bash
module-hsworkshop/tests/run-tests.sh` 53 PASS / 0 FAIL** (first fully
green run since Task 2), standard renders base 42 / overlay 47 /
certificate-only 2 (no flags). LIVE GATES (not performed, by scope): no
Secret provisioning, no cluster mutation/deploy/full-sync, no live API
writes; the Task 2 owner-admin sanitized-inventory deploy gate still
applies; runtime fixture proofs are Task 6.

Files: `install/openwebui-provisioning.yaml` (new),
`provisioning/kustomization.yaml` (new),
`provisioning/configuration-models.json` (moved from module root,
content unchanged), `provisioning/provision_openwebui.py` (default
PRESETS_PATH), `install/kustomization.yaml`, `tests/
test_headless_manifests.py` (new), `tests/test_openwebui_provisioning.py`,
`tests/run-tests.sh`, `docs/headless-openwebui.md` §12, module README,
root README.

Files: new `install/openwebui-provisioning.yaml`, `tests/test_headless_manifests.py`;
modify `install/kustomization.yaml`, `tests/run-tests.sh`, root README,
`docs/headless-openwebui.md`.

- Hash CM `openwebui-workshop-provision` (script + sole presets).
- Job `openwebui-workshop-provision`: PostSync, `BeforeHookCreation` ONLY (retain last
  result); existing `hsworkshop-anyuid` SA (existing pattern, no added RBAC or
  privileges — architect originally suggested a dedicated SA; planner follows the
  existing-SA minimization, accepted); `automount false`; pinned OpenWebUI image with
  explicit python; readonly script/data; no PVC mount; bootstrap Secret refs; internal
  URL; backoff 3, activeDeadline 600, restartNever, bounded HTTP.
- Test hash refs, keys, order, images, Job spec, no plaintext secrets, preservation of
  PVC/model/TLS.
- Existing custom-domain tests: adapt generator inputs/plural env/current active
  overlay; remove whole-base-equals-HEAD assertion, replace with semantic
  independence, preserving the 5 overlay additions / cert-only / determinism / model
  sources.
- Validation: Python unittest discovery `tests/test_*.py`, then
  `bash tests/run-tests.sh`, then kustomize base/full/cert-only renders.
- Docs: full app health, PostSync block on unrelated unhealthy (stop, don't bypass);
  selective sync without hook; no automatic failed-sync retry claim; repeat full
  sync/no-op; runtime vs startup drift.
- Rollback: additive — reapply prior presets; Git revert does not undo DB writes; no
  PVC delete / signing rotation. No default tools enabling.

## Task 6 — Separately approved persisted + fresh acceptance (deps: 1–5 reviewed)

Status 2026-11-04 (bounded predeployment unit; NOT complete):
`docs/headless-openwebui.md` §13.2 configuration gate PASS (434/434 keys,
0 undeclared customizations) and §13.3 fresh-isolated signup fixture PASS
(8/8 checks) close the local preflight items. **The pre-sync prerequisite
`existing admin recovery point` was UNCONFIRMED** (§13.4: read-only metadata
check found no `VolumeSnapshot` of `hsworkshop/openwebui-data`, and no project
backup document is named; absence of that one kind does not prove no backup).
**Owner decision 2026-11-04: explicitly waived — "I don't need a backup"**
(docs §13.4.1). No snapshot/backup was created; the waiver is owner risk
acceptance and does not assert a recovery point exists.
Every remaining bullet below — persisted sync/Job/data preservation/no-op/
restart, ACL, Pipelines startup, tracing, streaming finalization, TLS —
requires the deployed revision and is **explicit controlled post-deploy
acceptance, not proven**. Publication of the reviewed local work was explicitly
approved and performed on `zijeme-it`; no PR/merge.

Approval scopes (separate): named secret provision; deployment full sync; designated
synthetic account/model writes; isolated fresh fixture/restarts/fault tests. No
implicit Git publish.

- Reconfirm correct cluster/health/revisions; existing admin recovery point;
  sanitized baseline; no broad user/chat scan.
- Persisted: full sync, retained Job success; designated existing user/chats and
  signing secrets unchanged; public read, no write; unrelated private model
  preserved; no-op second run; startup settings across restart.
- Fresh isolated (approved): empty DB, identical pins; native admin and second-start
  signup restore, user role — else STOP, no auth hooks. Architect note: the
  fresh-isolated signup-restore-after-second-start test is still PENDING
  (fixture scope, this task). `/api/chat/completed` is deprecated — outlet
  filters run inline; never add a routine extra finalizer on that route
  (duplicate-finalization risk).
- Delayed Langfuse readiness: invalid keys fail startup fixture.
- Browser lifecycle: streaming + non-stream finalization when needed → completed
  generation (input/output/usage/session) vs blocked proxy — separate, no double
  count.
- Both TLS origins / canonical cookies / original Route / Qwen / guardrails
  unchanged.
- Record actual evidence; no broad logs; no credential leaks. No acceptance claims
  from discovery alone.

## Unresolved (after Task 1)

Resolved by Task 1 (see `docs/headless-openwebui.md`): digests/versions,
API presence and shapes, ACL representation, import ownership semantics,
plural connection env support, admin-bootstrap signup side effect,
`/api/chat/completed` deprecation (inline outlet finalization).

Still unresolved / owner inputs: **persisted connections inventory beyond
env — DONE 2026-11-04** (owner-admin authenticated, sanitized; one persisted
OpenAI connection, no hidden providers; see `docs/headless-openwebui.md`
§13). The two named gate items were resolved locally on 2026-11-04 (no owner
decision): `ENABLE_WEB_SEARCH=false` preserves web search OFF with the env
name 0.12.5 actually reads, and `code_execution`/`code_interpreter` are
declared explicitly ON with `pyodide`. The `ENABLE_PERSISTENT_CONFIG=false`
deploy gate **configuration comparison is now PASS** (docs §13.2): all 434
persisted keys were diffed against the env-derived defaults of the pinned
source — 430 equal, 3 manifest-declared differences, 1 Secret-backed shape
change, 0 undeclared customizations, so nothing stored is dropped beyond the
declared env. Of the Task 6 runtime proofs, the fresh-isolated second-start
signup-restore test is **CLOSED — PASS 8/8 checks** (docs §13.3); the
remaining runtime proofs (ACL, Pipelines startup, retained Job, tracing,
no-op sync, live user/chat data, TLS) need the deployment and are explicit
**post-deploy acceptance** (docs §13.4), not pre-sync work. The Task 6
prerequisite `existing admin recovery point` was **UNCONFIRMED**
(docs §13.4) and was **explicitly waived by the owner on 2026-11-04**
("I don't need a backup"; docs §13.4.1) — no snapshot/backup was created and
the waiver is not evidence of a recovery point. Pipelines
digest→commit mapping: resolved as not-needed (freeze by digest; no further
lookup).
Task 2 COMPLETE (LOCAL) 2026-11-03 — 25/25 unit tests, renders verified;
live deployment gated on owner-admin inventory (see Task 2 status).
Tasks 3+ not started. No optional
improvements.
Task 3 COMPLETE (LOCAL) — latest unit count 45/45 (initial 39/39,
extended by the review worker 2026-11-04); standard-kustomize
base/overlay/cert-only renders verified, run-tests.sh back at the Task 2
baseline (50 PASS / 3 pre-existing stale asserts owned by Task 5); live
deployment still gated (runtime fixture proofs are Task 6 scope).
Task 4 COMPLETE (LOCAL, amended 2026-11-04 after review) — 62/62
provisioning unit tests; full Python discovery across all four suites
159/159 OK (62 provisioning + 28 secrets + 45 langfuse + 24 manifests);
bash run-tests.sh 53 PASS / 0 FAIL; sanitized presets + stdlib API
provisioner with architect-approved
access-update endpoint implemented; live API writes and runtime
fixture proofs remain Task 6 scope.
Checkbox steps per task are independently verifiable as written above. Planner owns
revision; material changes return to the planner.
