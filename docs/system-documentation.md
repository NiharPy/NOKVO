# NOKVO System Documentation

Last updated: 2026-05-01

## 1. Executive Summary

NOKVO is a multi-tenant organization console and integration platform. It has two major operating surfaces:

- Superadmin console for provisioning tenant organizations and their cloud resources.
- Organization portal for organization admins and members to sign in, manage users, and connect operational systems such as databases, CRM, helpdesk, ERP, and shipping providers.

The current system supports:

- Superadmin authentication with MFA.
- Organization Google sign-in with organization-scoped MFA.
- Tenant provisioning with Azure resource metadata, Qdrant collection naming, Redis namespace metadata, Blob storage metadata, Key Vault secret references, and provider status tracking.
- Organization member management.
- Database ingestion and row indexing.
- CRM integration for Zoho CRM and Freshworks CRM.
- Zoho Desk ticket integration using the Zoho CRM OAuth grant.
- ERP integration for TallyPrime / Tally ERP over XML HTTP.
- Shipping integration for Shiprocket.
- Toolkit Generator for admin-reviewed MCP tool creation from integration embeddings.
- Deterministic embedding generation and Qdrant upsert for integration schema/action context.
- **AI Toolkit Engine**: Dynamic creation of Model Context Protocol (MCP) tools for voice agents.
- **Security Posture**: Enhanced scoping for Azure Service Principals and logical tenant isolation.
- **Audit Logging**: Comprehensive event tracking for toolkit generation and provisioning.

The core integration design is consistent across providers:

1. Admin submits credentials or begins OAuth.
2. Backend validates/scans provider metadata.
3. Credentials are stored through Key Vault service references.
4. `tenant_resources.provider_status` is updated with integration state.
5. Schema/action context is embedded and upserted into the tenant Qdrant collection.
6. Frontend renders status cards and action modals from `/api/org-auth/*` endpoints.

## 2. Repository Layout

Important backend paths:

- `app/main.py`: FastAPI app initialization, CORS, rate limiting, router registration.
- `app/api/auth.py`: Superadmin authentication endpoints.
- `app/api/organization_auth.py`: Organization auth, members, database, CRM, Desk, ERP, and shipping endpoints.
- `app/api/superadmin_tenant_provisioning.py`: Superadmin tenant provisioning API.
- `app/api/deps.py`: Authentication and role dependency guards.
- `app/core/config.py`: Environment-backed settings.
- `app/core/security.py`: JWT, refresh token, password, and encryption helpers.
- `app/models/*`: SQLAlchemy models.
- `app/schemas/*`: Pydantic request/response schemas.
- `app/services/*`: Provider and cloud service implementations.
- `migrations/versions/*`: Alembic schema migrations.
- `tests/*`: Async API and service tests.

Important frontend paths:

- `frontend/src/App.vue`: Top-level app shell/theme.
- `frontend/src/components/ConsoleLoginCard.vue`: Superadmin auth flow.
- `frontend/src/components/SuperAdminDashboard.vue`: Superadmin tenant provisioning UI.
- `frontend/src/components/OrganizationPortal.vue`: Organization login, dashboard, members, tickets, database/CRM/ERP/shipping modals.
- `frontend/src/style.css`: Global styles.

Utility scripts:

- `scripts/mock_tally_server.py`: Local fake Tally XML HTTP server for testing Tally integration.
- `scripts/create_demo_ecommerce_schema.py`: Demo database/schema helper.
- `scripts/purge_non_superadmin_data.py`: Data cleanup helper.

## 3. Runtime Architecture

### 3.1 Backend

The backend is a FastAPI application.

Registered routers:

- `/api/auth`: Superadmin auth.
- `/api/org-auth`: Organization auth and integrations.
- `/superadmin/tenants`: Tenant provisioning and tenant listing.
- `/health`: Health check.

The app uses:

- Async SQLAlchemy sessions via `app/db/session.py`.
- JWT access tokens with role and principal claims.
- Refresh sessions persisted in `superadmin_sessions` and `organization_sessions`.
- SlowAPI rate limiting middleware.
- CORS restricted to `settings.EXPECTED_ORIGIN`, default `http://localhost:5173`.

### 3.2 Frontend

The frontend is a Vue app built with Vite.

Main organization dashboard features:

- Google organization login.
- TOTP setup/verify.
- Organization switching when multiple organizations match a Google hosted domain.
- Dashboard cards for database, CRM, ERP, and shipping integrations.
- Tickets page for Zoho Desk readiness.
- Modals for database, CRM, ERP/Tally, and Shiprocket connection.
- Member invite/update controls.

### 3.3 Data Stores and External Resources

Primary relational data is stored in PostgreSQL.

Tenant metadata is centralized in `tenant_resources`:

- Azure resource group/region metadata.
- Qdrant collection name and URL reference.
- Redis namespace metadata.
- Blob storage metadata.
- Key Vault name and secret references.
- Integration status snapshots in `provider_status`.
- Provisioning state and steps.
- Usage/cost metadata.

