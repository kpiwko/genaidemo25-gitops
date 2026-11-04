#!/usr/bin/env bash
# Provision the headless OpenWebUI connection and admin-bootstrap Secrets in
# namespace hsworkshop on the hsworkshop cluster (plan Task 2):
#   - openwebui-connections-secret: GUARDRAILS_API_KEY / PIPELINES_API_KEY
#   - openwebui-admin-bootstrap-secret: WEBUI_ADMIN_EMAIL / WEBUI_ADMIN_PASSWORD
#
# SECURITY (modeled on apply-custom-domain-secret.sh):
#   - Context AND API server are validated BEFORE any secret value is read.
#   - Values are read interactively (hidden, read -s) or from files/stdin.
#     They are NEVER passed as CLI arguments (no shell history / process list).
#   - Values are NEVER echoed, logged, or written anywhere except the
#     temporary manifest files (mktemp, 0600, deleted immediately after apply).
#   - Server-side apply, no --force-conflicts; only the two named Secrets are
#     mutated; no restart/rollout/delete/patch; no global context switch.
#   - The existing openwebui-secret is NOT read, copied, or modified.
#
# Input protocol (structured, one value per line, order fixed):
#   line 1: WEBUI_ADMIN_EMAIL      (existing local admin email)
#   line 2: WEBUI_ADMIN_PASSWORD   (existing local admin password)
#   line 3: PIPELINES_API_KEY      (random new pipelines key; must match the
#                                   Pipelines manifest patched in Task 3)
# The GUARDRAILS_API_KEY is a fixed, explicitly-unused placeholder value
# applied by this script itself (documented as NOT real protection).
#
# File input: set OPENWEBUI_SECRETS_FILE to a 0600 file with the 3 lines above
# (a missing file is an error, never a silent prompt).
#
# DEPLOY GATE: applying these Secrets alone does not deploy anything; the
# install/openwebui.yaml changes (ENABLE_PERSISTENT_CONFIG=false et al.) are
# separately gated on the owner-admin sanitized effective-config inventory
# (see docs/headless-openwebui.md).

set -euo pipefail

EXPECTED_CONTEXT="hsworkshop/api-kpicl2-pool-rft8p-aws-rh-ods-com:6443/kpiwko"
EXPECTED_API="https://api.kpicl2-pool-rft8p.aws.rh-ods.com:6443"
NAMESPACE="hsworkshop"
CONNECTIONS_SECRET="openwebui-connections-secret"
ADMIN_SECRET="openwebui-admin-bootstrap-secret"

# Explicitly unused placeholder for the guardrails connection (the proxy does
# not authenticate); documented as NOT real protection.
GUARDRAILS_PLACEHOLDER="unused-guardrails-placeholder"

die() { echo "ERROR: $*" >&2; exit 1; }

command -v oc >/dev/null 2>&1 || die "oc CLI is required"

# --- Context guard (BEFORE any secret value is read) ----------------------------
CURRENT_CONTEXT="$(oc config current-context 2>/dev/null || true)"
CURRENT_SERVER="$(oc whoami --show-server 2>/dev/null || true)"
if [[ "${CURRENT_CONTEXT}" != "${EXPECTED_CONTEXT}" ]]; then
  die "refusing to run: current context is '${CURRENT_CONTEXT}', expected '${EXPECTED_CONTEXT}'.
       Switch with: oc config use-context ${EXPECTED_CONTEXT}"
fi
if [[ "${CURRENT_SERVER}" != "${EXPECTED_API}" ]]; then
  die "refusing to run: current server is '${CURRENT_SERVER}', expected '${EXPECTED_API}'"
fi

# --- Read the three input values without echoing them ---------------------------
read_line() { # read_line <varname> <label> <source-desc>
  local __var="$1" __label="$2" __src="$3" __val=""
  if [[ -n "${OPENWEBUI_SECRETS_FILE:-}" ]]; then
    IFS= read -r __val <&"${OPENWEBUI_SECRETS_FD}" || true
  elif [[ ! -t 0 ]]; then
    IFS= read -r __val || true
  else
    read -r -s -p "Enter ${__label} (input hidden): " __val
    echo
  fi
  printf -v "${__var}" '%s' "${__val}"
}

if [[ -n "${OPENWEBUI_SECRETS_FILE:-}" ]]; then
  [[ -f "${OPENWEBUI_SECRETS_FILE}" ]] \
    || die "OPENWEBUI_SECRETS_FILE='${OPENWEBUI_SECRETS_FILE}' does not exist or is not a regular file"
  [[ "$(stat -c '%a' "${OPENWEBUI_SECRETS_FILE}" 2>/dev/null || stat -f '%Lp' "${OPENWEBUI_SECRETS_FILE}")" == "600" ]] \
    || die "OPENWEBUI_SECRETS_FILE must have 0600 permissions"
  exec 3< "${OPENWEBUI_SECRETS_FILE}"
  OPENWEBUI_SECRETS_FD=3
