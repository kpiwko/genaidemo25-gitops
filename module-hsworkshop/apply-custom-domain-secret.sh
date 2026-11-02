#!/usr/bin/env bash
# Provision the Cloudflare DNS-01 API token as Kubernetes Secret cloudflare-dns01
# in namespace hsworkshop on the hsworkshop cluster.
#
# SECURITY:
#   - Context AND API server are validated BEFORE the token is read.
#   - The token is read interactively (hidden, read -s) or from a file/stdin.
#     It is NEVER passed as a CLI argument (no shell history / process list).
#   - The token is NEVER echoed, logged, or written anywhere except the
#     temporary manifest file (mktemp, 0600, deleted immediately after apply).
#   - Server-side apply is used, so no client-side last-applied-configuration
#     annotation (which would embed the token in the live object) is created.
#   - The script only creates/updates Secret cloudflare-dns01 in namespace
#     hsworkshop. It does not read, modify, or print any other secret or any
#     pre-existing secret file belonging to the hsworkshop application stack.
#   - It never restarts shared controllers (cert-manager watches Secrets and
#     reconciles on its own) and never switches the global kubeconfig context.
#
# Prerequisite token (create in the Cloudflare dashboard, one token):
#   User profile -> API Tokens -> Create Token
#     Permissions:      Zone - DNS - Edit
#                       Zone - Zone - Read
#     Zone Resources:   Include - Specific zone - fous.ai
#   fous.ai is the authoritative Cloudflare zone (verified: NS peaches/amos.ns.cloudflare.com,
#   amos.ns.cloudflare.com; hsworkshop.fous.ai is NOT a delegated zone, so the token
#   must be scoped to the fous.ai zone, not to a hsworkshop.fous.ai zone).
#
# Idempotent: re-running replaces the token value in the Secret. To rotate the
# token: create a new token in Cloudflare, re-run this script.

set -euo pipefail

EXPECTED_CONTEXT="hsworkshop/api-kpicl2-pool-rft8p-aws-rh-ods-com:6443/kpiwko"
EXPECTED_API="https://api.kpicl2-pool-rft8p.aws.rh-ods.com:6443"
NAMESPACE="hsworkshop"
SECRET_NAME="cloudflare-dns01"
KEY="api-token"

die() { echo "ERROR: $*" >&2; exit 1; }

command -v oc >/dev/null 2>&1 || die "oc CLI is required"

# --- Context guard (BEFORE any token is read) -----------------------------------
CURRENT_CONTEXT="$(oc config current-context 2>/dev/null || true)"
CURRENT_SERVER="$(oc whoami --show-server 2>/dev/null || true)"
if [[ "${CURRENT_CONTEXT}" != "${EXPECTED_CONTEXT}" ]]; then
  die "refusing to run: current context is '${CURRENT_CONTEXT}', expected '${EXPECTED_CONTEXT}'.
       Switch with: oc config use-context ${EXPECTED_CONTEXT}"
fi
if [[ "${CURRENT_SERVER}" != "${EXPECTED_API}" ]]; then
  die "refusing to run: current server is '${CURRENT_SERVER}', expected '${EXPECTED_API}'"
fi

# --- Read the token without echoing it ------------------------------------------
TOKEN=""
TOKEN_SOURCE=""
if [[ -n "${CLOUDFLARE_TOKEN_FILE:-}" ]]; then
  # Explicit file input. A missing file is an error, never a silent prompt.
  [[ -f "${CLOUDFLARE_TOKEN_FILE}" ]] \
    || die "CLOUDFLARE_TOKEN_FILE='${CLOUDFLARE_TOKEN_FILE}' does not exist or is not a regular file"
  TOKEN_SOURCE="file:${CLOUDFLARE_TOKEN_FILE}"
  IFS= read -r TOKEN < "${CLOUDFLARE_TOKEN_FILE}" || true
elif [[ ! -t 0 ]]; then
  # Non-interactive stdin (e.g. pipe): read one line.
  TOKEN_SOURCE="stdin"
  IFS= read -r TOKEN || true
else
  # Interactive: hidden prompt, confirmation against typos.
  TOKEN_SOURCE="prompt"
  read -r -s -p "Paste the Cloudflare API token (input hidden): " TOKEN
  echo
  read -r -s -p "Re-enter the token to confirm: " TOKEN_CONFIRM
  echo
  [[ "${TOKEN}" == "${TOKEN_CONFIRM}" ]] || die "tokens do not match; nothing was applied"
fi

# Reject whitespace/malformed input instead of applying something unusable.
if printf '%s' "${TOKEN}" | grep -q '[[:space:]]'; then
  die "token contains whitespace (read from ${TOKEN_SOURCE}); refusing to apply a malformed token"
fi
[[ -n "${TOKEN}" ]] || die "empty token (read from ${TOKEN_SOURCE}); nothing was applied"
[[ "${#TOKEN}" -ge 20 ]] || die "token looks too short for a Cloudflare API token (read from ${TOKEN_SOURCE}); nothing was applied"
[[ "${TOKEN}" =~ ^[A-Za-z0-9_-]+$ ]] || die "token contains characters outside [A-Za-z0-9_-] (read from ${TOKEN_SOURCE}); refusing to apply"

# --- Apply (token never in argv; server-side apply; temp file deleted) ----------
# base64 without line wrapping: GNU base64 wraps at 76 columns by default,
# which would produce invalid YAML. Normalize explicitly on both GNU and BSD.
B64="$(printf '%s' "${TOKEN}" | base64 | tr -d '\n')"
[[ -n "${B64}" ]] || die "failed to encode token"

TMP_MANIFEST="$(mktemp)"   # 0600 by default on macOS/Linux mktemp
trap 'rm -f "${TMP_MANIFEST}"' EXIT
printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: %s\n  namespace: %s\ntype: Opaque\ndata:\n  %s: %s\n' \
  "${SECRET_NAME}" "${NAMESPACE}" "${KEY}" "${B64}" > "${TMP_MANIFEST}"
oc apply --server-side -f "${TMP_MANIFEST}" >/dev/null
rm -f "${TMP_MANIFEST}"

echo "Secret ${SECRET_NAME} applied in namespace ${NAMESPACE} (key: ${KEY}, server-side apply)."
echo
echo "Next steps (see docs/custom-domain.md):"
echo "  1. Cloudflare DNS: CNAME chat.hsworkshop.fous.ai -> router-default.apps.kpicl2-pool-rft8p.aws.rh-ods.com (DNS only, NOT proxied)"
echo "  2. Render the overlay:  cd module-hsworkshop/domain && ./render.sh"
echo "  3. Phase 1 — certificate-only pre-provision, then production Certificate Ready:"
echo "       oc apply -k module-hsworkshop/overlays/custom-domain/certificate-only -n hsworkshop"
echo "       oc get certificate openwebui-custom-cert -n hsworkshop -w"
echo "  4. Phase 2 — commit/push the production render, THEN activate the Argo CD app"
echo "     (sources[0].path JSON patch). Full procedure: docs/custom-domain.md#activation"
