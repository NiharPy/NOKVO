"""Clinic appointment parsing and FSM: spoken date/time parsers, slot
availability handling, and the deterministic turn-policy executor.

Extracted from nokvo_one_voice_pipeline.py (turn_router helpers pattern:
functions taking ``helpers`` receive the ``NokvoOneVoicePipeline`` class and
call sibling statics through it, so class-attribute monkeypatches keep
working). The pipeline class keeps delegating wrappers - no API change.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo
import asyncio
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.tenant_resources import TenantResources
from app.services.datetime_parse import DateTimeParseError
from app.services.dynamic_tool_resolver import resolve_index
from app.services.predefined_tools_service import PredefinedToolsService
from app.services.sarvam_voice_service import SARVAM_LANGUAGE_OPTIONS, SarvamVoiceService
from app.services.voice_turn_policy import normalize_relative_datetime_text

logger = logging.getLogger(__name__)


_APPOINTMENT_LOCAL_TZ = ZoneInfo("Asia/Kolkata")


_MONTH_INDEX = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


_WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


_WEEKDAY_RE = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b")


# Spoken ordinals → day-of-month, so "first of July" / "the twenty third" parse.
_ORDINAL_WORDS: dict[str, int] = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11,
    "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
    "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
    "twentieth": 20, "twenty first": 21, "twenty second": 22, "twenty third": 23,
    "twenty fourth": 24, "twenty fifth": 25, "twenty sixth": 26, "twenty seventh": 27,
    "twenty eighth": 28, "twenty ninth": 29, "thirtieth": 30, "thirty first": 31,
}


def _next_day_of_month(day: int, today: "datetime.date"):
    """Next calendar date with the given day-of-month, today or later."""
    year, month = today.year, today.month
    for _ in range(13):
        try:
            candidate = datetime(year, month, day, tzinfo=_APPOINTMENT_LOCAL_TZ).date()
        except ValueError:
            candidate = None
        if candidate is not None and candidate >= today:
            return candidate
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return None


# Canonical date/time parse error lives in app.services.datetime_parse; this
# stays as a thin subclass so the appointment handler's existing
# ``except _AppointmentToolInputError`` clauses keep working while the parser
# logic is consolidated behind one module.
class _AppointmentToolInputError(DateTimeParseError):
    pass


def _parse_appointment_date(value: Any, *, now: datetime | None = None) -> datetime.date:
    raw = re.sub(r"\s+", " ", normalize_relative_datetime_text(str(value or "")).strip().lower())
    local_now = (now or datetime.now(timezone.utc)).astimezone(_APPOINTMENT_LOCAL_TZ)
    today = local_now.date()
    if not raw:
        raise _AppointmentToolInputError("preferred_date", "Which date should I note for the appointment?")
    # ISO `YYYY-MM-DD` (or `YYYY-MM-DDTHH:MM:SS…`) — emitted by the
    # slot-acceptance path and by any external integration. The legacy
    # numeric regex below treats this as DD/MM and produces month=20
    # nonsense, hence the explicit branch.
    iso_match = re.match(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if iso_match:
        try:
            year = int(iso_match.group(1))
            month = int(iso_match.group(2))
            day = int(iso_match.group(3))
            return datetime(year, month, day, tzinfo=_APPOINTMENT_LOCAL_TZ).date()
        except ValueError:
            pass
    if "day after tomorrow" in raw:
        return today + timedelta(days=2)
    if "tomorrow" in raw:
        return today + timedelta(days=1)
    if "today" in raw:
        return today
    # "in/after N days" → concrete offset.
    rel_days = re.search(r"\b(?:in|after)\s+(\d{1,2})\s+days?\b", raw)
    if rel_days:
        n = int(rel_days.group(1))
        if 0 < n <= 60:
            return today + timedelta(days=n)
    # "this/next weekend" → the upcoming Saturday.
    if "weekend" in raw:
        delta = (5 - today.weekday()) % 7
        return today + timedelta(days=delta or 7)
    # Weekday name — word-boundary match (so "mondayish" doesn't match) and
    # ALWAYS the upcoming occurrence, never today (fixes "Monday"/"next
    # Monday" resolving to today when today is that weekday).
    weekday_match = _WEEKDAY_RE.search(raw)
    if weekday_match:
        target = _WEEKDAY_INDEX[weekday_match.group(1)]
        delta = (target - today.weekday()) % 7
        if delta == 0:
            delta = 7
        return today + timedelta(days=delta)

    numeric = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", raw)
    if numeric:
        day = int(numeric.group(1))
        month = int(numeric.group(2))
        year = int(numeric.group(3) or today.year)
        if year < 100:
            year += 2000
        try:
            parsed = datetime(year, month, day, tzinfo=_APPOINTMENT_LOCAL_TZ).date()
        except ValueError as exc:
            raise _AppointmentToolInputError(
                "preferred_date",
                "That date does not look valid. Which date should I note?",
                clear_date=True,
            ) from exc
        return parsed if parsed >= today or numeric.group(3) else parsed.replace(year=parsed.year + 1)

    named = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)(?:\s+(\d{2,4}))?\b", raw)
    if not named:
        named = re.search(r"\b([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s+(\d{2,4}))?\b", raw)
        if named:
            month_token, day_token, year_token = named.group(1), named.group(2), named.group(3)
        else:
            month_token = day_token = year_token = None
    else:
        day_token, month_token, year_token = named.group(1), named.group(2), named.group(3)
    if day_token and month_token:
        month = _MONTH_INDEX.get(month_token[:3], _MONTH_INDEX.get(month_token))
        if month:
            year = int(year_token or today.year)
            if year < 100:
                year += 2000
            try:
                parsed = datetime(year, month, int(day_token), tzinfo=_APPOINTMENT_LOCAL_TZ).date()
            except ValueError as exc:
                raise _AppointmentToolInputError(
                    "preferred_date",
                    "That date does not look valid. Which date should I note?",
                    clear_date=True,
                ) from exc
            return parsed if parsed >= today or year_token else parsed.replace(year=parsed.year + 1)

    # Bare day-of-month with an ordinal suffix: "the 15th", "15th", "on the 3rd".
    # (Requires the suffix so a stray "15" isn't mistaken for a date.)
    bare_dom = re.search(r"\b(?:on\s+the\s+|the\s+)?(\d{1,2})(?:st|nd|rd|th)\b", raw)
    if bare_dom:
        cand = _next_day_of_month(int(bare_dom.group(1)), today)
        if cand is not None:
            return cand
        raise _AppointmentToolInputError(
            "preferred_date",
            "That date does not look valid. Which date should I note?",
            clear_date=True,
        )

    # Spoken word ordinals: "first of July", "the twenty third". Longest
    # phrase first so "twenty first" beats "first".
    for word in sorted(_ORDINAL_WORDS, key=len, reverse=True):
        if re.search(rf"\b{word}\b", raw):
            day = _ORDINAL_WORDS[word]
            month_for_word = None
            for mname, mnum in _MONTH_INDEX.items():
                if re.search(rf"\b{mname}\b", raw):
                    month_for_word = mnum
                    break
            if month_for_word is not None:
                try:
                    cand = datetime(today.year, month_for_word, day, tzinfo=_APPOINTMENT_LOCAL_TZ).date()
                except ValueError:
                    cand = None
                if cand is not None:
                    return cand if cand >= today else cand.replace(year=cand.year + 1)
            cand = _next_day_of_month(day, today)
            if cand is not None:
                return cand
            break

    raise _AppointmentToolInputError(
        "preferred_date",
        "I need the appointment date clearly. Which date should I note?",
        clear_date=True,
    )


def _parse_appointment_time(value: Any) -> time:
    raw = re.sub(r"\s+", " ", normalize_relative_datetime_text(str(value or "")).strip().lower())
    # STT often emits dotted meridiems ("8 p.m.", "9 a. m."). Collapse them to
    # bare "pm"/"am" so the AM/PM matcher below (which needs a contiguous
    # token) fires — otherwise "8 p.m." falls through and raises, and callers
    # silently lose the time (e.g. the out-of-hours guard couldn't see 8 PM).
    raw = re.sub(r"\b([ap])\.\s*m\.?", r"\1m", raw)
    if not raw:
        raise _AppointmentToolInputError("preferred_time", "What time should I note for the appointment?")
    # Midnight is out of booking hours — clarify instead of resolving it (it
    # used to substring-match "night" and book 7 PM).
    if re.search(r"\bmidnight\b", raw):
        raise _AppointmentToolInputError(
            "preferred_time",
            "We don't book at midnight — what daytime works for you?",
            clear_time=True,
        )

    def _daytime_hour(h: int) -> int | None:
        """Map a 1–12 spoken hour to 24h assuming a daytime booking
        (8–11 → AM, 12 → noon, 1–7 → PM). Returns None when ambiguous."""
        if h == 12:
            return 12
        if 8 <= h <= 11:
            return h
        if 1 <= h <= 7:
            return h + 12
        return None

    # Spoken fractions: "half past 4", "quarter past 4", "quarter to 5".
    half = re.search(r"\bhalf\s*past\s+(\d{1,2})\b", raw)
    if half:
        hh = _daytime_hour(int(half.group(1)))
        if hh is not None:
            return time(hh, 30)
    qpast = re.search(r"\bquarter\s*past\s+(\d{1,2})\b", raw)
    if qpast:
        hh = _daytime_hour(int(qpast.group(1)))
        if hh is not None:
            return time(hh, 15)
    qto = re.search(r"\bquarter\s*to\s+(\d{1,2})\b", raw)
    if qto:
        base = int(qto.group(1)) - 1
        hh = _daytime_hour(base if base >= 1 else 12)
        if hh is not None:
            return time(hh, 45)

    named_times = {
        "morning": time(9, 0),
        "afternoon": time(14, 0),
        "evening": time(17, 0),
        "night": time(19, 0),
        "noon": time(12, 0),
    }
    for label, parsed in named_times.items():
        if re.search(rf"\b{label}\b", raw):
            return parsed
    ampm = re.search(r"\b(\d{1,2})(?::([0-5]\d))?\s*(am|pm)\b", raw)
    if ampm:
        hour = int(ampm.group(1))
        minute = int(ampm.group(2) or 0)
        suffix = ampm.group(3)
        if hour < 1 or hour > 12:
            raise _AppointmentToolInputError(
                "preferred_time",
                "That time does not look valid. What time should I note?",
                clear_time=True,
            )
        if suffix == "pm" and hour != 12:
            hour += 12
        if suffix == "am" and hour == 12:
            hour = 0
        return time(hour, minute)
    twenty_four = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", raw)
    if twenty_four:
        return time(int(twenty_four.group(1)), int(twenty_four.group(2)))
    bare = re.fullmatch(r"(?:at\s+)?(\d{1,2})(?:\s*ish)?", raw)
    if bare:
        hour = int(bare.group(1))
        if 13 <= hour <= 23:
            return time(hour, 0)
        inferred = _daytime_hour(hour)
        if inferred is not None:
            return time(inferred, 0)
        raise _AppointmentToolInputError(
            "preferred_time",
            f"Just to confirm, is that {hour} AM or {hour} PM?",
            clear_time=True,
        )
    raise _AppointmentToolInputError(
        "preferred_time",
        "I need the appointment time clearly. What time should I note?",
        clear_time=True,
    )


def _appointment_datetime_iso(helpers: Any, appointment: dict[str, Any]) -> str:
    # Fast path: caller already accepted a proposed slot, which left a
    # canonical UTC ISO on the appointment. Trust it and skip re-parsing.
    proposed = appointment.get("appointment_time")
    if isinstance(proposed, str) and proposed:
        try:
            parsed = datetime.fromisoformat(proposed.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if parsed.astimezone(_APPOINTMENT_LOCAL_TZ) > datetime.now(_APPOINTMENT_LOCAL_TZ):
                return parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    local_date = helpers._parse_appointment_date(appointment.get("preferred_date"))
    local_time = helpers._parse_appointment_time(appointment.get("preferred_time"))
    local_dt = datetime.combine(local_date, local_time, tzinfo=_APPOINTMENT_LOCAL_TZ)
    if local_dt <= datetime.now(_APPOINTMENT_LOCAL_TZ):
        raise _AppointmentToolInputError(
            "preferred_date",
            "That appointment time is already past. Which future date and time should I note?",
            clear_date=True,
            clear_time=True,
        )
    return local_dt.astimezone(timezone.utc).isoformat()


def _should_offer_sms_confirmation(tenant_res: TenantResources | None) -> bool:
    """Return True only when the tenant has explicitly opted into the
    end-of-booking SMS confirmation offer. The platform default is False
    because SMS dispatch isn't wired in yet — offering a confirmation
    that never arrives is a worse caller experience than offering
    nothing. Tenants enable it via
    ``provider_status['agent_offer_sms_confirmation'] = True`` once
    their SMS gateway is connected."""
    if tenant_res is None:
        return bool(settings.NOKVO_AGENT_OFFER_SMS_CONFIRMATION)
    override = (tenant_res.provider_status or {}).get("agent_offer_sms_confirmation")
    if override is None:
        return bool(settings.NOKVO_AGENT_OFFER_SMS_CONFIRMATION)
    return bool(override)


def _appointment_tool_answer(
    result: dict[str, Any],
    args: dict[str, Any],
    *,
    language: str | None = None,
    offer_sms: bool = False,
) -> str:
    patient = str(args.get("patient_name") or "the patient")
    when = str(args.get("appointment_time") or "the requested time")
    try:
        parsed = datetime.fromisoformat(when.replace("Z", "+00:00"))
        local_when = parsed.astimezone(_APPOINTMENT_LOCAL_TZ).strftime("%d %b %Y at %I:%M %p")
    except Exception:
        local_when = when
    assignment_status = result.get("assignment_status")
    assigned_name = result.get("assigned_member_name")
    lang = SarvamVoiceService.normalize_language(language)
    if lang == "te":
        if assignment_status == "assigned" and assigned_name:
            return (
                f"Appointment request create అయ్యింది for {patient} on {local_when}. "
                f"It has been assigned to {assigned_name}."
            )
        if assignment_status == "no_available_member":
            return (
                f"Appointment request create అయ్యింది for {patient} on {local_when}. "
                "That slot note చేశాను, కానీ available doctor system లో కనిపించలేదు. "
                "Clinic team availability confirm చేస్తారు."
            )
        return (
            f"Appointment request create అయ్యింది for {patient} on {local_when}. "
            "Clinic team exact availability confirm చేస్తారు."
        )
    phone = str(args.get("phone") or "").strip()
    # End-of-call SMS offer is opt-in: empty unless the tenant has
    # wired SMS dispatch and toggled ``agent_offer_sms_confirmation``.
    sms_offer = ""
    if offer_sms and phone:
        spoken_phone = " ".join(list(phone[-10:])) if phone[-10:].isdigit() else phone
        if lang == "te":
            sms_offer = f" {spoken_phone} కి confirmation SMS పంపాలా?"
        elif lang == "hi":
            sms_offer = f" क्या {spoken_phone} पर confirmation SMS भेज दूँ?"
        else:
            sms_offer = f" Want me to send a confirmation SMS to {spoken_phone}?"
    if assignment_status == "assigned" and assigned_name:
        return (
            f"I have created the appointment request for {patient} on {local_when}. "
            f"It has been assigned to {assigned_name}.{sms_offer}"
        )
    if assignment_status == "no_available_member":
        return (
            f"I have created the appointment request for {patient} on {local_when}. "
            "That time is noted, but I could not find an available doctor in the system for that slot, "
            f"so the clinic team will confirm availability.{sms_offer}"
        )
    return (
        f"I have created the appointment request for {patient} on {local_when}. "
        f"The clinic team can confirm exact availability.{sms_offer}"
    )


async def _handle_availability_check(
    helpers: Any,
    tenant_res: TenantResources,
    db: AsyncSession | None,
    turn_policy: dict[str, Any],
) -> dict[str, Any] | None:
    """Consult the scheduler when the caller asks "is X available?" /
    "when can you book me?". Works across business types — picks the
    right request_type from the active flow or the industry default.
    Returns ``None`` when no scheduling-shaped flow applies (e.g.,
    ecommerce ticket creation), so the pipeline can fall back to RAG."""

    def _first_truthy(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for key in keys:
            value = mapping.get(key)
            if value:
                return value
        return None

    from app.services.nokvo_one_assignment_service import (
        NokvoOneAssignmentService,
        _aware_utc,
    )

    if db is None:
        return None
    context = await helpers._voice_business_context(db, tenant_res)
    if context is None:
        return None
    organization, _overrides, _custom_tabs = context

    # Identify the request_type to schedule against. Priority:
    #   1) the active appointment FSM (clinics) → "appointment"
    #   2) the active generic tool_flow → derived from flow_key
    #   3) industry default
    # If no scheduling-shaped flow applies, return None.
    state_patch = turn_policy.get("state_patch") or {}
    appointment = dict(state_patch.get("appointment") or {})
    tool_flow_state = dict(state_patch.get("tool_flow") or {})
    flow_key = str(tool_flow_state.get("flow_key") or "")
    industry = (organization.industry or "").lower()
    _FLOW_TO_REQUEST_TYPE = {
        "real_estate_site_visit": "site_visit",
    }
    _INDUSTRY_DEFAULT = {
        "clinics": "appointment",
        "real_estate": "site_visit",
        "hospitality": "callback",
    }
    request_type = _FLOW_TO_REQUEST_TYPE.get(flow_key) or _INDUSTRY_DEFAULT.get(industry)
    if not request_type:
        return None

    entities = turn_policy.get("entities") or {}
    language = turn_policy.get("language")

    # Resolve the candidate datetime in priority order:
    #   1) this turn's spoken date+time
    #   2) the in-progress appointment slot values
    #   3) "now" — caller asked "when can you book?" with no time
    # Source of the requested time: this turn's entities, or the in-progress
    # appointment / tool_flow slots, depending on which flow is active.
    collected = dict(tool_flow_state.get("collected") or {})
    date_slot_value = (
        entities.get("date_text")
        or appointment.get("preferred_date")
        or _first_truthy(collected, ("visit_date", "callback_date", "preferred_date", "date"))
    )
    time_slot_value = (
        entities.get("time_text")
        or appointment.get("preferred_time")
        or _first_truthy(collected, ("visit_time", "callback_time", "preferred_time", "time"))
    )

    # Tool_flow flows often store a combined "visit_at" / "callback_at" ISO
    # string instead of split date/time — try those before falling back.
    requested_at: datetime | None = None
    if not (date_slot_value and time_slot_value):
        for combined_key in ("visit_at", "callback_at", "confirm_at", "scheduled_at"):
            combined = collected.get(combined_key)
            if combined:
                try:
                    parsed_combined = datetime.fromisoformat(
                        str(combined).replace("Z", "+00:00")
                    )
                    requested_at = parsed_combined.astimezone(timezone.utc)
                except Exception:
                    pass
                break

    # Track whether the caller actually specified a time — used below to
    # decide between "X is taken — next free is Y" (specific) and a
    # cleaner "The next available slot is Y" (open-ended).
    caller_specified_time = False
    if requested_at is None and date_slot_value and time_slot_value:
        try:
            local_date = helpers._parse_appointment_date(date_slot_value)
            local_time = helpers._parse_appointment_time(time_slot_value)
            local_dt = datetime.combine(local_date, local_time, tzinfo=_APPOINTMENT_LOCAL_TZ)
            requested_at = local_dt.astimezone(timezone.utc)
            caller_specified_time = True
        except (_AppointmentToolInputError, Exception):
            requested_at = None
    # Adaptive disambiguation: caller gave a date but no time. Use
    # start-of-working-day (9 AM local) as the anchor so the scheduler
    # surfaces the first free slot on that date.
    if requested_at is None and date_slot_value and not time_slot_value:
        try:
            local_date = helpers._parse_appointment_date(date_slot_value)
            local_dt = datetime.combine(local_date, time(9, 0), tzinfo=_APPOINTMENT_LOCAL_TZ)
            requested_at = local_dt.astimezone(timezone.utc)
        except Exception:
            requested_at = None
    if requested_at is None:
        now_local = datetime.now(_APPOINTMENT_LOCAL_TZ)
        # Round up to the next 15-minute mark — feels less robotic than
        # "available at 14:37". Caller can refine afterwards.
        minute = (now_local.minute // 15 + 1) * 15
        if minute >= 60:
            now_local = now_local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        else:
            now_local = now_local.replace(minute=minute, second=0, microsecond=0)
        requested_at = now_local.astimezone(timezone.utc)

    # Load members + scheduling state.
    members = await NokvoOneAssignmentService._load_members(db, organization.id)
    settings_map = await NokvoOneAssignmentService._load_assignment_settings(db, organization.id)
    clinic_map = await NokvoOneAssignmentService._load_clinic_settings(db, organization.id)
    blocked_map = await NokvoOneAssignmentService._load_blocked_slots(db, organization.id)
    records = await NokvoOneAssignmentService._load_request_records(db, organization.id)

    _ROLE_LABEL = {
        "clinics": "doctor",
        "real_estate": "agent",
        "hospitality": "host",
    }
    member_role_label = _ROLE_LABEL.get(industry, "team member")

    # Walk every assignable member and collect their next available
    # slot. We do NOT short-circuit on the first member: we explicitly
    # want the slot CLOSEST to the caller's requested time, regardless
    # of which member it belongs to. So "Member 2 at 10am" beats
    # "Member 1 at 11am" when the caller asked for 10am — the second
    # member's same-time slot is strictly preferred over the first
    # member's next-time slot. Ties on shift_minutes are broken by
    # active_load so a less-busy member wins, then by member creation
    # order for full determinism.
    candidates: list[tuple[int, int, datetime, str]] = []
    for member in members:
        settings = settings_map.get(member.id)
        if settings is None or not settings.is_assignable:
            continue
        if request_type not in set(settings.request_types or []):
            continue
        member_blocks = list(blocked_map.get(member.id, []))
        member_blocks.extend(blocked_map.get("_org_wide", []))  # type: ignore[arg-type]
        slot = NokvoOneAssignmentService._find_next_available_slot(
            member_id=member.id,
            requested_at=_aware_utc(requested_at),
            settings=settings,
            clinic_settings=clinic_map.get(member.id) if industry == "clinics" else None,
            blocked_slots=member_blocks,
            records=records,
            exclude_record_id=None,
        )
        if slot is None:
            continue
        when_utc, shift_min = slot
        active_load = NokvoOneAssignmentService._active_load(records, member.id)
        candidates.append(
            (
                shift_min,
                active_load,
                when_utc,
                member.full_name or f"the on-call {member_role_label}",
            )
        )

    best: tuple[datetime, int, str] | None = None
    if candidates:
        # Time-first ordering. Same as the canonical sort in
        # assign_request, so the slot we propose to the caller
        # matches what the booking would actually pick.
        candidates.sort(key=lambda c: (c[0], c[1]))
        shift_min, _load, when_utc, member_name = candidates[0]
        best = (when_utc, shift_min, member_name)

    if best is None:
        answer = (
            "I checked the calendar and nothing fits within the working hours. "
            "Could you share another date or time?"
        )
        patch: dict[str, Any] = {}
        if appointment:
            patch["appointment"] = appointment
        if tool_flow_state:
            patch["tool_flow"] = tool_flow_state
        return {
            "answer": answer,
            "state_patch": patch,
            "state_slot": "availability_check_empty",
            "route_reason": "scheduler returned no slot",
            "tool_calls": [],
        }

    when_utc, shift_min, member_name = best
    when_local = when_utc.astimezone(_APPOINTMENT_LOCAL_TZ)
    when_label = when_local.strftime("%d %b at %I:%M %p").lstrip("0")
    if shift_min == 0:
        answer = (
            f"Yes, {when_label} is open with {member_name}. "
            "Want me to lock that in?"
        )
        slot_label = "availability_exact"
    elif caller_specified_time:
        # Caller named a specific time — acknowledge it's taken and
        # propose the next free slot.
        requested_local = requested_at.astimezone(_APPOINTMENT_LOCAL_TZ)
        requested_label = requested_local.strftime("%d %b at %I:%M %p").lstrip("0")
        answer = (
            f"{requested_label} is taken — the next free slot is {when_label} with {member_name}. "
            "Want me to book that?"
        )
        slot_label = "availability_next"
    else:
        # Caller asked open-endedly ("when is it available?"). The
        # "X is taken" preamble makes no sense here — just lead with
        # the proposal.
        answer = (
            f"The next available slot is {when_label} with {member_name}. "
            "Want me to book that?"
        )
        slot_label = "availability_next"

    # Stash the offered slot on whichever flow is active. The follow-up
    # turn's policy looks at awaiting_slot_confirm in both shapes.
    if appointment or industry == "clinics":
        appointment["proposed_slot_utc"] = when_utc.isoformat()
        appointment["proposed_slot_label"] = when_label
        appointment["awaiting_slot_confirm"] = True
        appointment["active"] = True
    elif tool_flow_state:
        tool_flow_state["proposed_slot_utc"] = when_utc.isoformat()
        tool_flow_state["proposed_slot_label"] = when_label
        tool_flow_state["awaiting_slot_confirm"] = True
        tool_flow_state["active"] = True
    patch: dict[str, Any] = {}
    if appointment:
        patch["appointment"] = appointment
    if tool_flow_state:
        patch["tool_flow"] = tool_flow_state
    return {
        "answer": answer,
        "state_patch": patch,
        "state_slot": slot_label,
        "route_reason": "scheduler answered availability question",
        "tool_calls": [],
    }


async def _maybe_execute_turn_policy_action(
    helpers: Any,
    tenant_res: TenantResources,
    call_id: str | None,
    db: AsyncSession | None,
    turn_policy: dict[str, Any],
) -> dict[str, Any] | None:
    if turn_policy.get("intent") == "availability_check":
        return await helpers._handle_availability_check(
            tenant_res, db, turn_policy
        )
    if turn_policy.get("intent") != "appointment_flow" or turn_policy.get("state_slot") != "complete":
        return None
    appointment = dict(((turn_policy.get("state_patch") or {}).get("appointment") or {}))
    if appointment.get("created_record_id"):
        return None

    context = await helpers._voice_business_context(db, tenant_res)
    if context is None:
        return None
    organization, overrides, custom_tabs = context
    if organization.industry != "clinics":
        return None
    catalog = resolve_index(organization.industry, overrides, custom_tabs)
    tool = catalog.get("appointments_create")
    if tool is None:
        return None

    # Snapshot the FK primitive now (tenant_res is still fresh — nothing has
    # committed/rolled back yet). The fresh-session retry below rolls back on
    # failure, which expires tenant_res's attributes; re-reading them would
    # trigger a sync ORM reload outside the greenlet (MissingGreenlet).
    org_id = getattr(tenant_res, "organization_id")

    try:
        appointment_time = helpers._appointment_datetime_iso(appointment)
    except _AppointmentToolInputError as exc:
        appointment["completed"] = False
        appointment["pending_slot"] = exc.slot
        if exc.clear_date:
            appointment["preferred_date"] = None
        if exc.clear_time:
            appointment["preferred_time"] = None
        return {
            "answer": exc.answer,
            "state_patch": {"appointment": appointment},
            "state_slot": exc.slot,
            "route_reason": "appointment needs exact scheduling detail",
            "tool_calls": [],
        }

    args = {
        "patient_name": appointment["patient_name"],
        "phone": appointment["phone"],
        "appointment_time": appointment_time,
        "reason": appointment["reason"],
    }
    # Service-first routing (clinics): the captured service text is passed
    # to the booking tool, which resolves it to the doctors who provide it
    # and constrains assignment to them. Optional — omitted when not asked.
    _svc = appointment.get("service")
    if isinstance(_svc, str) and _svc.strip():
        args["service"] = _svc.strip()[:200]
    # Confirmation / audit metadata is patched onto the created record
    # *after* execution (it'd be rejected by the tool's strict
    # additionalProperties:false schema if passed as args).
    record_metadata: dict[str, Any] = {}
    for key in ("confirmations", "audit_trail", "proposed_slot_accepted"):
        value = appointment.get(key)
        if value:
            record_metadata[key] = value
    # Inline retry + graceful fallback. Retry count + delay come from the
    # canonical agent spec (:class:`RetryPolicy`) — not hardcoded.
    from app.services.agent_spec import RETRY_POLICY
    from app.db.session import AsyncSessionLocal

    result = None
    last_exc: Exception | None = None
    max_inline_attempts = 1 + RETRY_POLICY.inline_retries
    for attempt in range(max_inline_attempts):
        # First attempt uses the shared call session (tests assert
        # on its commit flag). Retries use a fresh AsyncSession to
        # sidestep greenlet_spawn / session-corruption issues that
        # the long-lived call session can accumulate across many turns.
        use_fresh_session = attempt > 0
        try:
            if use_fresh_session:
                async with AsyncSessionLocal() as tool_db:
                    result = await PredefinedToolsService.execute(
                        tool_db,
                        org_id,
                        None,
                        tool,
                        args,
                        session_id=call_id,
                    )
                    await tool_db.commit()
            else:
                result = await PredefinedToolsService.execute(
                    db,
                    org_id,
                    None,
                    tool,
                    args,
                    session_id=call_id,
                )
                await db.commit()
            last_exc = None
            break
        except Exception as exc:  # noqa: BLE001 — voice tool entry, broad catch by design
            last_exc = exc
            logger.warning(
                "NOKVO-APPT: %s failed (attempt %s/%s, fresh_session=%s): %r",
                tool.key,
                attempt + 1,
                max_inline_attempts,
                use_fresh_session,
                exc,
                exc_info=True,
            )
            if not use_fresh_session and db is not None:
                try:
                    await db.rollback()
                except Exception:
                    pass
            if attempt < max_inline_attempts - 1:
                await asyncio.sleep(RETRY_POLICY.inline_delay_seconds)
    if result is None:
        # Inline retries exhausted — persist to the retry queue so a
        # worker / admin / cron can pick it back up once the underlying
        # issue clears. The caller's data is *not* lost.
        try:
            from app.services.tool_retry_service import ToolRetryService

            await ToolRetryService.enqueue(
                db,
                organization_id=org_id,
                tool_key=tool.key,
                arguments=args,
                context={
                    "call_id": call_id,
                    "language": turn_policy.get("language"),
                    "intent": "appointment",
                },
                last_error=str(last_exc) if last_exc else None,
            )
        except Exception:
            pass
        appointment["completed"] = False
        appointment["pending_slot"] = None
        appointment["needs_callback"] = True
        from app.services.flow_session import append_audit_trail
        append_audit_trail(appointment, "tool_retry_enqueued", detail=str(last_exc)[:200] if last_exc else None)
        lang = SarvamVoiceService.normalize_language(turn_policy.get("language"))
        if lang == "te":
            fallback = (
                "I have all the details, kāni system temporarily unavailable. "
                "Clinic team mīkū call back chestāru same number ki."
            )
        elif lang == "hi":
            fallback = (
                "मेरे पास सारी जानकारी है, पर सिस्टम अभी temporarily unavailable है. "
                "Clinic team आपके इसी नंबर पर call back करेगी."
            )
        else:
            fallback = (
                "I have all your details, but I'm having trouble saving them right now. "
                "The clinic team will call you back on this number to confirm — your booking won't be missed."
            )
        return {
            "answer": fallback,
            "state_patch": {"appointment": appointment},
            "state_slot": "tool_error",
            "route_reason": "appointment tool failed after retry",
            "tool_calls": [
                {"tool": tool.key, "arguments": args, "ok": False, "error": str(last_exc)[:240]},
            ],
        }

    appointment.update(
        {
            "active": False,
            "completed": True,
            "pending_slot": None,
            "appointment_time": appointment_time,
            "created_record_id": result.get("id"),
            "assignment_status": result.get("assignment_status"),
            "assigned_member_name": result.get("assigned_member_name"),
        }
    )
    # Patch the persisted record with confirmation/audit metadata so
    # downstream consumers see what the caller actually confirmed.
    if record_metadata and result.get("id") and db is not None:
        await helpers._patch_record_metadata(
            db, result["id"], record_metadata
        )
    return {
        "answer": helpers._appointment_tool_answer(
            result,
            args,
            language=turn_policy.get("language"),
            offer_sms=helpers._should_offer_sms_confirmation(tenant_res),
        ),
        "state_patch": {"appointment": appointment},
        "state_slot": "complete",
        "route_reason": "appointment tool executed",
        "tool_calls": [{"tool": tool.key, "arguments": args, "result": result}],
    }
