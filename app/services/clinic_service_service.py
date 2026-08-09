"""Helpers for surfacing a clinic's service catalog + doctor mapping.

Mirrors ``real_estate_project_service`` for the Clinics vertical: the catalog
is small (1–50 doctors, tens of services) so we read it directly from Postgres
each turn rather than caching. Used by the voice pipeline to (a) inject the
service/doctor catalog into the system prompt and (b) resolve a chosen service
to the doctors who provide it, so the assignment engine only considers eligible
doctors (service-first routing).
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.clinic_service import ClinicService, ClinicServiceProvider
from app.models.organization_user import OrganizationUser
from app.services.real_estate_project_service import format_inr


# ── Loaders ─────────────────────────────────────────────────────────────────


async def load_active_services(
    db: AsyncSession | None,
    organization_id: uuid.UUID | str | None,
) -> list[ClinicService]:
    if db is None or organization_id is None:
        return []
    # P2 Option-B read swap: catalog from `offerings` via the byte-parity adapter. Off by default.
    from app.core.config import settings
    if settings.OFFERINGS_READ:
        from app.models.offering import Offering
        from app.services.offering_adapters import offering_to_clinic_service
        try:
            res = await db.execute(
                select(Offering)
                .where(
                    Offering.organization_id == organization_id,
                    Offering.kind == "service",
                    Offering.status == "active",
                )
                .order_by(Offering.name.asc())
            )
        except Exception:
            return []
        return [offering_to_clinic_service(o) for o in res.scalars().all()]
    try:
        res = await db.execute(
            select(ClinicService)
            .where(
                ClinicService.organization_id == organization_id,
                ClinicService.is_active.is_(True),
            )
            .order_by(ClinicService.name.asc())
        )
    except Exception:
        return []
    return list(res.scalars().all())


async def provider_member_ids_for_service(
    db: AsyncSession | None, service_id: uuid.UUID | str | None
) -> list[uuid.UUID]:
    """Doctor (member) ids that provide a given service."""
    if db is None or service_id is None:
        return []
    try:
        res = await db.execute(
            select(ClinicServiceProvider.member_id).where(
                ClinicServiceProvider.service_id == service_id
            )
        )
    except Exception:
        return []
    return [row[0] for row in res.all()]


async def load_services_with_providers(
    db: AsyncSession | None,
    organization_id: uuid.UUID | str | None,
) -> list[tuple[ClinicService, list[str]]]:
    """Active services paired with the display names of their doctors.

    One round-trip for the catalog + a single map query for the providers, so
    the prompt builder can render "Service — doctors: A, B" without N queries.
    """
    services = await load_active_services(db, organization_id)
    if not services or db is None:
        return [(s, []) for s in services]
    service_ids = [s.id for s in services]
    try:
        rows = (
            await db.execute(
                select(ClinicServiceProvider.service_id, OrganizationUser.full_name, OrganizationUser.email)
                .join(OrganizationUser, OrganizationUser.id == ClinicServiceProvider.member_id)
                .where(ClinicServiceProvider.service_id.in_(service_ids))
            )
        ).all()
    except Exception:
        rows = []
    names_by_service: dict[uuid.UUID, list[str]] = {}
    for service_id, full_name, email in rows:
        label = (full_name or "").strip() or (email or "").strip()
        if label:
            names_by_service.setdefault(service_id, []).append(label)
    return [(s, names_by_service.get(s.id, [])) for s in services]


# ── Formatting / prompt ──────────────────────────────────────────────────────


def _format_service_price(service: ClinicService) -> str:
    if service.price_display:
        return service.price_display
    if service.price is not None:
        return format_inr(service.price)
    return ""


def service_summary_lines(services_with_doctors: list[tuple[ClinicService, list[str]]]) -> list[str]:
    """One human-readable line per service, capped to keep the prompt sane."""
    lines: list[str] = []
    for service, doctors in services_with_doctors[:60]:
        bits: list[str] = [service.name]
        if service.department:
            bits.append(f"({service.department})")
        if service.duration_minutes:
            bits.append(f"~{service.duration_minutes} min")
        price = _format_service_price(service)
        if price:
            bits.append(f"price: {price}")
        if doctors:
            bits.append("doctors: " + ", ".join(doctors[:8]))
        else:
            bits.append("doctors: any available")
        lines.append(" — ".join(bits))
        description = (service.description or "").strip()
        if description:
            lines.append(f"    notes: {description[:240]}")
    return lines


def services_prompt_section(services_with_doctors: list[tuple[ClinicService, list[str]]]) -> str:
    """Authoritative system-prompt block for the clinic's service catalog.

    Empty string when there are no services so the prompt stays lean.
    """
    if not services_with_doctors:
        return ""
    lines = service_summary_lines(services_with_doctors)
    body = "\n".join(f"- {line}" if not line.startswith("    ") else line for line in lines)
    return (
        "THIS LIST IS THE AUTHORITATIVE SOURCE OF TRUTH FOR THE CLINIC'S SERVICES, "
        "PRICES, AND WHICH DOCTORS PROVIDE THEM. Answer questions about services, "
        "pricing, and doctors ONLY from this list. Never invent a service, price, "
        "or claim a doctor offers something not listed here.\n"
        "When booking, identify the SERVICE first, then book with one of the "
        "doctors listed for it (the system picks the best-available such doctor). "
        "If the caller names a doctor directly, honour that. If a requested "
        "service isn't listed, say the team will confirm and offer to take "
        "details.\n\n"
        f"{body}"
    )


def service_choices_for_tool_schema(services: list[ClinicService]) -> dict[str, Any]:
    """JSONSchema fragment describing service_id / service fields for the tool."""
    names = [s.name for s in services if s.name]
    service_id_field = {"type": "string", "description": "UUID of the chosen clinic service."}
    service_field: dict[str, Any] = {
        "type": "string",
        "description": "Name of the service the caller wants.",
        "maxLength": 200,
    }
    if names:
        capped = names[:80]
        service_field["enum"] = capped
        service_field["description"] = (
            "Name of the requested service. Use the EXACT name of one of the "
            "clinic's services: " + "; ".join(capped) + "."
        )
    return {"service_id": service_id_field, "service": service_field}


# ── Fuzzy service-name matching (mirrors find_project_match) ─────────────────

_SERVICE_STOPWORDS: frozenset[str] = frozenset(
    {"the", "a", "an", "for", "my", "consultation", "consult", "appointment",
     "checkup", "check", "up", "visit", "service", "doctor", "test",
     # Common request filler so "i want a cleaning" still matches "Dental Cleaning".
     "i", "want", "need", "would", "like", "get", "book", "schedule", "please",
     "to", "can", "you", "do", "have", "me", "with", "and", "is", "there"}
)


def _normalize(value: str | None) -> str:
    text = re.sub(r"[^a-z0-9\s]", " ", (value or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def _tokens(value: str | None) -> set[str]:
    return {t for t in _normalize(value).split() if t and t not in _SERVICE_STOPWORDS}


def find_service_match(
    services: list[ClinicService],
    *,
    service_id: str | None = None,
    name: str | None = None,
) -> ClinicService | None:
    """Resolve a spoken service name (or id) to a configured service.

    Exact id wins; else exact normalized name; else best token-overlap match
    above a confidence floor (so "I want a cleaning" matches "Dental Cleaning"
    but a vague utterance doesn't false-match).
    """
    if service_id:
        for s in services:
            if str(s.id) == str(service_id):
                return s
    spoken = _normalize(name)
    if not spoken:
        return None
    spoken_tokens = _tokens(name)
    best: ClinicService | None = None
    best_score = 0.0
    for s in services:
        name_norm = _normalize(s.name)
        if not name_norm:
            continue
        if name_norm == spoken:
            return s
        score = 0.0
        if spoken in name_norm or name_norm in spoken:
            score = 0.8
        name_tokens = _tokens(s.name)
        if spoken_tokens and name_tokens:
            overlap = spoken_tokens & name_tokens
            if overlap:
                jaccard = len(overlap) / len(spoken_tokens | name_tokens)
                coverage = len(overlap) / len(spoken_tokens)
                score = max(score, 0.5 * jaccard + 0.5 * coverage)
        if score > best_score:
            best_score, best = score, s
    return best if best_score >= 0.45 else None
