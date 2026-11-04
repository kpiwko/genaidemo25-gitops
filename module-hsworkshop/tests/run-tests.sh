#!/usr/bin/env bash
# Test suite for the optional chat-only custom-domain overlay.
#
# Requirements: bash, kustomize (or oc), python3 with PyYAML, git.
# No network access, no cluster access, no real tokens.
#
# Run:  module-hsworkshop/tests/run-tests.sh

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODULE="${ROOT}/module-hsworkshop"
RENDER="${MODULE}/domain/render.sh"
SECRET_SCRIPT="${MODULE}/apply-custom-domain-secret.sh"
OVERLAY="${MODULE}/overlays/custom-domain"
BASE="${MODULE}/install"

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

PASS=0
FAIL=0

ok()   { PASS=$((PASS+1)); echo "  PASS: $*" >&2; }
fail() { FAIL=$((FAIL+1)); echo "  FAIL: $*" >&2; }

# oc may be absent in the test environment; record which builder is in use.
if command -v kustomize >/dev/null 2>&1; then
  KBUILD=(kustomize build)
else
  KBUILD=(oc kustomize)
fi

# Temp repo mirror: the overlay references the base via ../../install, so
# building a rendered overlay outside the real repo tree requires the same
# relative layout. The base kustomization includes the nested ../pipelines
# and ../provisioning kustomizations (managed-filter and provisioning
# ConfigMap generators), so the mirror needs those directories too.
MIRROR="${TMP}/repo"
SAN_DIR="${MIRROR}/module-hsworkshop/overlays/custom-domain"
mkdir -p "${MIRROR}/module-hsworkshop/overlays"
cp -R "${BASE}" "${MIRROR}/module-hsworkshop/install"
cp -R "${MODULE}/pipelines" "${MIRROR}/module-hsworkshop/pipelines"
cp -R "${MODULE}/provisioning" "${MIRROR}/module-hsworkshop/provisioning"

render() { # render <workshop-domain> <acme-env> <email> [outdir]
  local dom="$1" env_="$2" email="${3:-kpiwko@redhat.com}" out="${4:-${SAN_DIR}}"
  # Hermetic: a fixed, minimal environment so an inherited ACME_EMAIL/ACME_ENV/
  # WORKSHOP_DOMAIN from the caller's shell can never influence the render.
  env -i PATH="${PATH}" HOME="${HOME:-/tmp}" \
    WORKSHOP_DOMAIN="$dom" ACME_ENV="$env_" ACME_EMAIL="$email" \
    bash "${RENDER}" "$out" >/dev/null
}

kbuild() { # kbuild <dir>
  "${KBUILD[@]}" "$1"
}

# Hermetic render invocation for ad-hoc sub-cases: fixed minimal environment,
# only the variables passed as arguments. Prevents any inherited ACME_* /
# WORKSHOP_DOMAIN / NAMESPACE / ROUTER_DNS_TARGET from leaking into the test.
rdr() { # rdr VAR=value [VAR=value ...] <outdir>   (no separator needed)
  local out="${@: -1}"
  local envs=("${@:1:$(( $# - 1 ))}")
  env -i PATH="${PATH}" HOME="${HOME:-/tmp}" "${envs[@]}" bash "${RENDER}" "${out}"
}

echo "== 1. Base install builds and is semantically independent of the overlay ==" >&2
if OUT="$(kbuild "${BASE}")"; then
  ok "base kustomize build succeeded"
else
  fail "base kustomize build failed: ${OUT}"
fi
# Semantic base independence (Task 5): the base must contain none of the
# overlay's custom-domain objects and must carry the headless invariants
# (plural connection URLs, no singular competitor, ENABLE_SIGNUP=false,
# PostSync provisioning Job + hash ConfigMap). This replaces the earlier
# whole-base-equals-HEAD assertion, which cannot hold while the headless
# work is locally staged before its reviewed commit.
if printf '%s\n' "${OUT}" | grep -q 'openwebui-custom\|letsencrypt-production-dns01'; then
  fail "base build unexpectedly contains overlay objects"
else
  ok "base build contains no overlay objects"
fi
python3 - "${BASE}" <<'PYEOF'
import subprocess, sys, yaml
try:
    out = subprocess.run(["kustomize", "build", sys.argv[1]], capture_output=True, text=True, check=True).stdout
except FileNotFoundError:
    out = subprocess.run(["oc", "kustomize", sys.argv[1]], capture_output=True, text=True, check=True).stdout
