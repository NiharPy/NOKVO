"""Real-estate OUTBOUND agent FSM (Phase 1 — real-estate only).

Outbound mirrors the inbound FSM but with two crucial differences:

  * There is **no** ``query`` mode. An outbound call is the agent's
    initiative — the default state is ``sale``: a proactive seller
    driven by the campaign's ``agent_prompt`` (the source of truth for
    knowledge and persona — nothing else).
  * The lead-capture mode is ``outbound_lead`` (not ``inbound_lead``).
    Captured leads use the org's full Lead Fields schema and land under
    the originating campaign's tab in the Leads page (NOT Uncategorized).

Modes:

  * ``sale``         — default. Agent pitches based on the campaign
                       agent_prompt + pitch_summary, drives toward at
                       least one campaign objective.
  * ``site_visit``   — booking flow active (only enabled when the
                       campaign carries the ``site_visit`` objective).
                       Slot fields come from the org's Site Visit
                       Fields, same as inbound.
  * ``outbound_lead`` — lead-capture flow active (only enabled when
                        the campaign carries the ``lead`` objective).
                        Slot fields come from the org's Lead Fields.

Campaign objectives are a structured list of codes (see
``ALLOWED_OBJECTIVES``) selected by the operator from the campaign
form. They gate which side flows can start during the call.

The FSM is **derived** from existing state (``tool_flow.*`` + the
``OutboundCampaignContext.objectives`` codes). No new write-through
field; only the final mode at call end is persisted on the lead row
for telemetry.
"""
from __future__ import annotations

from typing import Any


AGENT_MODE_SALE = "sale"
AGENT_MODE_OBJECTION_HANDLING = "objection_handling"
AGENT_MODE_SITE_VISIT = "site_visit"
AGENT_MODE_OUTBOUND_LEAD = "outbound_lead"
# Caller asked for the brochure on WhatsApp (also reachable on follow-up calls,
# which run on this outbound FSM). Confirm number → system sends pre-set template.
AGENT_MODE_WHATSAPP = "whatsapp"

ALL_MODES = (AGENT_MODE_SALE, AGENT_MODE_OBJECTION_HANDLING, AGENT_MODE_SITE_VISIT, AGENT_MODE_OUTBOUND_LEAD, AGENT_MODE_WHATSAPP)


# Campaign-objective codes. Adding a code is a backwards-compatible
# change as long as both ``OBJECTIVE_LABELS`` and the FSM transition
# table are kept in sync.
OBJECTIVE_SITE_VISIT = "site_visit"
OBJECTIVE_LEAD = "lead"
ALLOWED_OBJECTIVES = (OBJECTIVE_SITE_VISIT, OBJECTIVE_LEAD)

OBJECTIVE_LABELS = {
    OBJECTIVE_SITE_VISIT: "Book a site visit at one of the company's projects",
    OBJECTIVE_LEAD: "Capture a lead with contact details + budget / configuration interest",
}


def enabled_for_business_type(business_type: str | None) -> bool:
    """Phase 1 gate. Outbound FSM is real-estate only."""
    return str(business_type or "").strip().lower() == "real_estate"


def normalize_objectives(raw: Any) -> list[str]:
    """Coerce whatever the operator (or a legacy campaign row) passed
    into the canonical list of objective codes.

    Accepts:
      * a list of strings (post-migration shape from the dropdown)
      * a single string ("site_visit") or comma/newline-separated string
      * legacy free-text lines — silently dropped (unknown codes are
        ignored; we don't reject the whole campaign because that would
        brick existing rows)

    Returns the de-duplicated subset of ``ALLOWED_OBJECTIVES`` actually
    present in the input.
    """
    if raw in (None, ""):
        return []
    items: list[str] = []
    if isinstance(raw, str):
        # Allow comma / newline / pipe separated input from the form.
        for chunk in raw.replace("|", "\n").replace(",", "\n").split("\n"):
            cleaned = chunk.strip().lower()
            if cleaned:
                items.append(cleaned)
    elif isinstance(raw, (list, tuple, set)):
        for item in raw:
            cleaned = str(item or "").strip().lower()
            if cleaned:
                items.append(cleaned)
    else:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in ALLOWED_OBJECTIVES and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _live_objection_codes(memory: Any) -> list[str]:
    """All objection codes raised on the most recent signal-bearing turn.

    Returns ``[]`` when the memory has no objections, or when the most
    recent signal on the call wasn't an objection (i.e. the lead has
    already moved on). Multiple codes can be live in the same turn
    (e.g. ``price_concern`` + ``competitor`` in one breath).
    """
    if memory is None:
        return []
    objections = getattr(memory, "objections", None)
    if not objections:
        return []
    # Local import — ``conversation_strategy`` imports from us via the
    # caller chain, so top-level would be a cycle.
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
        # ``resolved_at_turn`` is the memory's "this rebuttal landed" stamp
        # (see :meth:`ConversationalMemory._resolve_prior_objections`). Skip
        # those so a successfully-handled objection doesn't re-trigger the
        # objection_handling mode block on subsequent turns.
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
    """True when the lead's most recent signal was an objection (i.e. the
    objection is still un-rebutted)."""
    return bool(_live_objection_codes(memory))


