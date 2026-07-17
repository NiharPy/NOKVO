"""Real-estate lead + site-visit capture: opt-out and interest signals,
memory extraction, record shaping/routing, deterministic call notes, and
the end-of-call lead creator.

Extracted from nokvo_one_voice_pipeline.py (turn_router helpers pattern:
functions taking ``helpers`` receive the ``NokvoOneVoicePipeline`` class and
call sibling statics through it, so class-attribute monkeypatches keep
working). The pipeline class keeps delegating wrappers - no API change.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any
import asyncio
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant_resources import TenantResources
from app.services.agent_outbound_context import OutboundCampaignContext, update_outbound_memory
from app.services.dynamic_tool_resolver import resolve_index
from app.services.pipeline.appointments import _APPOINTMENT_LOCAL_TZ
from app.services.agent_session_store import AgentSessionStore
from app.services.predefined_tools_service import PredefinedToolsService
from app.services.tool_flow_questions import build_tool_flow_questions, format_field_questions_prompt

logger = logging.getLogger(__name__)


# Unambiguous "not interested / don't want it" phrases for the multilingual
# capture-block backstop (``_real_estate_opt_out``). STT emits native script for
# te/hi, so we substring-match those directly; romanized forms cover transliteration.
# A disinterested prospect must NEVER be captured as a lead, in any language.
_MULTILINGUAL_DISINTEREST_PHRASES = (
    # Hindi (native)
    "नहीं चाहिए", "नही चाहिए", "इंटरेस्ट नहीं", "interest नहीं", "ज़रूरत नहीं",
    "जरूरत नहीं", "रहने दो", "मत करो", "मत कीजिए", "नहीं भाई",
    # Hindi (romanized)
    "nahi chahiye", "nahin chahiye", "interest nahi", "zaroorat nahi",
    "zarurat nahi", "rehne do", "mat karo",
    # Telugu (native)
    "వద్దు", "ఇంటరెస్ట్ లేదు", "interest లేదు", "అవసరం లేదు", "అక్కర్లేదు",
    # Telugu (romanized)
    "vaddu", "interest ledu", "avasaram ledu", "akkarledu",
)


def _map_lead_data_to_ticket_shape(data: dict[str, Any], industry: str | None) -> dict[str, Any]:
    """Project the lead-shaped fields onto the keys the ticket schema
    expects so the Tickets tab renders populated cells rather than blank
    ones. Per business-template:

    * real_estate tickets need ``customer``, ``issue_type``, ``priority``
      (the lead has ``name``, ``phone``, ``visit_date``).
    * clinics tickets need ``subject``, ``customer``, ``priority``.
    * ecommerce / hospitality follow the same name → customer convention.

    We only ADD; existing data is preserved so anything downstream that
    was looking for the old keys still finds them.
    """
    merged = dict(data or {})
    ind = (industry or "").lower()

    # Common "customer" alias from whatever name-like field the lead had.
    customer = (
        merged.get("customer")
        or merged.get("name")
        or merged.get("customer_name")
        or merged.get("patient_name")
        or merged.get("guest_name")
        or merged.get("contact_name")
    )
    if customer and not merged.get("customer"):
        merged["customer"] = customer

    # Default priority / issue_type / subject so the required ticket
    # columns aren't empty. We err on the safe side ("normal") and let
    # the operator re-classify in the dashboard if needed.
    merged.setdefault("priority", "normal")
    if ind == "real_estate":
        merged.setdefault("issue_type", "site_visit")
        # "Property" column → prefer the matched project name (what the
        # caller is actually visiting); fall back to the free-text area.
        if not merged.get("property_id"):
            property_value = merged.get("project_name") or merged.get("location")
            if property_value:
                merged["property_id"] = property_value
    elif ind == "clinics":
        merged.setdefault("subject", merged.get("care_need") or merged.get("reason") or "Patient request")
        merged.setdefault("priority", "normal")
    elif ind == "ecommerce":
        merged.setdefault("subject", merged.get("subject") or merged.get("issue_summary") or "Customer inquiry")
        merged.setdefault("issue_type", merged.get("issue_type") or "support_request")
    elif ind == "hospitality":
        merged.setdefault("subject", merged.get("subject") or "Guest inquiry")
        merged.setdefault("reservation_id", merged.get("reservation_id") or merged.get("booking_id"))
    return merged


async def _route_record_by_surface(
    helpers: Any,
    db: AsyncSession,
    record_ids: list[Any],
    *,
    call_surface: str | None,
    industry: str | None = None,
    force_ticket: bool = False,
) -> None:
    """Decide which tab a macro-created record belongs in.

    Two rules, in priority order:

    * ``force_ticket`` — the *action* is tab-defining. A booked site
      visit always belongs in the Site Visits (tickets) tab no matter
      who placed the call, so callers set this for action-routed flows
      (e.g. ``real_estate_site_visit``). This is what the operator means
      by "site visits go to the Site Visits tab" — it's about what the
      caller booked, not the call direction.
    * Otherwise fall back to the call-direction heuristic: inbound
      callers reached out for help → tickets tab; outbound calls we
      initiated → leads tab (the macro already creates leads there, so
      outbound needs no rewrite).

    When we do rewrite, we flip ``record_type`` from ``lead`` to
    ``ticket`` AND project the data dict onto the ticket schema's
    expected field keys so the UI renders populated cells (otherwise the
    row looks blank and the operator thinks no record was created)."""
    if not record_ids:
        return
    rewrite_to_ticket = force_ticket or call_surface == "voice_inbound"
    if not rewrite_to_ticket:
        return
    from app.models.nokvo_one_tool_record import NokvoOneToolRecord
    from app.services.nokvo_one_business_templates import STATUS_VOCABULARIES
    from sqlalchemy import select
    import uuid as _uuid

    ticket_status = (
        (STATUS_VOCABULARIES.get((industry or "").lower(), {}).get("tickets") or {}).get("initial")
        or "open"
    )
    for rid in record_ids:
        try:
            rid_uuid = _uuid.UUID(str(rid))
        except (TypeError, ValueError):
            continue
        try:
            res = await db.execute(
                select(NokvoOneToolRecord).where(NokvoOneToolRecord.id == rid_uuid)
            )
            rec = res.scalars().first()
            if rec is None or rec.record_type != "lead":
                continue
            rec.record_type = "ticket"
            rec.status = ticket_status
            projected = helpers._map_lead_data_to_ticket_shape(rec.data or {}, industry)
            projected["routed_from"] = "lead"
            if call_surface:
                projected["call_surface"] = call_surface
            rec.data = projected
            db.add(rec)
            await db.commit()
        except Exception:
            try:
                await db.rollback()
            except Exception:
                pass


def _campaign_contact(campaign_context: dict[str, Any] | None) -> dict[str, Any]:
    contact = (campaign_context or {}).get("contact")
    return contact if isinstance(contact, dict) else {}


def _phone_from_call_context(
    helpers: Any,
    memory: dict[str, Any],
    campaign_context: dict[str, Any] | None,
) -> str:
    contact = helpers._campaign_contact(campaign_context)
    raw = (
        memory.get("phone")
        or contact.get("phone")
        or contact.get("phone_e164")
        or (campaign_context or {}).get("from_phone")
        or (campaign_context or {}).get("to_phone")
        or ""
    )
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) >= 10:
        return digits[-10:]
    return str(raw).strip()


def _budget_number(value: Any) -> float | None:
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(value or "").replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _real_estate_opt_out(
    *,
    memory: dict[str, Any],
    history: list[dict[str, str]],
) -> bool:
    """True when the caller explicitly opted out — wrong number / do-not-call
    / not interested. Such callers must NEVER become a follow-up-eligible
    lead (DND/TRAI). Checks both the extracted objection and recent caller
    utterances."""
    blob = str(memory.get("objection") or "").lower()
    user_text = " ".join(
        str(turn.get("content") or "")
        for turn in (history or [])[-12:]
        if turn.get("role") == "user"
    ).lower()
    combined = f"{blob} {user_text}"
    if re.search(
        r"\b(not interested|don'?t call|do not call|do-not-call|remove me|"
        r"wrong number|stop calling|take me off|unsubscribe|not looking|"
        r"don'?t need|do not need|not needed|leave me alone)\b",
        combined,
    ):
        return True
    # Multilingual disinterest backstop: STT emits NATIVE script for te/hi, so
    # match those directly (``\b`` is unreliable on non-ASCII). Romanized forms
    # too, in case the caller's words were transliterated. Kept to UNAMBIGUOUS
    # refusal phrases — we'd rather not-capture a borderline case than capture a
    # disinterested one (an explicit product requirement). ``combined`` is
    # already lowercased; native script is unaffected by ``.lower()``.
    return any(p in combined for p in _MULTILINGUAL_DISINTEREST_PHRASES)


def _real_estate_interest_signal(
    *,
    memory: dict[str, Any],
    history: list[dict[str, str]],
    call_surface: str | None,
    outbound_context: OutboundCampaignContext | None,
) -> bool:
    objection = str(memory.get("objection") or "").lower()
    if re.search(r"\b(not interested|don't call|do not call|remove me|wrong number)\b", objection):
        return False
    if any(memory.get(key) for key in (
        "purpose", "bhk", "budget", "timeline", "location_preference",
        "visit_preference", "requested_info",
    )):
        return True
    user_text = " ".join(
        str(turn.get("content") or "")
        for turn in history[-12:]
        if turn.get("role") == "user"
    ).lower()
    if re.search(
        r"\b(property|flat|apartment|villa|plot|bhk|site\s+visit|brochure|"
        r"pricing|price|cost|floor\s*plan|details?|rera|interested|investment|self[-\s]?use)\b",
        user_text,
    ):
        return True
    # Outbound calls should not create a lead just because the opener ran.
    # Require at least one customer utterance beyond a tiny permission reply.
    if call_surface == "voice_outbound" and outbound_context is not None:
        substantial_user_turns = [
            str(turn.get("content") or "").strip()
            for turn in history[-12:]
            if turn.get("role") == "user" and len(str(turn.get("content") or "").split()) > 2
        ]
        return bool(substantial_user_turns)
    return False


def _real_estate_memory_from_history(
    memory: dict[str, Any],
    history: list[dict[str, str]],
) -> dict[str, Any]:
    merged = dict(memory or {})
    for turn in (history or [])[-16:]:
        if turn.get("role") != "user":
            continue
        merged = update_outbound_memory(
            merged,
            caller_text=str(turn.get("content") or ""),
        )
    return merged


def _lead_args_from_call_memory(
    helpers: Any,
    *,
    memory: dict[str, Any],
    campaign_context: dict[str, Any] | None,
    outbound_context: OutboundCampaignContext | None,
) -> dict[str, Any]:
    contact = helpers._campaign_contact(campaign_context)
    name = (
        memory.get("name")
        or contact.get("name")
        or contact.get("full_name")
        or contact.get("customer_name")
        or "Property inquiry"
    )
    phone = helpers._phone_from_call_context(memory, campaign_context)
    # A lead is name + phone only. The rest of the conversation (BHK, budget,
    # area, intent) is captured in the post-call "call notes"
    # (data.handoff_note), not as structured lead fields.
    args: dict[str, Any] = {
        "name": str(name).strip()[:200] or "Property inquiry",
        "phone": phone,
    }
    return {key: value for key, value in args.items() if value not in (None, "")}


def _site_visit_args_from_call_state(
    helpers: Any,
    *,
    state: dict[str, Any],
    organization: Any,
    overrides: dict[str, Any],
    custom_tabs: list[dict[str, Any]],
    memory: dict[str, Any],
    campaign_context: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Build ``qualify_lead_and_schedule_visit`` args when the call holds a
    FIRM site-visit booking — name + phone + a parseable visit date AND
    time. Returns ``None`` for enquiry / vague calls (no firm date/time) so
    those stay leads. Used by the end-of-call safety net so a booking the
    deterministic flow didn't capture is filed as a Site Visit, not a Lead."""
    try:
        from app.services.conversational_memory import (
            ConversationalMemory as _CM,
            FACT_NAME as _FACT_NAME,
            FACT_PHONE as _FACT_PHONE,
            FACT_PROPERTY as _FACT_PROPERTY,
            FACT_VISIT_DATE as _FACT_VISIT_DATE,
            FACT_VISIT_TIME as _FACT_VISIT_TIME,
        )

        cm = _CM.from_state_blob((state or {}).get("memory") or {})
    except Exception:
        return None

    collected = dict((state.get("tool_flow") or {}).get("collected") or {})

    def _collected_by(predicate) -> Any:
        for key, value in collected.items():
            if value not in (None, "") and predicate(key):
                return value
        return None

    date_raw = cm.get(_FACT_VISIT_DATE) or _collected_by(lambda k: "date" in k.lower())
    time_raw = cm.get(_FACT_VISIT_TIME) or _collected_by(lambda k: "time" in k.lower())
    if not (date_raw and time_raw):
        return None
    # A firm booking needs a concrete date AND time. Vague input ("morning",
    # "sometime next week") raises here, which correctly keeps it a lead.
    try:
        visit_date = helpers._parse_appointment_date(date_raw)
        visit_time = helpers._parse_appointment_time(time_raw)
    except Exception:
        return None

    name_val = (
        cm.get(_FACT_NAME)
        or memory.get("name")
        or _collected_by(lambda k: "name" in k.lower())
    )
    phone_val = (
        cm.get(_FACT_PHONE)
        or helpers._phone_from_call_context(memory, campaign_context)
    )
    if not (name_val and phone_val):
        return None

    project_val = (
        cm.get(_FACT_PROPERTY)
        or _collected_by(lambda k: "project" in k.lower())
        or collected.get("property_id")
    )

    visit_at = datetime.combine(
        visit_date, visit_time, tzinfo=_APPOINTMENT_LOCAL_TZ
    ).astimezone(timezone.utc).isoformat()

    # Project record_data onto the org's configured Site Visit Fields so the
    # Site Visits tab renders populated cells, mirroring the deterministic
    # flow's construction.
    canonical = {
        "date": visit_date.isoformat(),
        "time": visit_time.strftime("%I:%M %p").lstrip("0"),
        # Combined "Date and Time" field (a single datetime slot) renders one
        # human-readable cell. Mirrors the split date/time formatting above so
        # the Site Visits tab shows the same values, just in one column.
        "datetime": f"{visit_date.isoformat()} {visit_time.strftime('%I:%M %p').lstrip('0')}",
        "name": str(name_val),
        "phone": str(phone_val),
        "project": str(project_val) if project_val else None,
    }
    record_data: dict[str, Any] = {}
    try:
        from app.services.tool_flow_questions import build_tool_flow_questions

        sv_bundle = build_tool_flow_questions(
            getattr(organization, "industry", None), overrides, custom_tabs
        )
        flow_slots = (
            ((sv_bundle.get("flows") or {}).get("real_estate_site_visit") or {}).get("slots") or []
        )
        for slot in flow_slots:
            kind = str(slot.get("kind") or "")
            fkey = str(slot.get("source_field") or slot.get("key") or "")
            value = canonical.get(kind)
            if fkey and value not in (None, ""):
                record_data[fkey] = value
    except Exception:
        record_data = {}
    if not record_data:
        # Default real_estate Site Visit Fields.
        record_data = {
            "name": str(name_val),
            "phone": str(phone_val),
            "visit_date": canonical["date"],
            "visit_time": canonical["time"],
        }
        if project_val:
            record_data["project_name"] = str(project_val)

    args: dict[str, Any] = {
        "name": str(name_val),
        "phone": str(phone_val),
        "visit_at": visit_at,
        "record_data": record_data,
    }
    if project_val not in (None, ""):
        args["project_name"] = str(project_val)
    return args


