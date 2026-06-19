#!/usr/bin/env bash
#
# NOKVO production deploy — stands up the platform, loads secrets, builds the
# image, and deploys the Container App + migration job. Idempotent; safe to
# re-run (it re-tags a new image and rolls a new revision).
#
# Prereqs: az login + correct subscription selected; Docker NOT required (we use
# `az acr build`, which builds in the cloud and is always linux/amd64).
#
# Required env:
#   PG_ADMIN_PASSWORD   Postgres admin password (strong)
#   API_DOMAIN          e.g. api.nokvo.com   (custom domain for the API)
#   PORTAL_DOMAIN       e.g. portal.nokvo.com
#   RP_ID               registrable domain for WebAuthn, e.g. nokvo.com
# Optional env (defaults shown):
#   RG=rg-nokvo-prod  LOCATION=centralindia  SWA_LOCATION=eastasia
#   NAME_PREFIX=nokvo  ENV_FILE=../.env.prod  CONCURRENT_REQUESTS=10
#   IMAGE_TAG=<git short sha>
set -euo pipefail

RG="${RG:-rg-nokvo-prod}"
LOCATION="${LOCATION:-centralindia}"
SWA_LOCATION="${SWA_LOCATION:-eastasia}"
NAME_PREFIX="${NAME_PREFIX:-nokvo}"
CONCURRENT_REQUESTS="${CONCURRENT_REQUESTS:-10}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-$HERE/../.env.prod}"
PY="${PY:-python3}"
IMAGE_TAG="${IMAGE_TAG:-$(git -C "$HERE/.." rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M)}"

: "${PG_ADMIN_PASSWORD:?set PG_ADMIN_PASSWORD}"
# API_DOMAIN is optional — defaults to the Azure Container App FQDN
# (nokvo-api.<env-default-domain>) once the platform is deployed (see below).
: "${PORTAL_DOMAIN:?set PORTAL_DOMAIN, e.g. portal.nokvo.com}"
: "${RP_ID:?set RP_ID, e.g. nokvo.com}"
[ -f "$ENV_FILE" ] || { echo "ENV_FILE not found: $ENV_FILE" >&2; exit 1; }

az extension add --name containerapp --upgrade --only-show-errors >/dev/null 2>&1 || true

echo "==> [1/8] Resource group $RG ($LOCATION)"
az group create -n "$RG" -l "$LOCATION" -o none

echo "==> [2/8] Platform (Bicep)"
az deployment group create -g "$RG" -n platform -f "$HERE/platform.bicep" \
  -p location="$LOCATION" swaLocation="$SWA_LOCATION" namePrefix="$NAME_PREFIX" \
     pgAdminPassword="$PG_ADMIN_PASSWORD" -o none

get() { az deployment group show -g "$RG" -n platform --query "properties.outputs.$1.value" -o tsv; }
ACR_NAME="$(get acrName)";              ACR_LOGIN="$(get acrLoginServer)"
MENV_ID="$(get managedEnvironmentId)";  MI_ID="$(get identityId)"
MI_CLIENT="$(get identityClientId)";    KV_NAME="$(get keyVaultName)"
KV_URI="$(get keyVaultUri)";            PG_FQDN="$(get postgresFqdn)"
REDIS_HOST="$(get redisHostName)";      REDIS_PORT="$(get redisPort)"
STORAGE="$(get storageAccountName)";    AI_CONN="$(get appInsightsConnectionString)"
SWA_NAME="$(get staticWebAppName)"
ENV_DEFAULT_DOMAIN="$(get managedEnvironmentDefaultDomain)"
# Backend stays on the Azure-generated FQDN unless API_DOMAIN was supplied.
API_DOMAIN="${API_DOMAIN:-nokvo-api.${ENV_DEFAULT_DOMAIN}}"
echo "    API will be served at: https://${API_DOMAIN}"

echo "==> [3/8] Grant deployer Key Vault Secrets Officer (to load secrets)"
ME="$(az ad signed-in-user show --query id -o tsv)"
KV_ID="$(az keyvault show -n "$KV_NAME" -g "$RG" --query id -o tsv)"
az role assignment create --assignee-object-id "$ME" --assignee-principal-type User \
  --role "Key Vault Secrets Officer" --scope "$KV_ID" -o none || true
echo "    waiting for RBAC propagation..."; sleep 25

echo "==> [4/8] Load secrets into Key Vault from $ENV_FILE"
# Secret VALUES come from the env file; classification lives in gen_app_params.py.
KV_NAME="$KV_NAME" bash <("$PY" "$HERE/gen_app_params.py" --mode kv-script --env-file "$ENV_FILE")
# REDIS_URL carries the access key — assemble it from the platform output.
# Azure Managed Redis: key via `redisenterprise database list-keys`; single DB
# (no /0 selector); TLS on port 10000.
REDIS_RESOURCE="${REDIS_HOST%%.*}"
REDIS_KEY="$(az redisenterprise database list-keys --cluster-name "$REDIS_RESOURCE" -g "$RG" --query primaryKey -o tsv)"
az keyvault secret set --vault-name "$KV_NAME" --name redis-url \
  --value "rediss://:${REDIS_KEY}@${REDIS_HOST}:${REDIS_PORT}" --output none

echo "==> [5/8] Build & push image (cloud build → linux/amd64): nokvo-api:${IMAGE_TAG}"
az acr build --registry "$ACR_NAME" --image "nokvo-api:${IMAGE_TAG}" \
  --platform linux/amd64 "$HERE/.." -o none
IMAGE="${ACR_LOGIN}/nokvo-api:${IMAGE_TAG}"

echo "==> [6/8] Generate app params"
"$PY" "$HERE/gen_app_params.py" --mode params --env-file "$ENV_FILE" \
  --image "$IMAGE" --identity-id "$MI_ID" --identity-client-id "$MI_CLIENT" \
  --managed-env-id "$MENV_ID" --acr-login-server "$ACR_LOGIN" \
  --kv-uri "$KV_URI" --kv-name "$KV_NAME" --pg-fqdn "$PG_FQDN" \
  --storage-account "$STORAGE" --appinsights-conn "$AI_CONN" \
  --api-base "https://${API_DOMAIN}" --portal-base "https://${PORTAL_DOMAIN}" \
  --rp-id "$RP_ID" --concurrent-requests "$CONCURRENT_REQUESTS" \
  --out "$HERE/app.params.json"

echo "==> [7/8] Deploy Container App + migration job"
az deployment group create -g "$RG" -n app -f "$HERE/app.bicep" \
  -p @"$HERE/app.params.json" -p location="$LOCATION" namePrefix="$NAME_PREFIX" -o none

echo "==> [8/8] Run database migrations (Container Apps job)"
az containerapp job start -n "${NAME_PREFIX}-migrate" -g "$RG" -o none

API_FQDN="$(az deployment group show -g "$RG" -n app --query properties.outputs.apiFqdn.value -o tsv)"
cat <<EOF

==> Done.
   API (default):   https://${API_FQDN}
   Static Web App:  ${SWA_NAME}  (deploy the Vue build separately — see README)

Next (manual, see README.md):
  - Bind custom domains: ${API_DOMAIN} -> Container App, ${PORTAL_DOMAIN} -> SWA
  - DNS + managed certs; then re-run with the real domains already set
  - Point Plivo / Razorpay / Meta webhooks at https://${API_DOMAIN}/...
  - Decommission the old 'nokvotest' Postgres + Qdrant Cloud cluster
EOF