fi

read_line WEBUI_ADMIN_EMAIL "existing admin email (WEBUI_ADMIN_EMAIL)" "input"
read_line WEBUI_ADMIN_PASSWORD "existing admin password (WEBUI_ADMIN_PASSWORD)" "input"
read_line PIPELINES_API_KEY "pipelines API key (PIPELINES_API_KEY)" "input"

[[ -n "${OPENWEBUI_SECRETS_FILE:-}" ]] && exec 3<&-

# --- Validate without echoing values ---------------------------------------------
# Admin credentials are the EXISTING local admin's values: any nonempty email
# (reasonable shape) and any nonempty password (including spaces, semicolons,
# short) are valid — there is deliberately NO minimum length and NO forced
# reset. The delimiter constraint applies ONLY to PIPELINES_API_KEY, because
# it is composed into the ';'-separated OPENAI_API_KEYS env.
[[ -n "${WEBUI_ADMIN_EMAIL}" ]] || die "empty WEBUI_ADMIN_EMAIL; nothing was applied"
EMAIL_RE='^[^[:space:]]+@[^[:space:]]+$'
[[ "${WEBUI_ADMIN_EMAIL}" =~ ${EMAIL_RE} ]] \
  || die "WEBUI_ADMIN_EMAIL is not a valid email; nothing was applied"
[[ -n "${WEBUI_ADMIN_PASSWORD}" ]] || die "empty WEBUI_ADMIN_PASSWORD; nothing was applied"
[[ -n "${PIPELINES_API_KEY}" ]] || die "empty PIPELINES_API_KEY; nothing was applied"
# Semicolons are the OPENAI_API_KEYS separator in OpenWebUI; a pipelines key
# containing one would corrupt the composed value.
[[ "${PIPELINES_API_KEY}" != *';'* ]] \
  || die "PIPELINES_API_KEY contains ';' (OPENAI_API_KEYS separator); refusing to apply"
if printf '%s' "${PIPELINES_API_KEY}" | grep -q '[[:space:]]'; then
  die "PIPELINES_API_KEY contains whitespace; refusing to apply"
fi

# --- Apply (values never in argv; server-side apply; temp files deleted) ---------
# base64 without line wrapping: GNU base64 wraps at 76 columns by default,
# which would produce invalid YAML. Normalize explicitly on both GNU and BSD.
b64() { printf '%s' "$1" | base64 | tr -d '\n'; }

TMP_MANIFEST="$(mktemp)"   # 0600 by default on macOS/Linux mktemp
trap 'rm -f "${TMP_MANIFEST}"' EXIT

printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: %s\n  namespace: %s\ntype: Opaque\ndata:\n  %s: %s\n  %s: %s\n' \
  "${CONNECTIONS_SECRET}" "${NAMESPACE}" \
  "GUARDRAILS_API_KEY" "$(b64 "${GUARDRAILS_PLACEHOLDER}")" \
  "PIPELINES_API_KEY" "$(b64 "${PIPELINES_API_KEY}")" > "${TMP_MANIFEST}"
oc apply --server-side -f "${TMP_MANIFEST}" >/dev/null
rm -f "${TMP_MANIFEST}"

TMP_MANIFEST="$(mktemp)"
printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: %s\n  namespace: %s\ntype: Opaque\ndata:\n  %s: %s\n  %s: %s\n' \
  "${ADMIN_SECRET}" "${NAMESPACE}" \
  "WEBUI_ADMIN_EMAIL" "$(b64 "${WEBUI_ADMIN_EMAIL}")" \
  "WEBUI_ADMIN_PASSWORD" "$(b64 "${WEBUI_ADMIN_PASSWORD}")" > "${TMP_MANIFEST}"
oc apply --server-side -f "${TMP_MANIFEST}" >/dev/null
rm -f "${TMP_MANIFEST}"

echo "Secrets applied in namespace ${NAMESPACE} (server-side apply):"
echo "  - ${CONNECTIONS_SECRET} (keys: GUARDRAILS_API_KEY, PIPELINES_API_KEY)"
echo "  - ${ADMIN_SECRET} (keys: WEBUI_ADMIN_EMAIL, WEBUI_ADMIN_PASSWORD)"
echo
echo "NOTE: the GUARDRAILS_API_KEY placeholder is explicitly unused and is NOT"
echo "real protection (guardrails-proxy does not authenticate)."
echo
echo "DEPLOY GATE: install/openwebui.yaml startup-config changes"
echo "(ENABLE_PERSISTENT_CONFIG=false, ENABLE_SIGNUP=false, plural connections)"
echo "must NOT be synced until the owner-admin sanitized inventory of stored"
echo "effective config is recorded (docs/headless-openwebui.md). The Pipelines"
echo "key provisioned here must match the Pipelines manifest patched in Task 3;"
echo "do not deploy in between."