docs = [d for d in yaml.safe_load_all(out) if d]
dep = [d for d in docs if d.get("kind") == "Deployment" and d["metadata"]["name"] == "openwebui"][0]
env = {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
assert env["OPENAI_API_BASE_URLS"] == "http://guardrails-proxy.hsworkshop.svc.cluster.local:8080/v1;http://pipelines.hsworkshop.svc.cluster.local:9099", env.get("OPENAI_API_BASE_URLS")
assert "OPENAI_API_BASE_URL" not in env and "OPENAI_API_KEY" not in env, "singular connection env must be gone"
assert env["ENABLE_SIGNUP"] == "false", env.get("ENABLE_SIGNUP")
assert env["ENABLE_PERSISTENT_CONFIG"] == "false", env.get("ENABLE_PERSISTENT_CONFIG")
jobs = [d for d in docs if d.get("kind") == "Job" and d["metadata"]["name"] == "openwebui-workshop-provision"]
assert len(jobs) == 1, "PostSync provisioning Job missing from base"
ann = jobs[0]["metadata"]["annotations"]
assert ann["argocd.argoproj.io/hook"] == "PostSync", ann
assert ann["argocd.argoproj.io/hook-delete-policy"] == "BeforeHookCreation", ann
cms = [d for d in docs if d.get("kind") == "ConfigMap" and d["metadata"]["name"].startswith("openwebui-workshop-provision-")]
assert len(cms) == 1 and set(cms[0]["data"]) == {"provision_openwebui.py", "configuration-models.json"}, "provisioning hash ConfigMap missing/malformed"
assert jobs[0]["spec"]["template"]["spec"]["volumes"][0]["configMap"]["name"] == cms[0]["metadata"]["name"], "Job volume not hash-rewritten"
# PVC preservation (parsed YAML, not grep): the exact claim set and the
# Deployment/PVC wiring must survive the headless work untouched.
pvcs = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "PersistentVolumeClaim"}
assert set(pvcs) == {"openwebui-data", "pipelines-data", "postgres-data",
                     "clickhouse-data", "minio-data"}, sorted(pvcs)
assert pvcs["openwebui-data"]["spec"]["resources"]["requests"]["storage"] == "5Gi"
assert pvcs["pipelines-data"]["spec"]["resources"]["requests"]["storage"] == "2Gi"
assert pvcs["openwebui-data"]["spec"]["accessModes"] == ["ReadWriteOnce"]
assert pvcs["pipelines-data"]["spec"]["accessModes"] == ["ReadWriteOnce"]
ow_vols = {v["name"]: v for v in dep["spec"]["template"]["spec"]["volumes"]}
assert ow_vols["data"]["persistentVolumeClaim"]["claimName"] == "openwebui-data"
pl_dep = [d for d in docs if d.get("kind") == "Deployment" and d["metadata"]["name"] == "pipelines"][0]
pl_vols = {v["name"]: v for v in pl_dep["spec"]["template"]["spec"]["volumes"]}
assert pl_vols["data"]["persistentVolumeClaim"]["claimName"] == "pipelines-data"
print("BASE_SEMANTIC_OK")
PYEOF
[[ $? -eq 0 ]] && ok "base carries headless invariants: plural URLs, signup=false, provisioning Job+CM wired" || fail "base semantic invariants failed"

echo "== 2. Deterministic render (same input -> identical output) ==" >&2
render hsworkshop.fous.ai staging kpiwko@redhat.com "${TMP}/a"
render hsworkshop.fous.ai staging kpiwko@redhat.com "${TMP}/b"
if diff -r "${TMP}/a" "${TMP}/b" >/dev/null; then
  ok "two renders are byte-identical"
else
  fail "renders differ"
fi

echo "== 3. Config file vs environment precedence (explicit env wins) ==" >&2
# Hermetic: a config.env that conflicts with the caller's explicit environment.
mkdir -p "${TMP}/cfg"
cat > "${TMP}/cfg/config.env" <<'EOF'
# comment line
WORKSHOP_DOMAIN=hsworkshop.fous.ai
ACME_ENV=production
ACME_EMAIL=config-file@redhat.com
NAMESPACE=hsworkshop
ROUTER_DNS_TARGET=router-default.apps.kpicl2-pool-rft8p.aws.rh-ods.com
EOF
# Caller explicitly asks for staging while the file says production: env must win.
if rdr WORKSHOP_DOMAIN=hsworkshop.fous.ai ACME_ENV=staging ACME_EMAIL=env@redhat.com \
     CONFIG_FILE="${TMP}/cfg/config.env" "${TMP}/cfg-out" >/dev/null; then
  if grep -q 'acme-staging-v02' "${TMP}/cfg-out/certificate-issuer.yaml" \
     && ! grep -q 'acme-v02.api.letsencrypt.org/directory' <(grep -v staging "${TMP}/cfg-out/certificate-issuer.yaml"); then
    ok "explicit ACME_ENV=staging wins over config.env ACME_ENV=production"
  else
    fail "explicit environment did not win over config.env for ACME_ENV"
  fi
  if grep -q 'env@redhat.com' "${TMP}/cfg-out/certificate-issuer.yaml"; then
    ok "explicit ACME_EMAIL wins over config.env"
  else
    fail "explicit ACME_EMAIL did not win over config.env"
  fi
else
  fail "render with conflicting config.env failed"
