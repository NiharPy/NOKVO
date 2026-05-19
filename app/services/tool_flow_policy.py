from __future__ import annotations

import re
from typing import Any

from app.services.nokvo_one_business_templates import normalize_business_type
from app.services.tool_flow_questions import build_tool_flow_questions, generated_questions_from_status
from app.services.voice_turn_policy import (
    _coming_back_prefix,
    _is_side_question_during_booking,
    extract_turn_entities,
    normalize_phone_number,
)


_YES_RE = re.compile(r"\b(yes|yeah|yep|sure|ok|okay|please|book|schedule)\b|(అవును|సరే|చేయండి|బుక్|हाँ|हा|ठीक)", re.IGNORECASE)
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

    flow_key = str(flow_state.get("flow_key") or "")
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

    next_slot = _next_slot(flow_state, bundle)
    flow_state["pending_slot"] = next_slot
    if next_slot:
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