def current_mode(
    state: dict[str, Any] | None,
    objectives: list[str] | None,
    *,
    memory: Any = None,
) -> str:
    """Compute the current mode from session state + campaign objectives.

    Mirrors the inbound FSM's logic but maps ``leads_create`` to
    ``outbound_lead`` and defaults to ``sale`` (not ``query``). When
    the campaign doesn't carry an objective for a started flow, the
    flow can still complete (don't pull the rug mid-conversation) but
    the mode block on the next turn nudges the LLM back to selling.

    ``memory`` is the live :class:`ConversationalMemory` (or any object
    exposing ``.objections``). When provided, the most recent un-handled
    objection escalates the mode to ``objection_handling`` so the LLM
    drops the pitch and works the objection instead. ``None`` keeps the
    legacy tool_flow-only behaviour.
    """
    if memory is not None and _has_live_objection(memory):
        return AGENT_MODE_OBJECTION_HANDLING

    tool_flow = ((state or {}).get("tool_flow") or {})
    # Brochure-on-WhatsApp request → focused WHATSAPP mode, unless a booking/lead
    # slot-fill is actively running (finish that first).
    _flow_running = bool(tool_flow.get("active")) and not tool_flow.get("completed")
    if (tool_flow.get("whatsapp_intent") or {}).get("kind") == "brochure" and not _flow_running:
        return AGENT_MODE_WHATSAPP
    if not tool_flow.get("active") or tool_flow.get("completed"):
        return AGENT_MODE_SALE
    if tool_flow.get("deferred_for_kb"):
        # Mid-flow KB digression: route the digression through sale
        # mode so the LLM stays on persona, then resume on the next
        # turn when the tool_flow regex re-latches.
        return AGENT_MODE_SALE
    flow_key = str(tool_flow.get("flow_key") or "")
    if flow_key == "real_estate_site_visit":
        return AGENT_MODE_SITE_VISIT
    if flow_key == "leads_create":
        return AGENT_MODE_OUTBOUND_LEAD
    return AGENT_MODE_SALE


def is_objective_enabled(objectives: list[str] | None, code: str) -> bool:
    return code in normalize_objectives(objectives or [])


