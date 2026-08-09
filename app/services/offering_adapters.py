"""Adapters between the generic ``Offering`` catalog and the per-sector models.

P2 of the NOKVO ONE SDK: ``RealEstateProject`` and ``ClinicService`` become
adapters over the shared ``offerings`` table. Two directions:

  * ``*_to_offering_row`` — model → column dict (for the backfill migration + tests).
    JSON-safe: Decimals are stringified so ``attributes``/``media`` can be dumped.
  * ``offering_to_*``     — ``Offering`` → a TRANSIENT (unsaved) model instance, so the
    existing sector formatters (``projects_prompt_section`` / ``services_prompt_section``,
    ``*_choices_for_tool_schema``, ``project_inventory_spoken``, ``find_*_match``) run
    BYTE-IDENTICALLY once the loaders swap their data source to ``offerings``.

The bar is "no behaviour change": the round-trip
``model -> offering_row -> Offering -> model`` preserves every field the formatters
read (verified in tests/nokvo_one/test_offering_adapters.py, which asserts the actual
prompt-section / tool-schema output is identical before and after the round-trip).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.models.clinic_service import ClinicService
from app.models.offering import Offering
from app.models.real_estate_project import RealEstateProject


def _num(v: Any) -> str | None:
    """Decimal/number → JSON-safe string (exact round-trip via Decimal(str))."""
    return None if v is None else str(v)


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    return Decimal(str(v))


# ── Real estate (kind="project") ─────────────────────────────────────────────


def real_estate_project_to_offering_row(p: RealEstateProject) -> dict[str, Any]:
    return {
        "id": p.id,
        "organization_id": p.organization_id,
        "kind": "project",
        "name": p.name,
        "category": p.property_type,
        "description": p.description,
        "price": p.price_min,
        "price_display": p.price_display,
        "duration_min": None,
        "attributes": {
            "location": p.location,
            "rera_number": p.rera_number,
            "property_type": p.property_type,
            "price_min": _num(p.price_min),
            "price_max": _num(p.price_max),
            "configurations": list(p.configurations or []),
            "amenities": list(p.amenities or []),
            "possession_date": p.possession_date,
            "builder_name": p.builder_name,
            "contact_phone": p.contact_phone,
            "extra": dict(p.extra or {}),
        },
        "media": {
            "brochure_url": p.brochure_url,
            "whatsapp": dict(p.whatsapp or {}),
        },
        "availability": {},
        "provider_ids": [],
        "status": p.status,
        "created_by_user_id": p.created_by_user_id,
    }


def offering_to_real_estate_project(o: Offering) -> RealEstateProject:
    a = dict(o.attributes or {})
    m = dict(o.media or {})
    prop_type = a.get("property_type")
    return RealEstateProject(
        id=o.id,
        organization_id=o.organization_id,
        name=o.name,
        location=a.get("location"),
        rera_number=a.get("rera_number"),
        property_type=prop_type if prop_type is not None else o.category,
        price_min=_dec(a.get("price_min")),
        price_max=_dec(a.get("price_max")),
        price_display=o.price_display,
        configurations=list(a.get("configurations") or []),
        amenities=list(a.get("amenities") or []),
        description=o.description,
        possession_date=a.get("possession_date"),
        builder_name=a.get("builder_name"),
        brochure_url=m.get("brochure_url"),
        contact_phone=a.get("contact_phone"),
        whatsapp=dict(m.get("whatsapp") or {}),
        extra=dict(a.get("extra") or {}),
        status=o.status,
        created_by_user_id=o.created_by_user_id,
    )


# ── Clinic (kind="service") ──────────────────────────────────────────────────
# NOTE: the doctor mapping lives in ClinicServiceProvider (a join table), not on
# ClinicService, so it is NOT part of this row/instance adapter — the provider
# join in load_services_with_providers is preserved separately (or migrates to
# Offering.provider_ids in a later step).


def clinic_service_to_offering_row(s: ClinicService) -> dict[str, Any]:
    return {
        "id": s.id,
        "organization_id": s.organization_id,
        "kind": "service",
        "name": s.name,
        "category": s.department,
        "description": s.description,
        "price": s.price,
        "price_display": s.price_display,
        "duration_min": s.duration_minutes,
        "attributes": {},
        "media": {},
        "availability": {},
        "provider_ids": [],
        "status": "active" if s.is_active else "inactive",
        "created_by_user_id": s.created_by_user_id,
    }


def offering_to_clinic_service(o: Offering) -> ClinicService:
    return ClinicService(
        id=o.id,
        organization_id=o.organization_id,
        name=o.name,
        description=o.description,
        department=o.category,
        duration_minutes=o.duration_min,
        price=o.price,
        price_display=o.price_display,
        is_active=(o.status == "active"),
        created_by_user_id=o.created_by_user_id,
    )