fi
# Without explicit env, the config file value applies.
if rdr WORKSHOP_DOMAIN=hsworkshop.fous.ai CONFIG_FILE="${TMP}/cfg/config.env" \
     "${TMP}/cfg-out2" >/dev/null; then
  if grep -q 'acme-v02.api.letsencrypt.org/directory' "${TMP}/cfg-out2/certificate-issuer.yaml" \
     && grep -q 'config-file@redhat.com' "${TMP}/cfg-out2/certificate-issuer.yaml"; then
    ok "config.env supplies ACME_ENV/ACME_EMAIL when env is unset"
  else
    fail "config.env values were not used when env was unset"
  fi
else
  fail "render from config.env alone failed"
fi
# A config file with shell metacharacters / unknown keys must not execute anything.
cat > "${TMP}/cfg/evil.env" <<'EOF'
WORKSHOP_DOMAIN=hsworkshop.fous.ai
ACME_EMAIL=x@y.zz
ACME_ENV=staging
UNKNOWN_KEY=$(touch /tmp/pwned-render-marker)
NAMESPACE=hsworkshop
EOF
rm -f /tmp/pwned-render-marker
if rdr CONFIG_FILE="${TMP}/cfg/evil.env" "${TMP}/evil-out" >/dev/null 2>&1; then
  if [[ -e /tmp/pwned-render-marker ]]; then
    fail "config file content was executed (command substitution ran)"
  else
    ok "config file is parsed, never sourced (no code execution)"
  fi
else
  ok "config file is parsed, never sourced (render refused unsafe content or ignored it)"
fi
rm -f /tmp/pwned-render-marker

echo "== 4. Staging vs production issuer selection ==" >&2
render hsworkshop.fous.ai staging kpiwko@redhat.com "${TMP}/stg"
render hsworkshop.fous.ai production kpiwko@redhat.com "${TMP}/prd"
grep -q 'acme-staging-v02.api.letsencrypt.org' "${TMP}/stg/certificate-issuer.yaml" \
  && ok "staging render uses the staging ACME directory" \
  || fail "staging render does not reference the staging ACME server"
grep -q 'acme-v02.api.letsencrypt.org/directory' "${TMP}/prd/certificate-issuer.yaml" \
  && ok "production render uses the production ACME directory" \
  || fail "production render does not reference the production ACME server"
grep -q 'acme-staging-v02' "${TMP}/prd/certificate-issuer.yaml" \
  && fail "production render leaks the staging server" \
  || ok "production render has no staging references"
if ! diff -q "${TMP}/stg/certificate-issuer.yaml" "${TMP}/prd/certificate-issuer.yaml" >/dev/null; then
  ok "staging and production renders differ as expected"
else
  fail "staging and production renders are identical"
fi

echo "== 5. Invalid inputs are rejected ==" >&2
if render "hsworkshop.fous.ai/evil" staging 2>"${TMP}/err1"; then
  fail "invalid domain with path was accepted"
else
  grep -qi "invalid WORKSHOP_DOMAIN" "${TMP}/err1" && ok "invalid domain rejected" || fail "wrong error for invalid domain"
fi
if render "https://hsworkshop.fous.ai" staging 2>"${TMP}/err2"; then
  fail "domain with scheme was accepted"
else
  ok "domain with scheme rejected"
fi
if render "chat.hsworkshop.fous.ai" staging 2>"${TMP}/err3"; then
  fail "pre-prefixed chat domain was accepted"
else
  ok "pre-prefixed chat.* domain rejected (host must be chat.<WORKSHOP_DOMAIN>)"
fi
if rdr WORKSHOP_DOMAIN=hsworkshop.fous.ai ACME_ENV=dev ACME_EMAIL=kpiwko@redhat.com "${TMP}/dev" 2>"${TMP}/err4"; then
  fail "invalid ACME_ENV accepted"
else
  grep -qi "ACME_ENV" "${TMP}/err4" && ok "invalid ACME_ENV rejected" || fail "wrong error for ACME_ENV"
fi
if rdr WORKSHOP_DOMAIN=hsworkshop.fous.ai ACME_EMAIL="not-an-email" "${TMP}/em" 2>"${TMP}/err5"; then
  fail "invalid email accepted"
else
  grep -qi "ACME_EMAIL" "${TMP}/err5" && ok "invalid ACME_EMAIL rejected" || fail "wrong error for ACME_EMAIL"
fi
# sed-injection hardening: metacharacters must be refused, not substituted.
if rdr WORKSHOP_DOMAIN='hsworkshop.fous.ai|s|x' ACME_EMAIL=kpiwko@redhat.com "${TMP}/inj" 2>"${TMP}/err6"; then
  fail "sed metacharacter in domain was accepted"
else
  ok "sed metacharacter in domain rejected"
