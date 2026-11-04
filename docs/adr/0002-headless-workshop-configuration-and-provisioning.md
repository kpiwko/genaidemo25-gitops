# ADR-0002: Headless workshop configuration and provisioning

- Status: Accepted direction; implementation conditional on version-specific preflight
- Date: 2026-11-03
- Scope: `module-hsworkshop` (OpenWebUI chat + Pipelines + Langfuse tracing)
- Related: ADR-0001 (binding), Implementation plan
  `docs/plans/2026-11-03-headless-openwebui.md`

## Context

The browser workshop currently requires manual Admin Panel work: OpenAI-compatible
connection setup, model JSON import, and Langfuse tracing configuration. The deployed
OpenWebUI/Pipelines/Langfuse versions are not yet verified against upstream sources,
and the live environment carries irreplaceable state (users/chats PVCs, signing
secrets, user branding, ADR-0001 TLS setup).

## Decision

1. **Keep the browser and the current architecture.** No frontend Insights, no Goose,
   no upgrades, no auth-policy expansion, no infrastructure rewrite. Pin the current
   OpenWebUI/Pipelines digests and Langfuse 4.71; preserve PVCs, signing secrets,
   signup-enabled default, branding, SearXNG flags, both TLS origins, canonical
   cookies on the original Route, and the model Helm source (ADR-0001 remains
   binding).
2. **API-additive, not destructive sync.** Presets are provisioned through the
   OpenWebUI API (`signin`, `models/all`, `models/import {models:[changed]}`,
   discovery) — never direct DB access or `/models` sync. Imports are additive:
   only changed/missing models are written; unrelated and private models are
   preserved; a second run is a no-op. Partial imports and timeouts re-read state
   before retry; unknown schemas/ownership fail closed.
3. **Git owns startup, not the database.** Connection URLs and bootstrap credentials
   are declared in Git as explicit `secretKeyRef`s into two Secrets
   (`openwebui-connections-secret`, `openwebui-admin-bootstrap-secret`) applied by a
   guarded, server-side (non-forced) script modeled on the custom-domain pattern.
   Effective workshop settings are preserved before disabling
   `ENABLE_PERSISTENT_CONFIG`; there is no continuous database self-healing.
   Architect-verified refinement (2026-11-03, planner authorized): the live
   effective signup is `false`, so the startup env declares explicit
   `ENABLE_SIGNUP=false` alongside `ENABLE_PERSISTENT_CONFIG=false` (no auth
   hooks, no signup-policy reopening; with persistence off, declared env
   governs — fixture proof pending). The admin-bootstrap Secret holds local
   credentials metadata of the existing admin only and grants no permission to
   read, copy, or reset existing secrets. Deploying persistence-off is gated on
   an owner-sanitized inventory of other stored effective config (env list
   alone is insufficient).
4. **No account takeover.** The provisioning job authenticates as the existing admin
   via bootstrap Secret refs; missing credentials block the run. Never password
   reset, promotion, auth disablement, or signing-secret changes. Admin 403 fails
   closed. Credentials never appear in argv, logs, or docs.
5. **Reproducible Pipelines/tracing.** The Langfuse v4 filter is Git-owned with a
  content-hash ConfigMap, a checked dependency lock (`requirements.lock`), a
  readiness check, and a same-image init container that copies only the managed
   filter file onto the existing PVC (valves and other files preserved). Filter
   valves (managed host, `*` targets, priority/debug) are re-asserted before
   auth/valve updates; invalid credentials fail startup while runtime fail-open is
   preserved; `PIPELINES_URLS` mutable exec is removed.
6. **Bounded PostSync Job, additive rollback.** `openwebui-workshop-provision` runs
   as an Argo CD PostSync hook with `BeforeHookCreation` only (last result retained),
   on the existing `hsworkshop-anyuid` service account with no added RBAC, no PVC
   mount, pinned image, bounded HTTP/backoff/deadline, and restart policy Never.
   Rollback is additive: reapply prior presets; a Git revert does not undo DB writes.
7. **Approvals.** Secret provisioning, deployment sync, designated synthetic
   account/model writes, and isolated fresh-fixture acceptance are separately
   approved scopes. No implicit Git publish; publishing to the auto-deploying branch
   requires a verified feature branch and review target first.

## Preconditions

Implementation is conditional on a read-only Task 1 preflight that freezes the
actually deployed image versions and API contracts (signin, models list/import,
discovery, ACL representation, plural connections, persisted valves/finalization
lifecycle) into a sourced compatibility matrix accepted by the architect. Unsupported
findings stop implementation and return to the architect rather than being guessed.

## Consequences

- No manual Admin Panel setup is needed for connections, model presets, or tracing.
- The exact API shapes, digests, ACL representation, signup behavior on second
  start, dependency lock, and finalization semantics are unresolved until the
  preflight; this ADR does not claim any specific API is verified for the actual
  deployed version, nor any runtime proof.
- Acceptance (persisted and fresh-cluster) is a separately approved phase, including
  browser-level streaming and non-streaming trace finalization evidence.
- Architect-verified corrections (2026-11-03): the exact public-read ACL
  grant is `{principal_type:'user', principal_id:'*', permission:'read'}`
  (authenticated read only — not `anyone`/no-auth, not legacy
  `access_control`, not wildcard write; imports use the grants-list
  representation only). OpenWebUI 0.12.5 runs outlet filters inline;
  `/api/chat/completed` is deprecated and must not receive a routine extra
  finalizer (duplicate-finalization risk). Freeze values are final
  (OpenWebUI 0.12.5 digest/commit, Pipelines digest, Langfuse SDK 5.0.4);
  no further upstream lookups.
