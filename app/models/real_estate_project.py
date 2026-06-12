from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.db.session import Base


class RealEstateProject(Base):
    """A real-estate project (development) owned by an organization.

    Real-estate orgs typically market several projects in parallel; the inbound
    agent needs to know about them to answer questions and to attach leads /
    site-visits to the right project. ``amenities`` and ``configurations`` are
    free-form JSONB so admins can list per-project specifics without us
    rebuilding the schema for every new attribute.
    """

    __tablename__ = "real_estate_projects"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(200), nullable=False)
    location = Column(String(500), nullable=True)
    rera_number = Column(String(120), nullable=True)
    property_type = Column(String(80), nullable=True)
    price_min = Column(Numeric(14, 2), nullable=True)
    price_max = Column(Numeric(14, 2), nullable=True)
    price_display = Column(String(200), nullable=True)
    configurations = Column(JSONB, nullable=False, server_default="[]")
    amenities = Column(JSONB, nullable=False, server_default="[]")
    description = Column(Text, nullable=True)
    possession_date = Column(String(80), nullable=True)
    builder_name = Column(String(200), nullable=True)
    brochure_url = Column(String(1000), nullable=True)
    contact_phone = Column(String(40), nullable=True)
    # Per-project WhatsApp message config (Meta-approved templates). Shape:
    #   {"location": {"template": "<name>", "language": "en", "maps_url": "..."},
    #    "brochure": {"template": "<name>", "language": "en"}}
    # The location message auto-sends on a site-visit booking; the brochure
    # message sends from whatsapp_mode. Empty/unset → nothing is sent.
    whatsapp = Column(JSONB, nullable=False, server_default="{}")
    extra = Column(JSONB, nullable=False, server_default="{}")
    status = Column(String(40), nullable=False, server_default="active")
    created_by_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organization_users.id"),
        nullable=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
