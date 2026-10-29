# Working with Argo CD (OpenShift GitOps) on the hsworkshop cluster

Cluster: `https://api.kpicl2-pool-rft8p.aws.rh-ods.com:6443`, OCP 4.21, RHOAI 3.6.0-ea.2, 1x NVIDIA L40S.

## What is installed

| Item | Value |
|---|---|
| Operator | Red Hat OpenShift GitOps 1.22.2 (`openshift-gitops-operator`, channel `latest`) |
| Argo CD instance | `openshift-gitops` namespace |
| Console | https://openshift-gitops-server-openshift-gitops.apps.kpicl2-pool-rft8p.aws.rh-ods.com |
| Application | `hsworkshop`: tracks `kpiwko/genaidemo25-gitops` branch `zijeme-it` |
| Sync policy | automated (`prune: false`, `selfHeal: false`) |

> `argoproj.io` Workflow CRDs on this cluster come from RHOAI pipelines (Argo Workflows). They are
> not Argo CD.

Do **not** apply `shared-cluster/install-rhoai-argocd-app.yaml` here: RHOAI 3.6 is already installed.
`shared-cluster/install-llm-models-argocd-app.yaml` is also not used, because the cluster has a single GPU
and `hsworkshop` already runs Qwen3.6 on it.

## Log in

- **Web console:** use **Log in via OpenShift**. Members of `cluster-admins`, `dedicated-admins` and
  `rhods-admins` get the Argo CD `admin` role.
- **Local admin (break-glass):**

  ```bash
  oc get secret openshift-gitops-cluster -n openshift-gitops -o jsonpath='{.data.admin\.password}' | base64 -d
  ```

- **CLI**, if you have `argocd` installed (`brew install argocd`):

  ```bash
  argocd login openshift-gitops-server-openshift-gitops.apps.kpicl2-pool-rft8p.aws.rh-ods.com --sso --grpc-web
  ```

## Day-to-day workflow

1. Edit manifests on branch `zijeme-it`, then validate locally:

   ```bash
   oc kustomize module-hsworkshop/install >/dev/null
   helm template t shared-cluster/deploy-model -f shared-cluster/deploy-model/qwen36-35b-a3b-nvfp4-hsworkshop.yaml
   ```

2. Commit and push to `origin zijeme-it`. Argo CD polls every ~3 minutes and syncs automatically.
3. To apply a change immediately, either click **Refresh**, then **Sync**, in the console, or run:

   ```bash
   oc annotate applications.argoproj.io hsworkshop -n openshift-gitops argocd.argoproj.io/refresh=hard --overwrite
   ```

## Check status

```bash
oc get applications.argoproj.io -n openshift-gitops
oc get applications.argoproj.io hsworkshop -n openshift-gitops \
  -o jsonpath='{.status.sync.status} {.status.health.status} {.status.sync.revisions}{"\n"}'
argocd app get hsworkshop --grpc-web          # per-resource view
argocd app diff hsworkshop --grpc-web         # what Git would change
```

## A sync is stuck

`Running: waiting for healthy state of ...` blocks newer commits until it finishes. To terminate and resync:

```bash
argocd app terminate-op hsworkshop --grpc-web
argocd app sync hsworkshop --grpc-web
```

If you have no local CLI, run the same commands inside the server pod:

```bash
oc exec -n openshift-gitops deploy/openshift-gitops-server -- sh -c \
  'export HOME=/tmp; argocd login localhost:8080 --insecure --grpc-web --username admin --password "$PASS"; argocd app terminate-op hsworkshop --grpc-web'
```

## Things Argo CD does not manage

- **Secrets:** create these once with `module-hsworkshop/apply-secrets.sh`.
  `guardrails-langfuse-secret` is optional; create it after you generate Langfuse API keys.
- **TrustyAI patches:** run `module-hsworkshop/apply-trustyai-patches.sh` after every orchestrator
  reconcile.
- **GuardrailsOrchestrator stuck in `Error`/`AutoConfigFailed`:** the operator never retries. Delete and
  re-apply the CR once `qwen36-service` exists, then re-run the patch script. Sync-wave 5 on the CR prevents this
  for fresh syncs.
- **Controller permissions:** `openshift-gitops-argocd-application-controller` is bound to `cluster-admin`
  (ClusterRoleBinding `openshift-gitops-controller-cluster-admin`), because the app creates cluster-scoped RBAC.
  This is acceptable for a demo cluster; scope it down for shared clusters.
