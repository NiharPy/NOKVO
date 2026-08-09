"""Dual-write sync — mirror project/service CRUD into the ``offerings`` catalog.

Stage A of the P2 Option-B rollout. Every function is a **no-op unless**
``settings.OFFERINGS_DUAL_WRITE`` is on, so wiring these into the CRUD endpoints
is behaviour-neutral until the flag is flipped. The column mapping is the shared
``offering_adapters.*_to_offering_row`` (same as the backfill migration), so the
mirror stays byte-consistent with the source.

These helpers add to / delete from the session but do NOT commit — the caller's
transaction owns the commit.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.models.offering import Offering
from app.services.offering_adapters import (
    clinic_service_to_offering_row,
    real_estate_project_to_offering_row,
)

_JSONB_FIELDS = ("attributes", "media", "availability", "provider_ids")


async def _upsert(db: AsyncSession, row: dict[str, Any]) -> None:
    existing = await db.get(Offering, row["id"])
    if existing is None:
        db.add(Offering(**row))
        return
    for key, value in row.items():
        if key == "id":
            continue
        setattr(existing, key, value)
    for field in _JSONB_FIELDS:
        flag_modified(existing, field)


async def sync_project_offering(db: AsyncSession | None, project: Any) -> None:
    if not settings.OFFERINGS_DUAL_WRITE or db is None or project is None:
        return
    await _upsert(db, real_estate_project_to_offering_row(project))


async def sync_service_offering(db: AsyncSession | None, service: Any) -> None:
    if not settings.OFFERINGS_DUAL_WRITE or db is None or service is None:
        return
    await _upsert(db, clinic_service_to_offering_row(service))


async def delete_offering(db: AsyncSession | None, offering_id: Any) -> None:
    if not settings.OFFERINGS_DUAL_WRITE or db is None or offering_id is None:
        return
    existing = await db.get(Offering, offering_id)
    if existing is not None:
        await db.delete(existing)
