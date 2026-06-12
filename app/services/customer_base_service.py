"""Customer base — upsert helpers for the per-tenant deduplicated caller list.

Every inbound call upserts one row keyed on (tenant_id, phone_e164) at call
END (the caller's name from conversational memory only exists at teardown).
Outbound customer follow-ups bump the same counters when the call closes.

Name policy: fill-only. STT mishears names ("Nihar" on call 1, "Neon" on
call 2), so call data may only fill a NULL name — never overwrite one already
on file. The admin PATCH endpoint is the only overwrite path.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.customer_base import CustomerBase
from app.services.voice_turn_policy import normalize_phone_number

logger = logging.getLogger(__name__)


def clean_customer_name(raw: object) -> str | None:
    """Normalize a call-extracted name to something worth storing, else None."""
    name = str(raw or "").strip()
    if not name or len(name) > 120:
        return None
    # Reject obviously-not-a-name captures (bare digits, placeholders).
    if name.replace(" ", "").isdigit():
        return None
    if name.lower() in {"unknown", "n/a", "na", "none", "null", "-"}:
        return None
    return name


async def upsert_customer_from_call(
    db: AsyncSession,
    *,
    tenant_id: str,
    phone: str,
    name: str | None = None,
    call_id: str | None = None,
    commit: bool = True,
) -> uuid.UUID | None:
    """Record one completed call from ``phone``. Returns the customer id.

    Idempotent INSERT … ON CONFLICT: a new caller gets a fresh row, a repeat
    caller gets last_call_at/call_count bumped. ``name`` only lands when the
    stored name is NULL (see module docstring).

    ``commit=False`` leaves the row in the caller's open transaction (used by
    the appointment macro, which commits everything together at the end).
    """
    phone_e164 = normalize_phone_number(str(phone or "")) or str(phone or "").strip()
    if not phone_e164:
        return None
    now = datetime.now(timezone.utc)
    cleaned_name = clean_customer_name(name)

    stmt = (
        pg_insert(CustomerBase)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            phone_e164=phone_e164,
            name=cleaned_name,
            first_seen_at=now,
            last_call_at=now,
            call_count=1,
            last_call_id=str(call_id) if call_id else None,
        )
        .on_conflict_do_update(
            constraint="uq_customer_base_tenant_phone",
            set_={
                "name": func.coalesce(CustomerBase.name, cleaned_name),
                "last_call_at": now,
                "call_count": CustomerBase.call_count + 1,
                "last_call_id": str(call_id) if call_id else CustomerBase.last_call_id,
                "updated_at": now,
            },
        )
        .returning(CustomerBase.id)
    )
    result = await db.execute(stmt)
    if commit:
        await db.commit()
    return result.scalar_one_or_none()


async def ensure_customer(
    db: AsyncSession,
    *,
    tenant_id: str,
    phone: str,
    name: str | None = None,
    commit: bool = True,
) -> uuid.UUID | None:
    """Ensure a customer row exists for ``phone`` WITHOUT bumping call
    counters (the call-end teardown owns those — this runs mid-call, e.g.
    when the appointment macro links a booking to a customer). Name is
    fill-only, same as the call upsert.
    """
    phone_e164 = normalize_phone_number(str(phone or "")) or str(phone or "").strip()
    if not phone_e164:
        return None
    now = datetime.now(timezone.utc)
    cleaned_name = clean_customer_name(name)

    stmt = (
        pg_insert(CustomerBase)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            phone_e164=phone_e164,
            name=cleaned_name,
            first_seen_at=now,
            call_count=0,
        )
        .on_conflict_do_update(
            constraint="uq_customer_base_tenant_phone",
            set_={
                "name": func.coalesce(CustomerBase.name, cleaned_name),
                "updated_at": now,
            },
        )
        .returning(CustomerBase.id)
    )
    result = await db.execute(stmt)
    if commit:
        await db.commit()
    return result.scalar_one_or_none()


async def record_call_summary(
    db: AsyncSession,
    *,
    tenant_id: str,
    summary: str,
    phone: str | None = None,
    customer_id: uuid.UUID | None = None,
    call_id: str | None = None,
) -> bool:
    """Write the post-call condenser summary onto the customer row.

    Resolved by customer_id when the outbound dispatch carried it, else by
    (tenant_id, normalized phone). Returns True when a row was updated.
    """
    where = None
    if customer_id is not None:
        where = CustomerBase.id == customer_id
    elif phone:
        phone_e164 = normalize_phone_number(str(phone)) or str(phone).strip()
        if phone_e164:
            where = (CustomerBase.tenant_id == tenant_id) & (
                CustomerBase.phone_e164 == phone_e164
            )
    if where is None:
        return False
    values: dict = {
        "last_call_summary": summary,
        "updated_at": datetime.now(timezone.utc),
    }
    if call_id:
        values["last_call_id"] = str(call_id)
    result = await db.execute(update(CustomerBase).where(where).values(**values))
    await db.commit()
    return bool(result.rowcount)


async def get_customer_for_tenant(
    db: AsyncSession, *, tenant_id: str, customer_id: uuid.UUID
) -> CustomerBase | None:
    result = await db.execute(
        select(CustomerBase).where(
            CustomerBase.id == customer_id, CustomerBase.tenant_id == tenant_id
        )
    )
    return result.scalars().first()
