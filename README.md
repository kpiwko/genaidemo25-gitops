# GenAI Demo 2025 - GitOps Repository

This repository provides streamlined GitOps configurations to automate the deployment and management of OpenShift clusters for the GenAI demo modules. It is designed to use ArgoCD to run on clusters provisioned through the Red Hat Demo Platform (RHDP).

## ⚠️ Prerequisites

**Red Hat OpenShift GitOps operator** (productized ArgoCD) must be installed on the target cluster(s) before deploying these configurations. This operator is typically available through the OpenShift OperatorHub.

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