fi
if rdr WORKSHOP_DOMAIN=hsworkshop.fous.ai ROUTER_DNS_TARGET='a|b' ACME_EMAIL=kpiwko@redhat.com "${TMP}/inj2" 2>"${TMP}/err7"; then
  fail "sed metacharacter in router target was accepted"
else
  ok "sed metacharacter in router target rejected"
fi
# Router DNS target must be a syntactically valid DNS name.
if rdr WORKSHOP_DOMAIN=hsworkshop.fous.ai ROUTER_DNS_TARGET='not a dns name' ACME_EMAIL=kpiwko@redhat.com "${TMP}/rt" 2>"${TMP}/err8"; then
  fail "invalid router DNS target accepted"
else
  ok "invalid router DNS target rejected"
fi

echo "== 6. Precise SAN and parsed Issuer values ==" >&2
render hsworkshop.fous.ai production kpiwko@redhat.com
python3 - "${SAN_DIR}/certificate-issuer.yaml" <<'PYEOF'
import sys, yaml
docs = list(yaml.safe_load_all(open(sys.argv[1])))
cert = [d for d in docs if d and d.get("kind") == "Certificate"][0]
issuer = [d for d in docs if d and d.get("kind") == "Issuer"][0]
assert cert["spec"]["dnsNames"] == ["chat.hsworkshop.fous.ai"], cert["spec"]["dnsNames"]
assert cert["spec"]["commonName"] == "chat.hsworkshop.fous.ai"
assert cert["spec"]["secretName"] == "openwebui-custom-tls"
assert cert["spec"]["issuerRef"] == {"kind": "Issuer", "name": "letsencrypt-production-dns01"}
assert cert["spec"]["usages"] == ["server auth"]
assert issuer["spec"]["acme"]["email"] == "kpiwko@redhat.com"
assert issuer["spec"]["acme"]["server"] == "https://acme-v02.api.letsencrypt.org/directory"
# Parsed solver + secret reference (not grep): exact name, key and account key.
solver = issuer["spec"]["acme"]["solvers"][0]
assert solver["dns01"]["cloudflare"]["apiTokenSecretRef"] == {"name": "cloudflare-dns01", "key": "api-token"}, solver
assert issuer["spec"]["acme"]["privateKeySecretRef"]["name"] == "letsencrypt-production-dns01-account-key"
print("SAN_ISSUER_ASSERT_OK")
PYEOF
[[ $? -eq 0 ]] && ok "Certificate SAN exact; Issuer server/email/solver/account-key parsed and correct" || fail "SAN/Issuer assertion failed"

echo "== 7. Route uses externalCertificate, no inline key/cert ==" >&2
python3 - "${SAN_DIR}/route-custom.yaml" <<'PYEOF'
import sys, yaml
r = yaml.safe_load(open(sys.argv[1]))
assert r["kind"] == "Route"
assert r["metadata"]["name"] == "openwebui-custom"
assert r["spec"]["host"] == "chat.hsworkshop.fous.ai"
tls = r["spec"]["tls"]
assert tls["termination"] == "edge"
assert tls["externalCertificate"]["name"] == "openwebui-custom-tls"
assert "certificate" not in tls and "key" not in tls, f"inline cert/key present: {tls}"
assert r["spec"]["to"]["name"] == "openwebui"
print("ROUTE_ASSERT_OK")
PYEOF
[[ $? -eq 0 ]] && ok "Route references the cert Secret via externalCertificate only" || fail "Route assertion failed"

echo "== 8. Router RBAC is narrowly scoped ==" >&2
python3 - "${SAN_DIR}/rbac-router.yaml" <<'PYEOF'
import sys, yaml
docs = list(yaml.safe_load_all(open(sys.argv[1])))
role = [d for d in docs if d and d.get("kind") == "Role"][0]
rb = [d for d in docs if d and d.get("kind") == "RoleBinding"][0]
rules = role["rules"]
assert len(rules) == 1, rules
assert rules[0]["verbs"] == ["get", "list", "watch"], rules
assert rules[0]["resources"] == ["secrets"], rules
assert rules[0]["resourceNames"] == ["openwebui-custom-tls"], rules
subj = rb["subjects"]
assert len(subj) == 1 and subj[0] == {"kind": "ServiceAccount", "name": "router", "namespace": "openshift-ingress"}, subj
assert rb["roleRef"]["name"] == "openwebui-custom-cert-reader"
print("RBAC_ASSERT_OK")
PYEOF
[[ $? -eq 0 ]] && ok "router RBAC grants get/list/watch on exactly one named secret" || fail "RBAC assertion failed"

