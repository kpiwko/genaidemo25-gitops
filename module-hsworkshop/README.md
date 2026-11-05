# HS Workshop — AI Stack

OpenWebUI + Langfuse v3 + TrustyAI guardrails running in the `hsworkshop` namespace on OpenShift, backed by a vLLM InferenceService.

## Architecture

```
User → OpenWebUI → guardrails-proxy → TrustyAI gateway → KServe InferenceService (vLLM)
                        ↓                    ↓
                  /v1/models          built-in regex detector
                  (direct to          (Czech + English swear words)
                   predictor)           ↓ blocked → tag trace in Langfuse
           ↓
       Pipelines (filter)
           ↓
       Langfuse (tracing)
           ↳ Web API → MinIO → Worker → ClickHouse
                                              ↓
                                    HSWorkshop Insights
                                    (word cloud · word graph · AI summary)

       SearXNG (self-hosted web search, available to presets)
```

| Component | Purpose |
|-----------|---------|
| OpenWebUI | Chat UI with OpenAI-compatible backend |
| guardrails-proxy | Nginx→Python proxy routing chat through TrustyAI, model listing direct to predictor; tags blocked requests in Langfuse |
| TrustyAI | Guardrails orchestrator — built-in regex detector blocks swear words (Czech + English) |
| SearXNG | Self-hosted metasearch engine for web search (currently not enabled in the Lunch/Greenhouse presets) |
| Pipelines | Filter layer enabling Langfuse tracing in OpenWebUI |
| Langfuse | LLM observability: traces, costs, latency |
| HSWorkshop Insights | Visualizes conversation traces — word cloud, word graph, AI summary; supports valid/blocked filtering |
| vLLM / KServe | Model serving (Qwen3.6 35B A3B NVFP4) |
| Langfuse | v4.71 (observations-first data model, OTLP ingestion) |
| PostgreSQL | Relational data for Langfuse |
| ClickHouse | Analytical storage for traces/spans |
| Redis | Queue for Langfuse worker |
| MinIO | S3-compatible blob store for event ingestion |

## Routes

| Service | URL |
|---------|-----|
| OpenWebUI | https://openwebui-hsworkshop.apps.kpicl2-pool-rft8p.aws.rh-ods.com |
| Langfuse | https://langfuse-hsworkshop.apps.kpicl2-pool-rft8p.aws.rh-ods.com |
| HSWorkshop Insights | https://insights-hsworkshop.apps.kpicl2-pool-rft8p.aws.rh-ods.com |

**Optional chat-only custom domain:** `https://chat.hsworkshop.fous.ai` — an opt-in Kustomize
overlay (`overlays/custom-domain`) serving OpenWebUI only, with a Let's Encrypt certificate
(cert-manager DNS-01 via Cloudflare) attached through `Route.spec.tls.externalCertificate`.
Not active by default. See [../docs/custom-domain.md](../docs/custom-domain.md).

## Initial Setup

### 1. Apply secrets (first time only)

```bash
cd module-hsworkshop
./apply-secrets.sh
```

This generates random credentials, prints them for your password manager, then applies them as OpenShift secrets. The script is idempotent-safe via `--dry-run=client` for the namespace but will fail if secrets already exist — delete them first if re-running.

Secrets created: `postgres-secret`, `clickhouse-secret`, `langfuse-secret`, `minio-secret`, `openwebui-secret`, `insights-db-secret`.

> **Note:** `guardrails-langfuse-secret` is NOT created by this script — it requires Langfuse API keys that only exist after Langfuse is running (step 5). See the Langfuse section below for the command.

### 2. Deploy via ArgoCD

Apply the ArgoCD application:

```bash
oc apply -f hsworkshop-argocd-app.yaml
```

Then sync in the ArgoCD UI or:

```bash
oc annotate application.argoproj.io hsworkshop -n openshift-gitops \
  argocd.argoproj.io/refresh=hard --overwrite
```

### 3. Log in to OpenWebUI

The admin account's credentials are referenced explicitly from `openwebui-admin-bootstrap-secret` (provisioned by `apply-openwebui-secrets.sh`) and override the legacy `openwebui-secret` envFrom — the existing admin is reused, never reset.

1. Open https://openwebui-hsworkshop.apps.kpicl2-pool-rft8p.aws.rh-ods.com
2. Log in with the email and password you saved in your password manager

