#!/usr/bin/env bash
# Generate, display, and apply hsworkshop secrets.
# 1. Run this script
# 2. Copy the printed values into your password manager
# 3. The secrets are applied to the cluster in the same run

set -euo pipefail

# Use hex for DB passwords to avoid URL-unsafe chars (/, +, =) in DATABASE_URL
POSTGRES_PASS=$(openssl rand -hex 24)
CH_PASS=$(openssl rand -hex 24)
NEXTAUTH_SECRET=$(openssl rand -base64 32)
SALT=$(openssl rand -base64 32)
ENCRYPTION_KEY=$(openssl rand -hex 32)
WEBUI_SECRET_KEY=$(openssl rand -base64 32)
MINIO_ROOT_USER="langfuse"
MINIO_ROOT_PASSWORD=$(openssl rand -hex 24)

echo "========================================="
echo "  Save these in your password manager!   "
echo "========================================="
echo "POSTGRES_PASS:       $POSTGRES_PASS"
echo "CH_PASS:             $CH_PASS"
echo "NEXTAUTH_SECRET:     $NEXTAUTH_SECRET"
echo "SALT:                $SALT"
echo "ENCRYPTION_KEY:      $ENCRYPTION_KEY"
echo "WEBUI_SECRET_KEY:    $WEBUI_SECRET_KEY"
echo "MINIO_ROOT_USER:     $MINIO_ROOT_USER"
echo "MINIO_ROOT_PASSWORD: $MINIO_ROOT_PASSWORD"
echo "========================================="
read -r -p "Press Enter once saved to continue applying secrets..."

oc create namespace hsworkshop --dry-run=client -o yaml | oc apply -f -
oc label namespace hsworkshop argocd.argoproj.io/managed-by=openshift-gitops --overwrite

oc create secret generic postgres-secret -n hsworkshop \
  --from-literal=POSTGRES_DB=langfuse \
  --from-literal=POSTGRES_USER=langfuse \
  --from-literal=POSTGRES_PASSWORD="$POSTGRES_PASS"

oc create secret generic clickhouse-secret -n hsworkshop \
  --from-literal=CLICKHOUSE_USER=clickhouse \
  --from-literal=CLICKHOUSE_PASSWORD="$CH_PASS"

oc create secret generic langfuse-secret -n hsworkshop \
  --from-literal=NEXTAUTH_SECRET="$NEXTAUTH_SECRET" \
  --from-literal=SALT="$SALT" \
  --from-literal=ENCRYPTION_KEY="$ENCRYPTION_KEY" \
  --from-literal=DATABASE_URL="postgresql://langfuse:${POSTGRES_PASS}@postgres:5432/langfuse" \
  --from-literal=CLICKHOUSE_URL="http://clickhouse:8123" \
  --from-literal=CLICKHOUSE_MIGRATION_URL="clickhouse://clickhouse:9000" \
  --from-literal=CLICKHOUSE_USER="clickhouse" \
  --from-literal=CLICKHOUSE_PASSWORD="$CH_PASS" \
  --from-literal=REDIS_HOST="redis" \
  --from-literal=REDIS_PORT="6379"

oc create secret generic minio-secret -n hsworkshop \
  --from-literal=MINIO_ROOT_USER="$MINIO_ROOT_USER" \
  --from-literal=MINIO_ROOT_PASSWORD="$MINIO_ROOT_PASSWORD"

oc create secret generic openwebui-secret -n hsworkshop \
  --from-literal=WEBUI_SECRET_KEY="$WEBUI_SECRET_KEY" \
  --from-literal=LANGFUSE_PUBLIC_KEY="pk-lf-placeholder" \
  --from-literal=LANGFUSE_SECRET_KEY="sk-lf-placeholder"

echo "Done - all secrets applied to hsworkshop namespace."