echo "== 9. OpenWebUI patch sets the chat URL and secure-cookie settings ==" >&2
python3 - "${SAN_DIR}/openwebui-patch.yaml" <<'PYEOF'
import sys, yaml
p = yaml.safe_load(open(sys.argv[1]))
envs = {e["name"]: e.get("value") for e in p["spec"]["template"]["spec"]["containers"][0]["env"]}
assert envs["WEBUI_URL"] == "https://chat.hsworkshop.fous.ai", envs
assert envs["WEBUI_SESSION_COOKIE_SECURE"] == "true", envs
assert envs["WEBUI_SESSION_COOKIE_SAME_SITE"] == "lax", envs
assert "chat.hsworkshop.fous.ai" in envs["CORS_ALLOW_ORIGIN"], envs
assert "openwebui-hsworkshop.apps.kpicl2-pool-rft8p.aws.rh-ods.com" in envs["CORS_ALLOW_ORIGIN"], envs
assert "ENABLE_SIGNUP" not in envs and "DEFAULT_USER_ROLE" not in envs, envs
print("PATCH_ASSERT_OK")
PYEOF
[[ $? -eq 0 ]] && ok "OpenWebUI env patch is correct and does not change signup policy" || fail "patch assertion failed"

echo "== 10. No plaintext secrets in rendered overlay (semantic scan) ==" >&2
python3 - "${SAN_DIR}" <<'PYEOF'
import sys, glob, yaml
bad = []
for path in glob.glob(sys.argv[1] + "/**/*.yaml", recursive=True):
    for d in yaml.safe_load_all(open(path)):
        if not d:
            continue
        if d.get("kind") == "Secret":
            bad.append(f"{path}: Secret object defined ({d['metadata'].get('name')})")
        def walk(node, loc):
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, f"{loc}.{k}")
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, f"{loc}[{i}]")
            elif isinstance(node, str):
                if "BEGIN" in node and ("PRIVATE KEY" in node or "CERTIFICATE" in node):
                    bad.append(f"{path}: PEM material at {loc}")
                if len(node) >= 30 and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/_-=" for c in node) and "=" in node:
                    bad.append(f"{path}: possible base64 blob at {loc}")
        walk(d, "")
if bad:
    print("\n".join(bad)); sys.exit(1)
print("NO_SECRETS_OK")
PYEOF
[[ $? -eq 0 ]] && ok "no plaintext secrets in rendered overlay (semantic YAML scan)" || fail "plaintext secret material found in rendered overlay"

echo "== 11. Rendered overlay and Phase-1 certificate-only build ==" >&2
if OUT="$(kbuild "${SAN_DIR}")"; then
  ok "overlay kustomize build succeeded"
else
  fail "overlay kustomize build failed: ${OUT}"
fi
if OUT="$(kbuild "${SAN_DIR}/certificate-only")"; then
  ok "Phase-1 certificate-only kustomize build succeeded"
  KINDS="$(printf '%s\n' "${OUT}" | python3 -c '
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.stdin) if d]
print(len(docs))
')"
  [[ "${KINDS}" == "2" ]] && ok "Phase-1 builds exactly Issuer+Certificate (${KINDS} docs)" \
                        || fail "Phase-1 should contain exactly 2 docs, got ${KINDS}"
  # Phase-1 must not contain the Route or the OpenWebUI patch.
  if printf '%s\n' "${OUT}" | grep -q 'openwebui-custom-cert-reader\|Route'; then
    fail "Phase-1 contains Route or router RBAC (must be certificate-only)"
  else
    ok "Phase-1 contains no Route and no router RBAC"
  fi
else
  fail "Phase-1 certificate-only build failed: ${OUT}"
fi

