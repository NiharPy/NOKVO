from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request
import uuid

from app.core.url_guard import UnsafeURLError, assert_safe_url
from app.models.tenant_resources import TenantResources
from app.services.azure_keyvault_service import AzureKeyVaultService
from app.services.qdrant_service import QdrantService
from app.services.text_embedding_service import TextEmbeddingService


SUPPORTED_SHIPPING_PROVIDER_OPTIONS = [
    {"value": "shiprocket", "label": "Shiprocket", "group": "shipping", "supported": True},
]


@dataclass
class ShippingScanResult:
    provider: str
    account_name: str
    modules: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    folder_path: str


class ShippingIntegrationService:
    @staticmethod
    def provider_options() -> list[dict[str, Any]]:
        return SUPPORTED_SHIPPING_PROVIDER_OPTIONS

    @staticmethod
    def _parse_provider(provider: str) -> str:
        normalized = (provider or "").strip().lower()
        if not normalized:
            raise ValueError("Shipping provider is required")
        known = {item["value"] for item in SUPPORTED_SHIPPING_PROVIDER_OPTIONS}
        if normalized not in known:
            raise ValueError("Unsupported shipping provider")
        return normalized

    @staticmethod
    def _folder_path(provider: str) -> str:
        return f"integrations/shipping/{provider}"

    @staticmethod
    def _normalize_shiprocket_base_url(base_url: str | None) -> str:
        url = (base_url or "https://apiv2.shiprocket.in/v1/external").strip().rstrip("/")
        if not url.startswith("http://") and not url.startswith("https://"):
            url = f"https://{url}"
        if not url.endswith("/v1/external"):
            url = f"{url}/v1/external" if "apiv2.shiprocket.in" in url else url
        return url

    @staticmethod
    async def _request_json(
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = dict(headers or {})
        if query:
            cleaned_query = {key: value for key, value in query.items() if value is not None and value != ""}
            url = f"{url}?{urllib_parse.urlencode(cleaned_query, doseq=True)}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        try:
            assert_safe_url(url)
        except UnsafeURLError as exc:
            raise RuntimeError(f"Shiprocket API request rejected: {exc}") from exc

        def _work() -> dict[str, Any]:
            request = urllib_request.Request(url=url, data=body, method=method.upper())
            request.add_header("Accept", "application/json")
            if payload is not None:
                request.add_header("Content-Type", "application/json")
            for key, value in headers.items():
                request.add_header(key, value)
            try:
                with urllib_request.urlopen(request, timeout=30) as response:
                    raw = response.read().decode("utf-8", errors="replace")
            except urllib_error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Shiprocket API request failed ({exc.code}) for {url}: {detail}") from exc
            except urllib_error.URLError as exc:
                raise RuntimeError(f"Shiprocket API request failed for {url}: {exc.reason}") from exc
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Shiprocket API returned invalid JSON for {url}") from exc

        return await asyncio.to_thread(_work)

    @staticmethod
    async def _authenticate_shiprocket(credentials: dict[str, Any]) -> dict[str, Any]:
        email = (credentials.get("email") or "").strip()
        password = (credentials.get("password") or "").strip()
        token = (credentials.get("token") or "").strip()
        base_url = ShippingIntegrationService._normalize_shiprocket_base_url(credentials.get("base_url"))
        if token:
            return {**credentials, "base_url": base_url, "token": token}
        if not email or not password:
            raise RuntimeError("Shiprocket API email and password are required")
        payload = await ShippingIntegrationService._request_json(
            "POST",
            f"{base_url}/auth/login",
            payload={"email": email, "password": password},
        )
        token = payload.get("token")
        if not token:
            raise RuntimeError("Shiprocket auth did not return a token")
        return {**credentials, "base_url": base_url, "token": token}

    @staticmethod
    async def scan_schema(provider: str, credentials: dict[str, Any]) -> ShippingScanResult:
        provider = ShippingIntegrationService._parse_provider(provider)
        if provider != "shiprocket":
            raise RuntimeError(f"{provider} shipping scanning is not available.")
        hydrated = await ShippingIntegrationService._authenticate_shiprocket(credentials)
        credentials.update(
            {
                "base_url": hydrated.get("base_url"),
                "token": hydrated.get("token"),
            }
        )
        account_name = hydrated.get("email") or "Shiprocket"
        return ShippingScanResult(
            provider="shiprocket",
            account_name=account_name,
            modules=ShippingIntegrationService._shiprocket_modules(),
            actions=ShippingIntegrationService._shiprocket_actions(),
            folder_path=ShippingIntegrationService._folder_path("shiprocket"),
        )

    @staticmethod
    def _shiprocket_modules() -> list[dict[str, Any]]:
        return [
            {
                "api_name": "auth",
                "label": "API Authentication",
                "fields": ["email", "password", "token"],
                "description": "Generate and use Shiprocket bearer tokens from API user credentials.",
            },
            {
                "api_name": "orders",
                "label": "Orders",
                "fields": ["order_id", "order_date", "billing_customer_name", "order_items", "payment_method", "sub_total"],
                "description": "Create adhoc orders and receive Shiprocket order and shipment identifiers.",
            },
            {
                "api_name": "couriers",
                "label": "Couriers",
                "fields": ["pickup_postcode", "delivery_postcode", "weight", "cod", "courier_id", "shipment_id"],
                "description": "Check courier serviceability, assign AWB, and generate pickup requests.",
            },
            {
                "api_name": "tracking",
                "label": "Tracking",
                "fields": ["order_id", "shipment_id", "awb_code"],
                "description": "Track shipments by order, shipment, or AWB identifiers.",
            },
            {
                "api_name": "returns",
                "label": "Returns",
                "fields": ["order_id", "shipment_id", "reason", "pickup_postcode", "delivery_postcode"],
                "description": "Return workflows can be handled with Shiprocket return/exchange APIs when enabled.",
            },
        ]

    @staticmethod
    def _shiprocket_actions() -> list[dict[str, Any]]:
        return [
            {
                "module": "couriers",
                "name": "check_serviceability",
                "method": "GET",
                "endpoint": "/courier/serviceability/",
                "description": "Check available courier partners, rates, and estimated delivery between pickup and delivery pincodes.",
            },
            {
                "module": "orders",
                "name": "create_adhoc_order",
                "method": "POST",
                "endpoint": "/orders/create/adhoc",
                "description": "Create a Shiprocket adhoc order and shipment from customer, item, payment, and package details.",
            },
            {
                "module": "couriers",
                "name": "assign_awb",
                "method": "POST",
                "endpoint": "/courier/assign/awb",
                "description": "Assign an AWB to a shipment, optionally selecting a courier partner.",
            },
            {
                "module": "couriers",
                "name": "generate_pickup",
                "method": "POST",
                "endpoint": "/courier/generate/pickup",
                "description": "Generate a pickup request for one or more Shiprocket shipments.",
            },
            {
                "module": "tracking",
                "name": "track_by_order_id",
                "method": "GET",
                "endpoint": "/courier/track",
                "description": "Fetch tracking status using a Shiprocket order id.",
            },
            {
                "module": "tracking",
                "name": "track_by_awb",
                "method": "GET",
                "endpoint": "/courier/track/awb/{awb_code}",
                "description": "Fetch tracking status using an AWB code.",
            },
        ]

    @staticmethod
    async def store_connection_secret(
        tenant_res: TenantResources,
        provider: str,
        credentials: dict[str, Any],
    ) -> str:
        secret_refs = dict(tenant_res.secret_refs or {})
        shipping_secret_ref = (secret_refs.get("shipping_connection") or {}).get("secret_name")
        if not shipping_secret_ref:
            shipping_secret_ref = AzureKeyVaultService._secret_name(tenant_res.tenant_id, "shipping-connection")
            secret_refs["shipping_connection"] = {
                "secret_name": shipping_secret_ref,
                **AzureKeyVaultService._rotation_metadata(),
            }
            tenant_res.secret_refs = secret_refs

        await AzureKeyVaultService.set_secret_value(
            shipping_secret_ref,
            json.dumps({"provider": provider, "credentials": credentials}),
            tenant_res.tenant_id,
            "shipping_connection",
        )
        return shipping_secret_ref

    @staticmethod
    async def load_connection_secret(tenant_res: TenantResources) -> tuple[str, dict[str, Any]]:
        secret_refs = tenant_res.secret_refs or {}
        secret_name = (secret_refs.get("shipping_connection") or {}).get("secret_name")
        if not secret_name:
            raise RuntimeError("Shipping connection secret is not configured for this organization")
        raw_secret = await AzureKeyVaultService.get_secret_value(secret_name)
        if not raw_secret:
            raise RuntimeError("Shipping connection secret could not be loaded from Key Vault")
        try:
            payload = json.loads(raw_secret)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Shipping connection secret is malformed") from exc
        provider = ShippingIntegrationService._parse_provider(payload.get("provider") or "")
        credentials = payload.get("credentials") or {}
        if not credentials:
            raise RuntimeError("Shipping connection secret is incomplete")
        return provider, credentials

    @staticmethod
    async def index_schema_embeddings(tenant_res: TenantResources, scan_result: ShippingScanResult) -> dict[str, Any]:
        points: list[dict[str, Any]] = []
        indexed_paths: list[str] = []
        records = []
        for module in scan_result.modules:
            module_name = module["api_name"]
            folder_path = f"{scan_result.folder_path}/schema/{module_name}"
            indexed_paths.append(folder_path)
            text = "\n".join(
                [
                    f"provider: {scan_result.provider}",
                    f"account: {scan_result.account_name}",
                    f"module: {module['label']}",
                    f"api_name: {module_name}",
                    f"fields: {', '.join(module.get('fields', []))}",
                    f"description: {module.get('description')}",
                ]
            )
            records.append({
                "point_type": "shipping_schema",
                "unique_key": f"schema:{module_name}",
                "text": text,
                "payload": {
                    "source_type": "shipping_schema",
                    "integration_type": "shipping",
                    "provider": scan_result.provider,
                    "folder_path": folder_path,
                    "module": module_name,
                }
            })
        for action in scan_result.actions:
            module_name = action["module"]
            action_name = action["name"]
            folder_path = f"{scan_result.folder_path}/actions/{module_name}"
            indexed_paths.append(folder_path)
            text = "\n".join(
                [
                    f"provider: {scan_result.provider}",
                    f"account: {scan_result.account_name}",
                    f"module: {module_name}",
                    f"action: {action_name}",
                    f"method: {action.get('method')}",
                    f"endpoint: {action.get('endpoint')}",
                    f"description: {action.get('description')}",
                ]
            )
            records.append({
                "point_type": "shipping_action",
                "unique_key": f"action:{module_name}:{action_name}",
                "text": text,
                "payload": {
                    "source_type": "shipping_action",
                    "integration_type": "shipping",
                    "provider": scan_result.provider,
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
                ShippingIntegrationService._build_point(
                    tenant_res,
                    scan_result.provider,
                    record["point_type"],
                    record["unique_key"],
                    record["text"],
                    vectors[index],
                    record["payload"],
                )
            )
        await QdrantService.delete_points_by_filter(
            tenant_res,
            {"integration_type": "shipping"},
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
        point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{tenant_res.tenant_id}:{provider}:{point_type}:{unique_key}"))
        return {"id": point_id, "vector": vector, "payload": {**payload, "text": text}}

    @staticmethod
    async def _authorized_request(
        credentials: dict[str, Any],
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        hydrated = await ShippingIntegrationService._authenticate_shiprocket(credentials)
        base_url = ShippingIntegrationService._normalize_shiprocket_base_url(hydrated.get("base_url"))
        token = hydrated["token"]
        return await ShippingIntegrationService._request_json(
            method,
            f"{base_url}{path}",
            headers={"Authorization": f"Bearer {token}"},
            payload=payload,
            query=query,
        )

    @staticmethod
    async def check_serviceability(credentials: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
        return await ShippingIntegrationService._authorized_request(
            credentials,
            "GET",
            "/courier/serviceability/",
            query=params,
        )

    @staticmethod
    async def create_order(credentials: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        return await ShippingIntegrationService._authorized_request(
            credentials,
            "POST",
            "/orders/create/adhoc",
            payload=payload,
        )

    @staticmethod
    async def assign_awb(credentials: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        return await ShippingIntegrationService._authorized_request(
            credentials,
            "POST",
            "/courier/assign/awb",
            payload=payload,
        )

    @staticmethod
    async def generate_pickup(credentials: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        return await ShippingIntegrationService._authorized_request(
            credentials,
            "POST",
            "/courier/generate/pickup",
            payload=payload,
        )

    @staticmethod
    async def track(credentials: dict[str, Any], *, order_id: str | None = None, awb_code: str | None = None) -> dict[str, Any]:
        if awb_code:
            return await ShippingIntegrationService._authorized_request(
                credentials,
                "GET",
                f"/courier/track/awb/{urllib_parse.quote(str(awb_code))}",
            )
        if order_id:
            return await ShippingIntegrationService._authorized_request(
                credentials,
                "GET",
                "/courier/track",
                query={"order_id": order_id},
            )
        raise RuntimeError("Either order_id or awb_code is required for Shiprocket tracking")