Qdrant is used for indexing structured integration context.

Key Vault service is used as the abstraction for storing provider secrets. In the code, integrations call `AzureKeyVaultService.set_secret_value(...)` and store only secret reference names in `tenant_resources.secret_refs`.

## 4. Configuration

Settings are defined in `app/core/config.py` and read from `.env`.

Key settings:

- `POSTGRES_SERVER`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`: PostgreSQL connection.
- `SECRET_KEY`, `ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES`, `REFRESH_TOKEN_EXPIRE_HOURS`: JWT/session security.
- `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`: Google organization login.
- `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REDIRECT_URI`, `ZOHO_ACCOUNTS_URL`: Zoho OAuth.
- `EXPECTED_ORIGIN`: Frontend origin for CORS and OAuth redirects.
- `AZURE_*`: Azure tenant provisioning and resource metadata settings.
- `QDRANT_URL`, `QDRANT_API_KEY`, `QDRANT_COLLECTION_PREFIX`, `QDRANT_VECTOR_SIZE`: Qdrant setup.
- `KEY_VAULT_SECRET_ROTATION_DAYS`: Secret rotation metadata.
- `TWILIO_*`, `SONIOX_*`: Voice/STT/TTS providers.
- Billing constants for usage estimation.

Local defaults:

- Frontend: `http://localhost:5173`
- Backend: `http://localhost:8000`
- Organization API base in frontend: `http://localhost:8000/api`

## 5. Authentication and Authorization

### 5.1 Principal Types

JWTs include a `principal_type` claim:

- `superadmin`: Used by superadmin console.
- `organization_user`: Used by organization portal.

Backend dependency guards reject tokens with the wrong principal type.

### 5.2 Superadmin Auth

Implemented in `app/api/auth.py` and guarded by `app/api/deps.py`.

Main concepts:

- Superadmin users are stored in `superadmin_users`.
- Passwords are hashed.
- MFA can be required.
- Refresh tokens are hashed and persisted in `superadmin_sessions`.
- Logout revokes sessions by setting `revoked_at`.

### 5.3 Organization Auth

Implemented in `app/api/organization_auth.py`.

Flow:

1. Frontend loads `/api/org-auth/config`.
2. Google Identity Services returns an ID token.
3. Backend verifies the ID token through `GoogleOAuthService`.
4. Email is normalized and domain is checked against organizations.
5. If multiple organizations match, backend returns `organization_selection_required`.
6. User selects organization.
7. Backend issues a short-lived pending token if MFA setup/verification is needed.
8. User completes TOTP setup or verification.
9. Backend creates an organization session and returns final access/refresh tokens.

Organization users are stored in `organization_users`.

Role checks:

- `RequireOrganizationRole(["admin"])`: Admin-only operations such as connecting providers and managing members.
- `RequireOrganizationRole(["admin", "manager"])`: Operational actions such as creating tickets or shipping operations.

### 5.4 Organization MFA

TOTP is used.

Important behavior:

- Users can be invited and then activated through Google login + TOTP verification.
- TOTP secrets are encrypted.
- Tokens include `mfa_completed`.

## 6. Tenant Provisioning

Superadmin tenant provisioning creates organization-level metadata and tenant resource records.

Key files:

- `app/api/superadmin_tenant_provisioning.py`
- `app/services/azure_tenant_provisioning_service.py`
- `app/services/azure_resource_group_service.py`
- `app/services/azure_blob_service.py`
- `app/services/azure_keyvault_service.py`
- `app/services/qdrant_service.py`
- `app/services/redis_tenant_service.py`
- `app/services/twilio_service.py`
- `app/services/soniox_stt_service.py`
- `app/services/soniox_tts_service.py`

Provisioning state is stored in `tenant_resources`:

- `provisioning_status`: `pending`, `success`, `partial`, or `failed`.
- `provisioning_steps`: JSON list of step status objects.
- `cleanup_required`: Boolean.
- `provider_status`: JSON object with status for providers.

The frontend superadmin dashboard streams provisioning status from the backend and displays step progress.

## 7. Core Data Model

### 7.1 `organizations`

Represents a tenant organization.

Important fields:

- `id`
- `name`
- `admin_email`
- `admin_name`
- `email_domain`
- `region`
- `environment`
- `call_type`
- `language`
- `plan_type`
- `stores_pii`
- `record_calls`
- `create_resource_group`
- `twilio_auto_provision`
- `industry`
- `country_code`

### 7.2 `organization_users`

Represents users inside an organization.

Important fields:

- `id`
- `organization_id`
- `email`
- `full_name`
- `role`: `admin`, `manager`, `member`, or `viewer`.
- `status`: `invited`, `active`, or `disabled`.
- `auth_provider`
- `mfa_required`
- `totp_secret_encrypted`
- `email_verified`
- `last_login_at`
- `last_login_ip`

Unique constraint:

