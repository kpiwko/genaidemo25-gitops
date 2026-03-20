# HS Workshop — AI Stack

OpenWebUI + Langfuse v3 + TrustyAI GuardrailsOrchestrator running in the `hsworkshop` namespace on OpenShift, backed by a vLLM InferenceService.

## Architecture

```
User → OpenWebUI → KServe InferenceService (vLLM)
                 ↘ Langfuse (tracing)
                      ↳ Web API → MinIO → Worker → ClickHouse
```

| Component | Purpose |
|-----------|---------|
| OpenWebUI | Chat UI with OpenAI-compatible backend |
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
2. Click **Sign up** — the first registered user becomes admin
3. Fill in name, email, password and register
4. You should see the chat interface with the model available in the top dropdown

### 4. Set up Langfuse

#### 4a. Create Langfuse admin account

1. Open https://langfuse-hsworkshop.apps.brno-hack-pool-s5z4r.aws.rh-ods.com
2. Click **Sign up** and create an admin account
3. Create an **Organization** and a **Project** (e.g. `hsworkshop`)

#### 4b. Generate API keys

1. In your project go to **Settings → API Keys**
2. Click **Create new API key**
3. Copy the **Public Key** (`pk-lf-...`) and **Secret Key** (`sk-lf-...`) — the secret is only shown once

#### 4c. Wire Langfuse keys into OpenWebUI

```bash
oc patch secret openwebui-secret -n hsworkshop \
  --type=merge \
  --patch='{"stringData":{
    "LANGFUSE_PUBLIC_KEY":"pk-lf-REPLACE_ME",
    "LANGFUSE_SECRET_KEY":"sk-lf-REPLACE_ME"
  }}'
```

Then restart OpenWebUI to pick up the new keys:

```bash
oc rollout restart deployment openwebui -n hsworkshop
```

After restart, conversations in OpenWebUI will appear as traces in the Langfuse UI under your project.

## Verifying the stack

```bash
# All pods should be Running
oc get pods -n hsworkshop

# Model should be READY=True
oc get inferenceservice -n hsworkshop

# Quick model health check
curl -s http://gpt-oss-20b-service-predictor.hsworkshop.svc.cluster.local/health
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

**Langfuse traces not appearing** — check the OpenWebUI secret has real (non-placeholder) Langfuse keys:
```bash
oc get secret openwebui-secret -n hsworkshop -o jsonpath='{.data.LANGFUSE_PUBLIC_KEY}' | base64 -d
```

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
Then trigger a fresh sync from the UI.