async def _send_brochure_and_location_sms(
    db: AsyncSession,
    org_id: Any,
    tenant_res: TenantResources,
    call_id: str,
    state: dict[str, Any],
) -> None:
    """At call end, text the caller (their ANI) the project's brochure +
    location links in ONE SMS. This is the inbound-real-estate delivery channel
    while WhatsApp is off — the number is the one they're calling from, so
    nothing is asked. Idempotent per call (``sms_sent``); best-effort — callers
    wrap it so an SMS failure never affects the lead."""
    ani = str((state or {}).get("caller_phone") or "").strip()
    if not ani:
        return
    if state.get("sms_sent"):
        return
    from app.services.real_estate_project_service import (
        find_project_match,
        load_active_projects,
    )
    from app.services.sms_service import SmsService

    projects = await load_active_projects(db, org_id)
    if not projects:
        return
    # Resolve which project to send: the one the caller discussed (memory
    # FACT_PROPERTY), else the sole active project. Never guess across many —
    # the QUERY prompt asks which project before promising when there's >1.
    captured = None
    try:
        from app.services.conversational_memory import (
            ConversationalMemory as _CM,
            FACT_PROPERTY as _FACT_PROPERTY,
        )

        captured = _CM.from_state_blob((state or {}).get("memory") or {}).get(_FACT_PROPERTY)
    except Exception:
        captured = None
    matched = find_project_match(
        projects, project_name=str(captured) if captured else None
    )
    if matched is None and len(projects) == 1:
        matched = projects[0]
    if matched is None:
        return

    # The two links: brochure (a column) + location maps URL (lives in the
    # project's whatsapp.location config — reused as the SMS map link).
    brochure_url = str(getattr(matched, "brochure_url", None) or "").strip()
    wa_cfg = getattr(matched, "whatsapp", None) or {}
    maps_url = str(((wa_cfg.get("location") or {}).get("maps_url")) or "").strip()
    if not brochure_url and not maps_url:
        return  # nothing to send

    name = getattr(matched, "name", None) or "your enquiry"
    parts = [f"Hi! Details for {name}:"]
    if brochure_url:
        parts.append(f"Brochure: {brochure_url}")
    if maps_url:
        parts.append(f"Location: {maps_url}")
    text = " ".join(parts)

    res = await SmsService.send_for_org(db, org_id, to_number=ani, text=text)
    if res.get("ok"):
        await AgentSessionStore.merge_state(tenant_res, call_id, {"sms_sent": True})


