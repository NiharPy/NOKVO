"""Real-estate inbound agent FSM (Phase 1 — real-estate only).

Three explicit modes:

  * ``query``        – default. Agent answers project / company questions
                       from the KB or the admin's single prompt.
  * ``site_visit``   – booking flow active. Agent drives slot-filling
                       (name, phone, project, date, time) to create a
                       site-visit ticket. KB side-questions pause the
                       flow and resume on the next turn with a "Coming
                       back to your booking — " prefix; that pause is
                       still ``site_visit`` from the operator's POV but
                       transiently routes through ``query`` to answer
                       the digression.
  * ``inbound_lead`` – terminal classification at call end. Caller showed
                       interest (asked about projects, mentioned
                       budget/BHK/location) but did NOT confirm a site
                       visit before hanging up. The auto-created lead
                       lands in the Leads page's **Uncategorized** tab.

The mode is **derived** from existing state (``tool_flow.*`` +
``call_surface`` + the safety-net's interest-signal evaluation) rather
than maintained as a separate write-through field. That keeps the
single source of truth in ``tool_flow`` and avoids the two-fields-out-of-
sync class of bugs.

Only the FINAL mode at call end is persisted (``agent_mode_final``) so
the lead row + telemetry can record which terminal state the call
ended in. Mid-call transitions are computed on demand via
:func:`current_mode`.

Scope: real-estate organisations only. Other industries continue to use
the implicit-mode behaviour (tool_flow.active = booking, otherwise
query). When the FSM is broadened to other industries the
:func:`enabled_for_business_type` check is the single point to change.
"""
from __future__ import annotations

from typing import Any


AGENT_MODE_QUERY = "query"
AGENT_MODE_OBJECTION_HANDLING = "objection_handling"
AGENT_MODE_SITE_VISIT = "site_visit"
# ACTIVE enquiry-lead capture (the ``leads_create`` flow is running). Distinct
# from AGENT_MODE_INBOUND_LEAD, which is the TERMINAL call-end classification
# for a caller who showed interest but never entered a capture flow.
AGENT_MODE_LEAD_CAPTURE = "lead_capture"
AGENT_MODE_INBOUND_LEAD = "inbound_lead"

ALL_MODES = (
    AGENT_MODE_QUERY,
    AGENT_MODE_OBJECTION_HANDLING,
    AGENT_MODE_SITE_VISIT,
    AGENT_MODE_LEAD_CAPTURE,
    AGENT_MODE_INBOUND_LEAD,
)


def enabled_for_business_type(business_type: str | None) -> bool:
    """Phase 1 gate. Real-estate only."""
    return str(business_type or "").strip().lower() == "real_estate"


def _live_objection_codes(memory: Any) -> list[str]:
    """All objection codes raised on the most recent signal-bearing turn.

    Returns ``[]`` when no objection is live (i.e. the lead's last
    signal wasn't an objection, or objections only carry prior-call
    entries). Multiple codes can be live at once ("price + competitor").
    """
    if memory is None:
        return []
    objections = getattr(memory, "objections", None)
    if not objections:
        return []
    try:
        from app.services.conversation_strategy import latest_turn as _latest_turn_helper
    except Exception:
        return []
    latest = _latest_turn_helper(memory)
    if latest < 0:
        return []
    codes: list[str] = []
    for entry in objections:
        if not isinstance(entry, dict):
            continue
        if entry.get("from_prior_call"):
            continue
        # Skip rebuttal-acknowledged objections. See
        # :meth:`ConversationalMemory._resolve_prior_objections`.
        if entry.get("resolved_at_turn") is not None:
            continue
        turn = entry.get("turn")
        if not isinstance(turn, int) or turn != latest:
            continue
        code = str(entry.get("code") or "").strip()
        if code and code not in codes:
            codes.append(code)
    return codes


def _has_live_objection(memory: Any) -> bool:
    """True when the caller's most recent signal was an objection."""
    return bool(_live_objection_codes(memory))


