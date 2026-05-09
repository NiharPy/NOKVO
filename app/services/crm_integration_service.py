from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from typing import Any
from urllib import parse as urllib_parse
from urllib import error as urllib_error
from urllib import request as urllib_request
import uuid

from app.core.config import settings
from app.models.tenant_resources import TenantResources
from app.services.azure_keyvault_service import AzureKeyVaultService
from app.services.qdrant_service import QdrantService
from app.services.text_embedding_service import TextEmbeddingService


SUPPORTED_CRM_PROVIDER_OPTIONS = [
    {"value": "zoho", "label": "Zoho CRM", "group": "crm", "supported": True},
    {"value": "freshworks", "label": "Freshworks CRM", "group": "crm", "supported": True},
]

FRESHWORKS_MODULE_CANDIDATES = [
    {"api_name": "contacts", "label": "Contacts", "fields_endpoint": "/api/settings/contacts/fields"},
    {"api_name": "sales_accounts", "label": "Accounts", "fields_endpoint": "/api/settings/sales_accounts/fields"},
    {"api_name": "deals", "label": "Deals", "fields_endpoint": "/api/settings/deals/fields"},
    {"api_name": "leads", "label": "Leads", "fields_endpoint": "/api/settings/leads/fields"},
    {"api_name": "tasks", "label": "Tasks", "fields_endpoint": "/api/settings/tasks/fields"},
]


@dataclass
class CRMScanResult:
    provider: str
    account_name: str
    modules: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    folder_path: str