def _captured_project(state: dict[str, Any] | None, memory: dict[str, Any] | None) -> str | None:
    """Project the caller is talking about, for the call note. Prefers the
    outbound-memory dict, then the durable ConversationalMemory
    FACT_PROPERTY (set by single-project auto-fill or a matched project
    name) — the same state blob the name fallback reads."""
    proj = str((memory or {}).get("property") or "").strip()
    if proj:
        return proj
    try:
        from app.services.conversational_memory import (
            ConversationalMemory as _CM,
            FACT_PROPERTY as _FACT_PROPERTY,
        )

        return str(
            _CM.from_state_blob((state or {}).get("memory") or {}).get(_FACT_PROPERTY) or ""
        ).strip() or None
    except Exception:
        return None


async def _resolve_inbound_project(
    db: AsyncSession,
    org_id: Any,
    *,
    candidate: str | None,
    history: list[dict[str, str]] | None,
) -> tuple[str | None, str | None]:
    """Resolve the site-visit's project to a REAL registered project, returning
    ``(canonical_name, project_id)``.

    The captured ``FACT_PROPERTY`` is heuristic and frequently grabs agent /
    caller phrasing ("Show You The Site", "Or Did You Mean Another") rather
    than a real project — storing that as the project is worse than storing
    nothing. So we (1) fuzzy-match the candidate against the catalog, then
    (2) scan recent caller turns for a real project mention, and only return
    a project when it actually matches a registered one. Returns
    ``(None, None)`` rather than echo garbage back onto the ticket."""
    try:
        from app.services.real_estate_project_service import (
            find_project_match,
            load_active_projects,
        )

        projects = await load_active_projects(db, org_id)
    except Exception:
        return None, None
    if not projects:
        cand = str(candidate or "").strip()
        return (cand or None), None
    matched = find_project_match(projects, project_name=candidate) if candidate else None
    if matched is None:
        for turn in reversed((history or [])[-12:]):
            if turn.get("role") != "user":
                continue
            matched = find_project_match(projects, project_name=str(turn.get("content") or ""))
            if matched is not None:
                break
    if matched is None and len(projects) == 1:
        matched = projects[0]
    if matched is not None:
        return matched.name, str(matched.id)
    return None, None


