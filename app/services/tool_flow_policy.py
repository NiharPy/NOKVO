from __future__ import annotations

import re
from typing import Any

from app.services.nokvo_one_business_templates import normalize_business_type
from app.services.tool_flow_questions import build_tool_flow_questions, generated_questions_from_status
from app.services.voice_turn_policy import (
    _AVAILABILITY_QUERY_RE,
    _appointment_is_past,
    _coming_back_prefix,
    _email_confirmation_prompt,
    _id_confirmation_prompt,
    _is_high_confidence_phone,
    _is_side_question_during_booking,
    _language_code,
    _looks_affirmative,
    _looks_negative,
    _phone_confirmation_prompt,
    _shift_to_next_day_prompt,
    extract_turn_entities,
    normalize_phone_number,
)


# ── Domain-specific inference cues ────────────────────────────────────────
# When the caller volunteers slot data in any utterance (often the opener
# or a free-text "reason"), we pre-fill the corresponding slots so the FSM
# doesn't ask again. Same idea as the clinic visit_type / urgent_symptoms
# inference, generalised per business.
_BUDGET_INFER_RE = re.compile(
    r"\b(?:budget(?:\s+(?:is|of|around|under|upto|up\s+to))?\s*(?:₹|rs\.?|inr)?\s*"
    r"(\d+(?:[.,]\d+)?)\s*(?:lakh|lakhs|l|crore|crores|cr|k|thousand)?)\b|"
    r"\b(?:around|approx(?:imately)?|under|upto|up\s+to)\s+(?:₹|rs\.?|inr)?\s*"
    r"(\d+(?:[.,]\d+)?)\s*(?:lakh|lakhs|l|crore|crores|cr|k|thousand)\b",
    re.IGNORECASE,
)
_PROPERTY_TYPE_INFER_RE = re.compile(
    r"\b(\d\s*(?:bhk|bedroom|bed|rk)|"
    r"apartment|flat|villa|studio|penthouse|plot|land|"
    r"independent\s+house|row\s+house|commercial|office\s+space|shop)\b",
    re.IGNORECASE,
)
_LOCATION_INFER_RE = re.compile(
    r"\b(?:in|at|near|around)\s+([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,2})\b"
)
_PARTY_SIZE_INFER_RE = re.compile(
    r"\b(?:for|of|table\s+for|party\s+of|group\s+of)\s+(\d{1,2})\s*"
    r"(?:people|persons|guests|adults|pax)?\b",
    re.IGNORECASE,
)
_ORDER_ID_INFER_RE = re.compile(
    r"\b(?:order\s*(?:id|number|no\.?|#)?|"
    r"ticket\s*(?:id|number|no\.?|#)?|"
    r"reference\s*(?:id|number|no\.?|#)?)\s*[:\-]?\s*"
    r"([A-Z0-9]{4,}[-A-Z0-9]*)\b",
    re.IGNORECASE,
)
_ISSUE_TYPE_INFER_RE = re.compile(
    r"\b(damaged|broken|wrong\s+(?:item|size|colour|color)|missing|defective|"
    r"not\s+working|delayed|never\s+(?:arrived|delivered)|"
    r"return(?:\s+request)?|refund\s+request)\b",
    re.IGNORECASE,
)


_BUSINESS_INFERENCE_KINDS = {
    "real_estate": {"budget", "property_type", "location"},
    "hospitality": {"party_size"},
    "ecommerce": {"order_id", "issue_type"},
}


