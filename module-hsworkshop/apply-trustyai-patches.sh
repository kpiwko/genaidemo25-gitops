#!/usr/bin/env bash
# Re-apply TrustyAI ConfigMap patches after operator reconcile.
#
# The TrustyAI operator regenerates guardrails-orchestrator-auto-config and
# guardrails-orchestrator-gateway-auto-config on reconcile, reverting:
#   1. The backend port fix (headless service needs pod port 8080, not svc port 80)
#   2. The swear-word regex (operator leaves $^ placeholder — matches nothing)
#
# Run this script any time the orchestrator reconciles. Triggers include:
#   - GuardrailsOrchestrator CR changes
#   - Services with label trustyai/guardrails-groupA appearing or disappearing
#
# Usage: ./apply-trustyai-patches.sh [namespace]
#   namespace defaults to "hsworkshop"

set -euo pipefail

NS="${1:-hsworkshop}"
PREDICTOR="eurollm-22b-service-predictor.${NS}.svc.cluster.local"

echo "Applying TrustyAI patches to namespace: ${NS}"

# ---------------------------------------------------------------------------
# Patch 1: guardrails-orchestrator-auto-config
# Fix backend port: headless Kubernetes service (ClusterIP: None) does NOT
# translate ports via kube-proxy. The pod listens on 8080; the auto-generated
# config uses service port 80 → connection refused.
# ---------------------------------------------------------------------------
echo "Patch 1: fixing backend port in guardrails-orchestrator-auto-config..."

oc patch configmap guardrails-orchestrator-auto-config -n "${NS}" --type merge -p "$(cat <<'PATCH'
{
  "data": {
    "config.yaml": "openai:\n  service:\n    hostname: PREDICTOR_PLACEHOLDER\n    port: 8080\ndetectors:\n  built-in-detector:\n    type: text_contents\n    service:\n      hostname: 127.0.0.1\n      port: 8080\n    chunker_id: whole_doc_chunker\n    default_threshold: 0.5\npassthrough_headers:\n  - Authorization\n  - Content-Type\n"
  }
}
PATCH
)"

# Replace placeholder with actual predictor hostname
oc patch configmap guardrails-orchestrator-auto-config -n "${NS}" \
  --type=json \
  -p "[{\"op\":\"replace\",\"path\":\"/data/config.yaml\",\"value\":\"openai:\n  service:\n    hostname: ${PREDICTOR}\n    port: 8080\ndetectors:\n  built-in-detector:\n    type: text_contents\n    service:\n      hostname: 127.0.0.1\n      port: 8080\n    chunker_id: whole_doc_chunker\n    default_threshold: 0.5\npassthrough_headers:\n  - Authorization\n  - Content-Type\n\"}]"

echo "  Done."

# ---------------------------------------------------------------------------
# Patch 2: guardrails-orchestrator-gateway-auto-config
# Replace the $^ (unmatchable) placeholder regex with actual swear-word list.
# IMPORTANT: Use single-quoted YAML for the regex string — \b in a YAML
# double-quoted string becomes a backspace (0x08) control character, causing
# the Rust YAML parser in the orchestrator to panic.
# ---------------------------------------------------------------------------
echo "Patch 2: applying swear-word regex to guardrails-orchestrator-gateway-auto-config..."

# Write the full gateway config to a temp file to avoid shell quoting issues
TMPFILE=$(mktemp /tmp/trustyai-gateway-config.XXXXXX.yaml)
trap 'rm -f "${TMPFILE}"' EXIT

cat > "${TMPFILE}" <<'GATEWAY_CONFIG'
detectors:
  - name: built-in-detector
    input: true
    output: false
    detector_params:
      regex:
        - '(?i)\b(fuck|fucking|fucker|fucked|motherfucker|motherfucking|clusterfuck|cunt|cunts|asshole|arsehole|cocksucker|dickhead|twat|wanker|bastard)\b'
        - '(?i)\b(kurv|zkurv|píč|pič|pic|čurák|curak|kund|mrdat|mrdka|jebat|jebe|jebeš|jebem|vyjebat|pojebat|ojebat|kokot|děvk|devk|šlapk|slapk|coura|couřit|posraný|posraná|posrat)'
routes:
  - name: all
    detectors:
      - built-in-detector
  - name: passthrough
    detectors: []
GATEWAY_CONFIG

# Apply as a JSON patch using the file content
GATEWAY_YAML=$(cat "${TMPFILE}")
oc patch configmap guardrails-orchestrator-gateway-auto-config -n "${NS}" \
  --type=merge \
  --patch "{\"data\":{\"config.yaml\":$(python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" < "${TMPFILE}")}}"

echo "  Done."

# ---------------------------------------------------------------------------
# Restart orchestrator to pick up the patched ConfigMaps
# ---------------------------------------------------------------------------
echo "Restarting guardrails-orchestrator deployment..."
oc rollout restart deployment/guardrails-orchestrator -n "${NS}"
oc rollout status deployment/guardrails-orchestrator -n "${NS}" --timeout=120s

echo ""
echo "All patches applied successfully."
echo "Verify with: oc logs -n ${NS} deployment/guardrails-orchestrator --tail=20"
