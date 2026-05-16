from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Time
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.db.session import Base


class OrganizationMemberAssignmentSettings(Base):
    __tablename__ = "organization_member_assignment_settings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    member_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organization_users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    is_assignable = Column(Boolean, nullable=False, server_default="false", default=False)
    working_days = Column(JSONB, nullable=False, server_default="[]", default=list)
    start_time = Column(Time(timezone=False), nullable=True)
    end_time = Column(Time(timezone=False), nullable=True)
    timezone = Column(String, nullable=False, server_default="Asia/Kolkata", default="Asia/Kolkata")
    request_types = Column(JSONB, nullable=False, server_default="[]", default=list)
    max_active_requests = Column(Integer, nullable=False, server_default="3", default=3)
    max_requests_per_day = Column(Integer, nullable=True)
    max_requests_per_hour = Column(Integer, nullable=False, server_default="6", default=6)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ClinicMemberScheduleSettings(Base):
    __tablename__ = "clinic_member_schedule_settings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    member_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organization_users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    appointment_duration_minutes = Column(Integer, nullable=False, server_default="30", default=30)
    buffer_minutes = Column(Integer, nullable=False, server_default="0", default=0)
    max_patients_per_hour = Column(Integer, nullable=True)
    max_patients_per_day = Column(Integer, nullable=True)
    consultation_types = Column(JSONB, nullable=False, server_default="[]", default=list)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class MemberBlockedSlot(Base):
    __tablename__ = "member_blocked_slots"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    member_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organization_users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    start_time = Column(DateTime(timezone=True), nullable=False)
    end_time = Column(DateTime(timezone=True), nullable=False)
    reason = Column(String, nullable=True)
    repeat_rule = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class NokvoOneAssignmentAuditLog(Base):
    __tablename__ = "nokvo_one_assignment_audit_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    selected_member_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organization_users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    request_type = Column(String, nullable=False)
    assignment_status = Column(String, nullable=False)
    reason = Column(String, nullable=True)
    skipped_member_reasons = Column(JSONB, nullable=False, server_default="{}", default=dict)
    selected_member_active_load = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