Signup is temporarily **enabled** for the workshop in `openwebui.yaml` (`ENABLE_SIGNUP=true`) with `DEFAULT_USER_ROLE=user`; participants register their own accounts. Set it back to `false` after the workshop.

> **Note:** If updating an existing install, the signup setting may already be stored in the database. Enable it manually if needed: Admin Panel → Settings → General → Enable New User Sign Up → on.

### 4. OpenWebUI API connections and model presets (automatic)

Connections and model presets are configured **headlessly** — no manual
Admin Panel setup is needed:

- **Connections** are declared in `install/openwebui.yaml` as the plural,
  index-paired `OPENAI_API_BASE_URLS`/`OPENAI_API_KEYS` env (guardrails
  proxy first, Pipelines second; keys composed from
  `openwebui-connections-secret` via explicit secretKeyRefs). With
  `ENABLE_PERSISTENT_CONFIG=false` the declared env governs startup config.
- **Model presets** (`provisioning/configuration-models.json` — Lunch
  (high temperature) and Greenhouse (low temperature, design-assistant system prompt), with public read visibility) are provisioned by the
  `openwebui-workshop-provision` PostSync Job on every Argo CD sync: after
  the sync's resources are healthy, the Job signs in as the existing admin
  (credentials from `openwebui-admin-bootstrap-secret`), diffs the managed
  presets against the server, and additively imports only changes —
  unrelated/private models are never touched, and a converged second run
  performs no writes.

> **Prerequisites for the hook to succeed:** the Secrets from
> `apply-openwebui-secrets.sh` (`openwebui-connections-secret`,
> `openwebui-admin-bootstrap-secret`) must exist in the namespace before
> the sync — the Job fails closed (and the sync is marked degraded) when
> they are missing; it never resets or promotes accounts. If the sync is
> unhealthy for unrelated reasons, fix that first — Argo CD blocks the
> PostSync hook until the main resources are healthy; do not run the Job
> manually to bypass it. The last hook result (success or failure) is
> retained between syncs for audit (`BeforeHookCreation` policy only).
> These headless changes are implemented and validated offline in this
> repository; they are **not yet deployed** — the live rollout is gated
> (see [../docs/headless-openwebui.md](../docs/headless-openwebui.md)).

### 5. Set up Langfuse

#### 5a. Create Langfuse admin account

1. Open https://langfuse-hsworkshop.apps.kpicl2-pool-rft8p.aws.rh-ods.com
2. Click **Sign up** and create an admin account
3. Create an **Organization** and a **Project** (e.g. `hsworkshop`)

#### 5b. Generate API keys

1. In your project go to **Settings → API Keys**
2. Click **Create new API key**
3. Copy the **Public Key** (`pk-lf-...`) and **Secret Key** (`sk-lf-...`) — the secret is only shown once

#### 5c. Pipelines connection (automatic)

The Pipelines connection (`http://pipelines.hsworkshop.svc.cluster.local:9099`
with `PIPELINES_API_KEY` from `openwebui-connections-secret`) is declared in
`install/openwebui.yaml` as part of the plural connection env — no manual
Admin Panel connection is needed (see step 4).

#### 5d. Install the Langfuse filter pipeline (automatic)

The Langfuse **v4** filter (`provisioning/../pipelines/langfuse_v4_filter_pipeline.py`,
see `pipelines/`) is deployed as a content-hash ConfigMap and copied onto the
existing Pipelines PVC by an init container on the same pinned image — no
manual installation is needed. Its Langfuse keys and host come from the
`guardrails-langfuse-secret` environment (see 5e below); the dependency lock
is `pipelines/requirements.lock`.

#### 5e. Connect guardrails proxy to Langfuse (blocked content tagging)

The guardrails proxy tags blocked requests in Langfuse with `tags: ["blocked"]` so the Insights app can show a "Blocked only" word cloud/graph. It needs its own API key (separate from the Pipelines filter).

1. In Langfuse UI go to **Settings → API Keys → Create new key** and copy the keys
2. Apply the secret to the cluster:
   ```bash
   oc create secret generic guardrails-langfuse-secret -n hsworkshop \
     --from-literal=LANGFUSE_HOST=http://langfuse:3000 \
     --from-literal=LANGFUSE_PUBLIC_KEY=pk-lf-... \
     --from-literal=LANGFUSE_SECRET_KEY=sk-lf-... \
     --dry-run=client -o yaml | oc apply -f -
   ```