- `(organization_id, email)`

### 7.3 `tenant_resources`

Central resource metadata row for a tenant.

Important fields:

- `organization_id`
- `tenant_id`
- `azure_resource_group_name`
- `azure_region`
- `qdrant_collection_name`
- `qdrant_url_ref`
- `redis_namespace`
- `storage_account_name`
- `storage_container_name`
- `blob_prefix`
- `key_vault_name`
- `secret_refs`
- `provider_status`
- `provisioning_status`
- `provisioning_steps`
- `cleanup_required`
- usage and cost fields.

`secret_refs` contains named references, not raw secrets.

Common keys:

- `db_connection_string`
- `crm_connection`
- `erp_connection`
- `shipping_connection`

`provider_status` contains integration snapshots.

Common prefixes:

- `db_*`
- `crm_*`
- `zoho_desk_*`
- `erp_*`
- `shipping_*`

## 8. Integration Indexing Pattern

All major integrations follow the same pattern:

1. Validate provider name.
2. Load or receive credentials.
3. Scan provider metadata or build action catalog.
4. Store credentials using `AzureKeyVaultService`.
5. Build embedding points with deterministic IDs.
6. Upsert points to the tenant Qdrant collection.
7. Update `tenant_resources.provider_status`.
8. Return typed status/metadata response to frontend.

Embedding service:

- `app/services/text_embedding_service.py`
- Produces stable vectors for structured text.

Qdrant service:

- `app/services/qdrant_service.py`
- Uses `tenant_res.qdrant_collection_name`.
- Upserts points with payloads including source type, integration type, provider, folder path, action/module metadata, and text.

Folder namespace conventions:

- Database: data records have database payloads.
- CRM: `integrations/crm/{provider}`
- Zoho Desk: `integrations/zoho-desk`
- ERP: `integrations/erp/{provider}`
- Shipping: `integrations/shipping/{provider}`

## 9. Database Integration

Service:

- `app/services/database_integration_service.py`

API:

- `GET /api/org-auth/database/providers`
- `GET /api/org-auth/database/status`
- `POST /api/org-auth/database/connect`
- `POST /api/org-auth/database/index`

Supported provider options:

- PostgreSQL: supported.
- Redis: supported.
- SQLite: supported.
- CockroachDB: supported through PostgreSQL family.
- Amazon Redshift: supported through PostgreSQL family.
- MySQL, MariaDB, SQL Server, Oracle, MongoDB, Snowflake, BigQuery: listed but not live-scanned in current backend.

Connect flow:

1. Admin opens Connect Database modal.
2. Selects provider.
3. Enters connection string.
4. Backend scans schema.
5. Connection string is encrypted and stored through Key Vault service.
6. `provider_status` is updated with `db_status="schema_scanned"`.
7. Frontend shows selectable schema/columns.
8. Admin selects columns and indexes them.
9. Backend fetches row previews and writes embeddings to Qdrant.
10. `provider_status` is updated with `db_status="indexed"`.

Indexed payload type:

- `source_type`: `database_schema_selection`
- Includes provider, table, selected columns, text, row preview.

Important safeguards:

- PostgreSQL SSL query params are normalized.
- Row limit is clamped to 1-200.
- Identifiers are quoted for PostgreSQL and SQLite.

## 10. CRM Integration

Service:

- `app/services/crm_integration_service.py`

API:

- `GET /api/org-auth/crm/providers`
- `GET /api/org-auth/crm/status`
- `POST /api/org-auth/crm/connect`
- `GET /api/org-auth/crm/zoho/authorize`
- `GET /api/org-auth/crm/zoho/callback`

Supported providers:

- Zoho CRM.
- Freshworks CRM.

### 10.1 Zoho CRM

Zoho uses OAuth.

OAuth config:

- `ZOHO_CLIENT_ID`
- `ZOHO_CLIENT_SECRET`
- `ZOHO_REDIRECT_URI`
- `ZOHO_ACCOUNTS_URL`

Requested scopes:

- `ZohoCRM.modules.ALL`
- `ZohoCRM.settings.ALL`
- `ZohoCRM.org.READ`
- `ZohoSearch.securesearch.READ`
- `Desk.tickets.ALL`
- `Desk.basic.READ`
- `Desk.settings.READ`

OAuth flow:

1. Frontend calls `/api/org-auth/crm/zoho/authorize`.
2. Backend creates signed state with organization ID and user ID.
3. User is redirected to Zoho.
4. Zoho redirects to `/api/org-auth/crm/zoho/callback`.
5. Backend exchanges code for access/refresh token.
6. Backend scans Zoho CRM metadata.
7. Backend stores credential secret.
8. Backend indexes CRM modules/actions.
9. Backend attempts Zoho Desk auto-index.
10. Backend redirects frontend with `crm_oauth=success` or `crm_oauth=error`.

Token refresh:

