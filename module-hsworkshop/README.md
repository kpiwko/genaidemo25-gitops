# HS Workshop — AI Stack

OpenWebUI + Langfuse v3 + TrustyAI GuardrailsOrchestrator running in the `hsworkshop` namespace on OpenShift, backed by a vLLM InferenceService.

## Architecture

```
User → OpenWebUI → Pipelines (filter) → KServe InferenceService (vLLM)
                       ↓
                   Langfuse (tracing)
                       ↳ Web API → MinIO → Worker → ClickHouse
```

| Component | Purpose |
|-----------|---------|
| OpenWebUI | Chat UI with OpenAI-compatible backend |
| Pipelines | Filter layer enabling Langfuse tracing in OpenWebUI |
| Langfuse | LLM observability: traces, costs, latency |
| vLLM / KServe | Model serving (gpt-oss-20b or Qwen3.5) |
| PostgreSQL | Relational data for Langfuse |
| ClickHouse | Analytical storage for traces/spans |
| Redis | Queue for Langfuse worker |
| MinIO | S3-compatible blob store for event ingestion |
| TrustyAI | Guardrails orchestrator for prompt safety |

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

### 3. Create OpenWebUI admin account

1. Open https://openwebui-hsworkshop.apps.brno-hack-pool-s5z4r.aws.rh-ods.com
2. Click **Sign up** — the first registered user becomes admin automatically
3. Fill in name, email, password and register

Subsequent users can self-register and get immediate access (`DEFAULT_USER_ROLE=user` is set in `openwebui.yaml`).

> **Note:** If you're updating an existing install rather than a fresh one, the signup setting may already be stored in the database with a different value. In that case enable it manually: Admin Panel → Settings → General → Enable New User Sign Up → on.

### 4. Set up Langfuse

#### 4a. Create Langfuse admin account

1. Open https://langfuse-hsworkshop.apps.brno-hack-pool-s5z4r.aws.rh-ods.com
2. Click **Sign up** and create an admin account
3. Create an **Organization** and a **Project** (e.g. `hsworkshop`)

#### 4b. Generate API keys

1. In your project go to **Settings → API Keys**
2. Click **Create new API key**
3. Copy the **Public Key** (`pk-lf-...`) and **Secret Key** (`sk-lf-...`) — the secret is only shown once

#### 4c. Connect OpenWebUI to Pipelines

Langfuse tracing in OpenWebUI works via the **Pipelines** service (a filter layer). The `pipelines` pod is deployed alongside the stack.

In OpenWebUI Admin Panel:
1. Go to **Settings → Connections**
2. Add a new OpenAI API connection:
   - **URL**: `http://pipelines:9099`
   - **API Key**: `0p3n-w3bu!`
3. Save

#### 4d. Install the Langfuse filter pipeline

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

## Verifying the stack

```bash
# All pods should be Running
oc get pods -n hsworkshop

# Model should be READY=True
oc get inferenceservice -n hsworkshop

# Quick model health check (run from inside the cluster, e.g. via oc exec)
oc exec -n hsworkshop deployment/openwebui -- \
  curl -s http://gpt-oss-20b-service-predictor.hsworkshop.svc.cluster.local:8080/v1/models
```

## Updating the model

The model URL in OpenWebUI is set via `OPENAI_API_BASE_URL` in `install/openwebui.yaml`. To switch to a different InferenceService, update that value and commit:

```yaml
- name: OPENAI_API_BASE_URL
  value: "http://<inferenceservice-name>-predictor.hsworkshop.svc.cluster.local/v1"
```

## Troubleshooting

**OpenWebUI shows no models** — the InferenceService is not Ready. Check:
```bash
oc get inferenceservice -n hsworkshop
oc get pods -n hsworkshop -l serving.kserve.io/inferenceservice=gpt-oss-20b-service
oc logs -n hsworkshop -l serving.kserve.io/inferenceservice=gpt-oss-20b-service -c kserve-container
```

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
oc get pods -n hsworkshop -l serving.kserve.io/inferenceservice=gpt-oss-20b-service
oc delete pod -n hsworkshop <old-predictor-pod-name>
```
