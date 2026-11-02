# Optional chat-only custom domain: `chat.hsworkshop.fous.ai`

Opt-in Kustomize overlay that serves **OpenWebUI only** (not Langfuse, not Insights) on
`https://chat.hsworkshop.fous.ai` with a Let's Encrypt certificate issued via cert-manager
DNS-01 (Cloudflare), attached to a **new** Route through `spec.tls.externalCertificate`.

The default Route `openwebui` (cluster domain) and all other URLs stay unchanged. The overlay
is **not active** until you point the Argo CD Application at it (see [Activation](#activation)).

## Hard prerequisites (all must be true before starting)

| # | Prerequisite | Why |
|---|---|---|
| 1 | cert-manager CRDs (`cert-manager.io/v1` `Issuer`, `Certificate`) served by the cluster API | The overlay applies these kinds; without the CRDs the sync fails |
| 2 | cert-manager controller/webhook/cainjector Running (Operator 1.20.1 installed) | Nothing issues or renews the certificate otherwise |
| 3 | `Route.spec.tls.externalCertificate` accepted by the cluster (OCP 4.21, `RouteExternalCertificate` feature gate — in the Default feature set) | The Route references the Secret this way |
| 4 | Cloudflare API token created and provisioned as Secret `cloudflare-dns01` | DNS-01 solver credential |
| 5 | Cloudflare DNS record for `chat.hsworkshop.fous.ai` | Traffic and the CNAME target must resolve |
| 6 | **The overlay is committed and pushed** | Argo CD can only activate what is in Git |

> Live cluster-side verification of items 1–3 was **not** possible at implementation time
> (expired credentials — see [Blockers](#blockers)). Verify them read-only before activation:
>
> ```bash
> oc api-resources --api-group=cert-manager.io            # Issuer + Certificate present
> oc get deployments -n cert-manager-operator             # controller/webhook/cainjector Running
> oc get clusterfeaturegate cluster -o yaml | grep -A2 RouteExternalCertificate   # if present
> ```

## What the overlay adds (exactly 5 objects, nothing else)

| Object | Sync wave | Purpose |
|---|---|---|
| `Issuer/letsencrypt-<env>-dns01` | -2 | Namespaced ACME issuer, Cloudflare DNS-01 solver, token from Secret `cloudflare-dns01` |
| `Certificate/openwebui-custom-cert` | -2 | Single SAN `chat.hsworkshop.fous.ai` → Secret `openwebui-custom-tls` |
| `Role/openwebui-custom-cert-reader` | -1 | Router SA `get,list,watch` on Secrets, `resourceNames: [openwebui-custom-tls]` only |
| `RoleBinding/openwebui-custom-cert-reader` | -1 | Binds the Role to `openshift-ingress:router` |
| `Route/openwebui-custom` | 0 | Edge termination, host `chat.hsworkshop.fous.ai`, `spec.tls.externalCertificate.name: openwebui-custom-tls` |

Plus one Deployment patch on `openwebui`: `WEBUI_URL=https://chat.hsworkshop.fous.ai`,
`WEBUI_SESSION_COOKIE_SECURE=true`, `WEBUI_SESSION_COOKIE_SAME_SITE=lax`,
`CORS_ALLOW_ORIGIN` = chat host **and** the existing cluster-domain Route (alias kept
intentionally during transition). Signup policy (`ENABLE_SIGNUP`, `DEFAULT_USER_ROLE`) is
deliberately untouched.

**Coupling to note:** the env patch changes the OpenWebUI **Deployment**, so activating or
deactivating the overlay **restarts the OpenWebUI pod** (rollout). Both Routes keep serving
during the transition (the base Route is never removed).

**Documented limitation:** OpenWebUI serves both Routes; it does not redirect the alias
host to the canonical chat host. A canonical redirect would need router-level logic and
would affect the shared default Route — out of scope.

## 1. Create the least-privilege Cloudflare token

Cloudflare dashboard → user profile → **API Tokens → Create Token**:

- Permissions: `Zone - DNS - Edit`, `Zone - Zone - Read`
- Zone Resources: `Include - Specific zone - fous.ai`

> `fous.ai` is the authoritative zone (verified: NS `peaches`/`amos.ns.cloudflare.com`);
> `hsworkshop.fous.ai` is **not** a delegated zone, so zone-scoping to `fous.ai` is the
> narrowest workable scope.

## 2. Provision the Kubernetes Secret (out-of-band, never in Git)

```bash
./module-hsworkshop/apply-custom-domain-secret.sh
# or non-interactive:
#   CLOUDFLARE_TOKEN_FILE=~/cf-token.txt ./module-hsworkshop/apply-custom-domain-secret.sh
#   echo "$TOKEN" | ./module-hsworkshop/apply-custom-domain-secret.sh   # stdin, one line
```

The script: validates context **and** API server **before** reading the token; reads it
hidden (`read -s` with confirmation, or file/stdin — a missing file is an error); rejects
whitespace/malformed tokens; never puts the token in argv or logs; uses **server-side
apply** (no `last-applied-configuration` annotation embedding the token) **without**
forcing ownership; writes only Secret `cloudflare-dns01` (key `api-token`) in namespace
`hsworkshop`; is idempotent; reads no other secrets; restarts no shared controllers
(cert-manager watches Secrets and reconciles on its own); never switches the kubeconfig
context.

## 3. Create the DNS record (Cloudflare)

| Type | Name | Target | Proxy |
|---|---|---|---|
| CNAME | `chat` (i.e. `chat.hsworkshop.fous.ai`) | `router-default.apps.kpicl2-pool-rft8p.aws.rh-ods.com` | **DNS only** (grey cloud — NOT proxied) |

DNS-only is required: the OpenShift router must terminate TLS with the issued certificate
for the exact host; a proxied record would present Cloudflare's edge certificate instead.

## 4. Render the overlay

```bash
cd module-hsworkshop/domain
cp config.env.example config.env   # adjust if needed; config.env is non-secret
./render.sh                        # renders ../overlays/custom-domain (staging by default)
```

`render.sh` is deterministic (same inputs → byte-identical output). Configuration
precedence: **explicit environment variables win over `config.env`**, which wins over
defaults — so `ACME_ENV=production ./render.sh` always renders production even if
`config.env` says staging. The config file is parsed (never sourced) and all inputs are
validated (DNS syntax, sed metacharacters rejected, `chat.`-prefix rejected).

The canonical rendered overlay is committed at `module-hsworkshop/overlays/custom-domain`.

### Staging vs production (explicit choice)

| `ACME_ENV` | ACME directory | Certificate |
|---|---|---|
| `staging` (default) | `https://acme-staging-v02.api.letsencrypt.org/directory` | **NOT trusted by browsers** — validation only, avoids production rate limits |
| `production` | `https://acme-v02.api.letsencrypt.org/directory` | Browser-trusted — render **only after** a staging run issued successfully |

```bash
ACME_ENV=production ./render.sh    # explicit re-render for the live host
```

> A staging certificate is **never** trusted: it presents browser security warnings on
> `chat.hsworkshop.fous.ai` by design. Do not use a staging certificate on the live
> public hostname, and do not instruct users to bypass certificate warnings.

## Activation (two-phase, opt-in — NOT done in this change)

The Argo CD Application `hsworkshop` still points at `module-hsworkshop/install`.

> **Prerequisite: commit and push the overlay first.** Argo CD activates only what is in
> Git, so the overlay and its rendered files must be committed and pushed to
> `origin/zijeme-it` **before** any source-path change.

### Phase 1 — certificate-only pre-provision (staging first)

Deploy only the Issuer + Certificate so the certificate is issued and Secret
`openwebui-custom-tls` exists **before** the custom Route is admitted. This is
`overlays/custom-domain/certificate-only/` — a self-contained kustomization that
intentionally contains **no Route and no router RBAC**.

```bash
# with valid credentials:
oc apply -k module-hsworkshop/overlays/custom-domain/certificate-only -n hsworkshop
oc get certificate openwebui-custom-cert -n hsworkshop -w   # wait for Ready=True
```

Validate the full DNS-01 → issuance → Secret wiring with the **staging** issuer. Because
Phase 1 deploys no Route, this needs **no browser access to a staging route**: verify by
inspecting the issued Secret instead:

```bash
# Verify the staged certificate (issuer and SAN) without any browser:
oc get secret openwebui-custom-tls -n hsworkshop -o jsonpath='{.data.tls\.crt}' \
  | base64 -d | openssl x509 -noout -issuer -subject -ext subjectAltName
```

Expect the Let's Encrypt **staging** issuer in the output and the single SAN
`chat.hsworkshop.fous.ai`. Do not trust a browser connection to this host while the
staging certificate is served.

### Phase 2 — switch to production, then activate the full overlay

1. Re-render with production:

   ```bash
   cd module-hsworkshop/domain && ACME_ENV=production ./render.sh
   oc apply -k module-hsworkshop/overlays/custom-domain/certificate-only -n hsworkshop
   ```

2. **Gate on the production certificate itself — do not trust a stale `Ready`.** After
   switching the `issuerRef` to the production issuer, the Certificate's `Ready=True`
   condition may still reflect the *previous* (staging) issuance until cert-manager
   re-issues. Confirm the **Secret's actual certificate** was issued by the production
   trusted CA, with the exact SAN, before going further:

   ```bash
   # Evidence of a fresh production issuance:
   oc get certificate openwebui-custom-cert -n hsworkshop \
     -o jsonpath='{.status.notAfter}{"  rev="}{.status.revision}{"\n"}'   # new revision
   oc get certificaterequests.cert-manager.io -n hsworkshop \
     -o custom-columns=NAME:.metadata.name,ISSUER:.spec.issuerRef.name    # production request

   # Decisive check: decode the issued certificate from the Secret.
   # Issuer MUST be a Let's Encrypt PRODUCTION CA (e.g. ...R10/R11/E5/E6 —
   # never "(STAGING) ..."), and the SAN must be exactly chat.hsworkshop.fous.ai:
   oc get secret openwebui-custom-tls -n hsworkshop -o jsonpath='{.data.tls\.crt}' \
     | base64 -d | openssl x509 -noout -issuer -ext subjectAltName
   ```

   Only proceed when the decoded issuer is a production CA and the SAN is exact. If the
   issuer still says `(STAGING)`, the re-issuance has not happened yet — wait and re-check
   (a `revision` bump and a new production `CertificateRequest` corroborate it).

3. **Commit and push the production render BEFORE changing the Argo CD source path.**
   Argo CD activates only what is in Git: commit `module-hsworkshop/overlays/custom-domain`
   (as re-rendered with `ACME_ENV=production`) and push it to `origin/zijeme-it` first.

4. Only then point the Argo CD Application at the full overlay. Use a **JSON patch on
   `sources[0].path` only** — this preserves the second (model) source untouched:

   ```bash
   oc patch application.argoproj.io hsworkshop -n openshift-gitops --type=json \
     -p='[{"op":"replace","path":"/spec/sources/0/path","value":"module-hsworkshop/overlays/custom-domain"}]'
   ```

   Prefer doing it in Git (`module-hsworkshop/hsworkshop-argocd-app.yaml`, first source
   `path: module-hsworkshop/overlays/custom-domain`, second source unchanged) and letting
   Argo CD pick it up. **Never** replace the whole `sources` array in a patch — the model
   source (`shared-cluster/deploy-model`, `qwen36-35b-a3b-nvfp4-hsworkshop.yaml`) must
   stay exactly as is.

   The sync waves (-2 certificate, -1 RBAC, 0 Route) order the objects within a sync, but
   they do **not** prove Certificate readiness — that is why Phase 1 and the certificate
   inspection in step 2 exist.

> **Do not assume Argo CD health-gates the Certificate.** Whether the built-in
> `Certificate` health check is available was not verifiable offline; the two-phase
> procedure above does not depend on it.

## Verify the live production setup

Secret readiness alone is not sufficient — verify what is actually **served**:

```bash
oc get certificate -n hsworkshop                                  # Ready=True (production)
oc get route openwebui-custom -n hsworkshop                       # admitted, no
                                                                  # ExternalCertificateValidationFailed
# Served certificate: issuer MUST be Let's Encrypt production and SAN must be exact:
echo | openssl s_client -connect chat.hsworkshop.fous.ai:443 -servername chat.hsworkshop.fous.ai 2>/dev/null \
  | openssl x509 -noout -issuer -ext subjectAltName
curl -fsS https://chat.hsworkshop.fous.ai/health                  # 200 (trusted production cert)
```

If the Route status shows `ExternalCertificateValidationFailed`: check the router RBAC
(`Role/RoleBinding openwebui-custom-cert-reader`) and that the Secret is type
`kubernetes.io/tls` with `tls.crt`/`tls.key`.

## Disable / clean up (targeted, no broad prune)

`prune: false` on the Application means removing the overlay from Git does **not** delete
the five objects — clean them up explicitly, in this order:

1. De-activate the overlay: restore the app source path
   (`oc patch ... --type=json -p='[{"op":"replace","path":"/spec/sources/0/path","value":"module-hsworkshop/install"}]'`
   or revert the Git change).
2. Delete exactly the added objects (tolerate absence so the sequence is re-runnable):

   ```bash
   N=hsworkshop
   oc delete --ignore-not-found -n ${N} \
     route/openwebui-custom \
     certificate/openwebui-custom-cert \
     issuer/letsencrypt-staging-dns01 \
     issuer/letsencrypt-production-dns01 \
     role/openwebui-custom-cert-reader \
     rolebinding/openwebui-custom-cert-reader
   ```

3. Delete the TLS Secret (contains the certificate **private key**) and the ACME account
   key Secrets created by cert-manager for the issuers used:

   ```bash
   oc delete --ignore-not-found -n ${N} \
     secret/openwebui-custom-tls \
     secret/letsencrypt-staging-dns01-account-key \
     secret/letsencrypt-production-dns01-account-key
   ```

4. Optionally delete the token Secret and roll the Cloudflare token. **Choice:** keep
   `cloudflare-dns01` if you plan to re-enable the domain later (token unchanged); delete
   it and roll the token in the Cloudflare dashboard if you are decommissioning:

   ```bash
   oc delete --ignore-not-found -n ${N} secret/cloudflare-dns01
   ```

5. Delete the Cloudflare CNAME record `chat.hsworkshop.fous.ai`.
6. The OpenWebUI env patch disappears with the overlay (this restarts the pod once more);
   the base Deployment is otherwise untouched.

> Do **not** run a broad prune or `argocd app delete` — that would remove the whole stack.

## Tests

```bash
bash module-hsworkshop/tests/run-tests.sh
```

No network, no cluster, no tokens. Last run: **53 PASS / 0 FAIL**
(base identical to HEAD and free of overlay objects; deterministic render; explicit-env
beats config.env; config file never executed; staging/production selection; invalid
domain/email/env/router-target and sed-injection rejection; exact SAN; parsed Issuer
solver/account-key; Route `externalCertificate` without inline key/cert; narrow router
RBAC; OpenWebUI patch with signup untouched; semantic no-secrets scan; overlay and
Phase-1 kustomize builds; exactly 5 added objects and no added Secrets; committed overlay
equals a fresh render; Phase-1 issuer byte-identical to the full overlay's; Argo sources
unchanged with model source invariant; mocked-`oc` secret-script tests: wrong
context/server refused with no apply, single server-side apply, token never in argv/log,
no restart/delete/patch, missing token file errors, malformed tokens rejected).

## Blockers / not done here

- **Cluster credentials expired** — the `hsworkshop` kubeconfig context token returns 401.
  All cluster-side read-only preflight (live CRD check, cert-manager operand readiness,
  `RouteExternalCertificate` gate state) and any mutation is deferred to the follow-up
  unit. No context switch was performed; the user's active context was left untouched.
- No DNS record created, no token provisioned, no certificate issued, no Argo source-path
  change, nothing committed or pushed — all deferred per scope.