def _deterministic_call_note(
    *,
    kind: str,
    name: str | None,
    ani: str | None,
    memory: dict[str, Any],
    history: list[dict[str, str]],
    project: str | None = None,
) -> str:
    """Plain-prose fallback call note built deterministically from captured
    facts, written SYNCHRONOUSLY at record creation so a flaky post-call LLM
    condenser can never leave the record noteless. The background condenser
    overwrites this with a richer summary when it succeeds. Shaped so
    ``REAgentScheduler``'s extractor can still read the visit date/time."""
    from app.services.voice_turn_policy import extract_datetime_phrase

    mem = memory or {}
    parts: list[str] = [
        "Caller agreed to a site visit."
        if kind == "site_visit"
        else "Caller enquired about properties."
    ]
    # Which project the visit/enquiry is about — the single most useful
    # routing fact for the sales team. Resolved from the captured property
    # fact (conversational memory FACT_PROPERTY / outbound memory).
    proj = str(project or mem.get("property") or "").strip()
    if proj:
        parts.append(f"Project: {proj}.")
    # Visit date/time — scan recent caller turns, normalising hi/te relative
    # tokens so a Telugu "రేపు 10" still yields "tomorrow 10 AM".
    when = ""
    for turn in reversed((history or [])[-12:]):
        if turn.get("role") != "user":
            continue
        when = extract_datetime_phrase(str(turn.get("content") or ""))
        if when:
            break
    if when:
        parts.append(f"Proposed visit time: {when}.")
    if mem.get("bhk"):
        parts.append(f"Configuration: {mem['bhk']}.")
    if mem.get("location_preference"):
        parts.append(f"Preferred area: {mem['location_preference']}.")
    if mem.get("purpose"):
        parts.append(f"Purpose: {mem['purpose']}.")
    if mem.get("budget"):
        parts.append(f"Budget: {mem['budget']}.")
    if mem.get("requested_info"):
        parts.append(f"Asked for: {mem['requested_info']}.")
    who = [bit for bit in (f"Name: {name}" if name else "", f"Phone: {ani}" if ani else "") if bit]
    if who:
        parts.append("; ".join(who) + ".")
    return " ".join(parts)


