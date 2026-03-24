# HS Workshop — AI Stack

OpenWebUI + Langfuse v3 + TrustyAI guardrails running in the `hsworkshop` namespace on OpenShift, backed by a vLLM InferenceService.

## Architecture

```
User → OpenWebUI → guardrails-proxy → TrustyAI gateway → KServe InferenceService (vLLM)
                        ↓                    ↓
                  /v1/models          built-in regex detector
                  (direct to          (Czech + English swear words)
                   predictor)
           ↓
       Pipelines (filter)
           ↓
       Langfuse (tracing)
           ↳ Web API → MinIO → Worker → ClickHouse

       SearXNG (self-hosted web search, used by Career Guide preset)
```

| Component | Purpose |
|-----------|---------|
| OpenWebUI | Chat UI with OpenAI-compatible backend |
| guardrails-proxy | Nginx→Python proxy routing chat through TrustyAI, model listing direct to predictor |
| TrustyAI | Guardrails orchestrator — built-in regex detector blocks swear words (Czech + English) |
| SearXNG | Self-hosted metasearch engine for web search in Career Guide preset |
| Pipelines | Filter layer enabling Langfuse tracing in OpenWebUI |
| Langfuse | LLM observability: traces, costs, latency |
| vLLM / KServe | Model serving (Qwen3.5 27B FP8) |
| PostgreSQL | Relational data for Langfuse |
| ClickHouse | Analytical storage for traces/spans |
| Redis | Queue for Langfuse worker |
| MinIO | S3-compatible blob store for event ingestion |

## Routes

| Service | URL |
|---------|-----|
| OpenWebUI | https://openwebui-hsworkshop.apps.brno-hack-pool-s5z4r.aws.rh-ods.com |
| Langfuse | https://langfuse-hsworkshop.apps.brno-hack-pool-s5z4r.aws.rh-ods.com |

## Initial Setup

### 1. Apply secrets (first time only)

```bash
cd module-hsworkshop
./apply-secrets.sh
```

This generates random credentials, prints them for your password manager, then applies them as OpenShift secrets. The script is idempotent-safe via `--dry-run=client` for the namespace but will fail if secrets already exist — delete them first if re-running.

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

The admin account is created automatically on first boot using `WEBUI_ADMIN_EMAIL` and `WEBUI_ADMIN_PASSWORD` from `openwebui-secret` (set by `apply-secrets.sh`).

1. Open https://openwebui-hsworkshop.apps.brno-hack-pool-s5z4r.aws.rh-ods.com
2. Log in with the email and password you saved in your password manager

Subsequent users can self-register and get immediate access — `DEFAULT_USER_ROLE=user` and `ENABLE_SIGNUP=true` are set in `openwebui.yaml`.

> **Note:** If updating an existing install, the signup setting may already be stored in the database. Enable it manually if needed: Admin Panel → Settings → General → Enable New User Sign Up → on.

### 4. Update OpenWebUI API connection (mandatory)

OpenWebUI stores the model API URL in its database, which overrides the env var after first boot. Update it to route through the guardrails proxy:

1. Admin Panel → Settings → Connections → OpenAI API
2. Set URL to: `http://guardrails-proxy.hsworkshop.svc.cluster.local:8080/v1`
3. Verify the connection shows green

### 5. Set up Langfuse

#### 5a. Create Langfuse admin account

1. Open https://langfuse-hsworkshop.apps.brno-hack-pool-s5z4r.aws.rh-ods.com
2. Click **Sign up** and create an admin account
3. Create an **Organization** and a **Project** (e.g. `hsworkshop`)

#### 5b. Generate API keys

1. In your project go to **Settings → API Keys**
2. Click **Create new API key**
3. Copy the **Public Key** (`pk-lf-...`) and **Secret Key** (`sk-lf-...`) — the secret is only shown once

#### 5c. Connect OpenWebUI to Pipelines

Langfuse tracing in OpenWebUI works via the **Pipelines** service (a filter layer). The `pipelines` pod is deployed alongside the stack.

In OpenWebUI Admin Panel:
1. Go to **Settings → Connections**
2. Add a new OpenAI API connection:
   - **URL**: `http://pipelines:9099`
   - **API Key**: `0p3n-w3bu!`
3. Save

#### 5d. Install the Langfuse filter pipeline

1. Go to **Admin Panel → Settings → Pipelines**
2. Click **Install from GitHub URL** and enter:
   ```
   https://github.com/open-webui/pipelines/blob/main/examples/filters/langfuse_v3_filter_pipeline.py
   ```
3. Once installed, open the pipeline settings and enter your Langfuse keys:
   - **Langfuse Public Key**: `pk-lf-...`
   - **Langfuse Secret Key**: `sk-lf-...`
   - **Langfuse Host**: `http://langfuse:3000`
4. Save — conversations will now appear as traces in Langfuse

### 6. Import model presets

The `configuration-models.json` file defines the two workshop presets (**Career Guide** and **Free Chat**). Import them:

1. Admin Panel → Workspace → Models → Import (upload icon)
2. Select `module-hsworkshop/configuration-models.json`

To hide the raw base model from the model selector so users only see the presets:

- Admin Panel → Settings → Models → toggle off `qwen35-27b-fp8-service`

## Configuration files

| File | Purpose |
|------|---------|
| `configuration-models.json` | OpenWebUI model presets (Career Guide, Free Chat) — import via Admin Panel |

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

1. **`install/openwebui.yaml`** — `OPENAI_API_BASE_URL` stays pointing at the guardrails proxy (no change needed unless proxy moves)
2. **`install/profanity-detector.yaml`** — update `PREDICTOR` variable to the new predictor hostname
3. **`install/guardrails.yaml`** — update `autoConfig.inferenceServiceToGuardrail` to the new InferenceService name
4. **`hsworkshop-argocd-app.yaml`** — update the model values file reference
5. **`configuration-models.json`** — update `base_model_id` in both presets to match the new model ID

After deploying, update the OpenWebUI API connection URL (step 4 above) if the predictor hostname changed.

## Troubleshooting

**OpenWebUI shows no models** — the InferenceService is not Ready. Check:
```bash
oc get inferenceservice -n hsworkshop
oc get pods -n hsworkshop -l serving.kserve.io/inferenceservice=qwen35-27b-fp8-service
oc logs -n hsworkshop -l serving.kserve.io/inferenceservice=qwen35-27b-fp8-service -c kserve-container
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
oc get pods -n hsworkshop -l serving.kserve.io/inferenceservice=qwen35-27b-fp8-service
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
    "config.yaml": "openai:\n  service:\n    hostname: qwen35-27b-fp8-service-predictor.hsworkshop.svc.cluster.local\n    port: 8080\ndetectors:\n  built-in-detector:\n    type: text_contents\n    service:\n      hostname: 127.0.0.1\n      port: 8080\n    chunker_id: whole_doc_chunker\n    default_threshold: 0.5\npassthrough_headers:\n  - Authorization\n  - Content-Type\n"
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