3. Restart the proxy to pick up the secret:
   ```bash
   oc rollout restart deployment/guardrails-proxy -n hsworkshop
   ```

> **Switching projects:** When moving to a new workshop run (e.g. `run-1`, `run-2`), create a new API key in that project's Langfuse settings and repeat steps 2–3 above. The Insights app picks up the new project automatically from the dropdown — no restart needed.

### 6. Model presets (automatic)

The two workshop presets (**Lunch** and **Greenhouse**, public read
visibility) live in `provisioning/configuration-models.json` and are
provisioned automatically by the `openwebui-workshop-provision` PostSync Job
on every Argo CD sync (see step 4) — no manual Admin Panel import is needed,
and the public-read visibility is part of the managed presets, not a manual
per-model setting.

To hide the raw base model from the model selector so users only see the
presets:

- Admin Panel → Workspace → Models → toggle off `qwen36-service`

## Configuration files

| File | Purpose |
|------|---------|
| `provisioning/configuration-models.json` | OpenWebUI model presets (Lunch, Greenhouse) — sole input of the PostSync provisioning Job |
| `provisioning/provision_openwebui.py` | Additive preset provisioner run by the PostSync Job (stdlib only) |
| `domain/render.sh` | Deterministic renderer for the optional custom-domain overlay (non-secret inputs) |
| `apply-custom-domain-secret.sh` | Out-of-band provisioning of the Cloudflare DNS-01 token Secret (never in Git) |
| `apply-openwebui-secrets.sh` | Out-of-band provisioning of the connections + admin-bootstrap Secrets (never in Git) |
| `tests/run-tests.sh` | Offline test suite for the base + custom-domain overlay |

## Verifying the stack

```bash
# All pods should be Running
oc get pods -n hsworkshop

# Model should be READY=True
oc get inferenceservice -n hsworkshop

# Quick model health check via guardrails proxy
oc run curl-test --rm -i --restart=Never --image=curlimages/curl -n hsworkshop -- \
  curl -s http://guardrails-proxy.hsworkshop.svc.cluster.local:8080/v1/models
```

## Updating the model

Chat completions flow through `guardrails-proxy` → TrustyAI gateway → predictor. Model listing goes direct from the proxy to the predictor. When switching models, update three places:

1. **`install/openwebui.yaml`** — the plural `OPENAI_API_BASE_URLS` first entry stays pointing at the guardrails proxy (no change needed unless proxy moves); `OPENAI_API_KEYS` is composed from `GUARDRAILS_API_KEY`/`PIPELINES_API_KEY` refs
2. **`install/profanity-detector.yaml`** — update `PREDICTOR` variable to the new predictor hostname
3. **`install/guardrails.yaml`** — update `autoConfig.inferenceServiceToGuardrail` to the new InferenceService name
4. **`hsworkshop-argocd-app.yaml`** — update the model values file reference
5. **`provisioning/configuration-models.json`** — update `base_model_id` in both presets to match the new model ID

After deploying, update the OpenWebUI API connection URL (step 4 above) if the predictor hostname changed.

## Resetting conversation data

To wipe all traces between workshop runs (keeps schema, secrets, and MinIO blobs intact):

```bash
CH_USER=$(oc get secret clickhouse-secret -n hsworkshop -o jsonpath='{.data.CLICKHOUSE_USER}' | base64 -d)
CH_PASS=$(oc get secret clickhouse-secret -n hsworkshop -o jsonpath='{.data.CLICKHOUSE_PASSWORD}' | base64 -d)
PROJECT_ID="<your-langfuse-project-id>"  # visible in Langfuse URL or traces query below

# Find the project ID
oc exec -n hsworkshop deployment/clickhouse -- \
  clickhouse-client --user "$CH_USER" --password "$CH_PASS" \
  --query "SELECT project_id, count() FROM default.traces GROUP BY project_id"

# Delete trace data (analytics_* are Views — no need to touch them)
for table in traces observations scores; do
  oc exec -n hsworkshop deployment/clickhouse -- \
    clickhouse-client --user "$CH_USER" --password "$CH_PASS" \
    --query "ALTER TABLE default.${table} DELETE WHERE project_id = '${PROJECT_ID}'"
done
```