async def _create_inbound_site_visit(
    helpers: Any,
    db: AsyncSession,
    org_id: Any,
    tenant_res: TenantResources,
    call_id: str,
    *,
    state: dict[str, Any],
    memory: dict[str, Any],
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any] | None:
    """Create a minimal inbound site-visit TICKET when the caller agreed to
    come — ANI + name (if captured) only. NO structured date/time/project
    fields: the date/time the agent clarified rides in the post-call note.
    We write a DETERMINISTIC ``data.handoff_note`` here synchronously (so the
    record is never noteless if the post-call LLM condenser fails); the
    condenser later overwrites it with a richer summary when it succeeds.
    Mirrors the phoneless-lead direct write; best-effort."""
    from app.models.nokvo_one_tool_record import NokvoOneToolRecord
    from app.services.nokvo_one_business_templates import STATUS_VOCABULARIES

    ani = str((state or {}).get("caller_phone") or "").strip() or None
    name = str((memory or {}).get("name") or "").strip() or None
    if not name:
        # Fall back to the durable captured name (ConversationalMemory
        # FACT_NAME) — the same source the structured booking path reads.
        try:
            from app.services.conversational_memory import (
                ConversationalMemory as _CM,
                FACT_NAME as _FACT_NAME,
            )

            name = str(
                _CM.from_state_blob((state or {}).get("memory") or {}).get(_FACT_NAME) or ""
            ).strip() or None
        except Exception:
            name = None
    status = (
        (STATUS_VOCABULARIES.get("real_estate", {}).get("tickets") or {}).get("initial")
        or "open"
    )
    data: dict[str, Any] = {
        "source": "voice_inbound",
        "auto_created_from_call": True,
        "request_type": "site_visit",
        "issue_type": "site_visit",
        "agent_mode_final": "site_visit",
        "call_id": call_id,
    }
    if name:
        data["name"] = name
    if ani:
        data["phone"] = ani
    # Resolve the project to a REAL registered one (catalog-validated) and
    # store it as a structured field up front — so the Ticket Board shows the
    # correct project even when the heuristic FACT_PROPERTY captured garbage.
    project_name, project_id = await helpers._resolve_inbound_project(
        db, org_id,
        candidate=helpers._captured_project(state, memory),
        history=history or [],
    )
    if project_name:
        data["project_name"] = project_name
    if project_id:
        data["project_id"] = project_id
    # Deterministic note up front — guarantees the ticket always carries a
    # readable note (with the visit date/time for RE_agent_scheduler) even
    # if the post-call condenser returns None. Uses the resolved real project
    # (never the raw heuristic capture).
    data["handoff_note"] = helpers._deterministic_call_note(
        kind="site_visit", name=name, ani=ani, memory=memory, history=history or [],
        project=project_name,
    )
    data["handoff_note_generated_at"] = datetime.now(timezone.utc).isoformat()
    data["handoff_note_source"] = "deterministic"
    record = NokvoOneToolRecord(
        id=uuid.uuid4(),
        organization_id=org_id,
        record_type="ticket",
        status=status,
        data=data,
        contact_phone=ani,
    )
    try:
        db.add(record)
        await db.commit()
    except Exception:
        logger.exception("NOKVO-SITE-VISIT: failed to persist inbound site visit")
        try:
            await db.rollback()
        except Exception:
            pass
        return None
    # auto_lead_created=True keeps the function idempotent and stops a
    # duplicate lead; auto_site_visit_id is what the post-call condenser
    # loop attaches the call note (with the clarified date/time) to.
    await AgentSessionStore.merge_state(
        tenant_res,
        call_id,
        {
            "auto_lead_created": True,
            "auto_site_visit_created": True,
            "auto_site_visit_id": str(record.id),
            "agent_mode_final": "site_visit",
        },
    )
    return {
        "tool": "site_visit_create_minimal",
        "arguments": data,
        "result": {"ok": True, "id": str(record.id)},
    }


