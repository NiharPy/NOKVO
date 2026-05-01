import re
import uuid
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, Filter, FieldCondition, FilterSelector, MatchValue, PointStruct, VectorParams
from app.core.config import settings
from app.models.audit import SuperAdminAuditLog
from app.models.tenant_resources import TenantResources
from sqlalchemy.ext.asyncio import AsyncSession

class QdrantService:
    @staticmethod
    def cluster_ref() -> str:
        return settings.QDRANT_URL

    @staticmethod
    def _client() -> QdrantClient:
        if settings.QDRANT_URL == ":memory:":
            return QdrantClient(location=":memory:")
        return QdrantClient(url=settings.QDRANT_URL, api_key=settings.QDRANT_API_KEY)

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
        point_structs = []
        for point in points:
            payload = dict(point.get("payload", {}) or {})
            payload["tenant_id"] = tenant_res.tenant_id
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
            must_conditions.append(
                FieldCondition(
                    key=key,
                    match=MatchValue(value=value),
                )
            )
        return Filter(must=must_conditions)

    @staticmethod
    async def delete_points_by_filter(
        tenant_res: TenantResources,
        payload_filters: dict,
        db: AsyncSession | None = None,
        superadmin_id: uuid.UUID | None = None,
    ) -> None:
        collection_name = QdrantService._required_collection(tenant_res)
        client = QdrantService._client()
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
        client.delete(collection_name=collection_name, points_selector=point_ids)
        await QdrantService._audit(
            db,
            superadmin_id,
            "qdrant_delete",
            tenant_res,
            {"tenant_id": tenant_res.tenant_id, "point_ids": point_ids},
        )