class CRMIntegrationService:
    @staticmethod
    def provider_options() -> list[dict[str, Any]]:
        return SUPPORTED_CRM_PROVIDER_OPTIONS

    @staticmethod
    def _parse_provider(provider: str) -> str:
        normalized = (provider or "").strip().lower()
        if not normalized:
            raise ValueError("CRM provider is required")
        known = {item["value"] for item in SUPPORTED_CRM_PROVIDER_OPTIONS}
        if normalized not in known:
            raise ValueError("Unsupported CRM provider")
        return normalized

    @staticmethod
    async def _request_json(
        method: str,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any] | None = None,
        allow_404: bool = False,
        form_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if payload is not None and form_payload is not None:
            raise RuntimeError("Only one payload type may be used for CRM requests")

        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
        elif form_payload is not None:
            body = urllib_parse.urlencode(form_payload).encode("utf-8")
        else:
            body = None

        def _work() -> dict[str, Any] | None:
            req = urllib_request.Request(url=url, data=body, method=method.upper())
            for key, value in headers.items():
                req.add_header(key, value)
            if payload is not None:
                req.add_header("Content-Type", "application/json")
            if form_payload is not None:
                req.add_header("Content-Type", "application/x-www-form-urlencoded")

            try:
                with urllib_request.urlopen(req, timeout=25) as response:
                    raw = response.read().decode("utf-8")
            except urllib_error.HTTPError as exc:
                if allow_404 and exc.code == 404:
                    return None
                try:
                    detail = exc.read().decode("utf-8")
                except Exception:
                    detail = str(exc)
                raise RuntimeError(f"CRM API request failed ({exc.code}) for {url}: {detail}") from exc
            except urllib_error.URLError as exc:
                raise RuntimeError(f"CRM API request failed for {url}: {exc.reason}") from exc

            if not raw:
                return {}
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"CRM API returned invalid JSON for {url}") from exc

        return await asyncio.to_thread(_work)

    @staticmethod
    def _folder_path(provider: str) -> str:
        return f"integrations/crm/{provider}"

    @staticmethod
    async def scan_schema(provider: str, credentials: dict[str, Any]) -> CRMScanResult:
        provider = CRMIntegrationService._parse_provider(provider)
        if provider == "zoho":
            hydrated_credentials = await CRMIntegrationService._hydrate_zoho_credentials(credentials)
            return await CRMIntegrationService._scan_zoho(hydrated_credentials)
        if provider == "freshworks":
            return await CRMIntegrationService._scan_freshworks(credentials)
        raise RuntimeError(f"{provider} CRM scanning is not available.")

    @staticmethod
    async def store_connection_secret(
        tenant_res: TenantResources,
        provider: str,
        credentials: dict[str, Any],
    ) -> str:
        secret_refs = dict(tenant_res.secret_refs or {})
        crm_secret_ref = (secret_refs.get("crm_connection") or {}).get("secret_name")
        if not crm_secret_ref:
            crm_secret_ref = AzureKeyVaultService._secret_name(tenant_res.tenant_id, "crm-connection")
            secret_refs["crm_connection"] = {
                "secret_name": crm_secret_ref,
                **AzureKeyVaultService._rotation_metadata(),
            }
            tenant_res.secret_refs = secret_refs

        await AzureKeyVaultService.set_secret_value(
            crm_secret_ref,
            json.dumps({"provider": provider, "credentials": credentials}),
            tenant_res.tenant_id,
            "crm_connection",
        )
        return crm_secret_ref

    @staticmethod
    async def load_connection_secret(tenant_res: TenantResources) -> tuple[str, dict[str, Any]]:
        secret_refs = tenant_res.secret_refs or {}
        crm_secret_name = (secret_refs.get("crm_connection") or {}).get("secret_name")
        if not crm_secret_name:
            raise RuntimeError("CRM connection secret is not configured for this organization")
        raw_secret = await AzureKeyVaultService.get_secret_value(crm_secret_name)
        if not raw_secret:
            raise RuntimeError("CRM connection secret could not be loaded from Key Vault")
        try:
            payload = json.loads(raw_secret)
        except json.JSONDecodeError as exc:
            raise RuntimeError("CRM connection secret is malformed") from exc
        provider = CRMIntegrationService._parse_provider(payload.get("provider") or "")
        credentials = payload.get("credentials") or {}
        if not credentials:
            raise RuntimeError("CRM connection secret is incomplete")
        return provider, credentials

    @staticmethod
    async def index_schema_embeddings(
        tenant_res: TenantResources,
        scan_result: CRMScanResult,
    ) -> dict[str, Any]:
        points = []
        indexed_paths: list[str] = []

        records = []
        for module in scan_result.modules:
            module_api_name = module.get("api_name") or module.get("name") or "module"
            schema_text = CRMIntegrationService._crm_module_schema_text(scan_result, module)
            schema_folder = f"{scan_result.folder_path}/schema/{module_api_name}"
            indexed_paths.append(schema_folder)
            records.append({
                "point_type": "crm_schema",
                "unique_key": f"schema:{module_api_name}",
                "text": schema_text,
                "payload": {
                    "source_type": "crm_schema",
                    "integration_type": "crm",
                    "provider": scan_result.provider,
                    "module": module_api_name,
                    "folder_path": schema_folder,
                    "fields": module.get("fields", []),
                }
            })

        for action in scan_result.actions:
            module_api_name = action.get("module") or "module"
            action_name = action.get("name") or "action"
            action_text = CRMIntegrationService._crm_action_text(scan_result, action)
            action_folder = f"{scan_result.folder_path}/actions/{module_api_name}"
            indexed_paths.append(action_folder)
            records.append({
                "point_type": "crm_action",
                "unique_key": f"action:{module_api_name}:{action_name}",
                "text": action_text,
                "payload": {
                    "source_type": "crm_action",
                    "integration_type": "crm",
                    "provider": scan_result.provider,
                    "module": module_api_name,
                    "action": action_name,
                    "method": action.get("method"),
                    "endpoint": action.get("endpoint"),
                    "folder_path": action_folder,
                }
            })

        texts = [record["text"] for record in records]
        vectors = await TextEmbeddingService.embed_texts(texts)
        
        for index, record in enumerate(records):
            points.append(
                CRMIntegrationService._build_point(
                    tenant_res=tenant_res,
                    provider=scan_result.provider,
                    point_type=record["point_type"],
                    unique_key=record["unique_key"],
                    text=record["text"],
                    vector=vectors[index],
                    payload=record["payload"],
                )
            )

        await QdrantService.delete_points_by_filter(
            tenant_res,
            {"integration_type": "crm"},
        )
        if points:
            await QdrantService.upsert_points(tenant_res, points)

        return {
            "indexed_points": len(points),
            "module_count": len(scan_result.modules),
            "action_count": len(scan_result.actions),
            "folder_path": scan_result.folder_path,
            "indexed_paths": sorted(set(indexed_paths)),
        }

    @staticmethod
    def _build_point(
        tenant_res: TenantResources,
        provider: str,
        point_type: str,
        unique_key: str,
        text: str,
        vector: list[float],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        point_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{tenant_res.tenant_id}:{provider}:{point_type}:{unique_key}",
            )
        )
        return {
            "id": point_id,
            "vector": vector,
            "payload": {
                **payload,
                "text": text,
            },
        }

    @staticmethod
    def _crm_module_schema_text(scan_result: CRMScanResult, module: dict[str, Any]) -> str:
        lines = [
            f"provider: {scan_result.provider}",
            f"account: {scan_result.account_name}",
            f"module: {module.get('label') or module.get('api_name')}",
            f"api_name: {module.get('api_name')}",
            "fields:",
        ]
        for field in module.get("fields", []):
            lines.append(
                f"- {field.get('name')} | label={field.get('label')} | type={field.get('type')} | required={field.get('required')}"
            )
        return "\n".join(lines)

    @staticmethod
    def _crm_action_text(scan_result: CRMScanResult, action: dict[str, Any]) -> str:
        return "\n".join(
            [
                f"provider: {scan_result.provider}",
                f"account: {scan_result.account_name}",
                f"module: {action.get('module')}",
                f"action: {action.get('name')}",
                f"method: {action.get('method')}",
                f"endpoint: {action.get('endpoint')}",
                f"description: {action.get('description')}",
            ]
        )

    @staticmethod
    def _normalize_zoho_api_domain(api_domain: str | None) -> str:
        domain = (api_domain or "https://www.zohoapis.com").strip().rstrip("/")
        if not domain.startswith("http://") and not domain.startswith("https://"):
            domain = f"https://{domain}"
        return domain

    @staticmethod
    def _is_zoho_unsupported_module_error(exc: Exception) -> bool:
        message = str(exc)
        return '"code":"NOT_SUPPORTED"' in message or "the given module is not supported in api" in message

    @staticmethod
    async def exchange_zoho_authorization_code(code: str) -> dict[str, Any]:
        if not settings.ZOHO_CLIENT_ID or not settings.ZOHO_CLIENT_SECRET or not settings.ZOHO_REDIRECT_URI:
            raise RuntimeError("Zoho OAuth is not configured on the server")

        print(
            "[Zoho OAuth] exchanging authorization code",
            {
                "accounts_url": settings.ZOHO_ACCOUNTS_URL,
                "redirect_uri": settings.ZOHO_REDIRECT_URI,
                "client_id_prefix": settings.ZOHO_CLIENT_ID[:12],
                "code_prefix": code[:18],
            },
        )
        token_payload = await CRMIntegrationService._request_json(
            "POST",
            f"{settings.ZOHO_ACCOUNTS_URL.rstrip('/')}/oauth/v2/token",
            headers={},
            form_payload={
                "grant_type": "authorization_code",
                "client_id": settings.ZOHO_CLIENT_ID,
                "client_secret": settings.ZOHO_CLIENT_SECRET,
                "redirect_uri": settings.ZOHO_REDIRECT_URI,
                "code": code,
            },
        )
        token_payload = token_payload or {}
        print(
            "[Zoho OAuth] token exchange response",
            {
                "keys": sorted(token_payload.keys()),
                "api_domain": token_payload.get("api_domain"),
                "has_access_token": bool(token_payload.get("access_token")),
                "has_refresh_token": bool(token_payload.get("refresh_token")),
                "scope": token_payload.get("scope"),
                "error": token_payload.get("error"),
            },
        )
        if token_payload.get("error"):
            detail = token_payload.get("error_description") or token_payload.get("error")
            raise RuntimeError(f"Zoho OAuth token exchange failed: {detail}")
        if not token_payload.get("access_token"):
            raise RuntimeError("Zoho OAuth token exchange did not return an access token")
        return token_payload

    @staticmethod
    async def _hydrate_zoho_credentials(credentials: dict[str, Any]) -> dict[str, Any]:
        access_token = (credentials.get("access_token") or "").strip()
        refresh_token = (credentials.get("refresh_token") or "").strip()
        if not refresh_token:
            if access_token:
                credentials["api_domain"] = CRMIntegrationService._normalize_zoho_api_domain(credentials.get("api_domain"))
                return credentials
            raise RuntimeError("Zoho access token or refresh token is required")

        # Zoho access tokens are short-lived. Refresh whenever possible so later
        # Desk operations do not reuse an expired token saved during CRM OAuth.
        token_payload = await CRMIntegrationService._request_json(
            "POST",
            f"{settings.ZOHO_ACCOUNTS_URL.rstrip('/')}/oauth/v2/token",
            headers={},
            form_payload={
                "grant_type": "refresh_token",
                "client_id": settings.ZOHO_CLIENT_ID,
                "client_secret": settings.ZOHO_CLIENT_SECRET,
                "refresh_token": refresh_token,
            },
        )
        token_payload = token_payload or {}
        print(
            "[Zoho OAuth] refresh token response",
            {
                "keys": sorted(token_payload.keys()),
                "api_domain": token_payload.get("api_domain"),
                "has_access_token": bool(token_payload.get("access_token")),
                "scope": token_payload.get("scope"),
                "error": token_payload.get("error"),
            },
        )
        if token_payload.get("error"):
            detail = token_payload.get("error_description") or token_payload.get("error")
            raise RuntimeError(f"Zoho token refresh failed: {detail}")
        if not token_payload.get("access_token"):
            raise RuntimeError("Zoho token refresh did not return an access token")

        refreshed_credentials = dict(credentials)
        refreshed_credentials["access_token"] = token_payload["access_token"]
        refreshed_credentials["api_domain"] = CRMIntegrationService._normalize_zoho_api_domain(
            token_payload.get("api_domain") or credentials.get("api_domain")
        )
        return refreshed_credentials

    @staticmethod
    async def _scan_zoho(credentials: dict[str, Any]) -> CRMScanResult:
        access_token = (credentials.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError("Zoho access token is required")
        api_domain = CRMIntegrationService._normalize_zoho_api_domain(credentials.get("api_domain"))
        headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}

        modules_payload = await CRMIntegrationService._request_json(
            "GET",
            f"{api_domain}/crm/v8/settings/modules",
            headers=headers,
        )
        modules_data = list((modules_payload or {}).get("modules") or [])
        if not modules_data:
            raise RuntimeError("Zoho CRM did not return any modules")

        modules: list[dict[str, Any]] = []
        actions: list[dict[str, Any]] = []
        for module_item in modules_data:
            api_name = module_item.get("api_name")
            if not api_name:
                continue

            try:
                metadata_payload = await CRMIntegrationService._request_json(
                    "GET",
                    f"{api_domain}/crm/v8/settings/modules/{api_name}",
                    headers=headers,
                )
            except Exception as exc:
                if CRMIntegrationService._is_zoho_unsupported_module_error(exc):
                    print("[Zoho OAuth] skipping unsupported module", {"api_name": api_name, "error": str(exc)})
                    continue
                raise
            metadata = ((metadata_payload or {}).get("modules") or [module_item])[0]
            fields = []
            for field in metadata.get("fields", []) or []:
                fields.append(
                    {
                        "name": field.get("api_name") or field.get("field_label") or field.get("display_label"),
                        "label": field.get("field_label") or field.get("display_label") or field.get("api_name"),
                        "type": field.get("data_type") or "unknown",
                        "required": bool(field.get("required")),
                    }
                )

            modules.append(
                {
                    "api_name": api_name,
                    "label": metadata.get("plural_label") or metadata.get("singular_label") or api_name,
                    "fields": fields,
                }
            )
            actions.extend(CRMIntegrationService._zoho_actions_for_module(api_name))

        if not modules:
            raise RuntimeError("Zoho CRM did not return any supported modules after filtering unsupported modules")

        return CRMScanResult(
            provider="zoho",
            account_name=api_domain.replace("https://", "").replace("http://", ""),
            modules=modules,
            actions=actions,
            folder_path=CRMIntegrationService._folder_path("zoho"),
        )

    @staticmethod
    def _zoho_actions_for_module(api_name: str) -> list[dict[str, Any]]:
        return [
            {
                "module": api_name,
                "name": "metadata_scan",
                "method": "GET",
                "endpoint": f"/crm/v8/settings/modules/{api_name}",
                "description": "Scan module metadata and field definitions.",
            },
            {
                "module": api_name,
                "name": "search_by_phone_or_email",
                "method": "GET",
                "endpoint": f"/crm/v8/{api_name}/search",
                "description": "Search Contacts or Leads by phone number or email.",
            },
            {
                "module": api_name,
                "name": "get_customer_full_context",
                "method": "POST",
                "endpoint": "/crm/v8/coql",
                "description": "Fetch advanced customer context with COQL across related modules.",
            },
            {
                "module": api_name,
                "name": "list_records",
                "method": "GET",
                "endpoint": f"/crm/v8/{api_name}",
                "description": "Retrieve records from the module.",
            },
            {
                "module": api_name,
                "name": "create_or_update_record",
                "method": "POST",
                "endpoint": f"/crm/v8/{api_name}",
                "description": "Create or update a Contact or Lead record.",
            },
            {
                "module": api_name,
                "name": "update_status",
                "method": "PUT",
                "endpoint": f"/crm/v8/{api_name}/{{record_id}}",
                "description": "Update Lead or Deal status.",
            },
            {
                "module": "Calls",
                "name": "create_call_record",
                "method": "POST",
                "endpoint": "/crm/v8/Calls",
                "description": "Create a call record in Zoho CRM.",
            },
            {
                "module": "Notes",
                "name": "add_note_or_call_summary",
                "method": "POST",
                "endpoint": "/crm/v8/Notes",
                "description": "Attach a note or call summary to the customer record.",
            },
            {
                "module": "Tickets",
                "name": "create_zoho_desk_ticket",
                "method": "POST",
                "endpoint": "/api/v1/tickets",
                "description": "Create a ticket in Zoho Desk.",
            },
            {
                "module": "Tickets",
                "name": "update_zoho_desk_ticket",
                "method": "PATCH",
                "endpoint": "/api/v1/tickets/{ticket_id}",
                "description": "Update a Zoho Desk ticket status or details.",
            },
        ]

    @staticmethod
    def _normalize_freshworks_account_url(account_url: str | None) -> str:
        base = (account_url or "").strip().rstrip("/")
        if not base:
            raise RuntimeError("Freshworks account URL is required")
        if not base.startswith("http://") and not base.startswith("https://"):
            base = f"https://{base}"
        if not base.endswith("/crm/sales"):
            base = f"{base}/crm/sales"
        return base

    @staticmethod
    async def _scan_freshworks(credentials: dict[str, Any]) -> CRMScanResult:
        access_token = (credentials.get("access_token") or "").strip()
        if not access_token:
            raise RuntimeError("Freshworks API token is required")

        account_url = CRMIntegrationService._normalize_freshworks_account_url(credentials.get("account_url"))
        headers = {"Authorization": f"Token token={access_token}"}

        modules: list[dict[str, Any]] = []
        actions: list[dict[str, Any]] = []
        for candidate in FRESHWORKS_MODULE_CANDIDATES:
            payload = await CRMIntegrationService._request_json(
                "GET",
                f"{account_url}{candidate['fields_endpoint']}",
                headers=headers,
                allow_404=True,
            )
            if payload is None:
                continue
            fields_data = list(payload.get("fields") or [])
            fields = []
            for field in fields_data:
                fields.append(
                    {
                        "name": field.get("name") or field.get("api_name"),
                        "label": field.get("label") or field.get("display_name") or field.get("name"),
                        "type": field.get("type") or field.get("field_type") or "unknown",
                        "required": bool(field.get("required") or field.get("mandatory")),
                    }
                )

            modules.append(
                {
                    "api_name": candidate["api_name"],
                    "label": candidate["label"],
                    "fields": fields,
                }
            )
            actions.extend(CRMIntegrationService._freshworks_actions_for_module(candidate["api_name"]))

        if not modules:
            raise RuntimeError("Freshworks CRM did not return any supported modules")

        return CRMScanResult(
            provider="freshworks",
            account_name=account_url.replace("https://", "").replace("http://", ""),
            modules=modules,
            actions=actions,
            folder_path=CRMIntegrationService._folder_path("freshworks"),
        )

    @staticmethod
    def _freshworks_actions_for_module(api_name: str) -> list[dict[str, Any]]:
        return [
            {
                "module": api_name,
                "name": "list_records",
                "method": "GET",
                "endpoint": f"/crm/sales/api/{api_name}/view/{{view_id}}",
                "description": "List records in a specific view for the module.",
            },
            {
                "module": api_name,
                "name": "view_record",
                "method": "GET",
                "endpoint": f"/crm/sales/api/{api_name}/{{id}}",
                "description": "View a record in the module.",
            },
            {
                "module": api_name,
                "name": "create_record",
                "method": "POST",
                "endpoint": f"/crm/sales/api/{api_name}",
                "description": "Create a new record in the module.",
            },
            {
                "module": api_name,
                "name": "update_record",
                "method": "PUT",
                "endpoint": f"/crm/sales/api/{api_name}/{{id}}",
                "description": "Update an existing record in the module.",
            },
        ]