- Zoho access tokens are short-lived.
- `_hydrate_zoho_credentials` refreshes whenever a refresh token exists.
- This prevents stale access tokens for CRM and Desk operations.

CRM scan:

- Gets modules from `/crm/v8/settings/modules`.
- Fetches metadata per module.
- Skips unsupported module errors.
- Indexes schema and generic actions.

### 10.2 Freshworks CRM

Freshworks uses account URL + API token.

Fields:

- `account_url`
- `access_token`

Scan:

- Normalizes URL to include `/crm/sales`.
- Scans known module candidates such as contacts, accounts, deals, leads, and tasks.
- Indexes discovered fields and actions.

## 11. Zoho Desk Integration

Service:

- `app/services/zoho_desk_service.py`

API:

- `GET /api/org-auth/crm/zoho-desk/status`
- `POST /api/org-auth/crm/zoho-desk/connect`
- `POST /api/org-auth/crm/zoho-desk/tickets`
- `PATCH /api/org-auth/crm/zoho-desk/tickets/{ticket_id}`

Current product behavior:

- Zoho Desk is treated as part of Zoho CRM OAuth.
- The UI does not show a separate Desk connect button.
- Tickets page shows readiness based on Zoho CRM/Desk status.
- During Zoho CRM OAuth callback, backend attempts to auto-index Desk metadata.

Desk scan behavior:

- Derives Desk base URL from Zoho API domain.
- Example: `https://www.zohoapis.in` maps to `https://desk.zoho.in`.
- Uses `myAccessibleModules` for metadata discovery to avoid unnecessary organization-list scope dependence.
- Attempts department scan, but skips if unavailable.
- Indexes Desk schema/actions into `integrations/zoho-desk`.

Desk actions:

- `create_ticket`
- `update_ticket`
- `get_ticket`
- `list_tickets`

Ticket create request:

- `subject`
- `department_id`
- `description`
- `contact_id`
- `email`
- `phone`
- `status`
- `priority`
- `custom_fields`

Ticket update request:

- `subject`
- `description`
- `contact_id`
- `email`
- `phone`
- `status`
- `priority`
- `custom_fields`

Known operational note:

- If Zoho returns `SCOPE_MISMATCH`, the saved grant lacks required scopes. Refreshing a token cannot add scopes. The user must revoke/reconnect the Zoho OAuth app.

## 12. ERP Integration: TallyPrime / Tally ERP

Service:

- `app/services/erp_integration_service.py`

API:

- `GET /api/org-auth/erp/providers`
- `GET /api/org-auth/erp/status`
- `POST /api/org-auth/erp/connect`
- `POST /api/org-auth/erp/tally/xml`

Provider:

- `tally`: TallyPrime / Tally ERP.

Tally integration method:

- XML over HTTP.
- Default URL: `http://localhost:9000`.
- TallyPrime must be running.
- HTTP server must be enabled.
- A company must be loaded.

Connection fields:

- `provider`: `tally`
- `base_url`: default `http://localhost:9000`
- `company_name`: optional; when omitted, Tally uses loaded company.
- `timeout_seconds`: clamped between 3 and 60.
- `max_items_per_module`: clamped between 1 and 100.

Scanned collections:

- Companies
- Accounting Groups
- Ledgers
- Stock Groups
- Stock Items
- Units of Measure
- Cost Centres
- Voucher Types
- Vouchers

Each module stores:

- `api_name`
- `label`
- `object_type`
- fields
- record count
- sample records
- scan status
- last error if unavailable

Actions indexed:

- `create_ledger`
- `create_stock_item`
- `create_sales_voucher`
- `create_purchase_voucher`
- `create_receipt_or_payment`
- `export_collection`
- `execute_tally_xml`

Raw XML endpoint:

- `POST /api/org-auth/erp/tally/xml`
- Requires connected Tally ERP.
- Accepts XML payload with `<ENVELOPE>`.
- Sends it to configured Tally HTTP server and returns response XML.

Mock Tally server:

- `scripts/mock_tally_server.py`
- Runs a fake XML HTTP server for local testing.
- Start command:

```bash
venv/bin/python scripts/mock_tally_server.py --host 127.0.0.1 --port 9000
```

Mock data includes:

- Company: `NOKVO Demo Pvt Ltd`
- Groups
- Ledgers
- Stock item/group
- Unit
- Cost centre
- Voucher types
- Voucher

Use in frontend modal:

- Tally HTTP URL: `http://127.0.0.1:9000`
- Company Name: `NOKVO Demo Pvt Ltd`

## 13. Shipping Integration: Shiprocket

Service:

- `app/services/shipping_integration_service.py`

API:

- `GET /api/org-auth/shipping/providers`
- `GET /api/org-auth/shipping/status`
- `POST /api/org-auth/shipping/connect`
- `POST /api/org-auth/shipping/shiprocket/serviceability`
- `POST /api/org-auth/shipping/shiprocket/orders`
- `POST /api/org-auth/shipping/shiprocket/awb`
- `POST /api/org-auth/shipping/shiprocket/pickup`
- `POST /api/org-auth/shipping/shiprocket/track`