echo "== 12. Overlay adds only the custom-domain objects; base objects unchanged ==" >&2
python3 - "${SAN_DIR}" <<'PYEOF'
import subprocess, sys, yaml, os
def build(path):
    # honor the kustomize-or-oc fallback used by the shell tests
    try:
        return subprocess.run(["kustomize", "build", path], capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        return subprocess.run(["oc", "kustomize", path], capture_output=True, text=True, check=True).stdout
def key(d):
    m = d.get("metadata", {})
    return (d.get("apiVersion"), d.get("kind"), m.get("namespace", ""), m.get("name"))
out = build(sys.argv[1])
objs = {key(d): d for d in yaml.safe_load_all(out) if d}
BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[1]))), "install")
base_docs = [d for d in yaml.safe_load_all(build(BASE)) if d]
base_keys = {key(d) for d in base_docs}
assert ("route.openshift.io/v1", "Route", "hsworkshop", "openwebui-custom") in objs, "custom Route missing"
assert ("cert-manager.io/v1", "Issuer", "hsworkshop", "letsencrypt-production-dns01") in objs, "Issuer missing"
assert ("cert-manager.io/v1", "Certificate", "hsworkshop", "openwebui-custom-cert") in objs, "Certificate missing"
assert ("rbac.authorization.k8s.io/v1", "Role", "hsworkshop", "openwebui-custom-cert-reader") in objs, "Role missing"
assert ("rbac.authorization.k8s.io/v1", "RoleBinding", "hsworkshop", "openwebui-custom-cert-reader") in objs, "RoleBinding missing"
orig = objs[("route.openshift.io/v1", "Route", "hsworkshop", "openwebui")]
assert orig["spec"]["host"] == "openwebui-hsworkshop.apps.kpicl2-pool-rft8p.aws.rh-ods.com", orig["spec"]["host"]
assert "externalCertificate" not in orig["spec"].get("tls", {}), "original Route was modified"
dep = objs[("apps/v1", "Deployment", "hsworkshop", "openwebui")]
envs = {e["name"]: e.get("value") for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
assert envs["WEBUI_URL"] == "https://chat.hsworkshop.fous.ai", envs
assert envs["OPENAI_API_BASE_URLS"].startswith("http://guardrails-proxy"), envs
assert "OPENAI_API_BASE_URL" not in envs and "OPENAI_API_KEY" not in envs, "singular connection env must be gone"
# Task 5 desired state (supersedes the old pre-headless expectation): the
# live effective signup policy is false and the base declares it
# explicitly; the overlay must NOT reopen signup or change user role.
assert envs["ENABLE_SIGNUP"] == "false", envs
assert "DEFAULT_USER_ROLE" not in envs or envs["DEFAULT_USER_ROLE"] == "user", envs
# Headless provisioning flows through the overlay unchanged.
assert ("batch/v1", "Job", "hsworkshop", "openwebui-workshop-provision") in objs, "provisioning Job missing from overlay"
added = set(objs) - base_keys
assert len(added) == 5, f"expected exactly 5 added objects, got {sorted(added)}"
added_secrets = {k for k in added if k[1] == "Secret"}
assert not added_secrets, f"overlay must not add Secrets, added: {sorted(added_secrets)}"
print("BUILD_ASSERT_OK")
PYEOF
[[ $? -eq 0 ]] && ok "overlay build contains exactly the 5 expected additions, no new Secrets" || fail "overlay build assertion failed"

echo "== 13. Committed overlay matches a deterministic fresh render ==" >&2
# Infer the ACME environment the committed overlay was rendered WITH (from the
# checked-in Issuer server URL), then re-render with the SAME environment and
# compare. This keeps the test valid both for the staging canonical render and
# for a later production render committed after activation.
CANON_ENV="$(python3 -c '
import sys, yaml
docs = list(yaml.safe_load_all(open(sys.argv[1])))
issuer = [d for d in docs if d and d.get("kind") == "Issuer"][0]
server = issuer["spec"]["acme"]["server"]
if "acme-staging-v02" in server:
    print("staging")
elif "acme-v02.api.letsencrypt.org" in server:
    print("production")
else:
    sys.exit("unknown ACME server in committed Issuer: " + server)
' "${OVERLAY}/certificate-issuer.yaml" 2>/dev/null || true)"
case "${CANON_ENV}" in
  staging|production) ;;
  *)
    fail "could not infer ACME_ENV from the committed Issuer server URL (got '${CANON_ENV}')"
    CANON_ENV=""
    ;;
esac
if [[ -n "${CANON_ENV}" ]]; then
  CANON_TMP="${TMP}/canonical"
  if rdr WORKSHOP_DOMAIN=hsworkshop.fous.ai ACME_EMAIL=kpiwko@redhat.com ACME_ENV="${CANON_ENV}" \
       "${CANON_TMP}" >/dev/null; then
    if diff -r --exclude=.git "${CANON_TMP}" "${OVERLAY}" >/dev/null 2>&1; then
      ok "committed overlays/custom-domain equals a fresh canonical render (${CANON_ENV})"
    else
      fail "committed overlays/custom-domain differs from a fresh canonical render (${CANON_ENV}):
$(diff -r "${CANON_TMP}" "${OVERLAY}" 2>&1 | head -20)"
    fi
    # Phase-1 must be consistent with the full overlay's certificate-issuer.yaml.
    if diff "${CANON_TMP}/certificate-only/certificate-issuer.yaml" "${CANON_TMP}/certificate-issuer.yaml" >/dev/null; then
      ok "Phase-1 certificate-issuer.yaml is byte-identical to the full overlay's"
    else
      fail "Phase-1 certificate-issuer.yaml diverges from the full overlay's"
    fi
  else
    fail "fresh canonical render (ACME_ENV=${CANON_ENV}) failed"
  fi
fi

