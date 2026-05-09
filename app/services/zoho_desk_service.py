from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse
import uuid

from app.models.tenant_resources import TenantResources
from app.services.crm_integration_service import CRMIntegrationService
from app.services.qdrant_service import QdrantService
from app.services.text_embedding_service import TextEmbeddingService


@dataclass
class ZohoDeskScanResult:
    account_name: str
    org_id: str | None
    modules: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    folder_path: str


class ZohoDeskService:
    @staticmethod
    def _folder_path() -> str:
        return "integrations/zoho-desk"

    @staticmethod
    def _desk_base_url(api_domain: str | None) -> str:
        normalized = CRMIntegrationService._normalize_zoho_api_domain(api_domain)
        parsed = urlparse(normalized)
        scheme = parsed.scheme or "https"
        host = parsed.netloc or parsed.path
        if "zohoapis." in host:
            suffix = host.split("zohoapis", 1)[1]
            return f"{scheme}://desk.zoho{suffix}"
        return f"{scheme}://desk.zoho.com"

    @staticmethod
    async def _desk_request_json(
        method: str,
        url: str,
        access_token: str,
        org_id: str | None = None,
        payload: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> dict[str, Any] | None:
        headers = {"Authorization": f"Zoho-oauthtoken {access_token}"}
        if org_id:
            headers["orgId"] = str(org_id)
        try:
            return await CRMIntegrationService._request_json(
                method=method,
                url=url,
                headers=headers,
                payload=payload,
                allow_404=allow_404,
            )
        except RuntimeError as exc:
            message = str(exc)
            if "INVALID_OAUTH" in message or "SCOPE_MISMATCH" in message:
                raise RuntimeError(
                    "Zoho Desk rejected the OAuth token. Reconnect Zoho CRM through OAuth so the saved refresh token "
                    "includes Desk.tickets.ALL, Desk.basic.READ, and Desk.settings.READ scopes. If you already "
                    "reconnected, revoke this app in Zoho Accounts first, then connect again."
                ) from exc
            raise

    @staticmethod
    async def _hydrate_credentials(credentials: dict[str, Any], refresh: bool = True) -> dict[str, Any]:
        if refresh:
            return await CRMIntegrationService._hydrate_zoho_credentials(dict(credentials))
        hydrated = dict(credentials)
        hydrated["api_domain"] = CRMIntegrationService._normalize_zoho_api_domain(hydrated.get("api_domain"))
        return hydrated

    @staticmethod
    async def _get_organization_binding(credentials: dict[str, Any], refresh: bool = True) -> dict[str, Any]:
        hydrated = await ZohoDeskService._hydrate_credentials(credentials, refresh=refresh)
        base_url = ZohoDeskService._desk_base_url(hydrated.get("api_domain"))
        access_token = hydrated["access_token"]
        payload = await ZohoDeskService._desk_request_json(
            "GET",
            f"{base_url}/api/v1/accessibleOrganizations",
            access_token=access_token,
        )
        organizations = list((payload or {}).get("data") or [])
        if not organizations:
            raise RuntimeError("Zoho Desk did not return any accessible organizations")
        default_org = next((org for org in organizations if str(org.get("isDefault")).lower() == "true"), None)
        return default_org or organizations[0]

    @staticmethod
    async def connect_and_scan(credentials: dict[str, Any]) -> tuple[dict[str, Any], ZohoDeskScanResult]:
        hydrated = await ZohoDeskService._hydrate_credentials(credentials)
        base_url = ZohoDeskService._desk_base_url(hydrated.get("api_domain"))
        access_token = hydrated["access_token"]

        modules_payload = await ZohoDeskService._desk_request_json(
            "GET",
            f"{base_url}/api/v1/myAccessibleModules",
            access_token=access_token,
        )
        modules_data = list((modules_payload or {}).get("modules") or (modules_payload or {}).get("data") or [])

        try:
            departments_payload = await ZohoDeskService._desk_request_json(
                "GET",
                f"{base_url}/api/v1/departments",
                access_token=access_token,
                allow_404=True,
            )
        except RuntimeError as exc:
            print("[Zoho Desk] skipping departments scan", {"error": str(exc)})
            departments_payload = None
        departments_data = list((departments_payload or {}).get("data") or [])

        modules = [
            {
                "api_name": module.get("apiName") or module.get("apiKey") or module.get("id"),
                "label": module.get("displayLabel") or module.get("pluralLabel") or module.get("singularLabel"),
                "plural_label": module.get("pluralLabel"),
                "singular_label": module.get("singularLabel"),
                "is_custom_module": bool(module.get("isCustomModule")),
                "name_field": module.get("nameField"),
            }
            for module in modules_data
        ]
        if departments_data:
            modules.append(
                {
                    "api_name": "departments",
                    "label": "Departments",
                    "plural_label": "Departments",
                    "singular_label": "Department",
                    "is_custom_module": False,
                    "name_field": "departmentName",
                    "records": [
                        {"id": str(item.get("id")), "name": item.get("name") or item.get("departmentName")}
                        for item in departments_data
                    ],
                }
            )

        actions = [
            {
                "module": "tickets",
                "name": "create_ticket",
                "method": "POST",
                "endpoint": "/api/v1/tickets",
                "description": "Create a Zoho Desk ticket.",
            },
            {
                "module": "tickets",
                "name": "update_ticket",
                "method": "PATCH",
                "endpoint": "/api/v1/tickets/{ticket_id}",
                "description": "Update a Zoho Desk ticket status or fields.",
            },
            {
                "module": "tickets",
                "name": "get_ticket",
                "method": "GET",
                "endpoint": "/api/v1/tickets/{ticket_id}",
                "description": "Fetch a Zoho Desk ticket with its latest state.",
            },
            {
                "module": "tickets",
                "name": "list_tickets",
                "method": "GET",
                "endpoint": "/api/v1/tickets",
                "description": "List tickets from the bound Zoho Desk organization.",
            },
        ]

        return hydrated, ZohoDeskScanResult(
            account_name=base_url.replace("https://", "").replace("http://", ""),
            org_id=None,
            modules=modules,
            actions=actions,
            folder_path=ZohoDeskService._folder_path(),
        )

    @staticmethod
    async def index_desk_embeddings(tenant_res: TenantResources, scan_result: ZohoDeskScanResult) -> dict[str, Any]:
        points: list[dict[str, Any]] = []
        indexed_paths: list[str] = []

        records = []
        for module in scan_result.modules:
            module_name = module.get("api_name") or "module"
            folder_path = f"{scan_result.folder_path}/schema/{module_name}"
            indexed_paths.append(folder_path)
            lines = [
                "provider: zoho_desk",
                f"account: {scan_result.account_name}",
                f"org_id: {scan_result.org_id}",
                f"module: {module.get('label') or module_name}",
                f"api_name: {module_name}",
                f"name_field: {module.get('name_field')}",
                f"is_custom_module: {module.get('is_custom_module')}",
            ]
            for record in module.get("records", []) or []:
                lines.append(f"record: {record.get('id')} | {record.get('name')}")
            text = "\n".join(lines)
            records.append({
                "point_type": "schema",
                "unique_key": module_name,
                "text": text,
                "payload": {
                    "source_type": "zoho_desk_schema",
                    "integration_type": "zoho_desk",
                    "provider": "zoho",
                    "folder_path": folder_path,
                    "module": module_name,
                    "label": module.get("label"),
                }
            })

        for action in scan_result.actions:
            action_name = action.get("name") or "action"
            module_name = action.get("module") or "module"
            folder_path = f"{scan_result.folder_path}/actions/{module_name}"
            indexed_paths.append(folder_path)
            text = "\n".join(
                [
                    "provider: zoho_desk",
                    f"account: {scan_result.account_name}",
                    f"org_id: {scan_result.org_id}",
                    f"module: {module_name}",
                    f"action: {action_name}",
                    f"method: {action.get('method')}",
                    f"endpoint: {action.get('endpoint')}",
                    f"description: {action.get('description')}",
                ]
            )
            records.append({
                "point_type": "action",
                "unique_key": f"{module_name}:{action_name}",
                "text": text,
                "payload": {
                    "source_type": "zoho_desk_action",
                    "integration_type": "zoho_desk",
                    "provider": "zoho",
                    "folder_path": folder_path,
                    "module": module_name,
                    "action": action_name,
                    "method": action.get("method"),
                    "endpoint": action.get("endpoint"),
                }
            })

        texts = [record["text"] for record in records]
        vectors = await TextEmbeddingService.embed_texts(texts)

        for index, record in enumerate(records):
            points.append(
                ZohoDeskService._build_point(
                    tenant_res,
                    record["point_type"],
                    record["unique_key"],
                    record["text"],
                    vectors[index],
                    record["payload"],
                )
            )

        await QdrantService.delete_points_by_filter(
            tenant_res,
            {"integration_type": "zoho_desk"},
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
        point_type: str,
        unique_key: str,
        text: str,
        vector: list[float],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        point_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{tenant_res.tenant_id}:zoho_desk:{point_type}:{unique_key}",
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
    async def create_ticket(credentials: dict[str, Any], org_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        hydrated = await ZohoDeskService._hydrate_credentials(credentials)
        base_url = ZohoDeskService._desk_base_url(hydrated.get("api_domain"))
        return (
            await ZohoDeskService._desk_request_json(
                "POST",
                f"{base_url}/api/v1/tickets",
                access_token=hydrated["access_token"],
                org_id=org_id,
                payload=payload,
            )
            or {}
        )

    @staticmethod
    async def update_ticket(
        credentials: dict[str, Any],
        org_id: str | None,
        ticket_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        hydrated = await ZohoDeskService._hydrate_credentials(credentials)
        base_url = ZohoDeskService._desk_base_url(hydrated.get("api_domain"))
        return (
            await ZohoDeskService._desk_request_json(
                "PATCH",
                f"{base_url}/api/v1/tickets/{ticket_id}",
                access_token=hydrated["access_token"],
                org_id=org_id,
                payload=payload,
            )
            or {}
        )