Provider:

- `shiprocket`

Authentication:

- Shiprocket does not use OAuth authorization-code flow in the implemented public API.
- It uses API user email/password to generate a bearer token.
- Token endpoint: `/auth/login`
- Default base URL: `https://apiv2.shiprocket.in/v1/external`
- Token is stored along with encrypted credentials.

Connection fields:

- `provider`: `shiprocket`
- `email`: Shiprocket API user email.
- `password`: Shiprocket API user password.
- `base_url`: default `https://apiv2.shiprocket.in/v1/external`

Important:

- Use a Shiprocket API user from Shiprocket panel `Settings > API`.
- The API user email should be different from the main Shiprocket account email.
- Shiprocket tokens are valid for approximately 10 days.

Indexed modules:

- Authentication
- Orders
- Couriers
- Tracking
- Returns

Indexed actions:

- `check_serviceability`
- `create_adhoc_order`
- `assign_awb`
- `generate_pickup`
- `track_by_order_id`
- `track_by_awb`

Operational endpoints:

### 13.1 Serviceability

Endpoint:

- `POST /api/org-auth/shipping/shiprocket/serviceability`

Request:

- `pickup_postcode`
- `delivery_postcode`
- `weight`
- `cod`
- `order_id`

Backend maps to:

- `GET /courier/serviceability/`

### 13.2 Create Order

Endpoint:

- `POST /api/org-auth/shipping/shiprocket/orders`

Request:

- `payload`: Raw Shiprocket adhoc order payload.

Backend maps to:

- `POST /orders/create/adhoc`

### 13.3 Assign AWB

Endpoint:

- `POST /api/org-auth/shipping/shiprocket/awb`

Request:

- `shipment_id`
- `courier_id`
- `status`

Backend maps to:

- `POST /courier/assign/awb`

### 13.4 Generate Pickup

Endpoint:

- `POST /api/org-auth/shipping/shiprocket/pickup`

Request:

- `shipment_id`: integer or list of integers.

Backend maps to:

- `POST /courier/generate/pickup`

### 13.5 Tracking

Endpoint:

- `POST /api/org-auth/shipping/shiprocket/track`

Request:

- `order_id` or `awb_code`

Backend maps to:

- `GET /courier/track?order_id=...`
- `GET /courier/track/awb/{awb_code}`

## 14. Organization Portal Frontend

Primary component:

- `frontend/src/components/OrganizationPortal.vue`

Main state groups:

- Auth state.
- Current user and organization.
- Members.
- Database providers/status/form/schema.
- CRM providers/status/form.
- Zoho Desk status.
- ERP providers/status/form.
- Shipping providers/status/form.
- UI state for modals and current page.

Current pages:

- `dashboard`
- `tickets`

Floating navigation:

- Dashboard
- Tickets
- Search
- Filter/sort buttons
- Notifications/settings placeholders
- Theme toggle
- Avatar/logout

Dashboard header actions:

- Connect Database
- Connect CRM
- Connect ERP
- Connect Shipping
- Log Out

Dashboard cards:

- Organization Snapshot
- Database Sync
- Access Overview
- ERP Sync
- CRM Sync
- Shipping Sync
- Workspace Controls
- Members

Tickets page:

- Shows Zoho Desk readiness.
- Explains that ticket operations use the Zoho OAuth grant created during CRM connection.

Modals:

- Database modal: provider, connection string, schema selection/indexing.
- CRM modal: Zoho OAuth or Freshworks token details.
- ERP modal: Tally provider, URL, company, timeout, sample count.
- Shipping modal: Shiprocket provider, API email, password, base URL.

## 15. Superadmin Frontend

Primary components:

- `frontend/src/components/ConsoleLoginCard.vue`
- `frontend/src/components/SuperAdminDashboard.vue`

Superadmin dashboard supports:

- Viewing provisioned organizations.
- Creating organizations.
- Streaming provisioning progress.
- Showing provisioning result state.
- Theme controls.

Tenant provisioning form fields include:

- Organization name.
- Admin email/name.
- Azure region.
- Environment.
- Call type.
- Language.
- Plan type.
- PII and recording flags.
- Resource group creation flag.
- Twilio auto-provisioning flag.

## 16. API Endpoint Reference

### 16.1 Health

- `GET /health`

### 16.2 Superadmin Auth

See `app/api/auth.py`.

Main categories:

- Login.
- MFA setup/verify.
- Refresh.
- Logout.

### 16.3 Superadmin Tenant Provisioning

Base:

- `/superadmin/tenants`

Main categories:

- List tenants.
- Provision tenant.
- Stream provisioning status.

### 16.4 Organization Auth

Base:

- `/api/org-auth`

Auth/config:

- `GET /config`
- `POST /google/login`
- `POST /mfa/setup`
- `POST /mfa/verify`
- `POST /refresh`
- `POST /logout`
- `GET /me`

