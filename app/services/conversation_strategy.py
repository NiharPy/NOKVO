"""Conversation strategy — turns structured memory into tactics.

Why this module exists
----------------------
:mod:`conversational_memory` answers *"what do I already know about this
caller?"* It is a deterministic state container: facts, objections,
commitments, salient notes. What it deliberately does **not** do is decide
*what the agent should do about that state*. So the inbound agent ended up
purely reactive — it filled the next slot and answered the last thing said,
with no lead prioritisation, no objection playbook, no sense of a buying-
journey arc, and no recovery when a call went sideways.

This module is that missing strategic layer. It is pure and synchronous —
no I/O, no LLM call — operating only on a :class:`ConversationalMemory`
snapshot, so it costs nothing per turn beyond a few regex scans. Its single
public output, :func:`compose_strategy_block`, is an English directive block
injected into the system prompt right after the "already known" memory block.
The reply LLM (which runs every turn anyway) is the brain that executes these
tactics; we just give it explicit marching orders derived from the state.

What it produces (gated by signal, capped in size):
  * recovery directives when the caller is frustrated or feels re-asked
    (universal — fires for any business type);
  * a per-code objection playbook (price, competitor, timing, …);
  * a lead tier + posture + the current journey stage and the goal for this
    turn;
  * permission to estimate affordability from a stated income;
  * a one-line continuity reference for a returning caller.

The sales layer (everything but recovery) is gated on real-estate / outbound
sales contexts. ``business_type`` may be ``None`` before an admin selects an
industry — the function must never raise on that and emits only the universal
recovery parts (or an empty string).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services.conversational_memory import (
    FACT_BHK,
    FACT_BUDGET,
    FACT_INCOME,
    FACT_LOCATION,
    FACT_NAME,
    FACT_PHONE,
    FACT_PURPOSE,
    FACT_TIMELINE,
    FACT_VISIT_DATE,
    FACT_VISIT_TIME,
    ConversationalMemory,
)


# Hard ceiling on the rendered block so it can't balloon the ~3KB system
# prompt and hurt time-to-first-token. ~300 tokens ≈ 1,800 characters.
_MAX_CHARS = 1800


def _is_sales(business_type: str | None, is_outbound: bool) -> bool:
    return bool(is_outbound) or str(business_type or "").strip().lower() == "real_estate"


def _current_codes(bucket: list[dict], *, key: str = "code") -> list[str]:
    """Codes from a bucket raised on THIS call (skip restored prior-call
    entries AND objections whose ``resolved_at_turn`` is set), de-duped,
    most-recent last. The resolved-skip is what lets the FSM exit
    ``objection_handling`` cleanly after a successful rebuttal."""
    out: list[str] = []
    for entry in bucket or []:
        if not isinstance(entry, dict) or entry.get("from_prior_call"):
            continue
        if entry.get("resolved_at_turn") is not None:
            continue
        code = str(entry.get(key) or "")
        if code and code not in out:
            out.append(code)
    return out


def _corpus(memory: ConversationalMemory) -> str:
    """All free text the caller produced this call (fact raws + bucket /
    salient texts), lower-cased, for cue scans like pre-approval."""
    parts: list[str] = []
    for fact in memory.facts.values():
        if fact.raw and fact.source_turn != -1:
            parts.append(str(fact.raw))
    for bucket in (memory.objections, memory.commitments):
        for entry in bucket:
            if not entry.get("from_prior_call") and entry.get("text"):
                parts.append(str(entry.get("text")))
    for note in memory.salient_notes:
        if not note.get("from_prior_call") and note.get("text"):
            parts.append(str(note.get("text")))
    return " ".join(parts).lower()


_PREAPPROVAL_RE = re.compile(
    r"\b(pre[\s-]?approv|loan (?:approv|sanction|ready)|sanction(?:ed|\s+letter)|"
    r"home loan (?:approved|ready|sorted)|funds? ready|ready to pay|cash purchase)\b",
    re.IGNORECASE,
)
_IMMEDIATE_TIMELINE_RE = re.compile(
    r"\b(immediate|right away|asap|as soon|this week|this month|abhi|turant|"
    r"ippud|ventane)\b",
    re.IGNORECASE,
)


def _has_preapproval(memory: ConversationalMemory) -> bool:
    return bool(_PREAPPROVAL_RE.search(_corpus(memory)))


def _timeline_is_immediate(memory: ConversationalMemory) -> bool:
    value = str(memory.get(FACT_TIMELINE) or "")
    return bool(value and _IMMEDIATE_TIMELINE_RE.search(value))


def _emotional_distress(memory: ConversationalMemory) -> bool:
    for note in memory.salient_notes:
        if not note.get("from_prior_call") and str(note.get("code") or "") == "emotional_flag":
            return True
    return False


_SIGNAL_FACTS = (
    FACT_BUDGET, FACT_BHK, FACT_LOCATION, FACT_INCOME, FACT_PURPOSE,
    FACT_TIMELINE, FACT_VISIT_DATE,
)


def _has_sales_signal(memory: ConversationalMemory) -> bool:
    """True when there's something for the sales layer to react to. A cold-open
    turn with nothing known yields no strategy block (keeps TTFT cheap)."""
    if _current_codes(memory.objections) or _current_codes(memory.commitments):
        return True
    if memory.prior_stage:
        return True
    return any(memory.has(k) for k in _SIGNAL_FACTS)


# ── Objection playbook ───────────────────────────────────────────────────────
#
# One concrete handling instruction per objection code emitted by
# ``_OBJECTION_PATTERNS`` in conversational_memory. Real-estate / sales framing.
# These are the "what a trainer would tell a rep" lines the agent never had.

OBJECTION_PLAYBOOK: dict[str, str] = {
    "price_concern": (
        "PRICE objection: do NOT just restate the number or get defensive. Acknowledge it, then "
        "reframe on value and affordability — payment plans, EMI/month, what's included, "
        "appreciation in the area. Cite the SPECIFIC project's price, configuration, and "
        "possession date from the PROJECT INVENTORY section above — concrete facts, not generic "
        "lines. Ask what range they had in mind so you can match a unit, and offer a site visit "
        "to anchor value against the price."
    ),
    "competitor": (
        "COMPETITOR / already-looking: don't bad-mouth anyone. Ask what they liked about the "
        "other option, then differentiate on a concrete edge from the PROJECT INVENTORY above — "
        "name the actual location, possession date, amenities, or RERA/legal clarity of the "
        "specific project rather than speaking in generalities. Offer a no-pressure comparison visit."
    ),
    "call_later": (
        "TIMING brush-off ('busy', 'call later'): respect it. Don't pitch. Confirm one good time "
        "to reconnect and one channel (call / WhatsApp), capture it, and close warmly in a sentence."
    ),
    "long_horizon": (
        "LONG-HORIZON buyer (months away): switch from closing to nurturing. Don't push a visit. "
        "Offer to send options / price trends and stay in touch; capture preferred channel and a "
        "rough timeline so a follow-up lands at the right moment."
    ),
    "do_not_call": (
        "DO-NOT-CALL / not interested: stop selling immediately. Acknowledge, confirm you'll note "
        "the preference, and end the call politely. Do not ask further qualification questions."
    ),
}


# ── Lead scoring ─────────────────────────────────────────────────────────────


@dataclass
class LeadAssessment:
    """Qualitative lead read derived from current-call signals."""

    tier: str  # hot | warm | cold | at_risk
    reasons: list[str] = field(default_factory=list)
    posture: str = ""


_POSTURE = {
    "hot": (
        "HOT lead — high intent. Move to close: propose a specific site-visit slot or the booking "
        "step now. Be confident and concrete; don't re-open discovery you've already covered."
    ),
    "warm": (
        "WARM lead — real interest, not yet committed. Advance one step: fill the most important "
        "missing qualifier (budget, BHK, or location), then offer a clear next action."
    ),
    "cold": (
        "COLD / exploratory lead — low urgency. Keep it light and low-pressure. Be helpful, "
        "capture how to follow up, and don't push for a visit."
    ),
    "at_risk": (
        "AT-RISK — the call is going badly. Repair the relationship before anything else: "
        "apologise once, slow down, and don't push the agenda this turn."
    ),
}


def score_lead(
    memory: ConversationalMemory,
    *,
    business_type: str | None = None,
) -> LeadAssessment:
    """Deterministic lead read from signals already in ``memory``. No I/O."""
    objections = _current_codes(memory.objections)
    commitments = _current_codes(memory.commitments)
    has_triad = sum(memory.has(k) for k in (FACT_BUDGET, FACT_BHK, FACT_LOCATION))
    visit = memory.has(FACT_VISIT_DATE)
    preapproved = _has_preapproval(memory)
    immediate = _timeline_is_immediate(memory)

    # 1. Explicit opt-out wins outright.
    if "do_not_call" in objections:
        return LeadAssessment("cold", ["caller asked not to be pursued"], _POSTURE["cold"])

    # 2. Relationship in danger.
    if "repetition_complaint" in objections or _emotional_distress(memory):
        reasons = []
        if "repetition_complaint" in objections:
            reasons.append("caller feels re-asked")
        if _emotional_distress(memory):
            reasons.append("caller is frustrated")
        return LeadAssessment("at_risk", reasons, _POSTURE["at_risk"])

    # 3. Hot signals.
    hot_reasons: list[str] = []
    if "ready_to_book" in commitments:
        hot_reasons.append("ready to book")
    if visit:
        hot_reasons.append("visit date discussed")
    if preapproved:
        hot_reasons.append("financing ready / pre-approved")
    if immediate and has_triad >= 2:
        hot_reasons.append("immediate timeline with clear requirements")
    if hot_reasons:
        return LeadAssessment("hot", hot_reasons, _POSTURE["hot"])

    # 4. Long-horizon explicitly cools an otherwise-warm lead.
    if "long_horizon" in objections and has_triad < 2:
        return LeadAssessment("cold", ["buying months out"], _POSTURE["cold"])

    # 5. Warm: any qualification or soft commitment.
    warm_reasons: list[str] = []
    if has_triad:
        warm_reasons.append("shared requirements")
    if memory.has(FACT_INCOME):
        warm_reasons.append("shared income")
    if any(c in commitments for c in ("interested", "info_requested", "callback_requested")):
        warm_reasons.append("expressed interest")
    if warm_reasons:
        return LeadAssessment("warm", warm_reasons, _POSTURE["warm"])

    # 6. Nothing actionable yet.
    return LeadAssessment("cold", ["not yet qualified"], _POSTURE["cold"])


# ── Buying-journey stage ─────────────────────────────────────────────────────

_STAGE_GOAL = {
    "booked": "Confirm the booking/visit details and what happens next.",
    "closing": "Lock the next concrete step — a specific visit slot or the booking.",
    "objection_handling": "Resolve the live concern before advancing.",
    "qualification": "Fill the most important missing qualifier, then advance.",
    "discovery": "Understand what they're looking for — purpose, BHK, location, budget.",
}


def journey_stage(
    memory: ConversationalMemory,
    *,
    business_type: str | None = None,
) -> str:
    """Where the caller is in the buying journey, from facts + commitments +
    objections. Independent of :func:`score_lead`. Public — also used by
    cross-call persistence to remember where a caller left off."""
    objections = _current_codes(memory.objections)
    commitments = _current_codes(memory.commitments)
    has_triad = sum(memory.has(k) for k in (FACT_BUDGET, FACT_BHK, FACT_LOCATION))

    if memory.has(FACT_VISIT_DATE) and "ready_to_book" in commitments:
        return "booked"
    if "ready_to_book" in commitments or memory.has(FACT_VISIT_DATE):
        return "closing"
    if any(o in objections for o in ("price_concern", "competitor", "long_horizon")):
        return "objection_handling"
    if has_triad or memory.has(FACT_INCOME) or memory.has(FACT_PURPOSE):
        return "qualification"
    return "discovery"


# ── Mid-call escalation ──────────────────────────────────────────────────────
#
# The current-turn utterance is merged into ``memory`` before this module runs,
# so a cold→hot pivot already reaches ``score_lead`` this turn. What was missing
# was *prominence*: when a hot driver first appears on the current turn, lead with
# an explicit "close now" directive instead of relying on the lead tier quietly
# ticking up. "Current turn" is derived from the memory itself (no turn index is
# threaded in), keeping this module pure.


def latest_turn(memory: ConversationalMemory) -> int:
    """Public alias for :func:`_latest_turn` — exported so the inbound /
    outbound FSM modules can reuse the same "what is the most recent
    turn that captured any signal" computation instead of duplicating
    it. The leading-underscore version stays for backwards-compat with
    any in-repo caller that already imports the private name.
    """
    return _latest_turn(memory)


def _latest_turn(memory: ConversationalMemory) -> int:
    """Highest turn index any current-call signal was captured on. ``-1`` when
    nothing has been captured yet (cold open). Bootstrap facts (source_turn
    ``-1``) and restored prior-call bucket entries are ignored."""
    turns: list[int] = []
    for fact in memory.facts.values():
        if isinstance(fact.source_turn, int) and fact.source_turn >= 0:
            turns.append(fact.source_turn)
    for bucket in (memory.objections, memory.commitments):
        for entry in bucket:
            if entry.get("from_prior_call"):
                continue
            t = entry.get("turn")
            if isinstance(t, int):
                turns.append(t)
    return max(turns) if turns else -1


def _text_at_turn(memory: ConversationalMemory, turn: int) -> str:
    """All free text the caller produced on a specific turn, lower-cased — used
    to spot a fresh pre-approval / immediacy cue introduced this turn."""
    parts: list[str] = []
    for fact in memory.facts.values():
        if fact.source_turn == turn and fact.raw:
            parts.append(str(fact.raw))
    for bucket in (memory.objections, memory.commitments):
        for entry in bucket:
            if not entry.get("from_prior_call") and entry.get("turn") == turn and entry.get("text"):
                parts.append(str(entry.get("text")))
    for note in memory.salient_notes:
        if not note.get("from_prior_call") and note.get("turn") == turn and note.get("text"):
            parts.append(str(note.get("text")))
    return " ".join(parts).lower()


def _fresh_hot_signal(memory: ConversationalMemory) -> str | None:
    """Short reason when a HOT-tier driver was first introduced on the current
    (latest) turn — i.e. the caller just escalated. ``None`` otherwise."""
    latest = _latest_turn(memory)
    if latest < 0:
        return None

    for entry in memory.commitments:
        if entry.get("from_prior_call"):
            continue
        if str(entry.get("code") or "") == "ready_to_book" and entry.get("turn") == latest:
            return "ready to book"

    visit = memory.get_fact(FACT_VISIT_DATE)
    if visit and visit.value not in (None, "", []) and visit.source_turn == latest:
        return "wants to schedule a visit"

    if _PREAPPROVAL_RE.search(_text_at_turn(memory, latest)):
        return "financing ready / pre-approved"

    timeline = memory.get_fact(FACT_TIMELINE)
    if timeline and timeline.source_turn == latest and _timeline_is_immediate(memory):
        return "immediate timeline"
    return None


def _escalation_section(memory: ConversationalMemory) -> str | None:
    reason = _fresh_hot_signal(memory)
    if not reason:
        return None
    return (
        f"# ESCALATION — the caller just signalled strong intent ({reason})\n"
        "- Pivot to closing NOW: propose a specific site-visit slot or the booking step this "
        "turn. Don't re-open discovery you've already covered, and don't bury this under smaller "
        "questions — match their momentum."
    )


# ── Cold-open discovery agenda ───────────────────────────────────────────────
#
# The sales blocks below all gate on ``_has_sales_signal(memory)`` so a turn-1
# cold open emits nothing — which is why the inbound real-estate agent kept
# defaulting to passive "How can I help you?" framing. The discovery agenda
# is the explicit "you are the host, not the receptionist" steering wheel for
# that first turn: name the project, ask one concrete discovery question, and
# don't open with help-desk language.

# Project-specific suggested first questions. The pipeline can pass any of
# these via ``cold_open_question_override`` if the org wants a different
# opener; the default is purpose + configuration in one breath because that
# splits 90% of inbound real-estate calls into useful branches.
_DEFAULT_DISCOVERY_QUESTION = (
    "What kind of property are you looking for today — investment or for living in?"
)


def _discovery_agenda_section(
    *,
    focus_project: str | None,
    company_name: str | None,
    discovery_question_override: str | None = None,
) -> str:
    """Cold-open marching orders. Fires only when there's no sales signal yet
    AND we're in a sales context. Tells the LLM to take control on turn 1.
    """
    project_label = (focus_project or "").strip() or (company_name or "").strip() or "our project"
    expert_line = (
        f"a property expert for {project_label}"
        if focus_project
        else f"a property expert at {project_label}"
        if company_name
        else "a property expert here"
    )
    question = (discovery_question_override or "").strip() or _DEFAULT_DISCOVERY_QUESTION
    return (
        "# COLD OPEN — YOU LEAD THIS TURN\n"
        "This is the start of the call. You must take control of the conversation — the caller "
        "expects you to host them, not the other way around.\n"
        f"- Greet briefly + warmly. Say you are {expert_line}.\n"
        f"- Immediately ask ONE open-ended discovery question: \"{question}\"\n"
        "- BANNED openers: 'How can I help you?', 'How may I assist you?', 'What can I do for you?'. "
        "These hand the floor back; you need to keep it.\n"
        "- BANNED on this turn: pitching features, listing amenities, naming prices. Discovery first."
    )


# ── Upsell / cross-sell ──────────────────────────────────────────────────────
#
# Rules fire when the memory holds enough signal to justify the pitch. They
# are pure deterministic checks — no LLM call, no DB access — and gated to
# fire at most once per turn to avoid stacking three competing directives.


# Budget headroom heuristic. Indian income strings reaching the canonical
# parser come through as raw integers (₹2L → 200000) when normalised; we
# stay generous with the threshold and let the LLM make the final judgement.
_INCOME_HEADROOM_MONTHLY_INR = 200_000  # ₹2L / month
_BUDGET_LOW_TIER_INR = 12_000_000        # ₹1.2 Cr — anything under this from a high-income lead is upsell candidate

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def _coerce_inr_amount(value: object) -> float | None:
    """Pull the largest number out of a raw INR string and scale crude
    "lakhs / crore" tokens. Returns ``None`` when the input is unparseable.
    Approximate by design — used only to bucket leads into upsell tiers."""
    if value in (None, ""):
        return None
    text = str(value).lower()
    match = _NUMBER_RE.search(text.replace(",", ""))
    if not match:
        return None
    try:
        n = float(match.group(0))
    except ValueError:
        return None
    if "cr" in text or "crore" in text:
        n *= 10_000_000
    elif "lakh" in text or "lac" in text or " l" in text or text.strip().endswith("l"):
        n *= 100_000
    elif "k" in text and n < 1000:
        n *= 1_000
    return n


def _upsell_section(
    memory: ConversationalMemory,
    *,
    sold_out_locations: list[str] | None = None,
    alternative_pitch: str | None = None,
) -> str | None:
    """Two rules. At most one fires per turn (whichever matches first), so the
    strategy block never tries to upsell AND cross-sell in the same breath.

    Rule 1 — budget headroom: caller stated an income above the headroom
    threshold but a budget below the upsell tier. Ask if they'd be open to
    seeing a premium unit before committing to a visit.

    Rule 2 — location cross-sell: caller's stated location is in
    ``sold_out_locations`` and the operator provided an ``alternative_pitch``.
    Don't reject — pivot to the alternative with launch-pricing framing.
    """
    # Rule 2 has higher priority because losing the lead to a sold-out
    # project is the more expensive failure mode.
    if alternative_pitch and sold_out_locations:
        location_value = str(memory.get(FACT_LOCATION) or "").strip().lower()
        if location_value:
            for entry in sold_out_locations:
                if not entry:
                    continue
                if str(entry).strip().lower() in location_value or location_value in str(entry).strip().lower():
                    return (
                        "# UPSELL / CROSS-SELL — location cross-sell\n"
                        f"- The caller's preferred location is sold out. Do NOT reject them. Pivot to "
                        f"the alternative: {alternative_pitch.strip()}\n"
                        "- Frame the alternative as a positive ('only X mins away', 'launch pricing'); "
                        "don't apologise for the substitution."
                    )

    if memory.has(FACT_INCOME):
        income_raw = memory.get(FACT_INCOME)
        budget_raw = memory.get(FACT_BUDGET) if memory.has(FACT_BUDGET) else None
        income = _coerce_inr_amount(income_raw)
        budget = _coerce_inr_amount(budget_raw)
        if income is not None and income >= _INCOME_HEADROOM_MONTHLY_INR:
            # Budget is missing → still upsell-eligible (offer the higher tier
            # before they anchor low). Budget present but low → explicit upsell.
            if budget is None or budget <= _BUDGET_LOW_TIER_INR:
                income_label = str(income_raw).strip() if income_raw else "their income"
                budget_phrase = (
                    f"a budget around {str(budget_raw).strip()}"
                    if budget_raw
                    else "no budget yet"
                )
                return (
                    "# UPSELL — budget headroom\n"
                    f"- The caller's income (~{income_label}/month) qualifies them for a premium "
                    "tier, but they've signalled "
                    f"{budget_phrase}. Before booking the visit, proactively ask if they'd be open to "
                    "seeing a higher-tier unit (e.g. a larger BHK or a premium configuration) for "
                    "better ROI. Frame it as a one-line offer, not a hard sell."
                )
    return None


# ── Next Best Action ─────────────────────────────────────────────────────────
#
# The bare "fill the most important missing qualifier" line in the warm-lead
# posture gives the LLM too much latitude. Compute the missing fact in
# priority order and dictate the exact question.


# Ordered list — each tuple is ``(fact_key, label, question)``. The first
# missing entry wins. ``visit_date`` and ``visit_time`` come last because the
# slot-fill FSM owns those once we reach the closing stage.
_NEXT_BEST_ACTION_PRIORITY: list[tuple[str, str, str]] = [
    (FACT_NAME, "name", "Could you tell me your name, please?"),
    (FACT_PHONE, "phone number", "What phone number should our team use to reach you?"),
    (FACT_PURPOSE, "purpose (self-use or investment)", "Are you looking at this for self-use or as an investment?"),
    (FACT_BHK, "BHK / configuration", "How many bedrooms are you looking for — 2 BHK, 3 BHK, or larger?"),
    (FACT_LOCATION, "preferred location / area", "Which area or part of town are you focused on?"),
    (FACT_BUDGET, "budget range", "What's the budget range you're working with?"),
    (FACT_TIMELINE, "buying timeline", "When are you looking to buy or move in?"),
    (FACT_VISIT_DATE, "visit date", "Would a weekday or weekend work better for the site visit?"),
    (FACT_VISIT_TIME, "visit time", "What time of day suits you best for the visit?"),
]


def _next_best_action(memory: ConversationalMemory) -> tuple[str, str] | None:
    """Return ``(label, question)`` for the highest-priority missing fact.

    ``None`` when every priority slot is already captured — at which point
    the closing flow takes over.
    """
    for fact_key, label, question in _NEXT_BEST_ACTION_PRIORITY:
        if not memory.has(fact_key):
            return label, question
    return None


# ── Block assembly ───────────────────────────────────────────────────────────


def _recovery_section(memory: ConversationalMemory) -> str | None:
    """Universal — fires for any business type when the caller signals the
    call is going wrong. Addresses 'you keep asking me the same thing'."""
    objections = _current_codes(memory.objections)
    repetition = "repetition_complaint" in objections
    distressed = _emotional_distress(memory)
    if not (repetition or distressed):
        return None
    bits = ["# RECOVER THE CALL — do this before anything else"]
    if repetition:
        bits.append(
            "- The caller feels you're repeating yourself or re-asking what they already told you. "
            "Apologise ONCE, do NOT ask again for anything listed in CONVERSATIONAL MEMORY, briefly "
            "summarise what you already know, then ask only the single most important missing item — "
            "or just move to the next step."
        )
    if distressed:
        bits.append(
            "- The caller is upset. Acknowledge the feeling in one short phrase, slow down, and "
            "don't push your agenda this turn."
        )
    return "\n".join(bits)


def _objection_section(
    memory: ConversationalMemory,
    *,
    focus_project: str | None = None,
) -> str | None:
    codes = [c for c in _current_codes(memory.objections) if c in OBJECTION_PLAYBOOK]
    if not codes:
        return None
    # Most-recent objections first, cap at two so the block stays tight.
    shown = list(reversed(codes))[:2]
    lines = ["# HANDLE THE ACTIVE OBJECTION(S)"]
    for code in shown:
        lines.append("- " + OBJECTION_PLAYBOOK[code])
    # When the caller has named a specific project and the live objection is
    # about price or the competition, point the agent at THAT project's facts
    # so it defends the right unit instead of generalising.
    if focus_project and any(c in ("price_concern", "competitor") for c in shown):
        lines.append(
            f"- The caller is focused on: {focus_project}. Defend THIS project specifically "
            "with its concrete price / configuration / possession; don't generalise."
        )
    return "\n".join(lines)


def _lead_section(
    lead: LeadAssessment,
    stage: str,
    memory: ConversationalMemory | None = None,
    *,
    capture_flow_active: bool = False,
) -> str:
    reason = "; ".join(lead.reasons) if lead.reasons else "—"
    lines = [
        "# LEAD STRATEGY",
        f"- Lead read: {lead.tier.upper()} ({reason}).",
        f"- {lead.posture}",
        f"- Journey stage: {stage}. Goal this turn: {_STAGE_GOAL.get(stage, '')}",
    ]
    # Next Best Action — only meaningful for leads we're still qualifying.
    # The closing / booked / at-risk postures already give the LLM a specific
    # next move (close, confirm, recover) so a redundant "ask name" directive
    # would compete with the posture itself.
    #
    # Critically, SUPPRESS the hardcoded "ask exactly X" directive when a
    # record-capture flow (site visit / lead) is active: that flow's
    # FIELD-COLLECTION SCRIPT is the authoritative source of what to ask, using
    # the operator's CONFIGURED fields. Letting this hardcoded priority list
    # ("ask budget", "ask BHK") fire on top of it is the root cause of the agent
    # ignoring the configured schema.
    if (
        not capture_flow_active
        and memory is not None
        and lead.tier in {"hot", "warm"}
        and stage in {"discovery", "qualification"}
    ):
        nba = _next_best_action(memory)
        if nba is not None:
            _, question = nba
            lines.append(f"- Next Best Action: ask exactly — \"{question}\"")
    return "\n".join(lines)


def _affordability_section(memory: ConversationalMemory) -> str | None:
    """Issue #5: synthesise across turns. If income is known but budget isn't,
    let the LLM give a ballpark of what they can afford — as an estimate,
    deferring exact eligibility to the team (keeps the no-hallucination rule)."""
    if not memory.has(FACT_INCOME) or memory.has(FACT_BUDGET):
        return None
    income = memory.get(FACT_INCOME)
    return (
        "# AFFORDABILITY\n"
        f"- The caller's monthly income is ~{income}. If they ask what they can afford (or you "
        "need a budget to suggest units), reason it out loud as a ROUGH ballpark — a common rule of "
        "thumb is an EMI around 40% of monthly income, and total loan eligibility roughly 5-6× "
        "annual income. Present it as an estimate, suggest a matching range, and offer to connect "
        "them with a loan specialist for exact eligibility. Never state a precise sanction figure as fact."
    )


def _continuity_section(memory: ConversationalMemory) -> str | None:
    """Issue #7: a returning caller. The narrative continuity note already
    renders inside the memory block; here we give one tactical instruction."""
    if not memory.prior_stage:
        return None
    return (
        "# RETURNING CALLER\n"
        f"- This caller spoke to us before and had reached the {memory.prior_stage.replace('_', ' ')} "
        "stage. Reference that continuity naturally and pick up from there rather than restarting "
        "discovery; confirm rather than assume any prior detail before acting on it."
    )


def compose_strategy_block(
    memory: ConversationalMemory,
    *,
    business_type: str | None = None,
    is_outbound: bool = False,
    language: str | None = None,
    focus_project: str | None = None,
    company_name: str | None = None,
    discovery_question_override: str | None = None,
    sold_out_locations: list[str] | None = None,
    alternative_pitch: str | None = None,
    capture_flow_active: bool = False,
) -> str:
    """Render the strategy directive block for this turn, or ``""`` when there
    is no actionable signal. Safe for ``business_type=None`` (emits only the
    universal recovery part, if any). ``focus_project`` is a one-line summary of
    the specific project the caller named (matched upstream from FACT_PROPERTY);
    when present it sharpens price/competitor objection handling.

    Cold-open handling: when ``_is_sales`` is True but ``_has_sales_signal`` is
    False (the inbound real-estate cold-open problem), a discovery-agenda block
    is emitted so the LLM takes control of turn 1 instead of falling back to
    "How can I help you?" framing.

    Upsell / cross-sell hooks: ``sold_out_locations`` + ``alternative_pitch``
    let the caller pass project inventory state to drive the cross-sell rule;
    the budget-headroom rule is purely memory-driven.

    Output is capped at :data:`_MAX_CHARS`."""
    if memory is None:
        return ""

    # Priority order: a call in trouble is salvaged before we chase the sale.
    sections: list[str] = []
    recovery = _recovery_section(memory)
    if recovery:
        sections.append(recovery)

    if _is_sales(business_type, is_outbound):
        # Cold-open agenda: real-estate / outbound calls cannot afford a
        # passive turn 1. When there's no sales signal yet AND we're not in
        # recovery mode, lead with the discovery-agenda directive so the
        # LLM names the project and asks one specific question.
        if not _has_sales_signal(memory) and not recovery:
            sections.append(
                _discovery_agenda_section(
                    focus_project=focus_project,
                    company_name=company_name,
                    discovery_question_override=discovery_question_override,
                )
            )
        if _has_sales_signal(memory):
            # A fresh escalation leads the sales guidance so the agent pivots to
            # closing on a dramatic intent signal — but a call in trouble (recovery)
            # takes precedence: don't push a close on a frustrated caller.
            if not recovery:
                escalation = _escalation_section(memory)
                if escalation:
                    sections.append(escalation)
            objection = _objection_section(memory, focus_project=focus_project)
            if objection:
                sections.append(objection)
            lead = score_lead(memory, business_type=business_type)
            stage = journey_stage(memory, business_type=business_type)
            sections.append(_lead_section(lead, stage, memory, capture_flow_active=capture_flow_active))
            # Upsell / cross-sell rules — at most one fires. Suppressed on
            # recovery and at-risk leads (don't pile a pitch on someone we're
            # trying to calm down).
            if not recovery and lead.tier != "at_risk":
                upsell = _upsell_section(
                    memory,
                    sold_out_locations=sold_out_locations,
                    alternative_pitch=alternative_pitch,
                )
                if upsell:
                    sections.append(upsell)
            affordability = _affordability_section(memory)
            if affordability:
                sections.append(affordability)
            continuity = _continuity_section(memory)
            if continuity:
                sections.append(continuity)

    if not sections:
        return ""

    header = (
        "# CONVERSATION STRATEGY — how to play this turn\n"
        "Use this together with what's already known above. It is tactical guidance, not a script; "
        "it overrides lower-priority slot-filling when they conflict.\n"
    )
    out = header
    for section in sections:
        candidate = out + "\n" + section + "\n"
        if len(candidate) > _MAX_CHARS:
            break
        out = candidate
    # Header-only means no section fit — treat as nothing to say.
    return out.rstrip() if out.strip() != header.strip() else ""


__all__ = [
    "LeadAssessment",
    "OBJECTION_PLAYBOOK",
    "compose_strategy_block",
    "journey_stage",
    "score_lead",
]
