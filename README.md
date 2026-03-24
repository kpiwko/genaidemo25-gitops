# GenAI Demo 2025 - GitOps Repository

This repository provides streamlined GitOps configurations to automate the deployment and management of OpenShift clusters for the GenAI demo modules. It is designed to use ArgoCD to run on clusters provisioned through the Red Hat Demo Platform (RHDP).

## ⚠️ Prerequisites

**Red Hat OpenShift GitOps operator** (productized ArgoCD) must be installed on the target cluster(s) before deploying these configurations. This operator is typically available through the OpenShift OperatorHub.

## Model Deployment

This repo now separates namespace/RBAC bootstrap from model deployment. Deploy the namespace once, then manage any number of models safely.

### 0) Prereqs

- OpenShift GitOps (Argo CD) installed
- RHOAI/KServe installed (use `shared-cluster/install-rhoai-argocd-app.yaml`)

### 1) Create image pull secret (if needed)

```bash
oc -n genai25-deployments create secret docker-registry genai2025-pull-secret \
  --docker-server=quay.io \
  --docker-username='<user-or-robot>' \
  --docker-password='<password-or-token>' \
  --docker-email='<you@example.com>'
```

### 2) Configure models (values)

- `shared-cluster/deploy-model/gpt-oss-20b.yaml` and `shared-cluster/deploy-model/gemma-3-27b.yaml`
- Ensure:
  - `connection.name` points to your pull secret (if required)
  - Unique `runtime.name` and `inference.name` per model

### 3) Install models via Argo CD (multi-source App)

```bash
oc apply -n openshift-gitops -f shared-cluster/install-llm-models-argocd-app.yaml
```

This single Application deploys both models from the same chart (multi-source). The app also creates the namespace where models run.

### 4) Verify

```bash
oc get ns genai25-deployments
oc -n genai25-deployments get servingruntimes.serving.kserve.io
oc -n genai25-deployments get inferenceservices.serving.kserve.io
oc -n genai25-deployments get ksvc
```

Notes:

- For `oss-gpt-20b`, this chart sets a custom vLLM runtime image (0.10.1) and supports extra args via `runtime.extraArgs`.
- To dry-run Helm locally before Argo sync:

```bash
helm template test ./shared-cluster/deploy-model \
  -n genai25-deployments \
  -f ./shared-cluster/deploy-model/gpt-oss-20b-hsworkshop.yaml \
| kubectl apply --dry-run=client -f -
```

## 📁 Repository Structure

```
genaidemo25-gitops/
├── shared-cluster/                    # Cluster-wide shared resources
│   ├── user-setup-argocd-app.yaml    # ArgoCD app for user authentication
│   ├── rhoai-setup-argocd-app.yaml   # ArgoCD app for RHOAI operator
│   ├── user-setup/                   # User authentication resources
│   │   ├── kustomization.yaml
│   │   ├── hackathon-secret.yaml     # HTPasswd for hackathon user
│   │   ├── htpasswd-secret.yaml      # HTPasswd for test user
│   │   ├── oauth-cluster.yaml        # OAuth configuration
│   │   ├── test-user.yaml
│   │   └── test-user-rbac.yaml
│   ├── install-rhoai/                # RHOAI operator installation
│   │   ├── kustomization.yaml
│   │   ├── namespace.yaml
│   │   ├── operator-group.yaml
│   │   ├── rbac-presync-monitoring.yaml
│   │   ├── subscription-authorino.yaml
│   │   └── subscription-rhoai.yaml
│   └── deploy-model/                 # Helm values files for model deployments
│       ├── gpt-oss-20b-hsworkshop.yaml       # Active: GPT-OSS 20B in hsworkshop
│       ├── qwen35-27b-fp8-hsworkshop.yaml    # Reference (not deployed)
│       └── qwen35-35b-a3b-hsworkshop.yaml    # Reference (not deployed)
└── module-hsworkshop/                # HS Workshop AI stack
    ├── hsworkshop-argocd-app.yaml    # ArgoCD Application definition
    ├── configuration-models.json     # OpenWebUI model presets (import via Admin Panel)
    ├── apply-secrets.sh              # One-time secret generation and apply script
    ├── apply-trustyai-patches.sh     # Re-apply TrustyAI config patches after reconcile
    └── install/                      # Kustomize resources
        ├── kustomization.yaml
        ├── namespace.yaml
        ├── rbac.yaml
        ├── postgres.yaml
        ├── redis.yaml
        ├── clickhouse.yaml
        ├── minio.yaml
        ├── langfuse.yaml
        ├── pipelines.yaml
        ├── searxng.yaml
        ├── openwebui.yaml
        ├── guardrails.yaml
        ├── profanity-detector.yaml   # TrustyAI guardrails proxy with Langfuse tagging
        ├── insights.yaml             # HSWorkshop Insights backend + frontend
        └── langfuse-proxy-secret.yaml  # Template for guardrails-langfuse-secret (manual apply)
```

## 🔧 ArgoCD Application Configuration

### Key Fields Explained

#### **finalizers**

Ensures ArgoCD cleans up resources before deleting the Application.

#### **targetRevision**

Specifies which Git branch or commit to use (e.g., `shared-cluster-setup` or `HEAD`).

#### **syncOptions**

Controls sync behavior, like auto-creating namespaces and resource cleanup order.

#### **automated sync policies**

Enables automatic pruning and self-healing to match cluster state to Git.

## 🔐 Authentication Setup

### User Credentials

- **hackathon user (admin)**: `hackathon:brno123`
- **test user**: `test-user:password123`