Members:

- `GET /members`
- `POST /members`
- `PATCH /members/{member_id}`

Database:

- `GET /database/providers`
- `GET /database/status`
- `POST /database/connect`
- `POST /database/index`

CRM:

- `GET /crm/providers`
- `GET /crm/status`
- `POST /crm/connect`
- `GET /crm/zoho/authorize`
- `GET /crm/zoho/callback`

Zoho Desk:

- `GET /crm/zoho-desk/status`
- `POST /crm/zoho-desk/connect`
- `POST /crm/zoho-desk/tickets`
- `PATCH /crm/zoho-desk/tickets/{ticket_id}`

ERP:

- `GET /erp/providers`
- `GET /erp/status`
- `POST /erp/connect`
- `POST /erp/tally/xml`

Shipping:

- `GET /shipping/providers`
- `GET /shipping/status`
- `POST /shipping/connect`
- `POST /shipping/shiprocket/serviceability`
- `POST /shipping/shiprocket/orders`
- `POST /shipping/shiprocket/awb`
- `POST /shipping/shiprocket/pickup`
- `POST /shipping/shiprocket/track`

Toolkit:

- `POST /toolkit/generate`
- `GET /toolkit/registry`
- `POST /toolkit/drafts/{draft_id}/approve`
- `POST /toolkit/drafts/{draft_id}/reject`

## 13.1 Toolkit Generator and MCP Registry

Service:

- `app/services/toolkit_generator_service.py`

API:

- `POST /api/org-auth/toolkit/generate`
- `GET /api/org-auth/toolkit/registry`
- `POST /api/org-auth/toolkit/drafts/{draft_id}/approve`
- `POST /api/org-auth/toolkit/drafts/{draft_id}/reject`

Purpose:

- Converts an admin's natural-language prompt into a draft MCP tool definition.
- Uses only the selected integration's indexed Qdrant context and stored integration snapshots.
- Requires admin approval before the generated tool is added to the MCP registry.
- Registry is tenant-specific and integration-specific, stored in `mcp_tool_registry_entries`.

Global Azure OpenAI configuration:

- `AZURE_OPENAI_GLOBAL_ENDPOINT`
- `AZURE_OPENAI_GLOBAL_API_KEY`
- `AZURE_OPENAI_GLOBAL_DEPLOYMENT`
- `AZURE_OPENAI_GLOBAL_API_VERSION`

Azure OpenAI operational note:

- `AZURE_OPENAI_GLOBAL_ENDPOINT` can be a normal Azure OpenAI resource base URL or a direct Azure AI Foundry `/openai/v1/responses` URL.
- For a direct `/responses` endpoint, the backend sends the configured deployment/model as the `model` field and does not append `api-version`.
- For a resource base URL, Azure OpenAI calls use deployment names, not raw model names, in the URL.
- If the Azure deployment/model is named `gpt-5.4-mini`, set `AZURE_OPENAI_GLOBAL_DEPLOYMENT=gpt-5.4-mini`.
- The API key must be created/retrieved in Azure portal or Azure control plane and placed in `.env`; the app cannot invent or mint a real Azure key without Azure permissions.

Generation flow:

1. Admin opens Toolkit Generator.
2. Admin selects integration type and provider.
3. Admin enters NLP prompt and optionally overrides the system prompt.
4. Backend embeds the prompt and searches the tenant Qdrant collection.
5. Backend filters context to the selected integration/provider where possible.
6. Backend adds stored snapshots from `tenant_resources.provider_status`.
7. Backend sends only that context to the global Azure OpenAI deployment.
8. Backend stores the result as a draft in `provider_status["toolkit_drafts"]`.
9. Admin reviews JSON.
10. Approval upserts the tool into `mcp_tool_registry_entries` for the organization, integration type, provider, and tool name.

Draft fields:

- `id`
- `status`
- `integration_type`
- `provider`
- `nlp_prompt`
- `tool`
- `context_summary`
- `created_at`
- `reviewed_at`
- `reviewed_by`
- `review_notes`

## 13.2 Toolkit Hardening Design

Immediate safeguards now enforced:

- Azure OpenAI request handling uses a simple Responses API payload first and retries with a simpler string input if Azure rejects the request shape.
- Tool names are normalized to short snake_case names and are safe for MCP registry lookup.
- Empty or malformed `input_schema` values are replaced with a concrete object schema.
- Every tool receives an `output_schema` with `success`, `data`, `message`, and metadata fields.
- Every tool receives an `execution` contract.
- Database tools receive a `database_sql` execution contract with explicit mode, allowed statements, blocked destructive statements, allowed table context, and PII redaction requirements.
- Database write tools are allowed for parameterized `INSERT`, `UPDATE`, and `DELETE` only when marked `write_requires_admin_approval`, `requires_admin_confirmation`, and `test_run_required`.
- Destructive schema/admin SQL prompts are converted to `unsupported_tool_request`.
- Approval re-sanitizes drafts before publishing, so legacy drafts cannot bypass current rules.
- Approved tools are stored in `mcp_tool_registry_entries`, scoped by organization, tenant, integration type, provider, and tool name.
- Draft generation, approval, and rejection append audit events to `provider_status["toolkit_audit_events"]`.
- Publishing remains admin-only through the Toolkit approval endpoints.

