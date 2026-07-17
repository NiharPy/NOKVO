"""Generic tool-flow FSM executor and its success/reprompt answers.

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

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization
from app.models.tenant_resources import TenantResources
from app.services.agent_session_store import AgentSessionStore
from app.services.dynamic_tool_resolver import resolve_index
from app.services.pipeline.appointments import _APPOINTMENT_LOCAL_TZ, _AppointmentToolInputError
from app.services.predefined_tools_service import PredefinedToolsService
from app.services.sarvam_voice_service import SARVAM_LANGUAGE_OPTIONS, SarvamVoiceService
from app.services.tool_flow_questions import build_tool_flow_questions, format_field_questions_prompt

logger = logging.getLogger(__name__)


def _tool_flow_success_answer(result: dict[str, Any], args: dict[str, Any], *, flow_key: str, language: str | None, offer_sms: bool = False) -> str:
    lang = SarvamVoiceService.normalize_language(language)
    assigned_name = result.get("assigned_member_name")
    assignment_status = result.get("assignment_status")
    name = str(args.get("name") or args.get("customer_name") or args.get("phone") or "the customer")
    phone = str(args.get("phone") or args.get("contact_phone") or "").strip()
    # End-of-call SMS offer is opt-in (mirrors clinic flow). Disabled
    # by default because SMS dispatch isn't wired in yet.
    sms_offer = ""
    if offer_sms and phone:
        spoken_phone = " ".join(list(phone[-10:])) if phone[-10:].isdigit() else phone
        if lang == "te":
            sms_offer = f" {spoken_phone} కి confirmation SMS పంపాలా?"
        elif lang == "hi":
            sms_offer = f" क्या {spoken_phone} पर confirmation SMS भेज दूँ?"
        else:
            sms_offer = f" Want me to send a confirmation SMS to {spoken_phone}?"
    if flow_key == "real_estate_site_visit":
        when = str(args.get("visit_at") or "the requested time")
        try:
            parsed = datetime.fromisoformat(when.replace("Z", "+00:00"))
            local_when = parsed.astimezone(_APPOINTMENT_LOCAL_TZ).strftime("%d %b %Y at %I:%M %p")
        except Exception:
            local_when = when
        if lang == "te":
            if assignment_status == "assigned" and assigned_name:
                return f"Site visit request create అయ్యింది for {name} on {local_when}. It has been assigned to {assigned_name}.{sms_offer}"
            return f"Site visit request create అయ్యింది for {name} on {local_when}. Team availability confirm చేస్తారు.{sms_offer}"
        if lang == "hi":
            if assignment_status == "assigned" and assigned_name:
                return f"Site visit request create हो गया for {name} on {local_when}. It has been assigned to {assigned_name}.{sms_offer}"
            return f"Site visit request create हो गया for {name} on {local_when}. Team availability confirm करेगी.{sms_offer}"
        if assignment_status == "assigned" and assigned_name:
            return f"I have created the site visit request for {name} on {local_when}. It has been assigned to {assigned_name}.{sms_offer}"
        return f"I have created the site visit request for {name} on {local_when}. The team will confirm availability.{sms_offer}"

    if lang == "te":
        return f"Lead create అయ్యింది for {name}. Team follow up చేస్తారు.{sms_offer}"
    if lang == "hi":
        return f"Lead create हो गया for {name}. Team follow up करेगी.{sms_offer}"
    return f"I have created the lead for {name}. The team will follow up.{sms_offer}"


def _site_visit_hours_reprompt(
    *,
    requested_dt: datetime,
    suggestion_dt: datetime | None,
    defaults: Any,
    language: str | None,
) -> str:
    """Spoken rejection for an out-of-hours site-visit time: states the org
    working window, says the requested time isn't available, and offers the
    closest valid slot (when one could be computed)."""
    lang = SarvamVoiceService.normalize_language(language)

    def _fmt_time(value: time | None) -> str:
        if value is None:
            return ""
        return value.strftime("%I:%M %p").lstrip("0")

    def _fmt_dt(value: datetime | None) -> str:
        if value is None:
            return ""
        return value.astimezone(_APPOINTMENT_LOCAL_TZ).strftime("%I:%M %p").lstrip("0")

    start = _fmt_time(defaults.start_time)
    end = _fmt_time(defaults.end_time)
    requested = _fmt_dt(requested_dt)
    suggestion = _fmt_dt(suggestion_dt)

    if lang == "te":
        base = f"మా site visits {start} నుంచి {end} వరకు మాత్రమే. {requested} కి కుదరదు."
        if suggestion:
            return f"{base} దగ్గరగా {suggestion} కి కుదురుతుంది. అది ok నా?"
        return f"{base} working hours లో వేరే time చెప్పగలరా?"
    if lang == "hi":
        base = f"हमारी site visits {start} से {end} तक होती हैं. {requested} संभव नहीं है."
        if suggestion:
            return f"{base} सबसे करीब {suggestion} पर हो सकता है. क्या यह ठीक है?"
        return f"{base} कृपया working hours के अंदर कोई और time बताइए."
    base = f"Our site visits run {start} to {end}, so {requested} isn't possible."
    if suggestion:
        return f"{base} The closest I can do is {suggestion}. Does that work?"
    return f"{base} Could you pick a time within working hours?"


async def _maybe_execute_tool_flow_action(
    helpers: Any,
    tenant_res: TenantResources,
    call_id: str | None,
    db: AsyncSession | None,
    tool_flow: dict[str, Any],
    *,
    business_context: tuple[Organization, dict[str, Any], list[dict[str, Any]]] | None = None,
    language: str | None = None,
) -> dict[str, Any] | None:
    # Snapshot the FK primitive (see _maybe_execute_turn_policy_action) so the
    # fresh-session retry below can't force a sync ORM reload → MissingGreenlet.
    org_id = getattr(tenant_res, "organization_id")
    if tool_flow.get("intent") != "tool_flow" or tool_flow.get("state_slot") != "complete":
        return None
    action = tool_flow.get("action") if isinstance(tool_flow.get("action"), dict) else None
    if not action:
        return None
    context = business_context or await helpers._voice_business_context(db, tenant_res)
    if context is None:
        return None
    organization, overrides, custom_tabs = context
    catalog = resolve_index(organization.industry, overrides, custom_tabs)
    tool_key = str(action.get("tool_key") or "")
    tool = catalog.get(tool_key)
    if tool is None:
        return None
    raw_args = dict(action.get("arguments") or {})
    flow_key = str(action.get("flow_key") or tool_flow.get("flow_key") or "")
    args: dict[str, Any] = {}
    if flow_key == "real_estate_site_visit":
        # Resolve the flow's slots so we can (a) find date/time/name/phone/
        # project slots by KIND (slot keys equal the admin's Site Visit
        # Field keys, which are arbitrary), and (b) store each captured
        # value back under its configured field key (``source_field``) so
        # the Site Visits tab renders the admin's Site Visit Fields.
        from app.services.tool_flow_questions import build_tool_flow_questions

        sv_bundle = build_tool_flow_questions(
            getattr(organization, "industry", None), overrides, custom_tabs
        )
        flow_slots = (
            ((sv_bundle.get("flows") or {}).get("real_estate_site_visit") or {}).get("slots") or []
        )

        def _slot_keys(kind: str) -> list[str]:
            return [str(s.get("key")) for s in flow_slots if s.get("kind") == kind]

        date_keys = _slot_keys("date") or ["visit_date"]
        time_keys = _slot_keys("time") or ["visit_time"]
        date_raw = next((raw_args.get(k) for k in date_keys if raw_args.get(k)), None)
        time_raw = next((raw_args.get(k) for k in time_keys if raw_args.get(k)), None)
        try:
            visit_date = helpers._parse_appointment_date(date_raw)
            visit_time = helpers._parse_appointment_time(time_raw)
        except _AppointmentToolInputError as exc:
            flow_state = dict(((tool_flow.get("state_patch") or {}).get("tool_flow") or {}))
            flow_state["active"] = True
            flow_state["completed"] = False
            flow_state["pending_slot"] = (
                date_keys[0] if exc.slot == "preferred_date" else time_keys[0]
            )
            return {
                "answer": exc.answer,
                "state_patch": {"tool_flow": flow_state},
                "state_slot": flow_state["pending_slot"],
                "route_reason": "tool flow needs exact scheduling detail",
                "tool_calls": [],
            }
        visit_at_dt = datetime.combine(visit_date, visit_time, tzinfo=_APPOINTMENT_LOCAL_TZ).astimezone(timezone.utc)
        visit_at = visit_at_dt.isoformat()

        # Out-of-hours guard: site visits must fall inside the org-wide
        # working window. If the caller asked for a time outside it (e.g.
        # 8 PM when hours are 9 AM–7 PM), don't book — re-prompt with the
        # window stated and the closest valid slot offered. Only enforced
        # when the org has actually configured hours; otherwise no limit.
        from app.services.nokvo_one_assignment_service import (
            _within_working_window,
            suggest_within_working_hours,
        )

        # NOTE: NokvoOneAssignmentService is intentionally NOT imported (verbatim
        # from the pre-extraction module, where the name was also unbound here):
        # reaching this line raises NameError, so THIS path's out-of-hours guard
        # has never fired (the stream-service guard covers out-of-hours). Import
        # it to actually enable the guard on the tool-flow path.
        org_defaults = await NokvoOneAssignmentService.resolve_org_working_window(db, organization.id)
        if (
            org_defaults is not None
            and not _within_working_window(org_defaults, visit_at_dt)
        ):
            suggestion_dt = suggest_within_working_hours(org_defaults, visit_at_dt)
            reprompt = helpers._site_visit_hours_reprompt(
                requested_dt=visit_at_dt,
                suggestion_dt=suggestion_dt,
                defaults=org_defaults,
                language=language,
            )
            flow_state = dict(((tool_flow.get("state_patch") or {}).get("tool_flow") or {}))
            flow_state["active"] = True
            flow_state["completed"] = False
            flow_state["pending_slot"] = time_keys[0]
            return {
                "answer": reprompt,
                "state_patch": {"tool_flow": flow_state},
                "state_slot": time_keys[0],
                "route_reason": "site visit time is outside working hours",
                "tool_calls": [],
            }

        # Field-keyed site-visit data for the Site Visits tab.
        record_data: dict[str, Any] = {}
        for slot in flow_slots:
            skey = str(slot.get("key") or "")
            fkey = str(slot.get("source_field") or skey)
            value = raw_args.get(skey)
            if value in (None, ""):
                continue
            kind = slot.get("kind")
            if kind == "date":
                record_data[fkey] = visit_date.isoformat()
            elif kind == "time":
                record_data[fkey] = visit_time.strftime("%I:%M %p").lstrip("0")
            else:
                record_data[fkey] = value

        name_val = next((raw_args.get(k) for k in _slot_keys("name") if raw_args.get(k)), None) or raw_args.get("name")
        phone_val = next((raw_args.get(k) for k in _slot_keys("phone") if raw_args.get(k)), None) or raw_args.get("phone")
        project_val = next((raw_args.get(k) for k in _slot_keys("project") if raw_args.get(k)), None) or raw_args.get("project_name")

        args = {
            "name": name_val,
            "phone": phone_val,
            "visit_at": visit_at,
            "record_data": record_data,
        }
        if project_val not in (None, ""):
            args["project_name"] = project_val
        if raw_args.get("project_id") not in (None, ""):
            args["project_id"] = raw_args["project_id"]
    else:
        args = {k: v for k, v in raw_args.items() if v not in (None, "")}
    # Same retry shape as the clinic appointment path — reads from spec.
    from app.services.agent_spec import RETRY_POLICY
    from app.db.session import AsyncSessionLocal

    result = None
    last_exc: Exception | None = None
    max_inline_attempts = 1 + RETRY_POLICY.inline_retries
    for attempt in range(max_inline_attempts):
        # First attempt uses the shared call session (matches the
        # historical behaviour the tests cover). Retries fall back to
        # a fresh AsyncSession because the long-lived WS-bound session
        # can sit in a state where ``await db.commit()`` raises
        # ``greenlet_spawn has not been called`` — a one-shot session
        # sidesteps that entire class of session-corruption issues.
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
                "NOKVO-TOOL-FLOW: %s failed (attempt %s/%s, fresh_session=%s) args=%s: %r",
                tool.key,
                attempt + 1,
                max_inline_attempts,
                use_fresh_session,
                {k: v for k, v in args.items() if k != "record_data"},
                exc,
                exc_info=True,
            )
            # Only roll back the shared session — the fresh session's
            # ``async with`` block already rolls itself back on
            # exception.
            if not use_fresh_session and db is not None:
                try:
                    await db.rollback()
                except Exception:
                    logger.exception(
                        "NOKVO-TOOL-FLOW: rollback after %s failure crashed", tool.key
                    )
            if attempt < max_inline_attempts - 1:
                await asyncio.sleep(RETRY_POLICY.inline_delay_seconds)
    if result is None:
        try:
            from app.services.tool_retry_service import ToolRetryService

            await ToolRetryService.enqueue(
                db,
                organization_id=org_id,
                tool_key=tool.key,
                arguments=args,
                context={
                    "call_id": call_id,
                    "language": language,
                    "intent": "tool_flow",
                    "flow_key": flow_key,
                },
                last_error=str(last_exc) if last_exc else None,
            )
        except Exception:
            pass
        flow_state = dict(((tool_flow.get("state_patch") or {}).get("tool_flow") or {}))
        flow_state["active"] = False
        flow_state["tool_error"] = str(last_exc)[:180]
        flow_state["needs_callback"] = True
        from app.services.flow_session import append_audit_trail
        append_audit_trail(flow_state, "tool_retry_enqueued", detail=str(last_exc)[:200] if last_exc else None)
        lang = SarvamVoiceService.normalize_language(language)
        phone_hint = str(args.get("phone") or args.get("contact_phone") or "this number")
        spoken_phone = " ".join(list(phone_hint[-10:])) if phone_hint[-10:].isdigit() else phone_hint
        if lang == "te":
            fallback = (
                f"Details అన్నీ note చేశాను, kāni system temporarily unavailable. "
                f"Team మీకు {spoken_phone} mīda call back chestāru — booking miss avadu."
            )
        elif lang == "hi":
            fallback = (
                f"मेरे पास सारी जानकारी है, पर system अभी temporarily unavailable है. "
                f"Team {spoken_phone} पर call back करेगी — booking miss नहीं होगी."
            )
        else:
            fallback = (
                f"I have all your details, but I'm having trouble saving them right now. "
                f"The team will call you back on {spoken_phone} to confirm — your request won't be missed."
            )
        return {
            "answer": fallback,
            "state_patch": {"tool_flow": flow_state},
            "state_slot": "tool_error",
            "route_reason": "tool flow tool failed after retry",
            "tool_calls": [{"tool": tool.key, "arguments": args, "ok": False, "error": str(last_exc)[:240]}],
        }

    flow_state = dict(((tool_flow.get("state_patch") or {}).get("tool_flow") or {}))
    flow_state.update(
        {
            "active": False,
            "completed": True,
            "created_record_id": result.get("id") or result.get("lead_id") or result.get("callback_id"),
            "assignment_status": result.get("assignment_status"),
            "assigned_member_name": result.get("assigned_member_name"),
        }
    )
    # Same as the clinic path: patch confirmation/audit metadata onto the
    # persisted record now that we have its id.
    record_metadata: dict[str, Any] = {}
    for key in ("confirmations", "audit_trail", "proposed_slot_accepted"):
        value = flow_state.get(key)
        if value:
            record_metadata[key] = value
    created_id = flow_state.get("created_record_id")
    if record_metadata and created_id and db is not None:
        await helpers._patch_record_metadata(db, created_id, record_metadata)

    # Record routing. A completed site-visit booking is tab-defining: it
    # always belongs in the Site Visits (tickets) tab regardless of who
    # placed the call (force_ticket below). Other macros fall back to the
    # call-direction heuristic — inbound → tickets, outbound → leads. The
    # macro defaults to creating leads, so a rewrite only happens when the
    # destination is the tickets tab.
    if db is not None and call_id is not None:
        try:
            session_state = await AgentSessionStore.get_state(tenant_res, call_id) or {}
            surface = session_state.get("call_surface")
            ids_to_route = [
                rid
                for rid in (
                    result.get("lead_id"),
                    result.get("id"),
                )
                if rid
            ]
            if ids_to_route:
                await helpers._route_record_by_surface(
                    db,
                    ids_to_route,
                    call_surface=surface,
                    industry=organization.industry,
                    force_ticket=(flow_key == "real_estate_site_visit"),
                )
        except Exception:
            pass

    return {
        "answer": helpers._tool_flow_success_answer(
            result,
            args,
            flow_key=flow_key,
            language=language,
            offer_sms=helpers._should_offer_sms_confirmation(tenant_res),
        ),
        "state_patch": {"tool_flow": flow_state},
        "state_slot": "complete",
        "route_reason": "tool flow tool executed",
        "tool_calls": [{"tool": tool.key, "arguments": args, "result": result}],
    }