def mode_block_for_prompt(
    mode: str,
    objectives: list[str] | None,
    *,
    pending_slot_label: str | None = None,
    pending_slot_question: str | None = None,
    memory: Any = None,
) -> str:
    """Render the outbound mode block.

    Carries the same don't-invent-caller-preferences guardrail the
    inbound FSM uses — outbound is even more prone to hallucinated
    preferences because the agent talks more.

    When ``mode == objection_handling`` and ``memory`` is supplied, the
    block inlines the per-code playbook entry for the specific
    objection(s) currently live so the LLM has the exact handling
    instruction in the same prompt section (no "see below" reference
    that the model has to hunt for).
    """
    objectives_resolved = normalize_objectives(objectives or [])
    objective_hint = ""
    if objectives_resolved:
        readable = ", ".join(
            OBJECTIVE_LABELS.get(code, code) for code in objectives_resolved
        )
        objective_hint = f"\nCampaign objectives (drive toward AT LEAST ONE): {readable}."

    if mode == AGENT_MODE_SITE_VISIT:
        slot_step = ""
        if pending_slot_label:
            slot_step = f"\nNext slot to fill: **{pending_slot_label}**."
            if pending_slot_question:
                slot_step += (
                    f' Reference question: "{pending_slot_question}". '
                    "Paraphrase in your persona — never recite verbatim, never stack two asks."
                )
        return _with_dont_invent(
            "# AGENT MODE — SITE_VISIT (outbound)\n"
            "The lead is open to visiting — great. Make it feel effortless and worth their time. Your "
            "only job this turn is to land the next missing slot with a warm, assumptive touch — offer "
            "a concrete option (\"does Saturday morning or Sunday evening work better?\") rather than an "
            "open-ended \"when suits you?\". Do NOT pivot back to general project chat. If the lead asks "
            "a side question, answer it in ONE short sentence and immediately re-anchor on the slot.\n"
            "# COMPLETION RULES — non-negotiable\n"
            "- The system writes the booking record itself. You DO NOT 'book', 'confirm', or 'create'\n"
            "  the visit — collect slots until the system fires its own past-tense confirmation.\n"
            "- BANNED phrases mid-flow: 'before I confirm', 'let me confirm', 'is scheduled', 'will be scheduled'.\n"
            "- BANNED tail questions: 'anything else you'd like noted', 'anything else before I confirm'."
            + slot_step
            + objective_hint
        )

    if mode == AGENT_MODE_OUTBOUND_LEAD:
        slot_step = ""
        if pending_slot_label:
            slot_step = f"\nNext slot to fill: **{pending_slot_label}**."
            if pending_slot_question:
                slot_step += (
                    f' Reference question: "{pending_slot_question}". Paraphrase naturally.'
                )
        return _with_dont_invent(
            "# AGENT MODE — OUTBOUND_LEAD\n"
            "The lead is interested but isn't ready to commit to a visit yet — that's fine, secure the "
            "relationship. If the moment genuinely feels right you may gently offer a visit ONE more "
            "time, low-pressure; otherwise warmly capture their details (name + phone first, then "
            "whichever of the org's Lead Fields apply) so the team can follow up. You're not "
            "interrogating — you're saving their place in the queue. One ask per turn, and keep it human."
            + slot_step
            + objective_hint
        )

    if mode == AGENT_MODE_OBJECTION_HANDLING:
        # Pull the LIVE objection code(s) off memory and inline the matching
        # playbook lines so the LLM has the concrete handling instruction
        # right here — no cross-referencing required. Cap at two codes so
        # the block stays tight; most calls only have one live objection
        # at a time anyway.
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
            # We know an objection is live but it's an uncoded one — at least
            # name it so the LLM doesn't paraphrase the wrong concern.
            playbook_block = (
                "\n# OBJECTION PLAYBOOK\n"
                f"- Live objection code(s): {', '.join(codes)}. Acknowledge the concern in one short "
                "phrase, address it with one concrete fact from the campaign brief, then ask a warm "
                "open question to re-engage."
            )
        return _with_dont_invent(
            "# AGENT MODE — OBJECTION_HANDLING\n"
            "The lead just raised a concern. Stop pitching immediately and handle it like a pro — calm, "
            "warm, never defensive.\n"
            "- FIRST make them feel heard: acknowledge the concern in one short, genuine phrase "
            "(\"totally fair\", \"I hear you\") before you respond.\n"
            "- THEN reframe to value and answer with ONE concrete fact from the campaign brief — "
            "confident and concise, no over-explaining.\n"
            "- Do NOT list features, do NOT get defensive, do NOT repeat the old pitch, do NOT push for a "
            "booking in the same breath.\n"
            "- Close with ONE warm, open question that re-engages them and gently checks if the concern "
            "is resolved."
            + playbook_block
            + objective_hint
        )

    if mode == AGENT_MODE_WHATSAPP:
        return _with_dont_invent(
            "# AGENT MODE — WHATSAPP\n"
            "The lead wants the project brochure on WhatsApp. To send it you need NOTHING from them "
            "except the number they're already on — the system has it.\n"
            "- Do NOT ask for their name. Do NOT ask for their email. Do NOT ask for or read back "
            "their phone number — you ALREADY have it (the number on this call).\n"
            "- Do NOT offer to email anything. WhatsApp only.\n"
            "- Simply tell them the brochure is on its way to their WhatsApp — one warm sentence — "
            "and let the system send the pre-set brochure message. Do NOT read the link aloud or "
            "claim you sent it yourself.\n"
            "- The only thing worth asking is WHICH project (when ambiguous). Never collect personal "
            "details for a brochure, and do NOT create a lead just to send one.\n"
            "- After confirming it's on the way, steer back toward booking a site visit."
            + objective_hint
        )

    # Default — sale mode.
    return _with_dont_invent(
        "# AGENT MODE — SALE\n"
        "You are a top-tier real-estate sales consultant — warm, confident, genuinely helpful. Sound "
        "like a trusted advisor a friend would refer, NOT a telemarketer reading a script. The "
        "campaign's agent_prompt + pitch_summary are your ONLY source of truth for product / pricing / "
        "location facts — never invent.\n"
        "# HOW YOU SELL (one beat per turn — this is a conversation, not a monologue)\n"
        "1. EARN IT — open warmly, respect their time, give a one-line reason for the call.\n"
        "2. DISCOVER before you pitch — ask ONE sharp question (what they want, area, timeline, budget) "
        "and truly listen; let their answer steer what comes next.\n"
        "3. TAILOR — tie ONE benefit to what THEY just said. Speak to their motivation, never a feature "
        "dump.\n"
        "4. BUILD DESIRE honestly — a light touch of social proof or genuine scarcity ONLY if it's in "
        "the brief (e.g. 'just a couple of units left in that layout'). Never manufacture urgency.\n"
        "5. ALWAYS ADVANCE — every turn moves one step closer to a commitment.\n"
        "# YOUR GOAL, IN ORDER\n"
        "- PRIMARY: land a site visit. Ask for it assumptively and low-friction once they're warm "
        "(\"would a quick look this Saturday or Sunday suit you?\") — when they agree, the booking flow "
        "takes over.\n"
        "- SECONDARY: if they're interested but won't commit to visiting yet, don't push — offer to "
        "note their details so the team follows up (this becomes a lead).\n"
        "- If they're genuinely not interested, be gracious, leave the door open, and wrap. Never argue "
        "or repeat the pitch louder.\n"
        "# DISCIPLINE\n"
        "- One pitch beat or one question per turn. Listen to their latest reply before continuing.\n"
        "- If they ask something not grounded in the brief, say so honestly and offer to have the team "
        "get back to them — never bluff a fact. Never claim you've booked or saved anything yourself."
        + objective_hint
    )


