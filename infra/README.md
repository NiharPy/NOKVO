# NOKVO — Azure deployment (infra/)

Cost-optimized Azure Container Apps deployment of the NOKVO voice API + Vue
portal. Target: **100 concurrent calls, 10 per tenant (enforced)**.

| File | Purpose |
|------|---------|
| `platform.bicep` | Platform: ACR, Log Analytics + App Insights, Postgres (Burstable), Redis (Standard C1), Storage, Key Vault, user-assigned identity + role assignments, Container Apps env, Static Web App. |
| `app.bicep` | The Container App (API) + the `alembic upgrade head` migration job. |
| `gen_app_params.py` | Turns `.env.prod` into Container Apps env vars + Key Vault secret refs (one place owns the secret-vs-plain split). |
| `deploy.sh` | One-shot orchestration: platform → secrets → image → app → migrate. |
| `.env.prod.example` | Annotated prod config template. |

## Architecture (cost posture)

- **Container Apps, Consumption only** (no Dedicated reservation). 1 uvicorn
  worker/replica; scale **out** on HTTP concurrency. `minReplicas` ≥ 2 in
  business hours (never scale-to-zero — cold start drops inbound calls).
- **Postgres Burstable B2ms, single-zone**; **Redis Standard C1**. Both off the
  per-turn hot path (Redis is the hot path, sized with headroom). Alert-and-upsize.
- **ACR Basic**, **Static Web Apps Free**, **India region** (latency + no
  cross-region egress). Log Analytics **daily cap** + **OTEL sampling**.
- Data tier is **public + firewall-locked** (no VNet injection), per decision.

## Prerequisites

```bash
az login
az account set --subscription <your-subscription-id>
# Build a prod env file from the template and fill in every value:
cp infra/.env.prod.example .env.prod    # then edit; DO NOT commit
```

## Deploy

```bash
cd infra
export PG_ADMIN_PASSWORD='<strong-password>'
export API_DOMAIN='api.nokvo.com'
export PORTAL_DOMAIN='portal.nokvo.com'
export RP_ID='nokvo.com'
./deploy.sh
```

`deploy.sh` is idempotent. Re-running builds a new image tag and rolls a new
revision. The migration job runs `alembic upgrade head` against the prod DB.

## After the first deploy (manual, one-time)

1. **Custom domains + managed TLS**
   - API: `az containerapp hostname add` + `bind` for `api.<domain>` on the
     `nokvo-api` app; add the asuid + CNAME DNS records it prints.
   - Portal: add `portal.<domain>` to the Static Web App; add the DNS records.
   - Re-run `deploy.sh` (or just redeploy `app.bicep`) so the env's public-URL
     vars match the real domains.
2. **Frontend** → Static Web App:
   ```bash
   cd frontend
   VITE_API_BASE_URL=https://api.<domain> npm ci && npm run build
   az staticwebapp deploy -n nokvo-portal -g rg-nokvo-prod \
     --source dist --env production
   ```
3. **External webhooks** → `https://api.<domain>`:
   - Plivo: re-point each tenant Application's answer_url + media WSS (set
     `PLIVO_WEBHOOK_AUTOSYNC=true` once, or use the manual resync path).
   - Razorpay: `/api/nokvo-one/payments/webhook`.
   - Meta leadgen: `/api/nokvo-one/agents/lead-sources/meta/webhook`.
4. **Decommission** the old `nokvotest` Postgres and the Qdrant Cloud cluster
   (KB is retired; prod sets `QDRANT_URL=:memory:`).

## RBAC note — runtime tenant auto-provisioning

The app's managed identity gets **narrow** data-plane roles only (AcrPull, Key
Vault Secrets User, Storage Blob Data Contributor). The runtime
`provision_resource_group` step (`app/services/azure_resource_group_service.py`)
does `create_or_update` on a **per-tenant resource group**, which requires
*subscription-level* `resourceGroups/write`. The slimmed provisioning flow
(shared Storage/KV/Redis) does not otherwise use that RG, so the default is to
**leave the identity narrow** and not exercise per-tenant RG creation.

If you onboard new tenants through the app's auto-provisioning and want that
step to succeed, grant the identity subscription Contributor explicitly:

```bash
MI_PRINCIPAL=$(az identity show -n nokvo-id -g rg-nokvo-prod --query principalId -o tsv)
az role assignment create --assignee-object-id "$MI_PRINCIPAL" \
  --assignee-principal-type ServicePrincipal --role Contributor \
  --scope /subscriptions/<subscription-id>
```

## Scaling calibration

`concurrentRequests` (calls/replica) defaults to 10. Set it from the load test
(`infra/loadtest/`): find the per-replica knee, then redeploy `app.bicep` with
`-p concurrentRequests=<measured>` and adjust `maxReplicas` to
`100 / density + headroom`.

## Cost guardrails / alerts to wire

- Postgres **Burstable CPU-credit depletion** → upsize to GP D-series.
- Redis **evictions / memory** → upsize C1 → C2.
- Replica count ≈ `maxReplicas` → capacity headroom.
- `/health` 503 streak, Redis latency, Postgres connection ceiling.