def _infer_domain_slots(
    text: str,
    business_type: str | None,
    bundle: dict[str, Any],
    flow_key: str,
    collected: dict[str, Any],
) -> list[str]:
    """Mine the caller's utterance for slot values they volunteered. Pre-fill
    only when the slot is empty AND it exists in this flow's schema."""
    filled: list[str] = []
    if not text:
        return filled
    normalised = (business_type or "").lower()
    kinds = _BUSINESS_INFERENCE_KINDS.get(normalised, set())
    if not kinds:
        return filled
    available_slots = {str(slot.get("key")): str(slot.get("kind") or "") for slot in _flow_slots(bundle, flow_key)}

    def _try_fill(kind: str, value: Any) -> None:
        for key, slot_kind in available_slots.items():
            if slot_kind == kind and not collected.get(key):
                collected[key] = value
                filled.append(key)
                return
        # Fall back to matching by slot key when the schema uses descriptive
        # kinds (e.g., kind="generic" with key "budget").
        for key in available_slots:
            if kind in key and not collected.get(key):
                collected[key] = value
                filled.append(key)
                return

    if "budget" in kinds:
        m = _BUDGET_INFER_RE.search(text)
        if m:
            raw = m.group(1) or m.group(2)
            if raw:
                try:
                    _try_fill("budget", float(raw.replace(",", "")))
                except ValueError:
                    _try_fill("budget", raw)
    if "property_type" in kinds:
        m = _PROPERTY_TYPE_INFER_RE.search(text)
        if m:
            _try_fill("property_type", m.group(1).strip())
    if "location" in kinds:
        m = _LOCATION_INFER_RE.search(text)
        if m:
            _try_fill("location", m.group(1).strip())
    if "party_size" in kinds:
        m = _PARTY_SIZE_INFER_RE.search(text)
        if m:
            try:
                _try_fill("party_size", int(m.group(1)))
            except ValueError:
                pass
    if "order_id" in kinds:
        m = _ORDER_ID_INFER_RE.search(text)
        if m:
            _try_fill("order_id", m.group(1).strip())
    if "issue_type" in kinds:
        m = _ISSUE_TYPE_INFER_RE.search(text)
        if m:
            _try_fill("issue_type", m.group(1).lower().strip())
    return filled


# Kinds that warrant a read-back handshake before we persist. Names are
# already handled; this list covers high-stakes numeric/identifier fields
# where STT errors are common and silent corruption is bad (wrong phone,
# wrong order ID).
_CONFIRMATION_KINDS = {"phone", "email", "reference_number"}


_YES_RE = re.compile(r"\b(yes|yeah|yep|sure|ok|okay|please|book|schedule)\b|(అవును|సరే|చేయండి|బుక్|हाँ|हा|ठीक)", re.IGNORECASE)


def _name_confirmation_prompt(name: str, language: str | None) -> str:
    lang = _language_code(language)
    if lang == "te":
        return f"Just to confirm — పేరు {name} అని. Right ఆ?"
    if lang == "hi":
        return f"बस कन्फर्म करना है — नाम {name} है। सही है?"
    return f"Just to confirm — the name is {name}. Is that right?"


def _date_kind_keys(bundle: dict[str, Any], flow_key: str) -> list[str]:
    return [
        str(slot.get("key"))
        for slot in _flow_slots(bundle, flow_key)
        if str(slot.get("kind") or "") == "date"
    ]


def _time_kind_keys(bundle: dict[str, Any], flow_key: str) -> list[str]:
    return [
        str(slot.get("key"))
        for slot in _flow_slots(bundle, flow_key)
        if str(slot.get("kind") or "") == "time"
    ]