def current_mode(
    state: dict[str, Any] | None,
    *,
    memory: Any = None,
) -> str:
    """Compute the current mode from session state (+ optional memory).

    A site-visit flow is "active" when ``tool_flow.active`` is set AND
    the flow targets the site-visit booking AND the flow has not been
    completed. A deferred flow (caller asked a KB side-question mid-
    booking) returns ``query`` for THIS turn so the digression block in
    the RAG composer doesn't drown out the KB answer. The next turn,
    after the regex extractor re-matches the flow, the mode flips back
    to ``site_visit`` and the prompt is prefixed with "Coming back to
    your booking — ".

    When ``memory`` is supplied and the caller's most recent signal is
    a live objection, the mode escalates to ``objection_handling`` so
    the agent drops slot-filling / chat and works the objection.
    """
    if memory is not None and _has_live_objection(memory):
        return AGENT_MODE_OBJECTION_HANDLING
    tool_flow = ((state or {}).get("tool_flow") or {})
    if not tool_flow.get("active"):
        return AGENT_MODE_QUERY
    if tool_flow.get("completed"):
        return AGENT_MODE_QUERY
    if tool_flow.get("deferred_for_kb"):
        return AGENT_MODE_QUERY
    flow_key = str(tool_flow.get("flow_key") or "")
    if flow_key == "real_estate_site_visit":
        return AGENT_MODE_SITE_VISIT
    # An active ``leads_create`` flow means the caller is leaving their details
    # for follow-up. Without this branch the mode fell through to QUERY, so the
    # agent got the "answer questions" instruction and under-collected the
    # admin-configured Lead Fields. LEAD_CAPTURE drives the slots deterministically.
    if flow_key == "leads_create":
        return AGENT_MODE_LEAD_CAPTURE
    return AGENT_MODE_QUERY


_DONT_INVENT_BLOCK = (
    "# DO NOT INVENT CALLER PREFERENCES — non-negotiable\n"
    "- The configurations, prices, and locations in PROJECT INVENTORY are PROJECT facts. They are "
    "NOT caller preferences. Never attribute them to the caller (\"your 4 BHK\", \"your ₹2 crore "
    "budget\", \"your Kompally requirement\") unless the caller said that exact thing earlier in "
    "the conversation.\n"
    "- BANNED phrasings: \"for your <BHK> within your <price> budget\", \"your interest in <project>\", "
    "\"based on what you're looking for\". If you didn't hear it from the caller's mouth, do not say "
    "they said it.\n"
    "- If you need a preference to answer, ASK for it instead of assuming. \"Are you looking at 3 "
    "or 4 BHK?\" is fine. \"For your 4 BHK\" is not."
)