Next build:

- Relationship discovery should enrich database context with foreign keys, primary keys, and likely joins before tool generation.
- Test-run mode should validate a draft against sample inputs without mutating external systems.
- PII redaction should become an executable output post-processor instead of only a tool contract rule.
- Tool approval UI should display schema, execution mapping, safety notes, version, and audit history as separate review panels.
- Audit logs should move from `provider_status["toolkit_audit_events"]` into a dedicated organization audit table.
- Tool versioning should retain every published version, not only the active row version counter.

Enterprise-grade later:

- Policy engine for organization-specific rules, data handling, and integration-level allowlists.
- Advanced permission system for per-role and per-tool execution grants.
- Usage analytics for generated tools, execution count, token spend, latency, failures, and integration cost.
- Tool marketplace/registry for reusable reviewed tools across tenants with explicit install approval.
- Workflow chaining across CRM, Desk, ERP, Shipping, and Database with typed handoffs, rollback policies, and approval gates.

Tool JSON fields:

- `name`
- `title`
- `description`
- `integration_type`
- `provider`
- `mcp`
- `input_schema`
- `execution_plan`
- `source_context`
- `safety_notes`

Fallback behavior:

- If Azure OpenAI is not configured or returns an error, the backend creates a conservative fallback draft from available integration snapshots.
- Fallback drafts still require admin approval.
- This allows the workflow to be tested before the Azure key is configured.

## 17. Security Model

### 17.1 Authentication

- JWT access tokens are short-lived.
- Refresh tokens are random, hashed, and stored server-side.
- Sessions can be revoked.
- Principal types prevent cross-use of superadmin and organization tokens.

### 17.2 Authorization

- Superadmin routes use role guards.
- Organization routes use organization role guards.
- Admin-only operations include provider connection and member management.
- Manager/admin operations include ticket and shipping operational actions.

### 17.3 Secret Storage

Credentials are not stored directly in tenant resource rows.

Flow:

1. Integration calls `AzureKeyVaultService.set_secret_value`.
2. `tenant_resources.secret_refs` stores the secret name.
3. Later operational calls load via `AzureKeyVaultService.get_secret_value`.

Secret ref keys:

- `db_connection_string`
- `crm_connection`
- `erp_connection`
- `shipping_connection`

### 17.4 Data Isolation

Isolation dimensions:

- Organization ID in user/session/token checks.
- Tenant ID in Qdrant point IDs.
- Tenant-specific Qdrant collection names.
- Tenant-specific Redis namespace metadata.
- Tenant-specific Key Vault secret names.
- Tenant-specific Blob prefix/container metadata.

### 17.5 External API Credentials

Zoho:

- OAuth tokens.
- Refresh token is used to avoid stale access tokens.

Freshworks:

- API token.

Tally:

- URL/company details, not necessarily secret credentials.
- Still stored through Key Vault path for consistency.

Shiprocket:

- API user email/password.
- Bearer token from `/auth/login`.

Toolkit Generator:

- Uses a global Azure OpenAI endpoint/API key shared across tenants.
- The key is server-side only and must not be exposed to the frontend.
- The generator receives only selected integration context.
- Generated tools are drafts until an organization admin approves them.
- Approved tools are tenant-scoped rows in `mcp_tool_registry_entries`.
- Drafts remain in `provider_status["toolkit_drafts"]` until they are approved or rejected.

## 18. Testing and Verification

Existing tests:

- `tests/test_auth.py`
- `tests/test_azure_tenant_provisioning.py`
- `tests/test_organization_auth.py`

Added/covered scenarios include:

- Zoho token refresh behavior.
- Database connect/index.
- CRM connect/index.
- Zoho Desk connect/ticket actions.
- Tally ERP connect/index.
- Shiprocket connect/index.

Local verification commands used:

```bash
python3 -m py_compile app/services/erp_integration_service.py app/services/shipping_integration_service.py app/api/organization_auth.py app/schemas/organization_auth.py tests/test_organization_auth.py
```

```bash
cd frontend
npm run build
```

Current limitation:

- `pytest` is not installed in the local Python environment used by this session, so tests could not be executed with `pytest` without installing dependencies.

## 19. Local Development Runbook

### 19.1 Start Backend

Typical command:

```bash
venv/bin/uvicorn app.main:app --reload
```

Backend default:

- `http://localhost:8000`

### 19.2 Start Frontend

```bash
cd frontend
npm run dev
```

Frontend default:

- `http://localhost:5173`

### 19.3 Start Mock Tally Server

