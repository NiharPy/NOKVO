"""S3 of the booking-engine unification: per-tenant flag + shadow-compare.

The clinic booking path is live, so the cutover from the bespoke appointment FSM
(``evaluate_voice_turn_policy``) to the unified engine (``evaluate_tool_flow_policy``)
is gated behind a per-tenant flag and observed in shadow first:

* flag OFF (default): the bespoke FSM stays live, AND the unified engine is run in
  shadow on a *copy* of the state so we can log how its decision would differ —
  zero caller impact.
* flag ON: ``turn_policy`` is forced to ``None`` so clinics fall through to the
  generic tool_flow block (the same path real-estate uses).

Flip per-tenant once the shadow diffs are clean on real traffic; Stage 4 then
deletes the bespoke FSM.
"""
from __future__ import annotations

import copy
import logging
from typing import Any

logger = logging.getLogger("nokvo_one.booking_shadow")

FLAG_KEY = "unified_booking_engine"


def unified_booking_engine_enabled(provider_status: dict[str, Any] | None) -> bool:
    """True when this tenant has opted into the unified engine for clinics."""
    return bool((provider_status or {}).get(FLAG_KEY))


def _summarize_bespoke(turn_policy: dict[str, Any] | None) -> dict[str, Any]:
    if not turn_policy:
        return {"engine": "bespoke", "intent": None, "state_slot": None}
    appt = (turn_policy.get("state_patch") or {}).get("appointment") or {}
    return {
        "engine": "bespoke",
        "intent": turn_policy.get("intent"),
        "state_slot": turn_policy.get("state_slot"),
        "completed": bool(appt.get("completed")),
        "pending_slot": appt.get("pending_slot"),
    }


def _summarize_unified(tool_flow_result: dict[str, Any] | None) -> dict[str, Any]:
    if not tool_flow_result:
        return {"engine": "unified", "intent": None, "pending_slot": None}
    tf = (tool_flow_result.get("state_patch") or {}).get("tool_flow") or {}
    action = tool_flow_result.get("action") or {}
    return {
        "engine": "unified",
        "intent": tool_flow_result.get("intent"),
        "pending_slot": tf.get("pending_slot"),
        "completed": bool(tf.get("completed")),
        "tool_key": action.get("tool_key") if isinstance(action, dict) else None,
    }


def run_shadow_compare(
    *,
    user_text: str,
    history: list[dict[str, str]],
    state: dict[str, Any],
    language: str | None,
    business_type: str | None,
    schema_overrides: dict[str, Any] | None,
    custom_tabs: list[dict[str, Any]] | None,
    provider_status: dict[str, Any] | None,
    bespoke_turn_policy: dict[str, Any] | None,
    call_id: str | None = None,
) -> None:
    """Run the unified engine on a COPY of the state and log how its decision
    compares to the bespoke FSM's. Best-effort: never raises, never writes state.
    """
    try:
        from app.services.tool_flow_policy import evaluate_tool_flow_policy

        shadow_state = copy.deepcopy(state or {})
        result = evaluate_tool_flow_policy(
            user_text,
            business_type=business_type,
            schema_overrides=schema_overrides,
            custom_tabs=custom_tabs,
            provider_status=provider_status or {},
            history=list(history or []),
            state=shadow_state,
            language=language,
        )
        bespoke = _summarize_bespoke(bespoke_turn_policy)
        unified = _summarize_unified(result)
        agree = (
            bespoke.get("pending_slot") == unified.get("pending_slot")
            and bool(bespoke.get("completed")) == bool(unified.get("completed"))
        )
        logger.info(
            "booking_shadow call=%s agree=%s bespoke=%s unified=%s",
            call_id, agree, bespoke, unified,
        )
    except Exception as exc:  # pragma: no cover - shadow must never break a call
        logger.debug("booking_shadow failed call=%s err=%s", call_id, exc)
