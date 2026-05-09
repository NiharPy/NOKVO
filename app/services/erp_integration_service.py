from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse
import uuid
import xml.etree.ElementTree as ET

from app.models.tenant_resources import TenantResources
from app.services.azure_keyvault_service import AzureKeyVaultService
from app.services.qdrant_service import QdrantService
from app.services.text_embedding_service import TextEmbeddingService


SUPPORTED_ERP_PROVIDER_OPTIONS = [
    {"value": "tally", "label": "TallyPrime / Tally ERP", "group": "erp", "supported": True},
]

TALLY_COLLECTIONS = [
    {
        "key": "companies",
        "label": "Companies",
        "type": "Company",
        "fields": ["Name", "GUID", "StartingFrom", "BooksFrom"],
    },
    {
        "key": "groups",
        "label": "Accounting Groups",
        "type": "Group",
        "fields": ["Name", "Parent", "PrimaryGroup", "GUID"],
    },
    {
        "key": "ledgers",
        "label": "Ledgers",
        "type": "Ledger",
        "fields": ["Name", "Parent", "OpeningBalance", "ClosingBalance", "GSTRegistrationType", "PartyGSTIN", "GUID"],
    },
    {
        "key": "stock_groups",
        "label": "Stock Groups",
        "type": "StockGroup",
        "tdl_type": "Stock Group",
        "fields": ["Name", "Parent", "GUID"],
    },
    {
        "key": "stock_items",
        "label": "Stock Items",
        "type": "StockItem",
        "tdl_type": "Stock Item",
        "fields": ["Name", "Parent", "BaseUnits", "OpeningBalance", "ClosingBalance", "GUID"],
    },
    {
        "key": "units",
        "label": "Units of Measure",
        "type": "Unit",
        "fields": ["Name", "OriginalName", "IsSimpleUnit", "GUID"],
    },
    {
        "key": "cost_centres",
        "label": "Cost Centres",
        "type": "CostCentre",
        "tdl_type": "Cost Centre",
        "fields": ["Name", "Parent", "Category", "GUID"],
    },
    {
        "key": "voucher_types",
        "label": "Voucher Types",
        "type": "VoucherType",
        "tdl_type": "Voucher Type",
        "fields": ["Name", "Parent", "NumberingMethod", "GUID"],
    },
    {
        "key": "vouchers",
        "label": "Vouchers",
        "type": "Voucher",
        "fields": ["Date", "VoucherTypeName", "VoucherNumber", "PartyLedgerName", "Amount", "GUID"],
    },
]


@dataclass
class ERPScanResult:
    provider: str
    account_name: str
    modules: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    folder_path: str


