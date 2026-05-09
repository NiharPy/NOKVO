from __future__ import annotations

import argparse
import asyncio
import logging
from typing import Any

from qdrant_client.http.models import PointStruct
from sqlalchemy import or_, select

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.organization import Organization
from app.models.tenant_resources import TenantResources
from app.services.qdrant_service import QdrantService
from app.services.text_embedding_service import TextEmbeddingService


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

TARGET_INTEGRATION_TYPES = {"database", "crm", "erp"}
TARGET_AGENT_SOURCE_TYPES = {"agent_knowledge"}


def _should_reembed(payload: dict[str, Any], *, force: bool = False) -> bool:
    if (
        not force
        and payload.get("embedding_provider") == "openai"
        and payload.get("embedding_model") == settings.OPENAI_EMBEDDING_MODEL
    ):
        return False
    integration_type = str(payload.get("integration_type") or "").lower()
    source_type = str(payload.get("source_type") or "").lower()
    if integration_type in TARGET_INTEGRATION_TYPES:
        return True
    return source_type in TARGET_AGENT_SOURCE_TYPES


async def _find_tenant_resources(organization_name: str) -> tuple[Organization, TenantResources]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Organization, TenantResources)
            .join(TenantResources, TenantResources.organization_id == Organization.id)
            .where(
                or_(
                    Organization.name.ilike(organization_name),
                    Organization.name.ilike(f"%{organization_name}%"),
                    Organization.email_domain.ilike(organization_name),
                    Organization.email_domain.ilike(f"%{organization_name}%"),
                )
            )
            .limit(1)
        )
        row = result.first()
        if not row:
            raise RuntimeError(f"Organization not found: {organization_name}")
        return row[0], row[1]


async def reembed_organization(
    organization_name: str,
    *,
    dry_run: bool = False,
    batch_size: int = 64,
    force: bool = False,
) -> dict[str, Any]:
    if not settings.OPENAI_API_KEY and not settings.OPENAI_EMBEDDING_ALLOW_ZERO_FALLBACK:
        raise RuntimeError("OPENAI_API_KEY is required before re-embedding.")

    organization, tenant_res = await _find_tenant_resources(organization_name)
    if not tenant_res.qdrant_collection_name:
        raise RuntimeError(f"Organization {organization.name} has no Qdrant collection.")

    client = QdrantService._client()
    collection = tenant_res.qdrant_collection_name
    QdrantService.ensure_payload_indexes(collection, client)

    logger.info("Re-embedding organization=%s collection=%s model=%s", organization.name, collection, settings.OPENAI_EMBEDDING_MODEL)
    offset = None
    scanned = 0
    eligible: list[dict[str, Any]] = []

    while True:
        records, next_offset = client.scroll(
            collection_name=collection,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        if not records:
            break
        for record in records:
            scanned += 1
            payload = dict(record.payload or {})
            text = str(payload.get("text") or "").strip()
            if text and _should_reembed(payload, force=force):
                eligible.append({"id": record.id, "payload": payload, "text": text})
        offset = next_offset
        if offset is None:
            break

    if dry_run:
        return {
            "organization": organization.name,
            "collection": collection,
            "model": settings.OPENAI_EMBEDDING_MODEL,
            "scanned_points": scanned,
            "eligible_points": len(eligible),
            "updated_points": 0,
            "dry_run": True,
        }

    updated = 0
    batch_size = max(1, min(batch_size, 128))
    for start in range(0, len(eligible), batch_size):
        batch = eligible[start:start + batch_size]
        vectors = await TextEmbeddingService.embed_texts([item["text"] for item in batch])
        points: list[PointStruct] = []
        for index, item in enumerate(batch):
            payload = dict(item["payload"])
            payload["embedding_provider"] = "openai"
            payload["embedding_model"] = settings.OPENAI_EMBEDDING_MODEL
            points.append(PointStruct(id=item["id"], vector=vectors[index], payload=payload))
        client.upsert(collection_name=collection, points=points)
        updated += len(points)
        logger.info("Updated %s/%s eligible points", updated, len(eligible))

    return {
        "organization": organization.name,
        "collection": collection,
        "model": settings.OPENAI_EMBEDDING_MODEL,
        "scanned_points": scanned,
        "eligible_points": len(eligible),
        "updated_points": updated,
        "dry_run": False,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Re-embed tenant Qdrant points with the configured OpenAI embedding model.")
    parser.add_argument("--organization", default="Needles", help="Organization name or email domain to re-embed.")
    parser.add_argument("--dry-run", action="store_true", help="Count eligible points without updating Qdrant.")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--force", action="store_true", help="Re-embed even points already marked with the configured embedding model.")
    args = parser.parse_args()

    result = await reembed_organization(
        args.organization,
        dry_run=args.dry_run,
        batch_size=args.batch_size,
        force=args.force,
    )
    logger.info("Result: %s", result)


if __name__ == "__main__":
    asyncio.run(main())