_VISIT_INTENT_RE = re.compile(
    r"\b(site\s+visit|visit|see\s+(?:it|property|flat|house)|view(?:ing)?|schedule\s+a\s+visit)\b|"
    r"(విజిట్|చూడాలి|చూడాలని|సైట్\s*విజిట్|ప్రాపర్టీ\s*చూడ|देखना|विजिट|साइट\s*विजिट)",
    re.IGNORECASE,
)
_LEAD_INTENT_RE = re.compile(
    r"\b(interested|looking\s+for|need\s+details|contact\s+me|call\s+me|enquiry|inquiry)\b|"
    r"(ఆసక్తి|డీటెయిల్స్|వివరాలు|కాంటాక్ట్|संपर्क|जानकारी|दिलचस्पी)",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b[^@\s]+@[^@\s]+\.[^@\s]+\b")
_NUMBER_RE = re.compile(r"(?:₹|rs\.?|inr)?\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)

# Free-text slot kinds where the user might inadvertently dictate a question
# (e.g., "what services do you offer?") that we must NOT accept as the slot
# value. Strict slot kinds like phone/email/date/time have their own narrow
# extractors and reject question-shaped input naturally.
_FREE_TEXT_SLOT_KINDS = {"name", "reason", "location", "property_type", "generic"}
_QUESTION_SHAPED_RE = re.compile(
    r"\?\s*$|"
    r"\b(what|where|when|why|how|can|could|would|will|do|does|did|is|are|tell|explain|list|share)\b\s+(?:you|i|me|us|the|your|me\s+about)|"
    r"\b(is\s+it\s+possible|could\s+you|can\s+you|would\s+you)\b|"
    r"(ఏమి|ఎక్కడ|ఎప్పుడు|ఎలా|చెప్పగలరా|క్या|कहाँ|कब|कैसे|बताइए|बताओ)",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _language(language: str | None) -> str:
    return (language or "en").split("-")[0].lower()


def _last_assistant_offered_visit(history: list[dict[str, str]]) -> bool:
    for turn in reversed((history or [])[-4:]):
        if turn.get("role") != "assistant":
            continue
        text = str(turn.get("content") or "")
        if _VISIT_INTENT_RE.search(text) or re.search(r"\b(schedule|book).{0,30}\bvisit\b", text, re.IGNORECASE):
            return True
    return False


def _question_for_slot(bundle: dict[str, Any], flow_key: str, slot_key: str, language: str | None) -> str:
    lang = _language(language)
    flow = ((bundle.get("flows") or {}).get(flow_key) or {})
    for slot in flow.get("slots") or []:
        if slot.get("key") == slot_key:
            questions = slot.get("questions") or {}
            return str(questions.get(lang) or questions.get("en") or f"Please share {slot.get('label') or slot_key}.")
    return {
        "hi": f"कृपया {slot_key.replace('_', ' ')} बताइए.",
        "te": f"{slot_key.replace('_', ' ')} చెప్పండి.",
    }.get(lang, f"Please share {slot_key.replace('_', ' ')}.")


def _field_kind_for_slot(bundle: dict[str, Any], flow_key: str, slot_key: str) -> str:
    flow = ((bundle.get("flows") or {}).get(flow_key) or {})
    for slot in flow.get("slots") or []:
        if slot.get("key") == slot_key:
            return str(slot.get("kind") or "generic")
    return "generic"


def _flow_slots(bundle: dict[str, Any], flow_key: str) -> list[dict[str, Any]]:
    flow = ((bundle.get("flows") or {}).get(flow_key) or {})
    return [slot for slot in (flow.get("slots") or []) if isinstance(slot, dict)]


def _next_slot(flow_state: dict[str, Any], bundle: dict[str, Any]) -> str | None:
    collected = dict(flow_state.get("collected") or {})
    for slot in _flow_slots(bundle, str(flow_state.get("flow_key") or "")):
        if slot.get("required", True) and not collected.get(slot.get("key")):
            return str(slot.get("key"))
    return None


def _extract_value(text: str, slot_key: str, kind: str) -> Any:
    value = _clean(text).strip(" ,.-")
    if not value:
        return None
    if kind == "phone" or slot_key in {"phone", "mobile", "contact_phone"}:
        return normalize_phone_number(value, expected=True)
    if kind == "email" or slot_key == "email":
        match = _EMAIL_RE.search(value)
        return match.group(0) if match else None
    if kind in {"date", "time"} or slot_key in {"visit_date", "visit_time"}:
        entities = extract_turn_entities(value, expected_slot="preferred_date" if kind == "date" else "preferred_time")
        return entities.get("date_text") if kind == "date" else entities.get("time_text")
    if kind == "budget":
        match = _NUMBER_RE.search(value.replace(",", ""))
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                return value
        return value
    if kind in _FREE_TEXT_SLOT_KINDS and _QUESTION_SHAPED_RE.search(value):
        # The caller dictated a question instead of answering this slot. Don't
        # consume the text — let the route layer yield to RAG and resume the
        # slot on the next turn.
        return None
    if kind == "name" or slot_key in {"name", "customer_name", "full_name"}:
        value = re.sub(r"^(?:my\s+name\s+is|name\s+is|i\s+am|this\s+is|నా\s+పేరు|పేరు|मेरा\s+नाम)\s+", "", value, flags=re.IGNORECASE)
        value = re.sub(r"(?:గారు|అండి|ండి|sir|madam|ji|जी)\.?\s*$", "", value, flags=re.IGNORECASE)
        # Strip Indic sentence terminators (Devanagari ।, double-danda ॥)
        # plus ASCII punctuation so the captured name doesn't carry STT
        # noise like "निहार।" through to the booking record.
        value = value.strip(" ,.-।॥…・「」（）")
        return _clean(value)
    return value


def _start_flow_key(text: str, business_type: str | None, history: list[dict[str, str]]) -> str | None:
    if normalize_business_type(business_type) == "real_estate" and (
        _VISIT_INTENT_RE.search(text) or (_YES_RE.search(text) and _last_assistant_offered_visit(history))
    ):
        return "real_estate_site_visit"
    if _LEAD_INTENT_RE.search(text):
        return "leads_create"
    return None


def _flow_action(flow_state: dict[str, Any]) -> dict[str, Any] | None:
    flow_key = str(flow_state.get("flow_key") or "")
    collected = dict(flow_state.get("collected") or {})
    if flow_key == "real_estate_site_visit":
        return {
            "tool_key": "qualify_lead_and_schedule_visit",
            "flow_key": flow_key,
            "arguments": collected,
        }
    if flow_key == "leads_create":
        return {
            "tool_key": "leads_create",
            "flow_key": flow_key,
            "arguments": collected,
        }
    return None


def evaluate_tool_flow_policy(
    text: str,
    *,
    business_type: str | None,
    schema_overrides: dict[str, Any] | None = None,
    custom_tabs: list[dict[str, Any]] | None = None,
    provider_status: dict[str, Any] | None = None,
    history: list[dict[str, str]] | None = None,
    state: dict[str, Any] | None = None,
    language: str | None = None,
) -> dict[str, Any] | None:
    value = _clean(text)
    if not value:
        return None
    history = history or []
    state = dict(state or {})
    persisted = generated_questions_from_status(provider_status)
    expected = build_tool_flow_questions(business_type, schema_overrides, custom_tabs)
    bundle = persisted if persisted.get("schema_hash") == expected.get("schema_hash") else expected

    # Anti-repeat: any slot we already know from the conversational
    # memory (snapshot lives in ``state['memory']['facts']`` keyed by
    # the canonical fact name) should pre-populate ``collected`` so
    # the flow skips asking. The hydrator only fills gaps — explicit
    # flow writes win.
    try:
        # Local import to avoid the import-time cycle with services
        # that themselves consume tool_flow_policy.
        from app.services.conversational_memory import (
            ConversationalMemory as _CM,
            hydrate_flow_collected as _hydrate,
        )
        memory_obj = _CM.from_state_blob((state or {}).get("memory") or {})
    except Exception:
        memory_obj = None
        _hydrate = None  # type: ignore[assignment]

    flow_state = dict(state.get("tool_flow") or {})
    newly_started = False
    if not flow_state.get("active"):
        flow_key = _start_flow_key(value, business_type, history)
        if not flow_key or flow_key not in (bundle.get("flows") or {}):
            return None
        # The caller asked a side question on the same turn that signals intent
        # ("Before I book, what services do you offer?"). Let the route fall
        # through to RAG; the pipeline will mark the tool_flow as deferred so
        # the next turn resumes with a "Coming back to your booking — " prefix.
        entities_for_pivot = extract_turn_entities(value, expected_slot=None)
        if _is_side_question_during_booking(value, entities=entities_for_pivot):
            return None
        newly_started = True
        flow_state = {
            "active": True,
            "flow_key": flow_key,
            "tool_key": ((bundle.get("flows") or {}).get(flow_key) or {}).get("tool_key"),
            "collected": {},
        }

    # Memory hydration — fill in any slot the caller has already told
    # us about (this or a prior call) so ``_next_slot`` won't ask for
    # things we know. Done once per evaluation pass; subsequent
    # writes by the flow itself still take precedence.
    if memory_obj is not None and _hydrate is not None:
        try:
            flow_state["collected"] = _hydrate(
                flow_state.get("collected") or {},
                memory_obj,
            )
        except Exception:
            pass

    flow_key = str(flow_state.get("flow_key") or "")

    # ── 1) Availability-lookup intent ─────────────────────────────────────
    # When the caller asks "is X available?" / "when can you book me?" / "any
    # slot tomorrow?" while a flow is in progress, return an intent the
    # pipeline can resolve against the scheduler instead of falling through
    # to a slow RAG path. Works for site_visit and reservation flows the
    # same way it does for clinic appointments.
    if flow_state.get("active") and _AVAILABILITY_QUERY_RE.search(value):
        availability_entities: dict[str, Any] = {}
        date_entities = extract_turn_entities(value, expected_slot=None)
        if date_entities.get("date_text"):
            availability_entities["date_text"] = date_entities["date_text"]
        if date_entities.get("time_text"):
            availability_entities["time_text"] = date_entities["time_text"]
        return {
            "answer": None,
            "intent": "availability_check",
            "entities": availability_entities,
            "language": _language_code(language),
            "state_patch": {"tool_flow": flow_state},
            "state_slot": "availability_check",
            "reason": "tool_flow caller asked about availability",
            "flow_key": flow_key,
        }

    # ── 2) Caller answering "yes/no" to a slot we just offered ──────────
    if flow_state.get("awaiting_slot_confirm"):
        if _looks_affirmative(value):
            proposed_utc = flow_state.pop("proposed_slot_utc", None)
            _label = flow_state.pop("proposed_slot_label", None)
            flow_state["awaiting_slot_confirm"] = False
            if proposed_utc:
                # Snap the offered slot into the appropriate date/time slots.
                from datetime import datetime as _dt
                try:
                    parsed = _dt.fromisoformat(proposed_utc.replace("Z", "+00:00"))
                    local_iso = parsed.astimezone().date().isoformat()
                    local_time = parsed.astimezone().strftime("%I:%M %p").lstrip("0")
                    collected = dict(flow_state.get("collected") or {})
                    for key in _date_kind_keys(bundle, flow_key):
                        collected[key] = local_iso
                    for key in _time_kind_keys(bundle, flow_key):
                        collected[key] = local_time
                    flow_state["collected"] = collected
                    from app.services.flow_session import stamp_proposed_slot_accepted
                    stamp_proposed_slot_accepted(
                        flow_state, slot_utc_iso=proposed_utc, member_name=None,
                    )
                except Exception:
                    pass
            # Fall through so the next missing slot (or completion) fires.
        elif _looks_negative(value):
            flow_state["awaiting_slot_confirm"] = False
            flow_state.pop("proposed_slot_utc", None)
            flow_state.pop("proposed_slot_label", None)
            # Clear date/time slots so the flow asks again.
            collected = dict(flow_state.get("collected") or {})
            for key in _date_kind_keys(bundle, flow_key) + _time_kind_keys(bundle, flow_key):
                collected.pop(key, None)
            flow_state["collected"] = collected

    # ── 3a) Caller answering phone/email/id confirmation ───────────────
    if flow_state.get("awaiting_id_confirmation"):
        confirmation_key = str(flow_state.get("id_confirmation_slot") or "")
        confirmation_kind = str(flow_state.get("id_confirmation_kind") or "")
        flow_state["awaiting_id_confirmation"] = False
        flow_state.pop("id_confirmation_slot", None)
        flow_state.pop("id_confirmation_kind", None)
        if _looks_affirmative(value):
            from app.services.flow_session import record_confirmation, append_audit_trail
            collected_now = dict(flow_state.get("collected") or {})
            record_confirmation(flow_state, confirmation_key, collected_now.get(confirmation_key))
            append_audit_trail(flow_state, f"{confirmation_kind}_confirmed", detail=collected_now.get(confirmation_key))
        elif _looks_negative(value):
            collected = dict(flow_state.get("collected") or {})
            if confirmation_key:
                collected.pop(confirmation_key, None)
            flow_state["collected"] = collected
            flow_state["pending_slot"] = confirmation_key
            return {
                "answer": _question_for_slot(bundle, flow_key, confirmation_key, language),
                "intent": "tool_flow",
                "flow_key": flow_key,
                "state_patch": {"tool_flow": flow_state},
                "state_slot": confirmation_key,
                "reason": f"{confirmation_kind} confirmation rejected",
            }
        else:
            replacement = _extract_value(value, confirmation_key, confirmation_kind)
            if replacement:
                collected = dict(flow_state.get("collected") or {})
                collected[confirmation_key] = replacement
                flow_state["collected"] = collected
                flow_state["awaiting_id_confirmation"] = True
                flow_state["id_confirmation_slot"] = confirmation_key
                flow_state["id_confirmation_kind"] = confirmation_kind
                if confirmation_kind == "phone":
                    prompt = _phone_confirmation_prompt(str(replacement), language)
                elif confirmation_kind == "email":
                    prompt = _email_confirmation_prompt(str(replacement), language)
                else:
                    label = confirmation_key.replace("_", " ")
                    prompt = _id_confirmation_prompt(str(replacement), label, language)
                return {
                    "answer": prompt,
                    "intent": "tool_flow",
                    "flow_key": flow_key,
                    "state_patch": {"tool_flow": flow_state},
                    "state_slot": f"{confirmation_key}_confirm",
                    "reason": f"{confirmation_kind} corrected, awaiting confirmation",
                }

    # ── 3b) Caller answering name confirmation ─────────────────────────
    if flow_state.get("awaiting_name_confirmation"):
        confirmation_key = str(flow_state.get("name_confirmation_slot") or "")
        flow_state["awaiting_name_confirmation"] = False
        flow_state.pop("name_confirmation_slot", None)
        if _looks_affirmative(value):
            from app.services.flow_session import record_confirmation, append_audit_trail
            collected_now = dict(flow_state.get("collected") or {})
            record_confirmation(flow_state, confirmation_key, collected_now.get(confirmation_key))
            append_audit_trail(flow_state, "name_confirmed", detail=collected_now.get(confirmation_key))
        elif _looks_negative(value):
            # Look for an embedded correction first so "No, my name is
            # Nihar" still moves the conversation forward. Strip the
            # leading negation so the name extractor doesn't carry the
            # "no" into the extracted value.
            stripped = re.sub(
                r"^\s*(?:no|nope|nah|not\s+really|actually|wait|sorry)\b[\s,]*",
                "",
                value or "",
                flags=re.IGNORECASE,
            )
            embedded_correction = (
                _extract_value(stripped, confirmation_key, "name")
                if confirmation_key and stripped and stripped != value
                else None
            )
            if embedded_correction:
                collected = dict(flow_state.get("collected") or {})
                collected[confirmation_key] = embedded_correction
                flow_state["collected"] = collected
                flow_state["awaiting_name_confirmation"] = True
                flow_state["name_confirmation_slot"] = confirmation_key
                return {
                    "answer": _name_confirmation_prompt(embedded_correction, language),
                    "intent": "tool_flow",
                    "flow_key": flow_key,
                    "state_patch": {"tool_flow": flow_state},
                    "state_slot": f"{confirmation_key}_confirm",
                    "reason": "name corrected, awaiting confirmation",
                }
            collected = dict(flow_state.get("collected") or {})
            if confirmation_key:
                collected.pop(confirmation_key, None)
            flow_state["collected"] = collected
            flow_state["pending_slot"] = confirmation_key
            return {
                "answer": _question_for_slot(bundle, flow_key, confirmation_key, language),
                "intent": "tool_flow",
                "flow_key": flow_key,
                "state_patch": {"tool_flow": flow_state},
                "state_slot": confirmation_key,
                "reason": "name confirmation rejected",
            }
        else:
            replacement = _extract_value(value, confirmation_key, "name") if confirmation_key else None
            if replacement:
                collected = dict(flow_state.get("collected") or {})
                collected[confirmation_key] = replacement
                flow_state["collected"] = collected
                flow_state["awaiting_name_confirmation"] = True
                flow_state["name_confirmation_slot"] = confirmation_key
                return {
                    "answer": _name_confirmation_prompt(replacement, language),
                    "intent": "tool_flow",
                    "flow_key": flow_key,
                    "state_patch": {"tool_flow": flow_state},
                    "state_slot": f"{confirmation_key}_confirm",
                    "reason": "name corrected, awaiting confirmation",
                }

    pending = str(flow_state.get("pending_slot") or "") or _next_slot(flow_state, bundle)
    collected = dict(flow_state.get("collected") or {})
    if pending and not newly_started:
        # Mid-flow digression check: if the caller pivoted to a KB question
        # rather than answering the pending slot, return None so the route
        # layer yields to RAG. The unchanged tool_flow state in Redis keeps
        # the pending slot intact; the next turn resumes the same question
        # (with a "Coming back" prefix once the pipeline marks deferred).
        entities_for_pivot = extract_turn_entities(value, expected_slot=None)
        if _is_side_question_during_booking(value, entities=entities_for_pivot):
            return None
        kind = _field_kind_for_slot(bundle, flow_key, pending)
        extracted = _extract_value(value, pending, kind)
        if extracted:
            collected[pending] = extracted
            flow_state["collected"] = collected

        # Domain inference: even when this slot didn't capture, the caller
        # may have volunteered budget/property_type/party_size/order_id etc.
        # in the same utterance. Pre-fill matching slots in the flow schema.
        inferred = _infer_domain_slots(value, business_type, bundle, flow_key, collected)
        if inferred:
            flow_state["collected"] = collected

        if extracted:
            # ── 4) Name-kind slot → confirm before moving on ─────────────
            if kind == "name":
                # When the slot was deferred for a side question, the
                # "Coming back to your booking" prefix must precede the
                # confirmation prompt — otherwise the caller hears the
                # confirmation without acknowledgment of the detour.
                resumed_from_kb = bool(flow_state.pop("deferred_for_kb", False))
                prefix = _coming_back_prefix(language) if resumed_from_kb else ""
                flow_state["awaiting_name_confirmation"] = True
                flow_state["name_confirmation_slot"] = pending
                flow_state["pending_slot"] = f"{pending}_confirm"
                return {
                    "answer": prefix + _name_confirmation_prompt(extracted, language),
                    "intent": "tool_flow",
                    "flow_key": flow_key,
                    "state_patch": {"tool_flow": flow_state},
                    "state_slot": f"{pending}_confirm",
                    "reason": "name captured, awaiting confirmation",
                }

            # ── 4b) Other high-stakes kinds → confirm too ─────────────────
            if kind in _CONFIRMATION_KINDS:
                # Spec-gated skip: phone confirmation can be waived for a
                # canonical 10-digit Indian mobile when the policy allows.
                from app.services.agent_spec import CONFIRMATION_POLICY

                skip = (
                    kind == "phone"
                    and CONFIRMATION_POLICY.confirm_phone_unless_high_confidence
                    and _is_high_confidence_phone(str(extracted))
                )
                if not skip:
                    if kind == "phone":
                        prompt = _phone_confirmation_prompt(str(extracted), language)
                    elif kind == "email":
                        prompt = _email_confirmation_prompt(str(extracted), language)
                    else:
                        label = pending.replace("_", " ")
                        prompt = _id_confirmation_prompt(str(extracted), label, language)
                    flow_state["awaiting_id_confirmation"] = True
                    flow_state["id_confirmation_slot"] = pending
                    flow_state["id_confirmation_kind"] = kind
                    flow_state["pending_slot"] = f"{pending}_confirm"
                    return {
                        "answer": prompt,
                        "intent": "tool_flow",
                        "flow_key": flow_key,
                        "state_patch": {"tool_flow": flow_state},
                        "state_slot": f"{pending}_confirm",
                        "reason": f"{kind} captured, awaiting confirmation",
                    }

            # ── 5) Past-time guard when both date+time now present ───────
            date_slots = _date_kind_keys(bundle, flow_key)
            time_slots = _time_kind_keys(bundle, flow_key)
            date_value = next((collected.get(k) for k in date_slots if collected.get(k)), None)
            time_value = next((collected.get(k) for k in time_slots if collected.get(k)), None)
            if date_value and time_value and _appointment_is_past(date_value, time_value):
                shift = _shift_to_next_day_prompt(date_value, time_value, language)
                if shift is not None:
                    next_iso, _nice_date, prompt = shift
                    flow_state["proposed_date"] = next_iso
                    flow_state["original_time"] = time_value
                    flow_state["awaiting_past_time_shift"] = True
                    return {
                        "answer": prompt,
                        "intent": "tool_flow",
                        "flow_key": flow_key,
                        "state_patch": {"tool_flow": flow_state},
                        "state_slot": "date_shift_confirm",
                        "reason": "tool_flow time in the past — offered same time next day",
                    }

    # ── 6) Answer to "same time tomorrow?" prompt ──────────────────────
    if flow_state.get("awaiting_past_time_shift"):
        flow_state["awaiting_past_time_shift"] = False
        proposed = flow_state.pop("proposed_date", None)
        original_time = flow_state.pop("original_time", None)
        if _looks_affirmative(value) and proposed:
            collected = dict(flow_state.get("collected") or {})
            for key in _date_kind_keys(bundle, flow_key):
                collected[key] = proposed
            if original_time:
                for key in _time_kind_keys(bundle, flow_key):
                    collected[key] = original_time
            flow_state["collected"] = collected
        else:
            collected = dict(flow_state.get("collected") or {})
            for key in _date_kind_keys(bundle, flow_key) + _time_kind_keys(bundle, flow_key):
                collected.pop(key, None)
            flow_state["collected"] = collected

    next_slot = _next_slot(flow_state, bundle)
    flow_state["pending_slot"] = next_slot
    if next_slot:
        # Adaptive disambiguation: caller gave a date but no time for this
        # flow. Trigger the scheduler so we offer a concrete free slot.
        collected_now = dict(flow_state.get("collected") or {})
        next_kind = _field_kind_for_slot(bundle, flow_key, next_slot)
        date_filled = any(collected_now.get(k) for k in _date_kind_keys(bundle, flow_key))
        if (
            next_kind == "time"
            and date_filled
            and not flow_state.get("offered_disambiguation")
        ):
            flow_state["offered_disambiguation"] = True
            date_text = next((collected_now.get(k) for k in _date_kind_keys(bundle, flow_key) if collected_now.get(k)), None)
            return {
                "answer": None,
                "intent": "availability_check",
                "entities": {"date_text": date_text} if date_text else {},
                "language": _language_code(language),
                "state_patch": {"tool_flow": flow_state},
                "state_slot": "availability_disambiguation",
                "reason": "date given without time — offering concrete slot",
                "flow_key": flow_key,
            }
        # Consume the deferred-for-kb marker exactly once when resuming a flow
        # that paused mid-slot for a side question. The prefix acknowledges the
        # detour ("Coming back to your booking — ") before the slot question.
        resumed_from_kb = bool(flow_state.pop("deferred_for_kb", False))
        prefix = _coming_back_prefix(language) if resumed_from_kb else ""
        return {
            "answer": prefix + _question_for_slot(bundle, flow_key, next_slot, language),
            "intent": "tool_flow",
            "flow_key": flow_key,
            "state_patch": {"tool_flow": flow_state},
            "state_slot": next_slot,
            "reason": f"{flow_key} slot collection",
        }

    flow_state["active"] = False
    flow_state["completed"] = True
    action = _flow_action(flow_state)
    return {
        "answer": "One moment, I’ll record that.",
        "intent": "tool_flow",
        "flow_key": flow_key,
        "state_patch": {"tool_flow": flow_state},
        "state_slot": "complete",
        "reason": f"{flow_key} slots complete",
        "action": action,
    }
