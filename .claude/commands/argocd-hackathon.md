Set up ArgoCD CLI access for the hackathon cluster.

1. Start port-forward to the ArgoCD server on port 12746 in the background:
```bash
oc port-forward svc/openshift-gitops-server -n openshift-gitops 12746:443 &>/tmp/argocd-portforward.log &
```

2. Wait a moment for the port-forward to establish, then log in using the admin password from the cluster secret:
```bash
sleep 2 && argocd login localhost:12746 --insecure \
  --username admin \
  --password "$(oc get secret openshift-gitops-cluster -n openshift-gitops -o jsonpath='{.data.admin\.password}' | base64 -d)"
```

3. Verify access:
```bash
argocd app list
```

After setup, use standard argocd commands:
- `argocd app list` — list all apps
- `argocd app get openshift-gitops/hsworkshop` — get app status
- `argocd app sync openshift-gitops/hsworkshop` — trigger sync
- `argocd app history openshift-gitops/hsworkshop` — show sync history

Note: The port-forward runs in the background and will stop when the terminal session ends. Re-run this command to restore access in a new session.
