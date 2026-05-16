from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import logging
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.member_assignment import (
    ClinicMemberScheduleSettings,
    MemberBlockedSlot,
    NokvoOneAssignmentAuditLog,
    OrganizationMemberAssignmentSettings,
)
from app.models.nokvo_one_tool_record import NokvoOneToolRecord
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser
from app.services.nokvo_one_business_templates import allowed_request_types


logger = logging.getLogger(__name__)

ACTIVE_REQUEST_STATUSES = {"assigned", "in_progress"}
NON_DAILY_COUNT_STATUSES = {"cancelled"}
DAY_INDEX = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
EMERGENCY_KEYWORDS = {
    "chest pain",
    "breathing",
    "severe bleeding",
    "unconscious",
    "stroke",
    "heart attack",
    "emergency",
    "severe pain",
}
ASSIGNMENT_TIMEZONE = "Asia/Kolkata"


@dataclass
class AssignmentCandidate:
    member: OrganizationUser
    settings: OrganizationMemberAssignmentSettings
    active_load: int
    daily_count: int
    hourly_count: int


def _coerce_zoneinfo(value: str | None) -> ZoneInfo:
    return ZoneInfo(ASSIGNMENT_TIMEZONE)


def _aware_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_time(value: str | time | None) -> time | None:
    if value is None or isinstance(value, time):
        return value
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid time: {value}") from exc


def _local_day(value: datetime, tz: ZoneInfo) -> str:
    return DAY_INDEX[value.astimezone(tz).weekday()]


def _within_working_window(settings: OrganizationMemberAssignmentSettings, requested_at: datetime) -> bool:
    tz = _coerce_zoneinfo(settings.timezone)
    local_dt = requested_at.astimezone(tz)
    if _local_day(requested_at, tz) not in set(settings.working_days or []):
        return False
    if not settings.start_time or not settings.end_time:
        return False
    local_t = local_dt.time().replace(tzinfo=None)
    if settings.start_time <= settings.end_time:
        return settings.start_time <= local_t <= settings.end_time
    return local_t >= settings.start_time or local_t <= settings.end_time


def _same_local_day(left: datetime | None, right_day: date, tz: ZoneInfo) -> bool:
    if left is None:
        return False
    if left.tzinfo is None:
        left = left.replace(tzinfo=timezone.utc)
    return left.astimezone(tz).date() == right_day


def _is_clinic_emergency(organization: Organization, request_type: str, summary: str | None) -> bool:
    if organization.industry != "clinics":
        return False
    if request_type == "emergency_escalation":
        return True
    text = (summary or "").lower()
    return any(keyword in text for keyword in EMERGENCY_KEYWORDS)