> **Note:** `analytics_traces`, `analytics_observations`, and `analytics_scores` are ClickHouse Views derived from the base tables — deleting from the base tables is sufficient and attempts to mutate Views will fail with `NOT_IMPLEMENTED`.

## Troubleshooting

**OpenWebUI shows no models** — the InferenceService is not Ready. Check:
```bash
oc get inferenceservice -n hsworkshop
oc get pods -n hsworkshop -l serving.kserve.io/inferenceservice=qwen36-service
oc logs -n hsworkshop -l serving.kserve.io/inferenceservice=qwen36-service -c kserve-container
```

**Chat responses are empty or stuck** — the guardrails proxy may be unhealthy. Check:
```bash
oc get pods -n hsworkshop -l app=guardrails-proxy
oc logs -n hsworkshop deployment/guardrails-proxy
```
Also verify OpenWebUI's API connection URL is pointing at `http://guardrails-proxy.hsworkshop.svc.cluster.local:8080/v1` (Admin Panel → Settings → Connections).

**Langfuse traces not appearing** — tracing goes through the Pipelines service. Check:
- Pipelines pod is Running: `oc get pods -n hsworkshop -l app=pipelines`
- OpenWebUI is connected to Pipelines: Admin Panel → Settings → Connections → `http://pipelines:9099`
- Langfuse filter pipeline is installed and configured with correct host/keys: Admin Panel → Settings → Pipelines

**Langfuse worker crashing** — check logs and MinIO connectivity:
```bash
oc logs -n hsworkshop deployment/langfuse-worker --tail=30
oc get pods -n hsworkshop -l app=minio
```

**ArgoCD sync stuck** — if sync hangs waiting for InferenceService health, force a new operation:
```bash
oc patch application.argoproj.io hsworkshop -n openshift-gitops \
  --type=json -p='[{"op":"remove","path":"/operation"}]'
```
Then do a hard refresh and trigger a fresh sync from the UI:
```bash
oc annotate application.argoproj.io hsworkshop -n openshift-gitops \
  argocd.argoproj.io/refresh=hard --overwrite
```

**Model pod stuck Pending after rolling update** — KServe rolling updates can deadlock when only one GPU is available: the new pod can't schedule until the old one is gone, but the old one won't terminate until the new one is ready. Fix by deleting the old pod manually:
```bash
oc get pods -n hsworkshop -l serving.kserve.io/inferenceservice=qwen36-service
oc delete pod -n hsworkshop <old-predictor-pod-name>
```

**TrustyAI AutoConfigFailed** — the operator locks into a failed state via an annotation. Fix by deleting and recreating the resource to force a fresh reconcile:
```bash
oc delete guardrailsorchestrator guardrails-orchestrator -n hsworkshop
oc apply -f module-hsworkshop/install/guardrails.yaml
```

**TrustyAI orchestrator broken after operator reconcile** — the operator regenerates its ConfigMaps on reconcile, reverting two manual patches. Re-apply after any reconcile:

```bash
# Fix 1: backend port (headless service requires pod port 8080, not service port 80)
oc patch configmap guardrails-orchestrator-auto-config -n hsworkshop --type merge -p '{
  "data": {
    "config.yaml": "openai:\n  service:\n    hostname: qwen36-service-predictor.hsworkshop.svc.cluster.local\n    port: 8080\ndetectors:\n  built-in-detector:\n    type: text_contents\n    service:\n      hostname: 127.0.0.1\n      port: 8080\n    chunker_id: whole_doc_chunker\n    default_threshold: 0.5\npassthrough_headers:\n  - Authorization\n  - Content-Type\n"
  }
}'

# Fix 2: swear word regex (replace $^ placeholder with actual pattern)
# Run the patch script — see module-hsworkshop/apply-trustyai-patches.sh
```

> The swear word regex patch is too long for inline kubectl. Use `apply-trustyai-patches.sh` after any reconcile (see below).

**TrustyAI reconcile is triggered** when: the `GuardrailsOrchestrator` CR changes, or a Service with label `trustyai/guardrails-groupA` appears/disappears. After re-applying patches, restart the orchestrator:
```bash
oc rollout restart deployment/guardrails-orchestrator -n hsworkshop
```
