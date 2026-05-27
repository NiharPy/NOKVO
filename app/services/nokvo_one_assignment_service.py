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
    OrganizationBlockedSlot,
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
SLOT_SEARCH_HORIZON_DAYS = 14


@dataclass
class AssignmentCandidate:
    member: OrganizationUser
    settings: OrganizationMemberAssignmentSettings
    active_load: int
    daily_count: int
    hourly_count: int
    scheduled_at: datetime
    shift_minutes: int


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


def _appointment_start_from_record(record: NokvoOneToolRecord) -> datetime | None:
    data = record.data or {}
    raw = data.get("scheduled_time") or data.get("appointment_start") or data.get("requested_time")
    if raw is None:
        raw = record.created_at
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return _aware_utc(raw)
    try:
        return _aware_utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
    except ValueError:
        return _aware_utc(record.created_at) if record.created_at else None


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
                "requested_time": requested_at,
                "scheduled_time": None,
                "time_adjusted": False,
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

            if reasons:
                skipped[str(member.id)] = reasons
                continue
            if settings is None:
                continue

            active_load = NokvoOneAssignmentService._active_load(records, member.id)
            if (
                getattr(settings, "max_active_requests", None) is not None
                and active_load >= int(settings.max_active_requests)
            ):
                skipped[str(member.id)] = ["active_request_capacity_reached"]
                continue

            clinic_settings = clinic_settings_by_member.get(member.id)
            if organization.industry == "clinics" and clinic_settings is not None:
                consultation_types = set(clinic_settings.consultation_types or [])
                if (
                    consultation_types
                    and request_type not in consultation_types
                    and request_type != "appointment"
                ):
                    skipped[str(member.id)] = ["consultation_type_not_supported"]
                    continue

            member_blocks = list(blocked_slots_by_member.get(member.id, []))
            member_blocks.extend(blocked_slots_by_member.get("_org_wide", []))  # type: ignore[arg-type]
            slot_result = NokvoOneAssignmentService._find_next_available_slot(
                member_id=member.id,
                requested_at=requested_at,
                settings=settings,
                clinic_settings=clinic_settings if organization.industry == "clinics" else None,
                blocked_slots=member_blocks,
                records=records,
                exclude_record_id=record.id,
            )
            if slot_result is None:
                skipped[str(member.id)] = ["no_available_slot_within_horizon"]
                continue

            scheduled_at, shift_minutes = slot_result
            daily_count = NokvoOneAssignmentService._daily_count(records, member.id, scheduled_at, settings)
            hourly_count = NokvoOneAssignmentService._hourly_count(records, member.id, scheduled_at, settings)
            candidates.append(
                AssignmentCandidate(
                    member=member,
                    settings=settings,
                    active_load=active_load,
                    daily_count=daily_count,
                    hourly_count=hourly_count,
                    scheduled_at=scheduled_at,
                    shift_minutes=shift_minutes,
                )
            )

        if not candidates:
            reason = "no_members_matched_assignment_rules"
            data = dict(record.data or {})
            data.update(
                {
                    "request_type": request_type,
                    "requested_time": requested_at.isoformat(),
                    "assignment_status": "no_available_member",
                    "assignment_reason": reason,
                    "skipped_member_reasons": skipped,
                }
            )
            record.data = data
            db.add(record)
            await db.flush()
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
                "requested_time": requested_at,
                "scheduled_time": None,
                "time_adjusted": False,
                "timestamp": now,
            }
            await NokvoOneAssignmentService._audit(db, result)
            return result

        # Time-first selection: prefer the candidate whose slot is
        # CLOSEST to what the caller asked for, regardless of which
        # member it belongs to. So if Member A's 10am is booked and
        # Member B's 10am is free, Member B (shift=0) wins over Member
        # A (shift=60). active_load + hourly_count + daily_count are
        # only used to break ties on equal shift_minutes — they never
        # rescue a member with a worse time. created_at is the final
        # deterministic tiebreaker so two equally-eligible members
        # always resolve identically.
        selected = sorted(
            candidates,
            key=lambda item: (
                item.shift_minutes,
                item.active_load,
                item.hourly_count,
                item.daily_count,
                item.member.created_at or now,
            ),
        )[0]
        scheduled_at = selected.scheduled_at
        time_adjusted = scheduled_at != requested_at

        data = dict(record.data or {})
        data.update(
            {
                "request_type": request_type,
                "assigned_member_id": str(selected.member.id),
                "assigned_member_name": selected.member.full_name,
                "assigned_at": now.isoformat(),
                "requested_time": scheduled_at.isoformat(),
                "scheduled_time": scheduled_at.isoformat(),
                "original_requested_time": requested_at.isoformat(),
                "time_adjusted": time_adjusted,
                "assignment_method": "lowest_load",
                "assignment_status": "assigned",
            }
        )
        if organization.industry == "clinics" and record.record_type == "appointment":
            data["doctor"] = selected.member.full_name
            data["assigned_doctor_id"] = str(selected.member.id)
            data["appointment_time"] = scheduled_at.isoformat()
            data["appointment_start"] = scheduled_at.isoformat()
        elif organization.industry == "real_estate":
            data["agent"] = selected.member.full_name
            data["assigned_agent_id"] = str(selected.member.id)
            data["assigned_to"] = selected.member.full_name
            data["visit_at"] = scheduled_at.isoformat()
        record.status = "assigned"
        record.data = data
        db.add(record)
        await db.flush()

        result = {
            "request_id": record.id,
            "organization_id": organization.id,
            "selected_member_id": selected.member.id,
            "selected_member_name": selected.member.full_name,
            "request_type": request_type,
            "assignment_status": "assigned",
            "reason": None,
            "active_load_count": selected.active_load,
            "skipped_member_reasons": skipped,
            "requested_time": requested_at,
            "scheduled_time": scheduled_at,
            "time_adjusted": time_adjusted,
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
        """Per-member blocked slots PLUS org-wide closures (public holidays,
        festivals, maintenance days). Org-wide slots are materialised against
        every member that has at least one entry so the scheduler treats them
        uniformly — and added under a sentinel `None` member key so members
        with no individual blocks still inherit them.
        """
        res = await db.execute(
            select(MemberBlockedSlot).where(MemberBlockedSlot.organization_id == organization_id)
        )
        slots: dict[uuid.UUID, list[MemberBlockedSlot]] = {}
        for item in res.scalars().all():
            slots.setdefault(item.member_id, []).append(item)

        # Org-wide closures: load and wrap each one in a MemberBlockedSlot-shaped
        # object so the existing conflict logic keeps working unchanged. We
        # attach them under a fresh key on lookup (see _find_next_available_slot).
        try:
            org_res = await db.execute(
                select(OrganizationBlockedSlot).where(
                    OrganizationBlockedSlot.organization_id == organization_id
                )
            )
            org_blocks = list(org_res.scalars().all())
        except Exception:
            org_blocks = []
        if org_blocks:
            slots.setdefault("_org_wide", []).extend(org_blocks)  # type: ignore[arg-type]
        return slots

    @staticmethod
    def _blocked_slot_conflict(
        requested_at: datetime,
        blocked_slots: list[MemberBlockedSlot],
        *,
        duration_minutes: int = 30,
    ) -> bool:
        end_at = requested_at + timedelta(minutes=duration_minutes)
        for slot in blocked_slots:
            slot_start = _aware_utc(slot.start_time)
            slot_end = _aware_utc(slot.end_time)
            if requested_at < slot_end and end_at > slot_start:
                return True
        return False

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
            appointment_at = _appointment_start_from_record(record)
            if appointment_at is None:
                continue
            if appointment_at.astimezone(tz).date() == day:
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
            appointment_at = _appointment_start_from_record(record)
            if appointment_at is None:
                continue
            record_local = appointment_at.astimezone(tz)
            if record_local.date() == requested_local.date() and record_local.hour == requested_local.hour:
                count += 1
        return count

    @staticmethod
    def _existing_intervals_for_member(
        records: list[NokvoOneToolRecord],
        member_id: uuid.UUID,
        default_duration_minutes: int,
        exclude_record_id: uuid.UUID | None,
    ) -> list[tuple[datetime, datetime]]:
        member_key = str(member_id)
        intervals: list[tuple[datetime, datetime]] = []
        for record in records:
            if record.status in NON_DAILY_COUNT_STATUSES:
                continue
            if exclude_record_id is not None and record.id == exclude_record_id:
                continue
            if str((record.data or {}).get("assigned_member_id")) != member_key:
                continue
            start = _appointment_start_from_record(record)
            if start is None:
                continue
            duration = int((record.data or {}).get("appointment_duration_minutes") or default_duration_minutes)
            intervals.append((start, start + timedelta(minutes=max(1, duration))))
        return intervals

    @staticmethod
    def _next_working_window_start(
        settings: OrganizationMemberAssignmentSettings,
        from_at: datetime,
        tz: ZoneInfo,
        horizon: datetime,
    ) -> datetime | None:
        working_days = set(settings.working_days or [])
        if not working_days or not settings.start_time or not settings.end_time:
            return None
        cursor = from_at.astimezone(tz)
        for _ in range(SLOT_SEARCH_HORIZON_DAYS + 1):
            day_slug = DAY_INDEX[cursor.weekday()]
            if day_slug in working_days:
                start_local = cursor.replace(
                    hour=settings.start_time.hour,
                    minute=settings.start_time.minute,
                    second=0,
                    microsecond=0,
                )
                end_local = cursor.replace(
                    hour=settings.end_time.hour,
                    minute=settings.end_time.minute,
                    second=0,
                    microsecond=0,
                )
                local_t = cursor.time().replace(tzinfo=None)
                if settings.start_time <= local_t <= settings.end_time:
                    candidate = cursor
                elif local_t < settings.start_time:
                    candidate = start_local
                else:
                    candidate = None
                if candidate is not None and candidate <= end_local:
                    utc_candidate = candidate.astimezone(timezone.utc)
                    if utc_candidate > horizon:
                        return None
                    return utc_candidate
            cursor = (cursor + timedelta(days=1)).replace(
                hour=settings.start_time.hour,
                minute=settings.start_time.minute,
                second=0,
                microsecond=0,
            )
            if cursor.astimezone(timezone.utc) > horizon:
                return None
        return None

    @staticmethod
    def _working_window_end_for(
        settings: OrganizationMemberAssignmentSettings,
        moment: datetime,
        tz: ZoneInfo,
    ) -> datetime:
        local = moment.astimezone(tz)
        end_local = local.replace(
            hour=settings.end_time.hour,
            minute=settings.end_time.minute,
            second=0,
            microsecond=0,
        )
        return end_local.astimezone(timezone.utc)

    @staticmethod
    def _find_next_available_slot(
        *,
        member_id: uuid.UUID,
        requested_at: datetime,
        settings: OrganizationMemberAssignmentSettings,
        clinic_settings: ClinicMemberScheduleSettings | None,
        blocked_slots: list[MemberBlockedSlot],
        records: list[NokvoOneToolRecord],
        exclude_record_id: uuid.UUID | None,
    ) -> tuple[datetime, int] | None:
        tz = _coerce_zoneinfo(settings.timezone)
        general_duration = int(getattr(settings, "appointment_duration_minutes", None) or 30)
        clinic_duration = int(getattr(clinic_settings, "appointment_duration_minutes", None) or general_duration) if clinic_settings else general_duration
        duration = max(5, clinic_duration if clinic_settings else general_duration)
        buffer_minutes = int(getattr(clinic_settings, "buffer_minutes", None) or 0) if clinic_settings else 0
        max_per_hour = (
            int(clinic_settings.max_patients_per_hour)
            if clinic_settings and clinic_settings.max_patients_per_hour is not None
            else int(getattr(settings, "max_requests_per_hour", None) or 0) or None
        )
        max_per_day = (
            int(clinic_settings.max_patients_per_day)
            if clinic_settings and clinic_settings.max_patients_per_day is not None
            else (int(settings.max_requests_per_day) if settings.max_requests_per_day is not None else None)
        )
        intervals = NokvoOneAssignmentService._existing_intervals_for_member(
            records, member_id, duration, exclude_record_id
        )
        step = max(5, min(duration, 15))
        horizon = requested_at + timedelta(days=SLOT_SEARCH_HORIZON_DAYS)

        candidate = requested_at
        guard = 0
        while candidate <= horizon and guard < 5000:
            guard += 1
            if not _within_working_window(settings, candidate):
                next_start = NokvoOneAssignmentService._next_working_window_start(
                    settings, candidate, tz, horizon
                )
                if next_start is None:
                    return None
                candidate = next_start
                continue

            candidate_end = candidate + timedelta(minutes=duration + buffer_minutes)
            window_end = NokvoOneAssignmentService._working_window_end_for(settings, candidate, tz)
            if candidate_end > window_end:
                next_day = (candidate.astimezone(tz) + timedelta(days=1)).replace(
                    hour=settings.start_time.hour,
                    minute=settings.start_time.minute,
                    second=0,
                    microsecond=0,
                )
                candidate = next_day.astimezone(timezone.utc)
                continue

            if NokvoOneAssignmentService._blocked_slot_conflict(
                candidate, blocked_slots, duration_minutes=duration + buffer_minutes
            ):
                candidate = candidate + timedelta(minutes=step)
                continue

            overlap = False
            for start, end in intervals:
                if candidate < end and candidate_end > start:
                    overlap = True
                    candidate = end
                    break
            if overlap:
                continue

            local = candidate.astimezone(tz)
            hour_count = 0
            day_count = 0
            for start, _end in intervals:
                start_local = start.astimezone(tz)
                if start_local.date() == local.date():
                    day_count += 1
                    if start_local.hour == local.hour:
                        hour_count += 1
            if max_per_hour is not None and hour_count >= max_per_hour:
                next_hour_local = (local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
                candidate = next_hour_local.astimezone(timezone.utc)
                continue
            if max_per_day is not None and day_count >= max_per_day:
                next_day_local = (local + timedelta(days=1)).replace(
                    hour=settings.start_time.hour,
                    minute=settings.start_time.minute,
                    second=0,
                    microsecond=0,
                )
                candidate = next_day_local.astimezone(timezone.utc)
                continue

            shift_seconds = int((candidate - requested_at).total_seconds())
            shift_minutes = max(0, shift_seconds // 60)
            return candidate, shift_minutes

        return None

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