async def maybe_create_real_estate_lead_from_call(
    helpers: Any,
    tenant_res: TenantResources,
    db: AsyncSession | None,
    call_id: str | None,
    *,
    campaign_context: dict[str, Any] | None = None,
    outbound_context: OutboundCampaignContext | None = None,
) -> dict[str, Any] | None:
    """Create a real-estate lead at call end when interest was expressed.

    This catches short calls that never complete the slot-filling flow:
    inbound property inquiries and outbound leads who ask for details /
    pricing / brochure and then hang up. It is idempotent per call and
    deliberately requires a phone number because ``leads_create`` does.
    """
    # Snapshot the FK primitive (see _maybe_execute_turn_policy_action) so a
    # later commit/rollback can't force a sync ORM reload → MissingGreenlet.
    org_id = getattr(tenant_res, "organization_id")
    if db is None or not call_id:
        return None
    state = await AgentSessionStore.get_state(tenant_res, call_id) or {}
    if state.get("auto_lead_created"):
        return None
    tool_flow = dict(state.get("tool_flow") or {})
    if tool_flow.get("created_record_id") or tool_flow.get("completed"):
        return None
    context = await helpers._voice_business_context(db, tenant_res)
    if context is None:
        return None
    organization, overrides, custom_tabs = context
    if str(getattr(organization, "industry", "") or "").lower() != "real_estate":
        return None
    history = await AgentSessionStore.get_history(tenant_res, call_id)
    memory = helpers._real_estate_memory_from_history(
        dict(state.get("outbound_memory") or {}),
        history,
    )
    call_surface = str(state.get("call_surface") or "")
    # Lead overhaul: ANY inbound real-estate call that didn't book a site
    # visit becomes a lead (ANI + call summary + name-if-known). We no longer
    # require a positive "interest" signal — the caller engaging at all is
    # enough. The ONE hard exclusion is an explicit opt-out (wrong number /
    # do-not-call / not interested): turning that into a follow-up-eligible
    # lead would be a DND/TRAI violation.
    if helpers._real_estate_opt_out(memory=memory, history=history):
        return None
    # Outbound keeps its engagement gate (don't lead-ify a call where only
    # the opener ran — the outbound outcome classifier owns those). Inbound
    # always proceeds.
    if call_surface == "voice_outbound" and not helpers._real_estate_interest_signal(
        memory=memory,
        history=history,
        call_surface=call_surface,
        outbound_context=outbound_context,
    ):
        return None

    # End-of-call SMS push (inbound real-estate). Replaces the old in-call
    # lead/site-visit interrogation: text the project brochure + location links
    # to the caller's own number (the ANI we already have). Placed after the
    # opt-out gate so we never message someone who opted out; bounded +
    # best-effort so a slow/failed send never delays or breaks the lead
    # creation below. Idempotent via the sms_sent state flag.
    if call_surface == "voice_inbound":
        try:
            await asyncio.wait_for(
                helpers._send_brochure_and_location_sms(
                    db, org_id, tenant_res, call_id, state
                ),
                timeout=25,
            )
        except Exception:
            logger.debug(
                "NOKVO-SMS: end-of-call brochure/location send failed", exc_info=True
            )

    catalog = resolve_index(organization.industry, overrides, custom_tabs)

    if call_surface == "voice_inbound":
        # INBOUND: a site visit is created the moment the caller agrees to
        # come — no field interrogation. The record is just ANI + name (if
        # captured); the date/time the agent clarified rides in the post-call
        # note (condenser → data.handoff_note on auto_site_visit_id). We do
        # NOT run the structured date/time path for inbound, so a clarified
        # date/time never gets persisted as fields.
        from app.services.tool_flow_policy import caller_agreed_to_site_visit

        if caller_agreed_to_site_visit(history):
            sv = await helpers._create_inbound_site_visit(
                db, org_id, tenant_res, call_id, state=state, memory=memory, history=history,
            )
            if sv is not None:
                return sv
        # No visit agreement → capture as a lead below.
    else:
        # OUTBOUND: a firm booking (date + time + name + phone) the
        # deterministic flow didn't capture must still file as a SITE VISIT,
        # not a lead. Outbound keeps the structured visit_at it needs to
        # schedule / assign the visit.
        site_visit_args = helpers._site_visit_args_from_call_state(
            state=state,
            organization=organization,
            overrides=overrides,
            custom_tabs=custom_tabs,
            memory=memory,
            campaign_context=campaign_context,
        )
        sv_tool = catalog.get("qualify_lead_and_schedule_visit") if site_visit_args else None
        if site_visit_args and sv_tool is not None:
            sv_result = None
            try:
                sv_result = await PredefinedToolsService.execute(
                    db,
                    org_id,
                    None,
                    sv_tool,
                    site_visit_args,
                    session_id=f"{call_id}:auto_real_estate_site_visit",
                )
                await db.commit()
            except Exception:
                if db is not None:
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                sv_result = None
            if sv_result and sv_result.get("ok"):
                await AgentSessionStore.merge_state(
                    tenant_res,
                    call_id,
                    {
                        "auto_lead_created": True,
                        "auto_site_visit_created": True,
                        "auto_site_visit_id": sv_result.get("ticket_id") or sv_result.get("id"),
                        # FSM terminal mode marker. Booking landed — call
                        # ended in site_visit, not inbound_lead.
                        "agent_mode_final": "site_visit",
                    },
                )
                return {
                    "tool": "qualify_lead_and_schedule_visit",
                    "arguments": site_visit_args,
                    "result": sv_result,
                }
            # Site-visit creation unavailable or failed — fall through to lead
            # so the prospect is still captured.

    args = helpers._lead_args_from_call_memory(
        memory=memory,
        campaign_context=campaign_context,
        outbound_context=outbound_context,
    )
    # Phone-less inbound lead: caller showed interest but hung up before
    # giving a number. By spec they still belong in the Leads → Uncategorized
    # tab so the operator can see the engagement. The ``leads_create`` tool
    # requires phone, so we write a NokvoOneToolRecord directly with whatever
    # facts we did capture.
    if not args.get("phone") and call_surface == "voice_inbound":
        from app.models.nokvo_one_tool_record import NokvoOneToolRecord

        # A lead is intentionally minimal: name + phone + the post-call
        # "call notes" (data.handoff_note, written by the condenser at end of
        # call). We deliberately do NOT persist structured facts (budget,
        # purpose, timeline, objection, property type, location) — the notes
        # carry that context in prose. Only name + routing markers here.
        direct_data: dict[str, Any] = {
            "source": "voice_inbound",
            "auto_created_from_call": True,
            "uncategorized": True,
            "agent_mode_final": "inbound_lead",
            "no_phone": True,
            "call_id": call_id,
            "name": str(args.get("name") or "Property inquiry"),
        }
        # Deterministic note up front so the lead is never noteless if the
        # post-call condenser fails (it overwrites this on success).
        direct_data["handoff_note"] = helpers._deterministic_call_note(
            kind="lead", name=args.get("name"), ani=None, memory=memory, history=history,
            project=helpers._captured_project(state, memory),
        )
        direct_data["handoff_note_generated_at"] = datetime.now(timezone.utc).isoformat()
        direct_data["handoff_note_source"] = "deterministic"
        direct_data = {k: v for k, v in direct_data.items() if v not in (None, "")}
        record = NokvoOneToolRecord(
            id=uuid.uuid4(),
            organization_id=org_id,
            record_type="lead",
            status="new",
            data=direct_data,
        )
        try:
            db.add(record)
            await db.commit()
            await AgentSessionStore.merge_state(
                tenant_res,
                call_id,
                {
                    "auto_lead_created": True,
                    "auto_lead_id": str(record.id),
                    "agent_mode_final": "inbound_lead",
                },
            )
            return {
                "tool": "leads_create_phoneless",
                "arguments": direct_data,
                "result": {"ok": True, "id": str(record.id)},
            }
        except Exception:
            logger.exception(
                "NOKVO-INBOUND-LEAD: failed to persist phoneless uncategorized lead"
            )
            try:
                await db.rollback()
            except Exception:
                pass
            return None
    if not args.get("phone"):
        return None
    tool = catalog.get("leads_create")
    if tool is None:
        return None
    result = await PredefinedToolsService.execute(
        db,
        org_id,
        None,
        tool,
        args,
        session_id=f"{call_id}:auto_real_estate_lead",
    )
    await db.commit()
    lead_id = result.get("id") or result.get("lead_id")

    # Inbound real-estate FSM terminal state. Caller showed interest
    # (asked questions, mentioned BHK/budget/location, maybe even
    # started a booking but didn't confirm) and hung up. By spec, the
    # auto-created lead lands in the Leads page's Uncategorized tab
    # (data.uncategorized=true is what the frontend filters on).
    #
    # We DO NOT mark outbound auto-leads as uncategorized — those have
    # their own outcome classifier that decides interested vs
    # partial vs not_interested for tab routing.
    is_inbound = (call_surface == "voice_inbound")
    # A lead is name + phone + post-call notes only. We keep routing markers
    # (source / uncategorized / agent_mode_final / campaign linkage) but
    # deliberately drop the structured facts (budget, purpose, timeline,
    # objection, project, partial visit slots) — that context now lives in
    # the prose "call notes" the condenser writes after the call ends.
    metadata = {
        "source": call_surface or "voice_call",
        "auto_created_from_call": True,
        "campaign_id": (campaign_context or {}).get("campaign_id"),
        # FSM terminal mode marker. The frontend Uncategorized tab
        # filters on ``data.uncategorized === true``; setting it here
        # is what routes the row off the campaign tab and into the
        # uncategorized bucket.
        "uncategorized": True if is_inbound else None,
        "agent_mode_final": "inbound_lead" if is_inbound else None,
    }
    if lead_id:
        await helpers._patch_record_metadata(
            db,
            lead_id,
            {k: v for k, v in metadata.items() if v not in (None, "")},
        )
    await AgentSessionStore.merge_state(
        tenant_res,
        call_id,
        {
            "auto_lead_created": True,
            "auto_lead_id": lead_id,
            "agent_mode_final": "inbound_lead" if is_inbound else "query",
        },
    )
    return {"tool": "leads_create", "arguments": args, "result": result}


async def _patch_record_metadata(
    db: AsyncSession,
    record_id: Any,
    metadata: dict[str, Any],
) -> None:
    """Merge ``metadata`` keys into a NokvoOneToolRecord.data after the
    tool has created it. Used to attach confirmation status, audit trail,
    proposed-slot acceptance — fields we can't pass in tool args because
    the tool schemas reject unknown properties."""
    from app.models.nokvo_one_tool_record import NokvoOneToolRecord
    from sqlalchemy import select
    import uuid as _uuid

    try:
        rid = _uuid.UUID(str(record_id))
    except (TypeError, ValueError):
        return
    try:
        res = await db.execute(select(NokvoOneToolRecord).where(NokvoOneToolRecord.id == rid))
        record = res.scalars().first()
        if record is None:
            return
        merged = dict(record.data or {})
        for key, value in metadata.items():
            merged[key] = value
        record.data = merged
        db.add(record)
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
