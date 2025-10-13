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
helm template test /Users/kpiwko/devel/ai-experiments/genaidemo25-gitops/shared-cluster/deploy-model \
  -n genai25-deployments \
  -f /Users/kpiwko/devel/ai-experiments/genaidemo25-gitops/shared-cluster/deploy-model/gpt-oss-20b.yaml \
| kubectl apply --dry-run=client -f -
```

## 📁 Repository Structure

```
genaidemo25-gitops/
├── shared-cluster/                    # Cluster-wide shared resources
│   ├── user-setup-argocd-app.yaml    # ArgoCD app for user authentication
│   ├── rhoai-setup-argocd-app.yaml   # ArgoCD app for RHOAI operator
│   ├── user-setup/                   # User authentication resources
│   │   ├── kustomization.yaml        # Kustomize resource list
│   │   ├── hackathon-secret.yaml     # HTPasswd for hackathon user
│   │   ├── htpasswd-secret.yaml      # HTPasswd for test user
│   │   ├── oauth-cluster.yaml        # OAuth configuration
│   │   ├── test-user.yaml            # User and Identity resources
│   │   └── test-user-rbac.yaml       # RBAC permissions
│   ├── install-rhoai/                  # RHOAI operator installation
│   │   ├── kustomization.yaml        # Ordered operator installation
│   │   ├── namespace.yaml            # redhat-ods-operator namespace
│   │   ├── operator-group.yaml       # OperatorGroup
│   │   ├── rbac-presync-monitoring.yaml  # Pre-sync RBAC
│   │   ├── subscription-authorino.yaml   # Authorino operator
│   │   └── subscription-rhoai.yaml       # RHOAI operator
│   └── deploy-model/                 # Future model deployment configs
│       └── .gitkeep
├── module-lightspeed/                 # Lightspeed module resources
│   ├── install-pipelines-argocd-app.yaml     # ArgoCD app for Pipelines
│   ├── install-web-terminal-argocd-app.yaml  # ArgoCD app for Web Terminal
│   ├── install-pipelines/            # OpenShift Pipelines operator
│   │   ├── kustomization.yaml        # Resource ordering
│   │   ├── namespace.yaml            # openshift-pipelines namespace
│   │   ├── operator-group.yaml       # OperatorGroup
│   │   └── subscription.yaml         # Pipelines operator subscription
│   └── install-web-terminal/         # Web Terminal operator
│       ├── kustomization.yaml        # Simple subscription-only setup
│       └── subscription.yaml         # Web Terminal subscription
└── module-receipts/                   # Future receipts module
    └── .gitkeep
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