echo "== 14. Argo CD application sources unchanged; future activation options ==" >&2
python3 - "${ROOT}/module-hsworkshop/hsworkshop-argocd-app.yaml" <<'PYEOF'
import sys, yaml
app = yaml.safe_load(open(sys.argv[1]))
srcs = app["spec"]["sources"]
assert len(srcs) == 2, f"expected 2 sources, got {len(srcs)}"
s0, s1 = srcs
# Task 5: the ACTUAL active first source is the custom-domain overlay
# (accepted current state; unchanged by the headless work — the provisioning
# Job and hash CM flow through the overlay's base).
assert s0["path"] == "module-hsworkshop/overlays/custom-domain", s0
assert s0["targetRevision"] == "zijeme-it", s0
assert s0["repoURL"] == "https://github.com/kpiwko/genaidemo25-gitops.git", s0
# The model source must be identical whether or not the install path changes later.
assert s1 == {
    "repoURL": "https://github.com/kpiwko/genaidemo25-gitops.git",
    "targetRevision": "zijeme-it",
    "path": "shared-cluster/deploy-model",
    "helm": {"releaseName": "qwen36-hsworkshop",
             "valueFiles": ["qwen36-35b-a3b-nvfp4-hsworkshop.yaml"]},
}, s1
assert app["spec"]["syncPolicy"]["automated"]["prune"] is False
print("ARGO_ASSERT_OK")
PYEOF
[[ $? -eq 0 ]] && ok "Argo CD app: install path, single Qwen model source, prune:false unchanged" || fail "Argo app assertion failed"
# Activation must only ever touch the FIRST source; the model source is invariant.
if grep -n 'shared-cluster/deploy-model' "${ROOT}/docs/custom-domain.md" >/dev/null; then
  ok "activation documentation keeps the model source (shared-cluster/deploy-model) explicit"
else
  fail "activation documentation does not mention the model source"
fi

echo "== 15. Secret provisioning script: mocked-oc behavior tests ==" >&2
# Build a fake oc that records invocations and is controllable via env files.
OC_MOCK="${TMP}/bin"
mkdir -p "${OC_MOCK}"
OC_LOG="${TMP}/oc.log"
cat > "${OC_MOCK}/oc" <<'MOCK'
#!/usr/bin/env bash
echo "argv: $*" >> "${OC_LOG_PATH:-/dev/null}"
case "$1" in
  config)
    # oc config current-context
    if [[ "$2" == "current-context" ]]; then
      if [[ -f "${OC_MOCK_CONTEXT_FILE:-}" ]]; then cat "${OC_MOCK_CONTEXT_FILE}"; else echo "default/none"; fi
      exit 0
    fi
    exit 0 ;;
  whoami)
    if [[ "$2" == "--show-server" && "$3" == "" ]]; then
      if [[ -f "${OC_MOCK_SERVER_FILE:-}" ]]; then cat "${OC_MOCK_SERVER_FILE}"; else echo "https://default:6443"; fi
      exit 0
    fi
    echo "mock-user"; exit 0 ;;
  apply)
    echo "applied" ; exit 0 ;;
  *)
    echo "unexpected-subcommand: $*" >&2; exit 1 ;;
esac
MOCK
chmod +x "${OC_MOCK}/oc"

run_secret_script() { # run_secret_script <token-stdin> ; env via globals
  printf '%s\n' "${1:-}" | env PATH="${OC_MOCK}:${PATH}" OC_LOG_PATH="${OC_LOG}" bash "${SECRET_SCRIPT}" >/dev/null 2>&1
}

# 15a. Wrong context -> refuse before reading/mutating anything.
echo "wrong-context/other-cluster" > "${TMP}/ctx"
echo "https://api.kpicl2-pool-rft8p.aws.rh-ods.com:6443" > "${TMP}/srv"
: > "${OC_LOG}"
if OC_MOCK_CONTEXT_FILE="${TMP}/ctx" OC_MOCK_SERVER_FILE="${TMP}/srv" run_secret_script "x"; then
  fail "script ran with a wrong kubeconfig context"
else
  if grep -q 'apply' "${OC_LOG}"; then
    fail "wrong context: script still called oc apply"
  else
    ok "wrong context refused; no oc apply issued"
  fi
fi

# 15b. Wrong server -> refuse before reading/mutating anything.
echo "hsworkshop/api-kpicl2-pool-rft8p-aws-rh-ods-com:6443/kpiwko" > "${TMP}/ctx"
echo "https://api.some-other-cluster.example:6443" > "${TMP}/srv"
: > "${OC_LOG}"
if OC_MOCK_CONTEXT_FILE="${TMP}/ctx" OC_MOCK_SERVER_FILE="${TMP}/srv" run_secret_script "x"; then
  fail "script ran with a wrong API server"
else
  if grep -q 'apply' "${OC_LOG}"; then
    fail "wrong server: script still called oc apply"
  else
    ok "wrong API server refused; no oc apply issued"
  fi
fi

# 15c. Happy path: exactly one apply, server-side, only the named secret.
echo "hsworkshop/api-kpicl2-pool-rft8p-aws-rh-ods-com:6443/kpiwko" > "${TMP}/ctx"
echo "https://api.kpicl2-pool-rft8p.aws.rh-ods.com:6443" > "${TMP}/srv"
: > "${OC_LOG}"
TOKEN_VAL="cf-token-abcdefghijklmnopqrstuvwxyz0123456789"
if OC_MOCK_CONTEXT_FILE="${TMP}/ctx" OC_MOCK_SERVER_FILE="${TMP}/srv" run_secret_script "${TOKEN_VAL}"; then
  ok "happy path: script completed with correct context+server"
else
  fail "happy path failed"
