# Plan: chat-only custom domain for OpenWebUI (chat.hsworkshop.fous.ai)

Status: **certificates issued (staging + production, certificate-only), not yet activated** —
token provisioned and both issuances verified live; the app DNS CNAME is still missing and
the overlay is not yet committed/pushed or activated in Argo CD.
Date: 2026-11-02. Branch: `zijeme-it` (uncommitted working tree at time of writing).

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
| 9 | Token provisioning + Cloudflare DNS record | **token done** (user applied out-of-band; verified Secret `cloudflare-dns01` metadata/key `api-token` only, value never read). **DNS record NOT created**: `chat.hsworkshop.fous.ai` is authoritative NXDOMAIN at `fous.ai` (Cloudflare `peaches`/`amos`). CNAME needed before Phase-2 traffic, but not a certificate blocker — DNS-01 issued without it |
| 10 | Phase 1 (staging certificate-only → production re-issue) → verify the **Secret's x509 issuer is a production trusted CA with exact SAN** (a stale `Ready` after the issuerRef switch is not trusted; revision bump + production CertificateRequest corroborate) → **commit/push the production render** → activate via `sources[0].path` JSON patch | **staging done + production done 2026-11-02, gated on actual cert** — staging rev=1 (issuer `(STAGING) Dastardly Durum YR1`); production re-render (`ACME_ENV=production`, same verified inputs) → rev=2, new `CertificateRequest/openwebui-custom-cert-2` with `issuerRef=letsencrypt-production-dns01` Ready; decoded `tls.crt`: issuer `C=US, O=Let's Encrypt, CN=YR1` (no STAGING), exact single SAN `chat.hsworkshop.fous.ai`, new serial `05E5E383F7BE2EA89519E513C386D8B91296`, expiry 2027-01-31; full chain (YR1 ← ISRG Root YR ← ISRG Root X1) `openssl verify` **OK against system trust**; Order `valid`. Remaining: DNS CNAME, commit/push, Argo path patch |

## Verification performed

- `bash module-hsworkshop/tests/run-tests.sh` → 53 PASS / 0 FAIL (details in
  `docs/custom-domain.md` §Tests).
- `kustomize build module-hsworkshop/overlays/custom-domain` → OK.
- `kustomize build module-hsworkshop/install` → OK (base unchanged).
- `bash -n` on all three scripts → OK.
- Live DNS evidence recorded in `docs/custom-domain.md` §Prerequisites.

## Out of scope / guarded

- No Routes/DNS activation, no certificate issuance, no cluster mutation, no commit/push.
- No change to Langfuse/Insights/Argo URLs, Qwen model, signup policy, global ingress/API.
- No secret manager; Kubernetes Secret only (user-approved).
- No generic secretName/inline-key-copy controller; native `externalCertificate` only —
  if the live cluster rejected it, the contract says return evidence, not install infra.
