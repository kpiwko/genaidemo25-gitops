#!/usr/bin/env bash
# Render the custom-domain Kustomize overlay deterministically from non-secret config.
#
# Usage:
#   WORKSHOP_DOMAIN=hsworkshop.fous.ai ACME_EMAIL=you@example.com ./render.sh [output-dir]
#
# Configuration precedence (highest first):
#   1. Explicit environment variables (WORKSHOP_DOMAIN, ACME_ENV, ACME_EMAIL,
#      NAMESPACE, ROUTER_DNS_TARGET)
#   2. Key=value config file (CONFIG_FILE, default: config.env next to this
#      script) -- parsed, never sourced: only the five keys above are read as
#      plain string values, so the file can neither execute code nor inject
#      content into the render.
#   3. Built-in defaults (ACME_ENV=staging, NAMESPACE=hsworkshop,
#      ROUTER_DNS_TARGET=<cluster router>)
#
# Never touches secrets: the only secret in the final deployment is the
# Cloudflare API token, applied separately by apply-custom-domain-secret.sh
# directly on the cluster.
#
# Output is deterministic: same inputs -> byte-identical files.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

die() { echo "ERROR: $*" >&2; exit 1; }

# --- Inputs --------------------------------------------------------------------
WORKSHOP_DOMAIN="${WORKSHOP_DOMAIN:-}"
ACME_ENV="${ACME_ENV:-}"
ACME_EMAIL="${ACME_EMAIL:-}"
NAMESPACE="${NAMESPACE:-}"
ROUTER_DNS_TARGET="${ROUTER_DNS_TARGET:-}"

CONFIG_FILE="${CONFIG_FILE:-${SCRIPT_DIR}/config.env}"
if [[ -f "${CONFIG_FILE}" ]]; then
  # Parse (never source) the config file: whitelisted keys only, plain values
  # only. A variable that is already set explicitly in the environment always
  # wins over the config file.
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%$'\r'}"
    if [[ "${line}" =~ ^[[:space:]]*(#|$) ]]; then
      continue
    fi
    if [[ "${line}" != *=* ]]; then
      continue
    fi
    key="${line%%=*}"
    val="${line#*=}"
    # strip one pair of surrounding quotes, if present
    if [[ "${#val}" -ge 2 ]]; then
      case "${val}" in
        \"*\") val="${val#\"}"; val="${val%\"}" ;;
        \'*\') val="${val#\'}"; val="${val%\'}" ;;
      esac
    fi
    case "${key}" in
      WORKSHOP_DOMAIN|ACME_ENV|ACME_EMAIL|NAMESPACE|ROUTER_DNS_TARGET)
        # explicit environment wins: only fill variables that are still empty
        if [[ -z "${!key:-}" ]]; then
          printf -v "${key}" '%s' "${val}"
        fi
        ;;
      *)
        # unknown keys are ignored; nothing from the file is ever executed
        ;;
    esac
  done < "${CONFIG_FILE}"
fi

# Built-in defaults (lowest precedence).
: "${ACME_ENV:=staging}"
: "${NAMESPACE:=hsworkshop}"
: "${ROUTER_DNS_TARGET:=router-default.apps.kpicl2-pool-rft8p.aws.rh-ods.com}"

OUT_DIR="${1:-${SCRIPT_DIR}/../overlays/custom-domain}"

# --- Validation ----------------------------------------------------------------
[[ -n "${WORKSHOP_DOMAIN}" ]] || die "WORKSHOP_DOMAIN is required (e.g. hsworkshop.fous.ai)"
[[ -n "${ACME_EMAIL}" ]] || die "ACME_EMAIL is required (e.g. you@example.com)"
case "${ACME_ENV}" in
  staging|production) ;;
  *) die "ACME_ENV must be 'staging' or 'production' (got '${ACME_ENV}')" ;;
esac

DNS_NAME_RE='^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$'
if ! printf '%s' "${WORKSHOP_DOMAIN}" | grep -Eq "${DNS_NAME_RE}"; then
  die "invalid WORKSHOP_DOMAIN '${WORKSHOP_DOMAIN}' (must be a lowercase DNS name, e.g. hsworkshop.fous.ai)"
fi
# The renderer derives the chat host as chat.<WORKSHOP_DOMAIN>; a domain that
# already carries the chat prefix would produce chat.chat.<domain>.
if [[ "${WORKSHOP_DOMAIN}" == chat.* ]]; then
  die "invalid WORKSHOP_DOMAIN '${WORKSHOP_DOMAIN}': do not include the 'chat.' prefix, the renderer adds it (host becomes chat.<WORKSHOP_DOMAIN>)"