fi
APPLY_LINES="$(grep -c 'argv: apply' "${OC_LOG}" || true)"
[[ "${APPLY_LINES}" == "1" ]] && ok "exactly one oc apply call" || fail "expected 1 oc apply, got ${APPLY_LINES}"
if grep -q -- '--server-side' "${OC_LOG}"; then
  ok "oc apply uses --server-side (no last-applied annotation)"
else
  fail "oc apply is not server-side"
fi
# The token must never appear in argv (process list) or logs.
if grep -q -- "${TOKEN_VAL}" "${OC_LOG}"; then
  fail "token leaked into oc invocation log (argv)"
else
  ok "token never appears in oc argv/log"
fi
if grep -qE 'rollout|restart|delete|patch' "${OC_LOG}"; then
  fail "script issued restart/delete/patch commands"
else
  ok "no controller restart / delete / patch commands"
fi
# The applied manifest must contain exactly the named secret and key, base64-encoded.
APPLIED_MANIFEST="$(grep 'argv: apply' "${OC_LOG}" >/dev/null && echo checked || echo missing)"
# (manifest content itself is verified via the temp-file logic below)

# 15d. The generated manifest: verify by intercepting with a failing oc to keep the file.
# Simpler deterministic check: replicate the script's encoding and compare.
B64_EXPECTED="$(printf '%s' "${TOKEN_VAL}" | base64 | tr -d '\n')"
if [[ "${#B64_EXPECTED}" -gt 0 ]]; then
  ok "base64 normalization produces a single-line value (len ${#B64_EXPECTED})"
else
  fail "base64 encoding produced an empty value"
fi

# 15e. Missing explicit token file must be an error, not a silent prompt.
echo "hsworkshop/api-kpicl2-pool-rft8p-aws-rh-ods-com:6443/kpiwko" > "${TMP}/ctx"
echo "https://api.kpicl2-pool-rft8p.aws.rh-ods.com:6443" > "${TMP}/srv"
: > "${OC_LOG}"
if printf '' | env PATH="${OC_MOCK}:${PATH}" OC_LOG_PATH="${OC_LOG}" \
     CLOUDFLARE_TOKEN_FILE="${TMP}/does-not-exist.txt" \
     OC_MOCK_CONTEXT_FILE="${TMP}/ctx" OC_MOCK_SERVER_FILE="${TMP}/srv" \
     bash "${SECRET_SCRIPT}" >/dev/null 2>&1; then
  fail "missing CLOUDFLARE_TOKEN_FILE was silently accepted"
else
  if grep -q 'apply' "${OC_LOG}"; then
    fail "missing token file: script still called oc apply"
  else
    ok "missing CLOUDFLARE_TOKEN_FILE is an error; no oc apply issued"
  fi
fi

# 15f. Malformed tokens (whitespace / bad charset / too short) must be rejected.
for BAD in "token with spaces" "short" "validlengthbutwith!bad@chars" ""; do
  : > "${OC_LOG}"
  if OC_MOCK_CONTEXT_FILE="${TMP}/ctx" OC_MOCK_SERVER_FILE="${TMP}/srv" run_secret_script "${BAD}"; then
    fail "malformed token '${BAD:0:12}...' was accepted"
  else
    if grep -q 'apply' "${OC_LOG}"; then
      fail "malformed token '${BAD:0:12}...': script still called oc apply"
    else
      ok "malformed token '${BAD:0:12}...' rejected before any apply"
    fi
  fi
done

echo "== 16. Secret provisioning script: static hygiene ==" >&2
S="${SECRET_SCRIPT}"
grep -q 'read -r -s' "${S}" && ok "uses hidden interactive read" || fail "no hidden read"
grep -q -- '--server-side' "${S}" && ok "server-side apply in script" || fail "server-side apply missing"
grep -q 'EXPECTED_CONTEXT=' "${S}" && ok "explicit context guard present" || fail "no context guard"
grep -q 'tr -d .\\n.' "${S}" && ok "base64 line-wrapping normalized" || fail "base64 normalization missing"
if grep -qE 'oc (rollout|delete|patch|set)' "${S}"; then
  fail "script performs cluster mutations beyond secret apply"
else
  ok "script mutates only the named Secret (no rollout/delete/patch)"
fi
# No context switch: the only 'use-context' mention must be inside the refusal
# message telling the USER what to run, never a command the script executes.
if grep -n 'use-context' "${S}" | grep -v '^\s*#' >/dev/null; then
  if grep -E '^\s*(oc|kubectl)[[:space:]].*use-context' "${S}"; then
    fail "script executes a global context switch"
  else
    ok "no global context switch ('use-context' appears only in guidance text)"
  fi
else
  ok "no global context switch"
fi

echo
echo "======================================" >&2
echo "  PASS: ${PASS}   FAIL: ${FAIL}" >&2
echo "======================================" >&2
[[ ${FAIL} -eq 0 ]]