```bash
venv/bin/python scripts/mock_tally_server.py --host 127.0.0.1 --port 9000
```

Use ERP modal values:

- URL: `http://127.0.0.1:9000`
- Company: `NOKVO Demo Pvt Ltd`

### 19.4 Connect Shiprocket

Steps:

1. In Shiprocket panel, go to Settings > API.
2. Create API user.
3. Use the API user email and password in NOKVO Connect Shipping modal.
4. Leave base URL as `https://apiv2.shiprocket.in/v1/external` unless using a proxy/mock.

### 19.5 Connect Zoho CRM and Desk

Steps:

1. Configure Zoho env vars.
2. Open Connect CRM.
3. Select Zoho.
4. Continue with Zoho.
5. Approve scopes.
6. Backend scans CRM and attempts Desk auto-index.
7. Tickets page reflects Desk status.

If scopes change:

- Revoke the old app grant in Zoho Accounts.
- Reconnect through OAuth.

## 20. Operational Notes and Known Gaps

### 20.1 Database

- MySQL/MariaDB/SQL Server/Oracle/MongoDB/Snowflake/BigQuery are listed but not fully implemented for live scanning.
- PostgreSQL-family, SQLite, and Redis are implemented.

### 20.2 Zoho Desk

- Desk is auto-indexed during Zoho CRM OAuth when possible.
- Desk scan is resilient to department scan failures.
- Organization ID header is optional for ticket operations in current implementation.

### 20.3 Tally

- Tally integration requires network access from backend to the local or exposed Tally HTTP server.
- If Tally runs on a desktop machine behind NAT, a secure tunnel or private network path is needed.
- Raw XML execution is powerful and should remain admin-only.
- Production hardening should include allowlists, XML operation policies, and audit logging for raw XML calls.

### 20.4 Shiprocket

- Public API flow uses API user credentials and bearer token, not OAuth redirect.
- Shiprocket operations affect real account data.
- There may be no public sandbox credentials by default.
- Create order/AWB/pickup should be tested carefully on a controlled account.

### 20.5 Frontend

- Tickets page currently shows readiness but does not yet expose a full ticket create/update form.
- Shiprocket operational actions are backend-ready but frontend currently only provides connect/status UI.
- ERP raw XML execution is backend-ready but not exposed in frontend yet.

## 15. AI Toolkit & MCP Registry

The **AI Toolkit** is a core automation feature that allows organization admins to create safe, schema-aware tools for their voice agents using natural language.

### 15.1 Generation Workflow
1.  **Context Retrieval**: The system fetches relevant schema and action context from the tenant's Qdrant collection (RAG).
2.  **LLM Proposal**: Uses the `ToolkitGeneratorService` to generate a tool definition (name, description, input/output schemas, and execution logic).
3.  **Drafting**: Tools are stored as `draft` entries in the `provider_status` metadata.
4.  **Admin Review**: Every tool must be reviewed by an admin. The system enforces:
    -   **SQL Sanitization**: Blocking destructive commands (`DROP`, `TRUNCATE`, etc.).
    -   **Parameterization**: Enforcing bound parameters to prevent injection.
    -   **PII Classification**: Automatic marking of sensitive fields (emails, phones) for redaction.
5.  **Publishing**: Approved tools are registered in the `MCPToolRegistryEntry` table and are instantly available to agents via the **Model Context Protocol**.

### 15.2 MCP Compatibility
Generated tools are natively compatible with the **Model Context Protocol**, allowing them to be "injected" into the context of an LLM-based voice agent.

---

## 16. Security Architecture & Compliance

### 16.1 Azure Service Principal Scoping
- **Subscription Scope**: The platform's Service Principal currently requires `Contributor` access at the **Subscription level**. This is necessary for dynamic Resource Group creation across different regions and environments.
- **Hardening**: It is highly recommended to use **Azure Managed Identity** (already supported in `azure_auth.py`) when running on Azure-native compute to eliminate the risk of leaked client secrets.

### 16.2 Network Isolation Model
- **Logical Isolation**: NOKVO uses a "Shared Infrastructure, Logically Isolated" model.
    - **Storage**: Tenants share a Storage Account but are isolated by Blob Prefixes.
    - **Key Vault**: Shared Vault with name-spaced secrets.
    - **Redis**: Shared instance with name-spaced keys.
- **Public Endpoints**: Resources (OpenAI, Key Vault) are currently provisioned with `public_network_access="Enabled"`. Future hardening should involve **Azure Private Links** and **VNets** for true network-level perimeter security.

---

## 17. Audit & Governance
The system maintains multiple layers of audit logs:
- **`SuperAdminAuditLog`**: Tracks sensitive platform actions (tenant provisioning, cost recalculations).
- **`toolkit_audit_events`**: Tracks every tool generation, edit, and approval at the organization level.
- **Azure Log Analytics**: Optional integration for Key Vault access and resource diagnostics.

---

## 18. Installation and Development

(Existing Section 18-20 content preserved...)