fi
if ! printf '%s' "${ROUTER_DNS_TARGET}" | grep -Eq "${DNS_NAME_RE}"; then
  die "invalid ROUTER_DNS_TARGET '${ROUTER_DNS_TARGET}' (must be a lowercase DNS name, e.g. router-default.apps.kpicl2-pool-rft8p.aws.rh-ods.com)"
fi
# Restricted charset: also keeps the value safe as a sed replacement below.
if ! printf '%s' "${ACME_EMAIL}" | grep -Eq '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$'; then
  die "invalid ACME_EMAIL '${ACME_EMAIL}'"
fi
if [[ ! "${NAMESPACE}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
  die "invalid NAMESPACE '${NAMESPACE}'"
fi
# Belt and braces for the sed substitutions below (delimiter is '|'): none of
# the substituted values may contain sed metacharacters.
for v in "${WORKSHOP_DOMAIN}" "${ROUTER_DNS_TARGET}" "${ACME_EMAIL}" "${NAMESPACE}"; do
  if printf '%s' "${v}" | grep -q '[|&\\]'; then
    die "input value contains a forbidden sed metacharacter (|, & or backslash)"
  fi
done

CHAT_HOST="chat.${WORKSHOP_DOMAIN}"
CERT_SECRET_NAME="openwebui-custom-tls"
ROUTE_NAME="openwebui-custom"
ISSUER_NAME="letsencrypt-${ACME_ENV}-dns01"

case "${ACME_ENV}" in
  staging)    ACME_SERVER="https://acme-staging-v02.api.letsencrypt.org/directory" ;;
  production) ACME_SERVER="https://acme-v02.api.letsencrypt.org/directory" ;;
esac

# --- Render --------------------------------------------------------------------
# Use a temp dir + atomic move so a failed render never leaves partial output.
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

mkdir -p "${TMP_DIR}"
render() {
  local src="$1" dst="$2"
  mkdir -p "${TMP_DIR}/$(dirname "${dst}")"
  sed \
    -e "s|__WORKSHOP_DOMAIN__|${WORKSHOP_DOMAIN}|g" \
    -e "s|__CHAT_HOST__|${CHAT_HOST}|g" \
    -e "s|__NAMESPACE__|${NAMESPACE}|g" \
    -e "s|__ACME_EMAIL__|${ACME_EMAIL}|g" \
    -e "s|__ACME_SERVER__|${ACME_SERVER}|g" \
    -e "s|__ISSUER_NAME__|${ISSUER_NAME}|g" \
    -e "s|__CERT_SECRET_NAME__|${CERT_SECRET_NAME}|g" \
    -e "s|__ROUTE_NAME__|${ROUTE_NAME}|g" \
    -e "s|__ROUTER_DNS_TARGET__|${ROUTER_DNS_TARGET}|g" \
    "${src}" > "${TMP_DIR}/${dst}"
}

render "${SCRIPT_DIR}/templates/kustomization.yaml" "kustomization.yaml"
render "${SCRIPT_DIR}/templates/certificate-only.yaml" "certificate-only/kustomization.yaml"
render "${SCRIPT_DIR}/templates/certificate-issuer.yaml" "certificate-issuer.yaml"
render "${SCRIPT_DIR}/templates/certificate-issuer.yaml" "certificate-only/certificate-issuer.yaml"
render "${SCRIPT_DIR}/templates/route-custom.yaml" "route-custom.yaml"
render "${SCRIPT_DIR}/templates/rbac-router.yaml" "rbac-router.yaml"
render "${SCRIPT_DIR}/templates/openwebui-patch.yaml" "openwebui-patch.yaml"

# Fail loudly if any placeholder survived rendering.
if grep -R -n '__[A-Z_]\+__' "${TMP_DIR}" >/dev/null 2>&1; then
  grep -R -n '__[A-Z_]\+__' "${TMP_DIR}" >&2
  die "unrendered placeholders remain"
fi

mkdir -p "${OUT_DIR}"
for f in kustomization.yaml certificate-only/kustomization.yaml certificate-issuer.yaml certificate-only/certificate-issuer.yaml route-custom.yaml rbac-router.yaml openwebui-patch.yaml; do
  mkdir -p "${OUT_DIR}/$(dirname "${f}")"
  cat "${TMP_DIR}/${f}" > "${OUT_DIR}/${f}"
done

cat <<EOF
Rendered custom-domain overlay into ${OUT_DIR}
  WORKSHOP_DOMAIN:     ${WORKSHOP_DOMAIN}
  Chat host (SAN):     ${CHAT_HOST}
  ACME environment:    ${ACME_ENV} (${ACME_SERVER})
  Certificate Secret:  ${CERT_SECRET_NAME} (referenced by Route ${ROUTE_NAME} via spec.tls.externalCertificate)
  Router DNS target:   ${ROUTER_DNS_TARGET} (Cloudflare DNS-only CNAME target)
EOF
