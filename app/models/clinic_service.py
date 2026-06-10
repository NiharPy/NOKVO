from __future__ import annotations

import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.db.session import Base


class ClinicService(Base):
    """A service a clinic offers (e.g. "Dental Cleaning", "Cardiology Consult").

    Clinic orgs define their own service catalog; the inbound agent uses it to
    answer "what do you offer / how much / which doctor does X" and to route a
    booking service-first to a doctor who actually provides that service (via
    :class:`ClinicServiceProvider`). ``department`` is an optional grouping
    (e.g. "Dentistry"); ``price`` / ``duration_minutes`` are optional so a
    clinic can list a service without committing to a public price.
    """

    __tablename__ = "clinic_services"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    department = Column(String(160), nullable=True)
    duration_minutes = Column(Integer, nullable=True)
    price = Column(Numeric(12, 2), nullable=True)
    price_display = Column(String(120), nullable=True)
    is_active = Column(Boolean, nullable=False, server_default="true")
    created_by_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organization_users.id"),
        nullable=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ClinicServiceProvider(Base):
    """Many-to-many map of which doctors (members) provide which service.

    A service is offered by N doctors; a doctor offers N services. The booking
    flow resolves a chosen service to its eligible doctors and constrains the
    assignment engine to that pool (see
    ``NokvoOneAssignmentService.assign_request(eligible_member_ids=...)``).
    """

    __tablename__ = "clinic_service_providers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_id = Column(
        UUID(as_uuid=True),
        ForeignKey("clinic_services.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    member_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organization_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("service_id", "member_id", name="uq_clinic_service_provider"),
    )