class NokvoOneAssignmentService:
    @staticmethod
    async def assign_request(
        db: AsyncSession,
        organization: Organization,
        request_type: str,
        request_id: uuid.UUID | None = None,
        requested_time: datetime | None = None,
        summary: str | None = None,
        metadata: dict[str, Any] | None = None,
        record_type: str = "request",
    ) -> dict[str, Any]:
        request_type = request_type.strip().lower()
        allowed = allowed_request_types(organization.industry)
        if request_type not in allowed:
            raise ValueError(f"Request type '{request_type}' is not allowed for this Business Type")

        now = datetime.now(timezone.utc)
        requested_at = _aware_utc(requested_time)
        skipped: dict[str, list[str]] = {}

        record = await NokvoOneAssignmentService._get_or_create_request_record(
            db,
            organization.id,
            request_id,
            record_type,
            request_type,
            requested_at,
            summary,
            metadata or {},
        )

        if _is_clinic_emergency(organization, request_type, summary):
            data = dict(record.data or {})
            data.update(
                {
                    "request_type": "emergency_escalation",
                    "priority": "urgent",
                    "urgent_escalation": True,
                    "summary": summary,
                }
            )
            record.status = "new"
            record.data = data
            db.add(record)
            await db.flush()
            result = {
                "request_id": record.id,
                "organization_id": organization.id,
                "selected_member_id": None,
                "request_type": "emergency_escalation",
                "assignment_status": "no_available_member",
                "reason": "urgent_escalation_requires_staff_review",
                "active_load_count": None,
                "skipped_member_reasons": skipped,
                "timestamp": now,
            }
            await NokvoOneAssignmentService._audit(db, result)
            return result

        members = await NokvoOneAssignmentService._load_members(db, organization.id)
        settings_by_member = await NokvoOneAssignmentService._load_assignment_settings(db, organization.id)
        records = await NokvoOneAssignmentService._load_request_records(db, organization.id)
        clinic_settings_by_member = await NokvoOneAssignmentService._load_clinic_settings(db, organization.id)
        blocked_slots_by_member = await NokvoOneAssignmentService._load_blocked_slots(db, organization.id)

        candidates: list[AssignmentCandidate] = []
        for member in members:
            reasons: list[str] = []
            settings = settings_by_member.get(member.id)
            if member.status == "disabled":
                reasons.append("member_disabled")
            if settings is None:
                reasons.append("assignment_settings_missing")
            elif not settings.is_assignable:
                reasons.append("not_assignable")
            if settings is not None and request_type not in set(settings.request_types or []):
                reasons.append("request_type_not_supported")
            if settings is not None and not _within_working_window(settings, requested_at):
                reasons.append("outside_working_hours")

            active_load = NokvoOneAssignmentService._active_load(records, member.id)
            daily_count = NokvoOneAssignmentService._daily_count(records, member.id, requested_at, settings)
            hourly_count = NokvoOneAssignmentService._hourly_count(records, member.id, requested_at, settings)
            if (
                settings is not None
                and getattr(settings, "max_requests_per_hour", None) is not None
                and hourly_count >= int(settings.max_requests_per_hour)
            ):
                reasons.append("hourly_request_capacity_reached")

            if organization.industry == "clinics" and settings is not None:
                clinic_reasons = NokvoOneAssignmentService._clinic_skip_reasons(
                    request_type,
                    requested_at,
                    member.id,
                    clinic_settings_by_member.get(member.id),
                    blocked_slots_by_member.get(member.id, []),
                    records,
                    settings,
                )
                reasons.extend(clinic_reasons)

            if reasons:
                skipped[str(member.id)] = reasons
                continue
            if settings is not None:
                candidates.append(AssignmentCandidate(member, settings, active_load, daily_count, hourly_count))

        if not candidates:
            reason = "no_members_matched_assignment_rules"
            logger.debug(
                "Nokvo One assignment skipped organization=%s request=%s request_type=%s reasons=%s",
                organization.id,
                record.id,
                request_type,
                skipped,
            )
            result = {
                "request_id": record.id,
                "organization_id": organization.id,
                "selected_member_id": None,
                "request_type": request_type,
                "assignment_status": "no_available_member",
                "reason": reason,
                "active_load_count": None,
                "skipped_member_reasons": skipped,
                "timestamp": now,
            }
            await NokvoOneAssignmentService._audit(db, result)
            return result

        selected = sorted(candidates, key=lambda item: (item.active_load, item.hourly_count, item.daily_count, item.member.created_at or now))[0]
        data = dict(record.data or {})
        data.update(
            {
                "request_type": request_type,
                "assigned_member_id": str(selected.member.id),
                "assigned_at": now.isoformat(),
                "requested_time": requested_at.isoformat(),
                "assignment_method": "lowest_load",
            }
        )
        record.status = "assigned"
        record.data = data
        db.add(record)
        await db.flush()

        result = {
            "request_id": record.id,
            "organization_id": organization.id,
            "selected_member_id": selected.member.id,
            "request_type": request_type,
            "assignment_status": "assigned",
            "reason": None,
            "active_load_count": selected.active_load,
            "skipped_member_reasons": skipped,
            "timestamp": now,
        }
        await NokvoOneAssignmentService._audit(db, result)
        return result

    @staticmethod
    async def _get_or_create_request_record(
        db: AsyncSession,
        organization_id: uuid.UUID,
        request_id: uuid.UUID | None,
        record_type: str,
        request_type: str,
        requested_at: datetime,
        summary: str | None,
        metadata: dict[str, Any],
    ) -> NokvoOneToolRecord:
        if request_id:
            res = await db.execute(
                select(NokvoOneToolRecord).where(
                    NokvoOneToolRecord.id == request_id,
                    NokvoOneToolRecord.organization_id == organization_id,
                )
            )
            record = res.scalars().first()
            if record is None:
                raise ValueError("Request not found")
            data = dict(record.data or {})
            data.setdefault("request_type", request_type)
            data.setdefault("requested_time", requested_at.isoformat())
            if summary:
                data.setdefault("summary", summary)
            data.update(metadata or {})
            record.data = data
            return record

        record = NokvoOneToolRecord(
            id=uuid.uuid4(),
            organization_id=organization_id,
            record_type=record_type or "request",
            status="new",
            data={
                "request_type": request_type,
                "requested_time": requested_at.isoformat(),
                "summary": summary,
                **(metadata or {}),
            },
        )
        db.add(record)
        await db.flush()
        return record

    @staticmethod
    async def _load_members(db: AsyncSession, organization_id: uuid.UUID) -> list[OrganizationUser]:
        res = await db.execute(
            select(OrganizationUser)
            .where(OrganizationUser.organization_id == organization_id)
            .order_by(OrganizationUser.created_at.asc())
        )
        return list(res.scalars().all())

    @staticmethod
    async def _load_assignment_settings(
        db: AsyncSession, organization_id: uuid.UUID
    ) -> dict[uuid.UUID, OrganizationMemberAssignmentSettings]:
        res = await db.execute(
            select(OrganizationMemberAssignmentSettings).where(
                OrganizationMemberAssignmentSettings.organization_id == organization_id
            )
        )
        return {item.member_id: item for item in res.scalars().all()}

    @staticmethod
    async def _load_clinic_settings(
        db: AsyncSession, organization_id: uuid.UUID
    ) -> dict[uuid.UUID, ClinicMemberScheduleSettings]:
        res = await db.execute(
            select(ClinicMemberScheduleSettings).where(
                ClinicMemberScheduleSettings.organization_id == organization_id
            )
        )
        return {item.member_id: item for item in res.scalars().all()}

    @staticmethod
    async def _load_blocked_slots(db: AsyncSession, organization_id: uuid.UUID) -> dict[uuid.UUID, list[MemberBlockedSlot]]:
        res = await db.execute(
            select(MemberBlockedSlot).where(MemberBlockedSlot.organization_id == organization_id)
        )
        slots: dict[uuid.UUID, list[MemberBlockedSlot]] = {}
        for item in res.scalars().all():
            slots.setdefault(item.member_id, []).append(item)
        return slots

    @staticmethod
    async def _load_request_records(db: AsyncSession, organization_id: uuid.UUID) -> list[NokvoOneToolRecord]:
        res = await db.execute(
            select(NokvoOneToolRecord).where(NokvoOneToolRecord.organization_id == organization_id)
        )
        return list(res.scalars().all())

    @staticmethod
    def _active_load(records: list[NokvoOneToolRecord], member_id: uuid.UUID) -> int:
        member_key = str(member_id)
        return sum(
            1
            for record in records
            if record.status in ACTIVE_REQUEST_STATUSES
            and str((record.data or {}).get("assigned_member_id")) == member_key
        )

    @staticmethod
    def _daily_count(
        records: list[NokvoOneToolRecord],
        member_id: uuid.UUID,
        requested_at: datetime,
        settings: OrganizationMemberAssignmentSettings | None,
    ) -> int:
        if settings is None:
            return 0
        tz = _coerce_zoneinfo(settings.timezone)
        day = requested_at.astimezone(tz).date()
        member_key = str(member_id)
        count = 0
        for record in records:
            if record.status in NON_DAILY_COUNT_STATUSES:
                continue
            if str((record.data or {}).get("assigned_member_id")) != member_key:
                continue
            if _same_local_day(record.created_at, day, tz):
                count += 1
        return count

    @staticmethod
    def _hourly_count(
        records: list[NokvoOneToolRecord],
        member_id: uuid.UUID,
        requested_at: datetime,
        settings: OrganizationMemberAssignmentSettings | None,
    ) -> int:
        if settings is None:
            return 0
        tz = _coerce_zoneinfo(settings.timezone)
        requested_local = requested_at.astimezone(tz)
        member_key = str(member_id)
        count = 0
        for record in records:
            if record.status in NON_DAILY_COUNT_STATUSES:
                continue
            if str((record.data or {}).get("assigned_member_id")) != member_key:
                continue
            request_value = (record.data or {}).get("requested_time") or record.created_at
            try:
                record_at = datetime.fromisoformat(request_value) if isinstance(request_value, str) else request_value
            except ValueError:
                record_at = record.created_at
            if record_at is None:
                continue
            record_local = _aware_utc(record_at).astimezone(tz)
            if record_local.date() == requested_local.date() and record_local.hour == requested_local.hour:
                count += 1
        return count

    @staticmethod
    def _clinic_skip_reasons(
        request_type: str,
        requested_at: datetime,
        member_id: uuid.UUID,
        clinic_settings: ClinicMemberScheduleSettings | None,
        blocked_slots: list[MemberBlockedSlot],
        records: list[NokvoOneToolRecord],
        assignment_settings: OrganizationMemberAssignmentSettings,
    ) -> list[str]:
        if clinic_settings is None:
            return ["clinic_schedule_missing"]
        consultation_types = set(clinic_settings.consultation_types or [])
        consultation_supported = request_type in consultation_types or (
            request_type == "appointment" and bool(consultation_types)
        )
        if not consultation_supported:
            return ["consultation_type_not_supported"]

        duration = int(clinic_settings.appointment_duration_minutes or 30)
        buffer_minutes = int(clinic_settings.buffer_minutes or 0)
        end_at = requested_at + timedelta(minutes=duration + buffer_minutes)
        for slot in blocked_slots:
            slot_start = _aware_utc(slot.start_time)
            slot_end = _aware_utc(slot.end_time)
            if requested_at < slot_end and end_at > slot_start:
                return ["blocked_slot_conflict"]

        tz = _coerce_zoneinfo(assignment_settings.timezone)
        member_key = str(member_id)
        requested_local = requested_at.astimezone(tz)
        hour_count = 0
        day_count = 0
        for record in records:
            if str((record.data or {}).get("assigned_member_id")) != member_key:
                continue
            if record.status == "cancelled":
                continue
            appointment_value = (record.data or {}).get("requested_time") or (record.data or {}).get("appointment_start")
            try:
                appointment_at = datetime.fromisoformat(appointment_value) if appointment_value else record.created_at
            except ValueError:
                appointment_at = record.created_at
            if appointment_at is None:
                continue
            appointment_at = _aware_utc(appointment_at)
            appointment_local = appointment_at.astimezone(tz)
            if appointment_local.date() == requested_local.date():
                day_count += 1
                if appointment_local.hour == requested_local.hour:
                    hour_count += 1

        if clinic_settings.max_patients_per_hour is not None and hour_count >= int(clinic_settings.max_patients_per_hour):
            return ["hourly_patient_capacity_reached"]
        if clinic_settings.max_patients_per_day is not None and day_count >= int(clinic_settings.max_patients_per_day):
            return ["daily_patient_capacity_reached"]
        return []

    @staticmethod
    async def _audit(db: AsyncSession, result: dict[str, Any]) -> None:
        logger.debug(
            "Nokvo One assignment decision request=%s org=%s selected=%s type=%s status=%s reason=%s load=%s skipped=%s",
            result.get("request_id"),
            result.get("organization_id"),
            result.get("selected_member_id"),
            result.get("request_type"),
            result.get("assignment_status"),
            result.get("reason"),
            result.get("active_load_count"),
            result.get("skipped_member_reasons"),
        )
        db.add(
            NokvoOneAssignmentAuditLog(
                id=uuid.uuid4(),
                request_id=result.get("request_id"),
                organization_id=result["organization_id"],
                selected_member_id=result.get("selected_member_id"),
                request_type=result["request_type"],
                assignment_status=result["assignment_status"],
                reason=result.get("reason"),
                skipped_member_reasons=result.get("skipped_member_reasons") or {},
                selected_member_active_load=result.get("active_load_count"),
                created_at=result.get("timestamp") or datetime.now(timezone.utc),
            )
        )
        await db.flush()
