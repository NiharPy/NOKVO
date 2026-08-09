from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.db.session import Base


class Offering(Base):
    """The SDK's generic catalog entry — one shape for everything a business offers.

    Real-estate projects, clinic services, ecommerce products, and service packages
    all live here, distinguished by ``kind``. Per-sector specifics ride in the
    ``attributes`` / ``media`` JSONB so we never reshape the schema per business.

    P2 of the NOKVO ONE SDK plan (see NOKVOSDK/docs/04): ``RealEstateProject`` and
    ``ClinicService`` become *adapters* over this model. The exit criterion is that
    their existing read paths — the prompt blocks (``projects_prompt_section`` /
    ``services_prompt_section``), the tool-choice enums (``*_choices_for_tool_schema``),
    the deterministic ``project_inventory_spoken``, and booking target resolution —
    are byte-preserved. So this model deliberately carries a superset of both:

      * ``duration_min`` + ``provider_ids``  → clinic services (+ doctor mapping)
      * ``price`` / ``price_display`` / ``category`` → both
      * ``attributes`` (RERA, configurations, amenities, property_type, possession…)
        and ``media`` (brochure_url, images, whatsapp templates) → real-estate projects
    """

    __tablename__ = "offerings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # project | service | product | package | resource
    kind = Column(String(40), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    category = Column(String(160), nullable=True)  # department / project-type / product-cat
    description = Column(Text, nullable=True)
    price = Column(Numeric(14, 2), nullable=True)
    price_display = Column(String(200), nullable=True)
    duration_min = Column(Integer, nullable=True)  # appointments (clinic/services)
    attributes = Column(JSONB, nullable=False, server_default="{}")   # per-sector free-form
    availability = Column(JSONB, nullable=False, server_default="{}")  # hours/capacity/slots or stock
    provider_ids = Column(JSONB, nullable=False, server_default="[]")  # eligible members (doctors/consultants)
    media = Column(JSONB, nullable=False, server_default="{}")         # brochure_url, images, whatsapp
    status = Column(String(40), nullable=False, server_default="active")
    created_by_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organization_users.id"),
        nullable=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
