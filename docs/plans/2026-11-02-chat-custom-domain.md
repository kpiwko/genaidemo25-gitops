# Plan: chat-only custom domain for OpenWebUI (chat.hsworkshop.fous.ai)

Status: **ACTIVATED and verified live (2026-11-02)** — `https://chat.hsworkshop.fous.ai`
serves OpenWebUI with the production Let's Encrypt certificate; HTTP redirects to HTTPS;
the original cluster-domain Route remains admitted and serving. See
[Verification performed](#verification-performed).
Date: 2026-11-02. Branch: `zijeme-it` (activation commits `9dd5a61`, `0e6db65`, `b256ee1`,
`62a4b3e` pushed to `origin/zijeme-it`).

## Goal

Serve OpenWebUI only, on `https://chat.hsworkshop.fous.ai`, with a Let's Encrypt
certificate via cert-manager DNS-01 (Cloudflare), attached to a new Route through
`spec.tls.externalCertificate`. No change to Langfuse/Insights/Argo URLs, the Qwen model,
signup policy, or global ingress/API settings.

## Design (accepted, see ADR-0001)

- Ordinary Kustomize overlay `module-hsworkshop/overlays/custom-domain` on `../../install`.
- Deterministic non-secret renderer `module-hsworkshop/domain/render.sh`
  (`WORKSHOP_DOMAIN` → chat host `chat.<WORKSHOP_DOMAIN>`; `ACME_ENV` staging|production).
  Explicit environment variables win over `config.env` (parsed, never sourced); inputs
  validated (DNS syntax, sed metacharacters, `chat.`-prefix).
- Namespaced `Issuer` + `Certificate` (SAN exactly `chat.hsworkshop.fous.ai`) in `hsworkshop`.
- **Two-phase activation**: Phase 1 deploys certificate-only
  (`overlays/custom-domain/certificate-only/`, no Route, no RBAC) so the production
  Certificate is `Ready` **before** the custom Route is admitted; Phase 2 activates the
  full overlay. Sync waves (-2 cert, -1 RBAC, 0 Route) order objects but are not relied
  on for readiness gating, since Argo CD `Certificate` health behavior was not verifiable
  offline.
- New `Route/openwebui-custom`, edge termination, `externalCertificate.name`.
- Minimal router RBAC: `get,list,watch` on Secrets, `resourceNames: [openwebui-custom-tls]`,
  bound to `openshift-ingress:router`. No blanket secret access, no extra cluster roles.
- Cloudflare token: Kubernetes Secret `cloudflare-dns01` applied out-of-band by
  `apply-custom-domain-secret.sh` (never in Git), **server-side apply without
  `--force-conflicts`** (no last-applied annotation embedding the token, no forced
  ownership). Zone-scoped to `fous.ai` (`Zone:DNS:Edit` + `Zone:Zone:Read`) —
  `hsworkshop.fous.ai` is not a delegated zone.
- DNS: Cloudflare **DNS-only** CNAME `chat.hsworkshop.fous.ai` →
  `router-default.apps.kpicl2-pool-rft8p.aws.rh-ods.com` (verified same LB IPs as the
  live openwebui Route host).
- OpenWebUI: `WEBUI_URL`, `WEBUI_SESSION_COOKIE_SECURE=true`, `WEBUI_SESSION_COOKIE_SAME_SITE=lax`,
  `CORS_ALLOW_ORIGIN` with chat host + existing cluster-domain alias (intentional).
  Activating/deactivating the overlay restarts the OpenWebUI pod (Deployment patch).
- Staging ACME by default; production rendered explicitly only after staging validates.
  Staging certificates are never trusted — verified via the issued Secret
  (`openssl x509`), never via a browser staging route.
- Argo CD app source path **unchanged** in this unit; activation is a documented,
  explicit opt-in: **commit/push the overlay first**, then a JSON patch on
  `sources[0].path` only (preserves the model source array).

## Steps

| # | Step | Status |
|---|---|---|
| 1 | Read-only preflight: repo state, DNS (zone authority, CNAME target, no existing record) | done |
| 2 | Docs research: OCP 4.21 Route `externalCertificate`, cert-manager Cloudflare DNS-01, OpenWebUI env vars | done |
| 3 | Renderer + templates + overlay | done |
| 4 | Token provisioning script (out-of-band, guarded, no controller restarts) | done |
| 5 | Test suite (offline) | done — 53 PASS / 0 FAIL |
| 6 | Canonical overlay rendered in-repo (staging) + Phase-1 certificate-only dir; both kustomize builds | done |
| 7 | Docs: `docs/custom-domain.md` (two-phase activation, prerequisites, verification, targeted cleanup), ADR-0001, README links | done |
| 8 | **Cluster-side read-only preflight** (live CRD/schema, router SA, cert-manager operand readiness, existing Issuers, ingress hostname) | **done 2026-11-02** (live preflight prior unit: RouteExternalCertificate enabled, router SA `openshift-ingress/router`, cert-manager operands 1/1, operator 1.20.1; credentials valid this run) |
| 9 | Token provisioning + Cloudflare DNS record | **done** — token applied out-of-band (Secret `cloudflare-dns01` verified, value never read); DNS CNAME created by the user: `chat.hsworkshop.fous.ai` → `router-default.apps.kpicl2-pool-rft8p.aws.rh-ods.com`, **DNS-only** (verified: resolves to the router LB IPs `16.58.185.248`/`3.130.202.84`, not Cloudflare edge IPs) |
| 10 | Phase 1 (staging certificate-only → production re-issue) → verify the **Secret's x509 issuer is a production trusted CA with exact SAN** (a stale `Ready` after the issuerRef switch is not trusted; revision bump + production CertificateRequest corroborate) → **commit/push the production render** → activate via `sources[0].path` JSON patch | **done 2026-11-02** — production cert verified (issuer Let's Encrypt `YR1`, serial `05E5E383F7BE2EA89519E513C386D8B91296`, exact single SAN, chain verified against system trust, rev=2, expiry 2027-01-31); overlay committed/pushed (`b256ee1`), Argo app manifest updated in Git (`62a4b3e`), live `sources[0].path` JSON patch applied + refresh; all 5 overlay objects Synced/Healthy; **live activation verified end-to-end** (see [Verification performed](#verification-performed)) |

## Verification performed

- `bash module-hsworkshop/tests/run-tests.sh` → 53 PASS / 0 FAIL (details in
  `docs/custom-domain.md` §Tests).
- `kustomize build module-hsworkshop/overlays/custom-domain` → OK.
- `kustomize build module-hsworkshop/install` → OK (base unchanged).
- `bash -n` on all three scripts → OK.
- Live DNS evidence recorded in `docs/custom-domain.md` §Prerequisites.

### Live activation verification (2026-11-02)

- DNS: `chat.hsworkshop.fous.ai` CNAME → `router-default.apps.kpicl2-pool-rft8p.aws.rh-ods.com`
  → LB IPs `16.58.185.248`, `3.130.202.84` (identical to the router's IPs; **DNS-only, not
  proxied** — verified before activation).
- Published: `9dd5a61` (docs/ADR/plan), `0e6db65` (renderer/templates/tests),
  `b256ee1` (production overlay), `62a4b3e` (Argo app `sources[0].path` → overlay) pushed
  to `origin/zijeme-it`; remote HEAD verified `62a4b3e` before the live patch.
- Live patch: JSON patch `sources[0].path` only (to
  `module-hsworkshop/overlays/custom-domain`) + refresh annotation. Second source
  (Qwen `shared-cluster/deploy-model`, `qwen36-35b-a3b-nvfp4-hsworkshop.yaml`) verified
  unchanged before and after.
- Argo CD: all 5 overlay objects **Synced and Healthy** —
  `Certificate/openwebui-custom-cert` (Healthy, up to date),
  `Issuer/letsencrypt-production-dns01` (Healthy, ACME account registered),
  `Role/openwebui-custom-cert-reader`, `RoleBinding/openwebui-custom-cert-reader`,
  `Route/openwebui-custom` (Healthy). No pending operation; no terminate needed.
  Pre-existing non-blocking statuses unrelated to this change: `Job/minio-create-bucket`
  (empty status) and `InferenceService/qwen36-service` OutOfSync (both observed before
  the patch).
- Route `openwebui-custom`: **Admitted=True**, host `chat.hsworkshop.fous.ai`,
  `routerCanonicalHostname router-default.apps.kpicl2-pool-rft8p.aws.rh-ods.com`,
  `spec.tls.externalCertificate.name: openwebui-custom-tls`; no
  `ExternalCertificateValidationFailed` condition.
- Served TLS (`openssl s_client`): issuer `C=US, O=Let's Encrypt, CN=YR1`, subject
  `CN=chat.hsworkshop.fous.ai`, **serial `05E5E383F7BE2EA89519E513C386D8B91296` — matches
  the production Secret exactly**, exact single SAN `chat.hsworkshop.fous.ai`;
  `-verify_return_error` → **Verification: OK (return code 0)**.
- `curl https://chat.hsworkshop.fous.ai/` → **200** (`ssl_verify_result=0`, no `-k`).
- `curl https://chat.hsworkshop.fous.ai/health` → **200** (`ssl_verify_result=0`).
- `curl http://chat.hsworkshop.fous.ai/` → **302 → `https://chat.hsworkshop.fous.ai/`**
  (HTTP → HTTPS redirect works).
- OpenWebUI pod `openwebui-857749fbb6-pdnb7` Running/ready after the Deployment rollout;
  live env verified: `WEBUI_URL=https://chat.hsworkshop.fous.ai`,
  `WEBUI_SESSION_COOKIE_SECURE=true`, `WEBUI_SESSION_COOKIE_SAME_SITE=lax`,
  `CORS_ALLOW_ORIGIN` = chat host + cluster-domain alias; `ENABLE_SIGNUP`/`DEFAULT_USER_ROLE`
  untouched.
- Original Route `openwebui` (cluster domain): **still Admitted=True and serving** (200 on
  `/health`). Note: it is edge-terminated with **no custom cert in the base**, so it serves
  the router's default self-signed certificate — pre-existing behavior, unchanged by this
  activation (browsers show the cluster-cert warning there; the custom domain is the trusted
  entrypoint).

## Out of scope / guarded

- No Routes/DNS activation, no certificate issuance, no cluster mutation, no commit/push.
- No change to Langfuse/Insights/Argo URLs, Qwen model, signup policy, global ingress/API.
- No secret manager; Kubernetes Secret only (user-approved).
- No generic secretName/inline-key-copy controller; native `externalCertificate` only —
  if the live cluster rejected it, the contract says return evidence, not install infra.