def mode_block_for_prompt(
    mode: str,
    *,
    pending_slot_label: str | None = None,
    pending_slot_question: str | None = None,
    memory: Any = None,
) -> str:
    """Render a short LLM-facing block describing the current mode.

    Kept tight on purpose — the bulk of the slot detail comes from the
    booking-flow renderer in :mod:`agent_outbound_context`. This block
    is the "what mode are we in and what's the job" header that sits at
    the top of the prompt.

    When ``mode == objection_handling`` and ``memory`` is supplied, the
    block inlines the per-code playbook entry for the specific
    objection(s) currently live so the LLM has the exact handling
    instruction in the same prompt section.
    """
    def _with_dont_invent(body: str) -> str:
        return body + "\n\n" + _DONT_INVENT_BLOCK

    if mode == AGENT_MODE_OBJECTION_HANDLING:
        # Inline the playbook for the LIVE objection code(s) so the LLM
        # gets the concrete handling instruction in the same prompt block,
        # not a "see below" pointer. Cap at two codes for prompt size.
        playbook_block = ""
        try:
            from app.services.conversation_strategy import OBJECTION_PLAYBOOK as _PLAYBOOK
        except Exception:
            _PLAYBOOK = {}
        codes = _live_objection_codes(memory) if memory is not None else []
        playbook_lines: list[str] = []
        for code in codes[:2]:
            line = _PLAYBOOK.get(code)
            if line:
                playbook_lines.append(f"- {line}")
        if playbook_lines:
            playbook_block = "\n# OBJECTION PLAYBOOK — apply this specifically\n" + "\n".join(playbook_lines)
        elif codes:
            playbook_block = (
                "\n# OBJECTION PLAYBOOK\n"
                f"- Live objection code(s): {', '.join(codes)}. Acknowledge in one short phrase, "
                "address with one concrete fact from PROJECT INVENTORY, then ask one warm question to re-engage."
            )
        return _with_dont_invent(
            "# AGENT MODE — OBJECTION_HANDLING\n"
            "The caller just raised a concern. Stop pitching immediately.\n"
            "- Your ONLY goal this turn is to gracefully resolve their objection using the playbook below.\n"
            "- Do NOT list features, do NOT push for a visit yet, do NOT get defensive, do NOT repeat the pitch.\n"
            "- Acknowledge in one short phrase, address concisely with a concrete fact from PROJECT INVENTORY, "
            "then ask ONE warm open question to re-engage."
            + playbook_block
        )

    if mode == AGENT_MODE_SITE_VISIT:
        next_step = ""
        if pending_slot_label:
            next_step = (
                f"\nNext slot to fill: **{pending_slot_label}**."
            )
            if pending_slot_question:
                next_step += (
                    f' Reference question: "{pending_slot_question}". '
                    "Paraphrase in your persona — never recite this verbatim, never stack two asks."
                )
        else:
            # No pending slot named = all slots either filled or the FSM
            # is mid-confirmation. The deterministic success message will
            # fire as soon as the FSM hits state_slot=="complete" — until
            # then, the LLM must NOT pre-emptively recap "your visit is
            # scheduled" or ask "anything else before I confirm?" because
            # nothing has actually been recorded yet.
            next_step = (
                "\nAll booking slots may be filled — wait for the system to "
                "fire the confirmation. Do NOT recap the booking ('your visit "
                "is scheduled…') and NEVER say 'before I confirm?'. If the "
                "caller volunteered all details in one breath, ask the one "
                "still-missing confirmation question if any, otherwise stay "
                "silent (return an empty line) so the deterministic success "
                "message plays."
            )
        return _with_dont_invent(
            "# AGENT MODE — SITE_VISIT\n"
            "You are currently capturing a site-visit booking. Your only job this turn is to land "
            "the next missing slot — do not pivot back to general project chat. If the caller asks "
            "a side question, answer it in ONE short sentence and immediately re-anchor on the slot.\n"
            "# COMPLETION RULES — non-negotiable\n"
            "- The system writes the booking record itself. You DO NOT 'book', 'confirm', or 'create'\n"
            "  the visit — you just collect slots until the system fires its own past-tense confirmation.\n"
            "- BANNED phrases mid-flow: 'before I confirm', 'let me confirm', 'is scheduled', 'will be scheduled'.\n"
            "- BANNED tail questions mid-flow: 'anything else you'd like noted', 'anything else before I confirm'.\n"
            "- If you find yourself recapping the booking, you're going off-script — ask the next slot instead."
            + next_step
        )
    if mode == AGENT_MODE_LEAD_CAPTURE:
        next_step = ""
        if pending_slot_label:
            next_step = f"\nNext detail to capture: **{pending_slot_label}**."
            if pending_slot_question:
                next_step += (
                    f' Reference question: "{pending_slot_question}". '
                    "Paraphrase in your persona — never recite verbatim, never stack two asks."
                )
        else:
            next_step = (
                "\nAll REQUIRED details may be captured. Offer ONCE — \"Anything else "
                "you'd like our team to note?\" — then let the system record the lead. "
                "Do NOT keep interrogating for optional details the caller didn't volunteer."
            )
        return _with_dont_invent(
            "# AGENT MODE — LEAD_CAPTURE\n"
            "The caller is leaving their details so the team can follow up. Your only job this "
            "turn is to capture the next REQUIRED detail the operator configured — ask ONE field, "
            "in order, using the FIELD-COLLECTION SCRIPT's exact phrasing. If the caller asks a "
            "side question, answer it in ONE short sentence and immediately re-anchor on the field.\n"
            "# RULES — non-negotiable\n"
            "- Ask only ONE field per turn. Required fields first; optional fields are OFFERED once "
            "(\"anything else to note?\"), never interrogated one-by-one.\n"
            "- The system writes the lead record itself. Do NOT say 'saved', 'created', or 'all set' — "
            "collect details until the system fires its own confirmation."
            + next_step
        )
    if mode == AGENT_MODE_INBOUND_LEAD:
        return _with_dont_invent(
            "# AGENT MODE — INBOUND_LEAD\n"
            "Caller asked about projects but did not confirm a site visit. Wrap politely and offer to "
            "have someone follow up. Do NOT promise a booking that hasn't been confirmed."
        )
    return _with_dont_invent(
        "# AGENT MODE — QUERY\n"
        "You're in question-answering mode. Answer the caller's project / company question from the "
        "PROJECT INVENTORY (or the admin prompt for non-project facts). After each answer, finish with "
        "ONE warm follow-up question that nudges toward a site visit — never lecture, never list more "
        "than one feature per turn."
    )


def terminal_mode_for_call_end(
    state: dict[str, Any] | None,
    *,
    interest_signal: bool,
    site_visit_created: bool,
) -> str:
    """Decide the final mode label written on the call summary / lead row.

    - ``site_visit`` — a booking was successfully created (ticket).
    - ``inbound_lead`` — caller showed interest but did not book; the
      safety-net lead lands in Uncategorized.
    - ``query`` — no interest signal, no booking. No lead is created.

    Mirrors the dispatch order of :func:`maybe_create_real_estate_lead_from_call`
    so the recorded mode matches the actual record that was written.
    """
    if site_visit_created:
        return AGENT_MODE_SITE_VISIT
    if interest_signal:
        return AGENT_MODE_INBOUND_LEAD
    return AGENT_MODE_QUERY


__all__ = (
    "AGENT_MODE_QUERY",
    "AGENT_MODE_SITE_VISIT",
    "AGENT_MODE_LEAD_CAPTURE",
    "AGENT_MODE_INBOUND_LEAD",
    "ALL_MODES",
    "enabled_for_business_type",
    "current_mode",
    "mode_block_for_prompt",
    "terminal_mode_for_call_end",
)