def terminal_mode_for_call_end(
    state: dict[str, Any] | None,
    *,
    interest_signal: bool,
    site_visit_created: bool,
    objectives: list[str] | None = None,
) -> str:
    """Decide the final mode label written on the call summary / lead row.

    Parallels the inbound version. Note ``outbound_lead`` only fires
    when the campaign actually has the ``lead`` objective enabled — if
    the campaign was visit-only and the lead didn't book, no record is
    created.
    """
    objectives_resolved = normalize_objectives(objectives or [])
    if site_visit_created:
        return AGENT_MODE_SITE_VISIT
    if interest_signal and OBJECTIVE_LEAD in objectives_resolved:
        return AGENT_MODE_OUTBOUND_LEAD
    return AGENT_MODE_SALE


# ----------------------------------------------------------------------------
# Shared anti-hallucination block — same idea as the inbound FSM's, repeated
# here because the outbound prompt is composed separately.
# ----------------------------------------------------------------------------
_DONT_INVENT_BLOCK = (
    "# DO NOT INVENT LEAD PREFERENCES — non-negotiable\n"
    "- Product configurations, prices, and locations in your campaign brief are PROJECT facts. "
    "They are NOT lead preferences. Never attribute them to the lead (\"your 4 BHK\", \"your ₹2 "
    "crore budget\") unless the lead said that exact thing earlier in the call.\n"
    "- BANNED phrasings: \"for your <BHK> within your <price> budget\", \"based on what you're "
    "looking for\". Ask, don't assume."
)


def _with_dont_invent(body: str) -> str:
    return body + "\n\n" + _DONT_INVENT_BLOCK


__all__ = (
    "AGENT_MODE_SALE",
    "AGENT_MODE_OBJECTION_HANDLING",
    "AGENT_MODE_SITE_VISIT",
    "AGENT_MODE_OUTBOUND_LEAD",
    "AGENT_MODE_WHATSAPP",
    "ALL_MODES",
    "OBJECTIVE_SITE_VISIT",
    "OBJECTIVE_LEAD",
    "ALLOWED_OBJECTIVES",
    "OBJECTIVE_LABELS",
    "enabled_for_business_type",
    "normalize_objectives",
    "current_mode",
    "is_objective_enabled",
    "mode_block_for_prompt",
    "terminal_mode_for_call_end",
)
