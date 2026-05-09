import re
import uuid
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, Filter, FieldCondition, FilterSelector, MatchAny, MatchValue, PayloadSchemaType, PointStruct, VectorParams
from app.core.config import settings
from app.models.audit import SuperAdminAuditLog
from app.models.tenant_resources import TenantResources
from sqlalchemy.ext.asyncio import AsyncSession

class QdrantService:
    _INDEXED_COLLECTIONS: set[str] = set()
    TOOLKIT_PAYLOAD_INDEX_FIELDS = [
        "organization_id",
        "tenant_id",
        "tenant_integration_id",
        "provider_connection_id",
        "context_snapshot_id",
        "selected_context_snapshot_id",
        "integration_type",
        "provider",
        "status",
        "source_kind",
        "source_type",
        "resource",
        "resource_type",
        "document_id",
        "document_version_id",
        "chunk_id",
        "document_status",
        "approval_status",
        "approved_by",
        "policy_version",
        "language",
        "doc_type",
        "active",
        "topic",
        "sensitivity",
        "source_title",
    ]
    PAYLOAD_INDEX_TYPES = {
        "active": PayloadSchemaType.BOOL,
    }

    @staticmethod
    def cluster_ref() -> str:
        return settings.QDRANT_URL

    @staticmethod
    def _client() -> QdrantClient:
        if settings.QDRANT_URL == ":memory:":
            return QdrantClient(location=":memory:")
        return QdrantClient(url=settings.QDRANT_URL, api_key=settings.QDRANT_API_KEY, timeout=60.0)

    @staticmethod
    def collection_name_for_tenant(tenant_id: str) -> str:
        normalized = re.sub(r"[^a-z0-9_-]", "_", tenant_id.lower())
        return f"{settings.QDRANT_COLLECTION_PREFIX}_{normalized}_knowledge"

    @staticmethod
    def _required_collection(tenant_res: TenantResources) -> str:
        if not tenant_res.qdrant_collection_name:
            raise RuntimeError("Tenant Qdrant collection is not provisioned.")
        return tenant_res.qdrant_collection_name

    @staticmethod
    async def _audit(
        db: AsyncSession | None,
        superadmin_id: uuid.UUID | None,
        action: str,
        tenant_res: TenantResources,
        metadata: dict,
    ) -> None:
        if not db or not superadmin_id:
            return
        db.add(
            SuperAdminAuditLog(
                superadmin_id=superadmin_id,
                action=action,
                risk_level="medium",
                target_type="qdrant_collection",
                target_id=tenant_res.qdrant_collection_name,
                metadata_=metadata,
            )
        )
        await db.commit()

    @staticmethod
    async def provision_collection(tenant_id: str) -> str:
        """
        Creates a Qdrant collection for the tenant.
        """
        collection_name = QdrantService.collection_name_for_tenant(tenant_id)

        try:
            client = QdrantService._client()
            # Check if collection exists
            if not client.collection_exists(collection_name):
                client.create_collection(
                    collection_name=collection_name,
                    vectors_config=VectorParams(
                        size=settings.QDRANT_VECTOR_SIZE,
                        distance=Distance.COSINE,
                    ),
                )
            QdrantService.ensure_payload_indexes(collection_name, client)
            
            return collection_name
        except Exception as e:
            # Re-raise to be caught by the orchestrator
            raise RuntimeError(f"Failed to provision Qdrant collection: {str(e)}")

    @staticmethod
    async def upsert_points(
        tenant_res: TenantResources,
        points: list[dict],
        db: AsyncSession | None = None,
        superadmin_id: uuid.UUID | None = None,
    ) -> None:
        collection_name = QdrantService._required_collection(tenant_res)
        client = QdrantService._client()
        QdrantService.ensure_payload_indexes(collection_name, client)
        point_structs = []
        for point in points:
            payload = dict(point.get("payload", {}) or {})
            payload = QdrantService._scope_payload(tenant_res, payload)
            if payload.get("context_snapshot_id") and not payload.get("selected_context_snapshot_id"):
                payload["selected_context_snapshot_id"] = payload["context_snapshot_id"]
            if payload.get("selected_context_snapshot_id") and not payload.get("context_snapshot_id"):
                payload["context_snapshot_id"] = payload["selected_context_snapshot_id"]
            payload.setdefault("status", "active")
            payload.setdefault("embedding_provider", "openai")
            payload.setdefault("embedding_model", settings.OPENAI_EMBEDDING_MODEL)
            point_structs.append(
                PointStruct(
                    id=point["id"],
                    vector=point["vector"],
                    payload=payload,
                )
            )
        client.upsert(collection_name=collection_name, points=point_structs)
        await QdrantService._audit(
            db,
            superadmin_id,
            "qdrant_upsert",
            tenant_res,
            {"tenant_id": tenant_res.tenant_id, "points_count": len(point_structs)},
        )

    @staticmethod
    async def search_points(
        tenant_res: TenantResources,
        query_vector: list[float],
        limit: int = 10,
        payload_filters: dict | None = None,
        db: AsyncSession | None = None,
        superadmin_id: uuid.UUID | None = None,
    ):
        collection_name = QdrantService._required_collection(tenant_res)
        client = QdrantService._client()
        QdrantService.ensure_payload_indexes(collection_name, client)
        response = client.query_points(
            collection_name=collection_name,
            query=query_vector,
            limit=limit,
            query_filter=QdrantService._payload_filter(tenant_res, payload_filters),
        )
        results = getattr(response, "points", [])
        await QdrantService._audit(
            db,
            superadmin_id,
            "qdrant_search",
            tenant_res,
            {"tenant_id": tenant_res.tenant_id, "limit": limit, "results_count": len(results)},
        )
        return results

    @staticmethod
    def _payload_filter(tenant_res: TenantResources, payload_filters: dict | None = None) -> Filter:
        must_conditions = [
            FieldCondition(
                key="tenant_id",
                match=MatchValue(value=tenant_res.tenant_id),
            )
        ]
        for key, value in (payload_filters or {}).items():
            if value is None:
                continue
            if isinstance(value, (list, tuple, set)):
                must_conditions.append(
                    FieldCondition(
                        key=key,
                        match=MatchAny(any=list(value)),
                    )
                )
                continue
            must_conditions.append(
                FieldCondition(
                    key=key,
                    match=MatchValue(value=value),
                )
            )
        return Filter(must=must_conditions)

    @staticmethod
    def _scope_payload(tenant_res: TenantResources, payload: dict) -> dict:
        scoped = dict(payload or {})
        provider_status = dict(tenant_res.provider_status or {})
        integration_type = str(scoped.get("integration_type") or "")
        provider = str(scoped.get("provider") or "")
        prefix = "db" if integration_type == "database" else integration_type
        scoped["tenant_id"] = tenant_res.tenant_id
        scoped.setdefault("organization_id", str(tenant_res.organization_id))
        if integration_type:
            scoped.setdefault("tenant_integration_id", provider_status.get(f"{prefix}_tenant_integration_id") or f"{tenant_res.tenant_id}:{integration_type}:{provider}")
            scoped.setdefault("provider_connection_id", provider_status.get(f"{prefix}_provider_connection_id") or f"{tenant_res.tenant_id}:{integration_type}:{provider}:connection")
            scoped.setdefault("context_snapshot_id", provider_status.get(f"{prefix}_context_snapshot_id"))
            scoped.setdefault("selected_context_snapshot_id", scoped.get("context_snapshot_id"))
        scoped.setdefault("source_kind", scoped.get("source_type") or "connector_template")
        scoped.setdefault("resource", QdrantService._resource_name(scoped))
        scoped.setdefault("resource_type", QdrantService._resource_type(scoped))
        return scoped

    @staticmethod
    def _resource_name(payload: dict) -> str:
        if payload.get("table"):
            table_name = str(payload.get("table"))
            if "." in table_name:
                return table_name
            schema = str(payload.get("schema") or payload.get("schema_name") or "public")
            return f"{schema}.{table_name}"
        return str(payload.get("endpoint") or payload.get("action") or payload.get("module") or payload.get("name") or "")

    @staticmethod
    def _resource_type(payload: dict) -> str:
        source_kind = str(payload.get("source_kind") or payload.get("source_type") or "")
        if "schema" in source_kind or payload.get("table"):
            return "table"
        if payload.get("endpoint"):
            return "endpoint"
        if payload.get("action"):
            return "action"
        return "object"

    @staticmethod
    async def delete_points_by_filter(
        tenant_res: TenantResources,
        payload_filters: dict,
        db: AsyncSession | None = None,
        superadmin_id: uuid.UUID | None = None,
    ) -> None:
        collection_name = QdrantService._required_collection(tenant_res)
        client = QdrantService._client()
        QdrantService.ensure_payload_indexes(collection_name, client)
        query_filter = QdrantService._payload_filter(tenant_res, payload_filters)
        client.delete(collection_name=collection_name, points_selector=FilterSelector(filter=query_filter))
        await QdrantService._audit(
            db,
            superadmin_id,
            "qdrant_delete_by_filter",
            tenant_res,
            {"tenant_id": tenant_res.tenant_id, "payload_filters": payload_filters},
        )

    @staticmethod
    async def delete_points(
        tenant_res: TenantResources,
        point_ids: list,
        db: AsyncSession | None = None,
        superadmin_id: uuid.UUID | None = None,
    ) -> None:
        collection_name = QdrantService._required_collection(tenant_res)
        client = QdrantService._client()
        QdrantService.ensure_payload_indexes(collection_name, client)
        client.delete(collection_name=collection_name, points_selector=point_ids)
        await QdrantService._audit(
            db,
            superadmin_id,
            "qdrant_delete",
            tenant_res,
            {"tenant_id": tenant_res.tenant_id, "point_ids": point_ids},
        )

    @staticmethod
    def ensure_payload_indexes(collection_name: str, client: QdrantClient | None = None) -> None:
        if collection_name in QdrantService._INDEXED_COLLECTIONS:
            return
        client = client or QdrantService._client()
        for field_name in QdrantService.TOOLKIT_PAYLOAD_INDEX_FIELDS:
            try:
                client.create_payload_index(
                    collection_name=collection_name,
                    field_name=field_name,
                    field_schema=QdrantService.PAYLOAD_INDEX_TYPES.get(field_name, PayloadSchemaType.KEYWORD),
                )
            except Exception as exc:
                message = str(exc).lower()
                if "already exists" in message or "exists" in message:
                    continue
                raise RuntimeError(f"Failed to create Qdrant payload index for {field_name}: {exc}") from exc
        QdrantService._INDEXED_COLLECTIONS.add(collection_name)