class ERPIntegrationService:
    @staticmethod
    def provider_options() -> list[dict[str, Any]]:
        return SUPPORTED_ERP_PROVIDER_OPTIONS

    @staticmethod
    def _parse_provider(provider: str) -> str:
        normalized = (provider or "").strip().lower()
        if not normalized:
            raise ValueError("ERP provider is required")
        known = {item["value"] for item in SUPPORTED_ERP_PROVIDER_OPTIONS}
        if normalized not in known:
            raise ValueError("Unsupported ERP provider")
        return normalized

    @staticmethod
    def _folder_path(provider: str) -> str:
        return f"integrations/erp/{provider}"

    @staticmethod
    def _normalize_tally_base_url(base_url: str | None) -> str:
        url = (base_url or "http://localhost:9000").strip().rstrip("/")
        if not url:
            raise RuntimeError("Tally URL is required")
        if not url.startswith("http://") and not url.startswith("https://"):
            url = f"http://{url}"
        parsed = urlparse(url)
        if not parsed.hostname:
            raise RuntimeError("Tally URL must include a hostname")
        return url

    @staticmethod
    async def scan_schema(provider: str, credentials: dict[str, Any]) -> ERPScanResult:
        provider = ERPIntegrationService._parse_provider(provider)
        if provider == "tally":
            return await ERPIntegrationService._scan_tally(credentials)
        raise RuntimeError(f"{provider} ERP scanning is not available.")

    @staticmethod
    async def store_connection_secret(
        tenant_res: TenantResources,
        provider: str,
        credentials: dict[str, Any],
    ) -> str:
        secret_refs = dict(tenant_res.secret_refs or {})
        erp_secret_ref = (secret_refs.get("erp_connection") or {}).get("secret_name")
        if not erp_secret_ref:
            erp_secret_ref = AzureKeyVaultService._secret_name(tenant_res.tenant_id, "erp-connection")
            secret_refs["erp_connection"] = {
                "secret_name": erp_secret_ref,
                **AzureKeyVaultService._rotation_metadata(),
            }
            tenant_res.secret_refs = secret_refs

        await AzureKeyVaultService.set_secret_value(
            erp_secret_ref,
            json.dumps({"provider": provider, "credentials": credentials}),
            tenant_res.tenant_id,
            "erp_connection",
        )
        return erp_secret_ref

    @staticmethod
    async def load_connection_secret(tenant_res: TenantResources) -> tuple[str, dict[str, Any]]:
        secret_refs = tenant_res.secret_refs or {}
        erp_secret_name = (secret_refs.get("erp_connection") or {}).get("secret_name")
        if not erp_secret_name:
            raise RuntimeError("ERP connection secret is not configured for this organization")
        raw_secret = await AzureKeyVaultService.get_secret_value(erp_secret_name)
        if not raw_secret:
            raise RuntimeError("ERP connection secret could not be loaded from Key Vault")
        try:
            payload = json.loads(raw_secret)
        except json.JSONDecodeError as exc:
            raise RuntimeError("ERP connection secret is malformed") from exc
        provider = ERPIntegrationService._parse_provider(payload.get("provider") or "")
        credentials = payload.get("credentials") or {}
        if not credentials:
            raise RuntimeError("ERP connection secret is incomplete")
        return provider, credentials

    @staticmethod
    async def _tally_request(
        base_url: str,
        xml_payload: str,
        timeout_seconds: int = 20,
    ) -> str:
        normalized_url = ERPIntegrationService._normalize_tally_base_url(base_url)
        timeout = max(3, min(int(timeout_seconds or 20), 60))

        def _work() -> str:
            request = urllib_request.Request(
                url=normalized_url,
                data=xml_payload.encode("utf-8"),
                method="POST",
                headers={"Content-Type": "text/xml; charset=utf-8"},
            )
            try:
                with urllib_request.urlopen(request, timeout=timeout) as response:
                    return response.read().decode("utf-8", errors="replace")
            except urllib_error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(f"Tally API request failed ({exc.code}) for {normalized_url}: {detail}") from exc
            except urllib_error.URLError as exc:
                raise RuntimeError(
                    f"Tally API request failed for {normalized_url}: {exc.reason}. "
                    "Ensure TallyPrime is running, HTTP server is enabled, and a company is loaded."
                ) from exc

        return await asyncio.to_thread(_work)

    @staticmethod
    def _collection_request_xml(collection: dict[str, Any], company_name: str | None = None) -> str:
        collection_name = f"NOKVO {collection['label']}"
        static_vars = ["<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"]
        if company_name:
            static_vars.append(f"<SVCURRENTCOMPANY>{ERPIntegrationService._xml_escape(company_name)}</SVCURRENTCOMPANY>")
        native_methods = "\n".join(
            f"<NATIVEMETHOD>{ERPIntegrationService._xml_escape(field)}</NATIVEMETHOD>"
            for field in collection["fields"]
        )
        return f"""
<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>{ERPIntegrationService._xml_escape(collection_name)}</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        {''.join(static_vars)}
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="{ERPIntegrationService._xml_escape(collection_name)}" ISMODIFY="No">
            <TYPE>{ERPIntegrationService._xml_escape(collection.get("tdl_type") or collection["type"])}</TYPE>
            {native_methods}
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>
""".strip()

    @staticmethod
    def _xml_escape(value: str) -> str:
        return (
            str(value)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
        )

    @staticmethod
    def _tag_name(element: ET.Element) -> str:
        return element.tag.rsplit("}", 1)[-1].upper()

    @staticmethod
    def _child_text(element: ET.Element, name: str) -> str | None:
        expected = name.upper().replace(" ", "")
        for child in list(element):
            if ERPIntegrationService._tag_name(child).replace(".", "").replace(" ", "") == expected.upper():
                text = "".join(child.itertext()).strip()
                return text or None
        return None

    @staticmethod
    def _parse_tally_collection(xml_response: str, collection: dict[str, Any], max_items: int) -> list[dict[str, Any]]:
        try:
            root = ET.fromstring(xml_response)
        except ET.ParseError as exc:
            raise RuntimeError(f"Tally returned invalid XML for {collection['label']}") from exc

        item_tag = collection["type"].upper()
        items: list[dict[str, Any]] = []
        for element in root.iter():
            if ERPIntegrationService._tag_name(element) != item_tag:
                continue
            record: dict[str, Any] = {}
            name_attr = element.attrib.get("NAME") or element.attrib.get("Name") or element.attrib.get("name")
            if name_attr:
                record["name"] = name_attr
            for field in collection["fields"]:
                normalized_key = field[:1].lower() + field[1:]
                value = ERPIntegrationService._child_text(element, field)
                if value:
                    record[normalized_key] = value
                    if field.lower() == "name":
                        record["name"] = value
            if record:
                items.append(record)
            if len(items) >= max_items:
                break
        return items

    @staticmethod
    async def _scan_tally(credentials: dict[str, Any]) -> ERPScanResult:
        base_url = ERPIntegrationService._normalize_tally_base_url(credentials.get("base_url"))
        company_name = (credentials.get("company_name") or "").strip() or None
        timeout_seconds = int(credentials.get("timeout_seconds") or 20)
        max_items_per_module = max(1, min(int(credentials.get("max_items_per_module") or 25), 100))

        modules: list[dict[str, Any]] = []
        for collection in TALLY_COLLECTIONS:
            try:
                response_xml = await ERPIntegrationService._tally_request(
                    base_url,
                    ERPIntegrationService._collection_request_xml(collection, company_name),
                    timeout_seconds=timeout_seconds,
                )
                records = ERPIntegrationService._parse_tally_collection(response_xml, collection, max_items_per_module)
                status = "available"
                error = None
            except Exception as exc:
                records = []
                status = "unavailable"
                error = str(exc)

            modules.append(
                {
                    "api_name": collection["key"],
                    "label": collection["label"],
                    "object_type": collection["type"],
                    "fields": [
                        {"name": field, "label": field, "type": "text", "required": field == "Name"}
                        for field in collection["fields"]
                    ],
                    "record_count": len(records),
                    "sample_records": records[:10],
                    "status": status,
                    "last_error": error,
                }
            )

        if not any(module["status"] == "available" for module in modules):
            first_error = next((module.get("last_error") for module in modules if module.get("last_error")), None)
            raise RuntimeError(first_error or "Tally did not return any supported ERP collections")

        account_name = company_name or urlparse(base_url).netloc or "Tally"
        return ERPScanResult(
            provider="tally",
            account_name=account_name,
            modules=modules,
            actions=ERPIntegrationService._tally_actions(),
            folder_path=ERPIntegrationService._folder_path("tally"),
        )

    @staticmethod
    def _tally_actions() -> list[dict[str, Any]]:
        return [
            {
                "module": "ledgers",
                "name": "create_ledger",
                "method": "IMPORT",
                "endpoint": "Import Data / All Masters / LEDGER",
                "description": "Create or alter a Tally ledger under a configured accounting group.",
            },
            {
                "module": "stock_items",
                "name": "create_stock_item",
                "method": "IMPORT",
                "endpoint": "Import Data / All Masters / STOCKITEM",
                "description": "Create or alter a stock item, base unit, and inventory grouping.",
            },
            {
                "module": "vouchers",
                "name": "create_sales_voucher",
                "method": "IMPORT",
                "endpoint": "Import Data / Vouchers / VOUCHER",
                "description": "Create a sales voucher with party ledger, inventory, tax, and accounting lines.",
            },
            {
                "module": "vouchers",
                "name": "create_purchase_voucher",
                "method": "IMPORT",
                "endpoint": "Import Data / Vouchers / VOUCHER",
                "description": "Create a purchase voucher with supplier ledger and inventory/accounting allocations.",
            },
            {
                "module": "vouchers",
                "name": "create_receipt_or_payment",
                "method": "IMPORT",
                "endpoint": "Import Data / Vouchers / VOUCHER",
                "description": "Create receipt or payment vouchers for AR/AP settlement.",
            },
            {
                "module": "reports",
                "name": "export_collection",
                "method": "EXPORT",
                "endpoint": "Export Collection",
                "description": "Export Tally collections such as ledgers, groups, stock items, vouchers, and cost centres.",
            },
            {
                "module": "raw_xml",
                "name": "execute_tally_xml",
                "method": "POST",
                "endpoint": "Tally XML HTTP endpoint",
                "description": "Send a controlled Tally XML envelope for advanced import/export operations.",
            },
        ]

    @staticmethod
    async def index_schema_embeddings(
        tenant_res: TenantResources,
        scan_result: ERPScanResult,
    ) -> dict[str, Any]:
        points: list[dict[str, Any]] = []
        indexed_paths: list[str] = []

        records = []
        for module in scan_result.modules:
            module_name = module.get("api_name") or "module"
            folder_path = f"{scan_result.folder_path}/schema/{module_name}"
            indexed_paths.append(folder_path)
            fields = ", ".join(field["name"] for field in module.get("fields", []))
            sample_records = json.dumps(module.get("sample_records", []), ensure_ascii=False)[:2500]
            text = "\n".join(
                [
                    f"provider: {scan_result.provider}",
                    f"account: {scan_result.account_name}",
                    f"module: {module.get('label') or module_name}",
                    f"api_name: {module_name}",
                    f"object_type: {module.get('object_type')}",
                    f"status: {module.get('status')}",
                    f"fields: {fields}",
                    f"sample_records: {sample_records}",
                ]
            )
            records.append({
                "point_type": "erp_schema",
                "unique_key": f"schema:{module_name}",
                "text": text,
                "payload": {
                    "source_type": "erp_schema",
                    "integration_type": "erp",
                    "provider": scan_result.provider,
                    "folder_path": folder_path,
                    "module": module_name,
                    "label": module.get("label"),
                    "object_type": module.get("object_type"),
                }
            })

        for action in scan_result.actions:
            action_name = action.get("name") or "action"
            module_name = action.get("module") or "module"
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
                "point_type": "erp_action",
                "unique_key": f"action:{module_name}:{action_name}",
                "text": text,
                "payload": {
                    "source_type": "erp_action",
                    "integration_type": "erp",
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
                ERPIntegrationService._build_point(
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
            {"integration_type": "erp"},
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
        return {
            "id": point_id,
            "vector": vector,
            "payload": {
                **payload,
                "text": text,
            },
        }

    @staticmethod
    async def execute_tally_xml(credentials: dict[str, Any], xml_payload: str) -> str:
        base_url = ERPIntegrationService._normalize_tally_base_url(credentials.get("base_url"))
        timeout_seconds = int(credentials.get("timeout_seconds") or 20)
        return await ERPIntegrationService._tally_request(base_url, xml_payload, timeout_seconds=timeout_seconds)
