#!/usr/bin/env bash
#
# Configure the GitHub Actions deploy pipeline (.github/workflows/deploy.yml) in
# one shot — sets every required repo SECRET and VARIABLE, pulling the values
# live from Azure so nothing is hand-typed or guessed.
#
# This is the fix for the "Azure login: client-id and tenant-id not present" /
# missing-variable failures: the workflow needs both GitHub *secrets* and GitHub
# *variables* (two different tabs), and a federated credential on the deploy SP.
#
# Prerequisites (run these yourself first):
#   1. az login                  # with access to rg-nokvo-prod
#   2. gh auth login             # GitHub CLI, as a repo admin of NiharPy/ProjectNokvo
#   (The federated credential `github-projectnokvo-main` on the SP is already created.)
#
# Then just run:  bash infra/setup-github-ci.sh
#
set -euo pipefail

REPO="NiharPy/ProjectNokvo"
RG="rg-nokvo-prod"

# Deploy service principal (nokvo-platform-sp) — has Contributor on the subscription
# and the federated credential trusting this repo's main branch.
CLIENT_ID="24677562-096c-4f8a-94a4-55460fa1a530"

echo "→ Reading values from Azure..."
SUB="$(az account show --query id -o tsv)"
TENANT="$(az account show --query tenantId -o tsv)"
SWA_TOKEN="$(az staticwebapp secrets list -n nokvo-portal -g "$RG" --query properties.apiKey -o tsv)"
API_FQDN="$(az containerapp show -n nokvo-api -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)"

echo "→ Setting GitHub SECRETS on $REPO ..."
gh secret set AZURE_CLIENT_ID                 --repo "$REPO" --body "$CLIENT_ID"
gh secret set AZURE_TENANT_ID                 --repo "$REPO" --body "$TENANT"
gh secret set AZURE_SUBSCRIPTION_ID           --repo "$REPO" --body "$SUB"
gh secret set AZURE_STATIC_WEB_APPS_API_TOKEN --repo "$REPO" --body "$SWA_TOKEN"

echo "→ Setting GitHub VARIABLES on $REPO ..."
gh variable set RESOURCE_GROUP --repo "$REPO" --body "$RG"
gh variable set ACR_NAME       --repo "$REPO" --body "nokvoacr7x5lvalgoibuo"
gh variable set API_APP        --repo "$REPO" --body "nokvo-api"
gh variable set MIGRATE_JOB    --repo "$REPO" --body "nokvo-migrate"
gh variable set API_BASE_URL   --repo "$REPO" --body "https://${API_FQDN}"

echo
echo "✓ Done. Trigger a deploy to verify:"
echo "    gh workflow run \"Deploy to Azure\" --repo $REPO --ref main"
echo "    gh run watch --repo $REPO"
