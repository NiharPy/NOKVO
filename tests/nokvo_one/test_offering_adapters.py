"""P2 adapter parity: model -> Offering -> model preserves formatter output.

The read-path swap (loaders reading `offerings` via adapters) is only "no
behaviour change" if the existing sector formatters produce IDENTICAL output on
a round-tripped instance. These tests assert exactly that on the real prompt /
tool-schema builders — plus JSON-safety of the backfill row.
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal

from app.models.clinic_service import ClinicService
from app.models.offering import Offering
from app.models.real_estate_project import RealEstateProject
from app.services.offering_adapters import (
    clinic_service_to_offering_row,
    offering_to_clinic_service,
    offering_to_real_estate_project,
    real_estate_project_to_offering_row,
)
from app.services.real_estate_project_service import (
    project_choices_for_tool_schema,
    project_inventory_spoken,
    project_summary_lines,
    projects_prompt_section,
)
from app.services.clinic_service_service import (
    service_choices_for_tool_schema,
    service_summary_lines,
    services_prompt_section,
)


def _project() -> RealEstateProject:
    return RealEstateProject(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        name="Skyline Residences",
        location="Gachibowli, Hyderabad",
        rera_number="P02400001234",
        property_type="Apartment",
        price_min=Decimal("8500000.00"),
        price_max=Decimal("14500000.00"),
        price_display="₹85L – ₹1.45Cr",
        configurations=["2BHK", "3BHK", "3BHK+Study"],
        amenities=["Clubhouse", "Pool", "Gym"],
        description="Premium gated community with 80% open space.",
        possession_date="Dec 2027",
        builder_name="Aparna Constructions",
        brochure_url="https://example.com/skyline.pdf",
        contact_phone="+919876543210",
        whatsapp={"location": {"template": "loc", "language": "en"}},
        extra={"towers": 6},
        status="active",
        created_by_user_id=uuid.uuid4(),
    )


def _roundtrip_project(p: RealEstateProject) -> RealEstateProject:
    row = real_estate_project_to_offering_row(p)
    json.dumps(row["attributes"])  # backfill migration dumps these
    json.dumps(row["media"])
    return offering_to_real_estate_project(Offering(**row))


def test_real_estate_prompt_output_is_byte_identical_through_offering():
    p = _project()
    p2 = _roundtrip_project(p)
    assert project_summary_lines([p]) == project_summary_lines([p2])
    assert projects_prompt_section([p]) == projects_prompt_section([p2])
    assert project_inventory_spoken([p], "en") == project_inventory_spoken([p2], "en")
    assert project_inventory_spoken([p], "hi") == project_inventory_spoken([p2], "hi")
    assert project_inventory_spoken([p], "te") == project_inventory_spoken([p2], "te")
    assert project_choices_for_tool_schema([p]) == project_choices_for_tool_schema([p2])


def _service() -> ClinicService:
    return ClinicService(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        name="Comprehensive Eye Exam",
        description="Full refraction + retina check.",
        department="Ophthalmology",
        duration_minutes=30,
        price=Decimal("800.00"),
        price_display="₹800",
        is_active=True,
        created_by_user_id=uuid.uuid4(),
    )


def _roundtrip_service(s: ClinicService) -> ClinicService:
    row = clinic_service_to_offering_row(s)
    json.dumps(row["attributes"])
    json.dumps(row["media"])
    return offering_to_clinic_service(Offering(**row))


def test_clinic_prompt_output_is_byte_identical_through_offering():
    s = _service()
    s2 = _roundtrip_service(s)
    doctors = ["Dr. Meera", "Dr. Rao"]
    assert service_summary_lines([(s, doctors)]) == service_summary_lines([(s2, doctors)])
    assert services_prompt_section([(s, doctors)]) == services_prompt_section([(s2, doctors)])
    assert service_choices_for_tool_schema([s]) == service_choices_for_tool_schema([s2])


def test_price_and_status_round_trip_exactly():
    p = _project()
    p2 = _roundtrip_project(p)
    assert p2.price_min == p.price_min and p2.price_max == p.price_max
    assert p2.status == p.status

    s = _service()
    row = clinic_service_to_offering_row(s)
    assert row["status"] == "active"
    assert offering_to_clinic_service(Offering(**row)).is_active is True
    # inactive round-trips too
    s.is_active = False
    assert offering_to_clinic_service(Offering(**clinic_service_to_offering_row(s))).is_active is False
