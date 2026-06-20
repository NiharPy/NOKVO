"""Per-call outbound campaign context.

The inbound voice agent reads three things at the top of every turn:

  * Tenant runtime bundle (organization, overrides, policy cards,
    optional tenant-wide single-prompt config).
  * Conversation history + state from Redis.
  * The retrieval result (Qdrant chunks scoped to the tenant KB).

The outbound voice agent reads everything inbound reads PLUS a fourth
input: the campaign itself. A campaign carries:

  * ``doc_text`` — the campaign script / reference document, already
    indexed into Qdrant under ``campaign_id``. The retrieval layer picks
    it up automatically when the call passes ``campaign_id``.
  * ``agent_config`` — the proactive-agent configuration. Holds the
    role / tone prompt, the ordered objective questions, and any
    exit-condition signals. Composed into the LLM system prompt.

This module owns the loader + a small process-local cache (since
campaign data is read once per call but does not change during the
call). The cache is keyed by ``campaign_id`` and TTL-bounded to a few
minutes so a freshly-pushed agent_config edit becomes effective on the
next call without restarting the process.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outbound_campaign import OutboundCampaign


_CAMPAIGN_TTL_SECONDS = 120.0
_CAMPAIGN_CACHE_MAX = 256
_DEFAULT_SILENCE_TIMEOUT_SECONDS = 12.0

DEFAULT_AGENT_PROMPT = (
    "You are making a consented outbound call for the configured business. "
    "Be concise, identify the reason for the call, and guide the lead toward "
    "one clear next step. If the lead says they are not interested, asks not "
    "to be called, says this is the wrong number, or sounds busy, stop pushing "
    "and close politely."
)

DEFAULT_OBJECTIVES = [
    "Confirm this is a good time to talk.",
    "Briefly explain why you are calling based on the campaign goal.",
    "Understand whether the lead is interested and what they need.",
    "Capture the next step: appointment, callback, site visit, demo, or opt-out.",
]

DEFAULT_EXIT_CONDITIONS = [
    "Lead asks not to be called again.",
    "Lead says they are not interested.",
    "Lead says this is the wrong number.",
    "Lead is busy and asks for a callback.",
    "All campaign objectives have been covered.",
]

_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "based", "by", "for", "from",
    "has", "have", "if", "in", "is", "it", "of", "on", "or", "that", "the",
    "their", "they", "this", "to", "toward", "what", "when", "where", "why",
    "with", "you", "your",
}


# Per-objective guidance lifted from the reference agent_lab/voice-rag-agent
# implementation. The system prompt plugs the matching paragraph in so the
# agent's behaviour shifts to fit the campaign's actual purpose (qualifying
# vs demoing vs surveying read differently on the line).
OBJECTIVE_DESCRIPTIONS: dict[str, str] = {
    "lead_qualification": (
        "Find out if they're a good fit. Ask 1-2 questions to understand their "
        "current situation and whether what we offer would help them."
    ),
    "demo_booking": (
        "Book a 15-minute product demo. If they're interested, propose a time and "
        "say a human will follow up to confirm."
    ),
    "info_outreach": (
        "Make them aware of the offer and offer to send details by SMS or email "
        "for them to review at their convenience."
    ),
    "survey": (
        "Ask 2-3 short questions. Thank them, no pitch."
    ),
    "renewal": (
        "They're an existing customer. Remind them their plan is up for renewal, "
        "answer questions, offer to connect them with an account manager."
    ),
}

OUTBOUND_OBJECTIVES = tuple(OBJECTIVE_DESCRIPTIONS.keys())


@dataclass(frozen=True)
class OutboundCampaignContext:
    """Snapshot of the campaign-level state for a single outbound call.

    Constructed by :func:`load_outbound_context` and threaded through
    the pipeline so every turn can compose the campaign system prompt
    + the remaining objectives without re-fetching from Postgres.

    The campaign-branding fields (caller_name / company_name / pitch_summary /
    objective) are what the reference implementation uses to drive a real
    sales/outreach persona and the deterministic opener. ``agent_prompt`` is
    kept as an optional override for advanced cases (custom personas, exotic
    flows). Legacy campaigns without the new fields default sensibly so
    existing rows keep working.
    """

    campaign_id: str
    name: str
    goal: str
    agent_prompt: str
    objectives: list[str]
    exit_conditions: list[str]
    tone: str | None
    doc_text: str | None
    silence_timeout_seconds: float = _DEFAULT_SILENCE_TIMEOUT_SECONDS
    # Sales-persona fields. Defaults preserve backwards compat for old
    # OutboundCampaign rows that didn't carry these.
    caller_name: str = "Riya"
    company_name: str = ""
    pitch_summary: str = ""
    objective: str = "lead_qualification"

    @property
    def is_proactive(self) -> bool:
        """A campaign is proactive when either the operator gave us an
        explicit ``agent_prompt``, objectives to land, or any campaign
        branding (caller_name / company_name / pitch_summary). Pure
        ``goal``-only campaigns stay reactive (legacy behaviour)."""
        return bool(
            self.agent_prompt.strip()
            or self.objectives
            or self.company_name.strip()
            or self.pitch_summary.strip()
        )

    @property
    def objective_description(self) -> str:
        return OBJECTIVE_DESCRIPTIONS.get(self.objective, OBJECTIVE_DESCRIPTIONS["lead_qualification"])

    def remaining_objectives(self, covered: Iterable[str]) -> list[str]:
        """Objectives that have NOT yet been marked covered for this
        call. Comparison is case-insensitive on the objective text so
        small drift in storage doesn't desync the list."""
        covered_lower = {(c or "").strip().lower() for c in covered if c}
        return [obj for obj in self.objectives if obj.strip().lower() not in covered_lower]

    def has_branding(self) -> bool:
        return bool(self.caller_name.strip() or self.company_name.strip())


_cache: dict[str, tuple[float, OutboundCampaignContext]] = {}
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(campaign_id: str) -> asyncio.Lock:
    lock = _locks.get(campaign_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[campaign_id] = lock
    return lock


def _evict_stale(now: float) -> None:
    expired = [cid for cid, (expires_at, _) in _cache.items() if expires_at <= now]
    for cid in expired:
        _cache.pop(cid, None)
    while len(_cache) > _CAMPAIGN_CACHE_MAX:
        _cache.pop(next(iter(_cache)), None)


def _coerce_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if not item:
                continue
            text = str(item).strip()
            if text:
                out.append(text[:500])
        return out[:32]
    if isinstance(value, str) and value.strip():
        return [value.strip()[:500]]
    return []


def _coerce_timeout(value: Any) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        timeout = _DEFAULT_SILENCE_TIMEOUT_SECONDS
    return max(2.0, min(30.0, timeout))


def build_agent_config(
    *,
    agent_prompt: str | None = None,
    objectives: Any = None,
    exit_conditions: Any = None,
    tone: str | None = None,
    silence_timeout_seconds: Any = None,
    caller_name: str | None = None,
    company_name: str | None = None,
    pitch_summary: str | None = None,
    objective: str | None = None,
    language: str | None = None,
    # Passthrough fields that the campaign service stores on agent_config
    # but build_agent_config doesn't validate or transform. ``followup_rules``
    # is the disposition-retry policy the follow-up scheduler reads via
    # ``effective_followup_rules``; anything in ``_extra`` is preserved
    # verbatim so future agent_config additions don't need a signature bump.
    followup_rules: Any = None,
    **_extra: Any,
) -> dict[str, Any]:
    """Normalize campaign proactive-agent config.

    Empty operator inputs still produce a proactive default. That makes
    every outbound campaign drive toward a next step instead of falling
    back to a passive RAG assistant.

    The four sales-persona fields (caller_name, company_name, pitch_summary,
    objective) are the reference shape — what the deterministic opener and
    sales-prompt template use. ``agent_prompt`` remains an optional override.
    """
    prompt = str(agent_prompt or "").strip()[:8000]
    objective_list = _coerce_str_list(objectives) or list(DEFAULT_OBJECTIVES)
    exits = _coerce_str_list(exit_conditions) or list(DEFAULT_EXIT_CONDITIONS)
    tone_value = str(tone or "").strip()[:80] or "warm, direct, and respectful"
    caller = str(caller_name or "").strip()[:60] or "Riya"
    company = str(company_name or "").strip()[:120]
    pitch = str(pitch_summary or "").strip()[:300]
    obj = (str(objective or "").strip().lower() or "lead_qualification")
    if obj not in OBJECTIVE_DESCRIPTIONS:
        obj = "lead_qualification"
    # Only fall back to the platform DEFAULT_AGENT_PROMPT when the operator
    # supplied no override AND no branding either — once branding is set, we
    # synthesize the persona from the sales prompt template at compose time
    # and the override stays empty.
    if not prompt and not company and not pitch:
        prompt = DEFAULT_AGENT_PROMPT
    # Normalise the language hint (BCP-47 → two-letter code). Stored on
    # the campaign so the outbound dialer / opener / system prompt all
    # agree on which language to drive the call in. ``""`` means
    # "auto-detect" (legacy behaviour).
    lang_raw = (str(language or "").strip().lower() or "")
    if lang_raw == "unknown":
        lang_raw = ""
    if lang_raw:
        lang_short = lang_raw.split("-", 1)[0][:2]
    else:
        lang_short = ""
    cfg: dict[str, Any] = {
        "agent_prompt": prompt,
        "objectives": objective_list,
        "exit_conditions": exits,
        "tone": tone_value,
        "silence_timeout_seconds": _coerce_timeout(silence_timeout_seconds),
        "caller_name": caller,
        "company_name": company,
        "pitch_summary": pitch,
        "objective": obj,
        "language": lang_short,
    }
    # Preserve the follow-up scheduler's per-campaign rules block when the
    # caller supplied one. The scheduler reads it via ``effective_followup_rules``
    # and merges with DEFAULT_FOLLOWUP_RULES, so a missing/None value here
    # just means "use the platform defaults".
    if isinstance(followup_rules, dict) and followup_rules:
        cfg["followup_rules"] = followup_rules
    return cfg


def _build_context(campaign: OutboundCampaign, *, goal: str | None = None) -> OutboundCampaignContext:
    agent_config = build_agent_config(**dict(campaign.agent_config or {}))
    return OutboundCampaignContext(
        campaign_id=str(campaign.id),
        name=str(campaign.name or ""),
        goal=str(goal or agent_config.get("goal") or "").strip(),
        agent_prompt=str(agent_config.get("agent_prompt") or "").strip()[:8000],
        objectives=_coerce_str_list(agent_config.get("objectives")),
        exit_conditions=_coerce_str_list(agent_config.get("exit_conditions")),
        tone=(str(agent_config.get("tone")).strip() or None) if agent_config.get("tone") else None,
        doc_text=(str(campaign.doc_text)[:4000] if campaign.doc_text else None),
        silence_timeout_seconds=_coerce_timeout(agent_config.get("silence_timeout_seconds")),
        caller_name=str(agent_config.get("caller_name") or "Riya"),
        company_name=str(agent_config.get("company_name") or ""),
        pitch_summary=str(agent_config.get("pitch_summary") or ""),
        objective=str(agent_config.get("objective") or "lead_qualification"),
    )


async def load_outbound_context(
    db: AsyncSession | None,
    campaign_id: Any,
    *,
    goal: str | None = None,
) -> OutboundCampaignContext | None:
    """Return the cached :class:`OutboundCampaignContext` for
    ``campaign_id``, fetching from Postgres on a miss.

    ``goal`` lets the caller force a goal override (e.g. when the
    campaign_context dict already carries one); empty / None preserves
    whatever the campaign's stored config says.

    Returns ``None`` when the campaign can't be loaded — the pipeline
    falls back to the legacy ``campaign_goal``-only behaviour.
    """
    if db is None or campaign_id is None:
        return None
    try:
        cid = str(uuid.UUID(str(campaign_id)))
    except (ValueError, TypeError):
        cid = str(campaign_id)

    now = time.monotonic()
    cached = _cache.get(cid)
    if cached is not None:
        expires_at, ctx = cached
        if expires_at > now:
            if goal and goal.strip() and ctx.goal != goal.strip():
                # Caller-supplied goal beats the cached version.
                return OutboundCampaignContext(
                    campaign_id=ctx.campaign_id,
                    name=ctx.name,
                    goal=goal.strip(),
                    agent_prompt=ctx.agent_prompt,
                    objectives=ctx.objectives,
                    exit_conditions=ctx.exit_conditions,
                    tone=ctx.tone,
                    doc_text=ctx.doc_text,
                    silence_timeout_seconds=ctx.silence_timeout_seconds,
                    caller_name=ctx.caller_name,
                    company_name=ctx.company_name,
                    pitch_summary=ctx.pitch_summary,
                    objective=ctx.objective,
                )
            return ctx

    lock = _lock_for(cid)
    async with lock:
        now = time.monotonic()
        cached = _cache.get(cid)
        if cached is not None:
            expires_at, ctx = cached
            if expires_at > now:
                return ctx
        try:
            res = await db.execute(
                select(OutboundCampaign).where(OutboundCampaign.id == uuid.UUID(cid))
            )
            campaign = res.scalars().first()
        except Exception:
            return None
        if campaign is None:
            return None
        # Detach so cross-session reads of scalar attrs stay safe.
        try:
            db.sync_session.expunge(campaign)
        except Exception:
            try:
                db.expunge(campaign)  # type: ignore[attr-defined]
            except Exception:
                pass
        ctx = _build_context(campaign, goal=goal)
        _cache[cid] = (now + _CAMPAIGN_TTL_SECONDS, ctx)
        _evict_stale(now)
        return ctx


def invalidate(campaign_id: Any) -> None:
    cid = str(campaign_id)
    _cache.pop(cid, None)


def invalidate_all() -> None:
    _cache.clear()


_OUTBOUND_BASE_TEMPLATE = """# OUTBOUND CAMPAIGN — SALES / OUTREACH PERSONA
You're {caller_name}, calling on behalf of {company_name}. You're a real human-sounding sales rep on an outbound call. You called the prospect — they didn't call you.

# Goal for this call
{objective_description}

# WHO YOU ARE — internalize this
You're a calm, attentive outreach rep. You're talking to a real person mid-day; their attention is borrowed. Sound natural and useful, not scripted or pushy.

# LISTEN FIRST — latest caller utterance wins
- First understand what the prospect just said. If they asked a question, answer it briefly before moving on.
- If they gave an objection, preference, budget, name, phone number, timing, or site-visit detail, use it. Do not re-ask it and do not ignore it to push the script.
- If their answer changes the path, adapt the next question to that answer. The objective list is a guide, not permission to monologue.
- If the previous assistant turn asked "Is now a good time?" and the prospect says "yes", "yeah", "ok", "sure", or similar, do NOT give a feature pitch. Ask exactly one discovery question next.

# TURN STRUCTURE — every reply follows this shape
1. **One acknowledgment (3–7 words, optional)** — vary it. Examples: "Mm, lovely.", "Cool.", "Right, makes sense.", "Awesome.", "Perfect.", "Mm-hm got it.", "Nice.", "Fair enough.", "Oh nice.", "Wonderful.", "Cool cool." Never repeat the same opener two turns in a row. Skip the acknowledgment entirely if it doesn't fit (e.g., after a question you asked).
2. **ONE concrete next step** — either a single short pitch beat (one specific benefit from the brief, not a list), a single qualifying question, OR a proposed close. Never stack two questions; never list three features; never give a paragraph.
Total reply length: 1–2 sentences. Period. If a third sentence feels necessary, you are probably saying too much.
Keep each sentence under 16 words unless confirming a final next step.

# BANNED OPENERS — variety is required
Never start more than 2 consecutive replies with the same word. Specifically forbidden as repeated openers:
  - "Great!" / "Great to hear that!"
  - "Got it!"
  - "Thanks for your time."
  - "Mm-hm."
  - "Sure."
  - The company name (don't say "Raghava Skyline" in every reply — they know).

# BANNED STANDALONE REPLIES — never end a turn with only one of these
- "Sure." / "Sure, go ahead." / "Go on." / "Go ahead." / "Mm-hm." / "Mhm."
- "Got it." / "Right." / "Okay." / "Alright."
- "Thanks for your time." (only allowed as the first half of a wrap-up; never alone mid-call)
If your draft reply is only one of these, you're NOT done — append the next concrete action.

# NO STACKING — one question per turn
Forbidden patterns (these all stack questions):
  - "What's your name and phone number?" → ask "What's your name?" ONLY. The phone comes on the next turn.
  - "Can you give me the date and time?" → ask date OR time, not both.
  - "Want me to share details and schedule a visit?" → propose one, not two.
Single-focus replies feel natural; stacked ones feel like a form.

# HANDLING SHORT / FILLER CALLER REPLIES
- "Yes" / "Yeah" / "Sure" / "Ok" mid-call: treat as **permission to continue**. Your next reply opens the pitch or asks the next qualifier. Do NOT echo "go ahead" / "sure" back.
- If that short reply is permission after the opener, ask the next qualifier directly. Example: "[warm]Great.[/warm] [question]Is this for self-use or investment?[/question]"
- "Hmm" / "Mm" / "Uh" / "Let me think" / "I would say" / similar filler: gently nudge with "Take your time" + restate the prior question. Example: "[warm]No rush.[/warm] [question]Weekdays or weekends easier for the visit?[/question]" Don't change topic.
- Long substantive reply (name + phone in one breath, BHK + budget together): acknowledge once briefly, then progress one step — don't re-ask anything they already told you.

# Hard rules — non-negotiable
- If asked "are you a real person?" or similar: be honest. "I'm an AI assistant calling on behalf of {company_name}." Continue helpfully.
- "Don't call again" / "remove me" / "do not call": apologise briefly, confirm DNC, end politely. Don't push back.
- They say no twice — accept and end gracefully. Don't escalate.
- Frustrated / angry — drop the pitch. Apologise. Offer to end.
- Never claim an action you can't take ("I've booked your demo"). Use "I'll have someone reach out to lock that in".

# Pitch facts (from the campaign brief — DO NOT INVENT)
The retrieved CONTEXT chunks come from the campaign's pitch document. Use them for any factual claim (pricing, features, terms, offers). If a specific isn't in CONTEXT, say honestly "I don't have that detail in front of me — I'll have someone get back to you on that."

# NEVER DO
- Don't use formal "Dear sir/madam".
- Don't quote the script verbatim — paraphrase.
- Don't ignore an objection by repeating the pitch.
- Don't read filenames or doc IDs.
- Don't re-ask for information the caller already gave (name, phone, BHK, budget, location).
- Don't repeat the company name every turn — they heard it in the opener.

# FEW-SHOT — copy this exact shape, vary the wording
Each AGENT line is one acknowledgment + one next step. Never two questions, never list features, never repeat the same opener.

CALLER: What is this? / Who is this? / For what?
AGENT: [neutral]Sorry — {caller_name} from {company_name}, this is about our project in your area.[/neutral] [question]Is now an okay time for a quick minute?[/question]

CALLER: But what is this regarding?
AGENT: [neutral]Totally fair — we're reaching out to folks who might be looking for a home nearby.[/neutral] [question]Are you currently looking, or just keeping an eye out?[/question]

CALLER: Yes
AGENT: [warm]Mm, lovely.[/warm] [question]Quick check — self-use or investment?[/question]

CALLER: 4 BHK, around 1 crore.
AGENT: [warm]Awesome — that fits the upper floors.[/warm] [question]Want me to set up a quick site visit?[/question]

CALLER: Nihar.
AGENT: [warm]Hey Nihar — best number to reach you on?[/question]

CALLER: മ്മ്.
AGENT: [warm]No rush.[/warm] [question]Weekday or weekend, whichever's lighter?[/question]

CALLER: 10 AM.
AGENT: [excited]Done — 10 AM on the 25th.[/excited] [neutral]I'll have the team confirm and send the brochure.[/neutral]

CALLER: Don't call me again.
AGENT: [empathy]Of course — sorry about the interruption.[/empathy] [neutral]I'll mark you do-not-call. Have a good one.[/neutral]"""


_OUTBOUND_UNIVERSAL_TURN_RULES = """# OUTBOUND TURN-TAKING RULES — ALWAYS FOLLOW
- Listen to the latest caller message before following the campaign objective.
- Reply in 1 to 2 short sentences only.
- Keep each sentence under 16 words.
- Ask at most one question per turn.
- Take only one next step per turn: answer their question, handle their objection, ask one qualifier, or propose one close.
- After a short permission reply to the opener, ask one discovery question. Do not pitch features first.
- If they already gave a detail, use it and move forward; do not ask again.
- If they are busy, not interested, wrong number, frustrated, or ask not to be called, stop pitching and close politely.
- Do not push past their answer. A respectful outbound call sounds like a conversation, not a script.
- If the prospect repeats a question or asks the same thing again, they did NOT get a clear answer the first time — answer it directly and plainly THIS turn; do not deflect, re-ask, or change topic.

# CALLBACK REQUEST — HARD RULE
If the caller asks to be called back at a specific time ("call me later",
"call me in two hours", "call me tomorrow at 4 PM", "call back next week"):
1. Acknowledge the time in one sentence: "Sure, I'll call you back in 2 hours."
2. Do NOT continue collecting name, phone, BHK, budget, visit date, or any
   other slot. The system already recorded the callback time from this turn.
3. Close politely in the next reply: "Thanks for your time, talk soon." Then stop.
4. Do NOT try to book anything, do NOT keep pitching, do NOT ask "while
   we're on the call." The caller said they want to end — honour it.
The follow-up scheduler will place the call at the requested time. Your job
on THIS call is to confirm the time and end gracefully.

# OUTBOUND FACTUAL SCOPE — HARD BOUNDARY
- This campaign brief (persona, goal, pitch summary, objectives, and the campaign document below) is your ENTIRE source of facts.
- Do NOT draw on the organisation's inbound knowledge base, property inventory, admin single-prompt, or general world knowledge for product / pricing / specification / availability claims.
- If the caller asks something that isn't covered by THIS brief, do not guess and do not improvise from training data. Say something like "Let me have our team confirm and get back to you on that," capture the question, and move on.
- Facts the caller gives you (name, BHK, budget, location, timeline) ARE in scope — they belong to this call's memory.
- The whole call is bounded by this campaign. Nothing outside it is authoritative for what you tell the prospect."""


# Cold-called prospects routinely ask "what is this?" before anything else. Small
# models tend to barrel past it into the qualification script ("self-use or
# investment?"), which reads as evasive and makes the prospect repeat themselves
# (observed in production). This block is rendered LATE (high recency) and carries
# a ready, concrete one-line answer composed from the campaign's own facts.
_OUTBOUND_EXPLAIN_CALL_RULE = """# "WHAT IS THIS?" — ANSWER IT, DO NOT QUALIFY (HIGHEST PRIORITY)
If the prospect asks what this is, who you are, why you're calling, or what it's about/regarding —
or sounds confused ("what is this?", "who is this?", "for what?", "what's this regarding?",
"why are you calling?") — STOP. Do NOT ask a qualifying question. Do NOT push the script.
Reply with ONE plain sentence naming the company and the reason for the call, then at most one
short check ("is now an okay time?"). NEVER answer "what is this?" with "is this for self-use or investment?".
Your ready one-line answer (paraphrase naturally, keep it short):
  {purpose_line}
Once they've heard it and understand, you may continue with ONE discovery question."""


# Small models routinely grab the agent's OWN name and use it to address the
# prospect (observed in production: "Thanks, Riya — noted that it's for self-use."
# where Riya is the rep, not the lead). This block hard-separates the two
# identities. Always rendered; formatted with the rep's name.
_OUTBOUND_NAME_GUARDRAIL = """# WHO IS WHO — never confuse yourself with the prospect
- YOUR name is {caller_name}. {caller_name} is the rep making this call — that's YOU. It is NEVER the prospect's name.
- You do NOT know the prospect's name unless THEY say it on this call (or it appears in the lead notes above). Do not guess it, do not borrow it from the brief, and NEVER address them as "{caller_name}" — that is you talking to yourself.
- Until they give their name, just speak to them directly as "you" — a warm reply needs no name at all.
- The instant they tell you their name, use it (correctly) from then on."""


def _call_purpose_line(context: "OutboundCampaignContext") -> str:
    """A ready, concrete one-line answer to 'what is this?', composed from the
    campaign's own company + pitch so the model doesn't have to improvise a vague
    'a quick home option'. Quoted so it reads as an example to paraphrase."""
    company = (context.company_name or "").strip()
    pitch = (context.pitch_summary or context.goal or "").strip()
    if company and pitch:
        return f'"This is {company} — {pitch}."'
    if company:
        return f'"This is a quick call from {company} about a project in your area."'
    if pitch:
        return f'"{pitch}."'
    return '"It\'s a quick courtesy call on behalf of the company — I\'ll keep it brief."'


def compose_outbound_system_section(
    context: OutboundCampaignContext | None,
    *,
    covered_objectives: list[str] | None = None,
    outbound_memory: dict[str, Any] | None = None,
    tool_flow_state: dict[str, Any] | None = None,
    tool_flow_bundle: dict[str, Any] | None = None,
    language: str | None = None,
    turn_index: int | None = None,
    conversational_memory: Any = None,
    business_type: str | None = None,
    latest_user_text: str | None = None,
) -> str:
    """Build the system-prompt fragment for an outbound turn.

    Empty when no proactive config is present. Sales-persona prompt is
    ported from the reference implementation (``agent_lab/voice-rag-agent``)
    — it's the template that's been tuned to produce humane outbound
    behaviour: respect the prospect's time, accept "no" twice and exit,
    disclose AI on ask, never claim actions the system can't take.

    ``tool_flow_state`` / ``tool_flow_bundle`` are the live booking-flow
    snapshot. When an inbound-style tool_flow (site visit or lead capture)
    is active, the rendered block tells the LLM exactly which slot to ask
    for next — without that, the model drifts back into general chat and
    never collects name/phone/date, so the tool never fires.

    ``turn_index`` is the 1-based turn number used to inject anti-loop
    guidance: by turn 5+, the model is told to close on the next slot or
    wrap politely, not re-open with "is this a good time to talk".
    """
    if context is None or not context.is_proactive:
        return ""
    remaining = context.remaining_objectives(covered_objectives or [])
    parts: list[str] = []

    # ── Follow-up preamble ──────────────────────────────────────────────
    # The call notes are the single source of prior-call context. The
    # post-call condenser (``call_condenser_service``) writes a 3-sentence
    # ``handoff_note`` onto the lead the moment the prior call ends — small
    # models read that prose far better than a structured slot dump, and
    # managers read the same note in the leads tab. When a note is present we
    # quote it; when it isn't (call too short / condenser failed), we fall
    # back to a MINIMAL acknowledgement rather than reconstructing
    # objections / commitments / preferences from memory.
    #
    # Source: ``outbound_memory['followup']`` — seeded once at session start
    # from the WebSocket handler's ``campaign_context``. Carries
    # ``is_followup``, ``attempt_n``, ``handoff_note`` (optional), and the
    # prior promise text.
    followup = (outbound_memory or {}).get("followup") if outbound_memory else None
    if followup and followup.get("is_followup"):
        attempt_n = int(followup.get("attempt_n") or 1)
        handoff_note = str(followup.get("handoff_note") or "").strip()

        # Admin-commanded follow-up (clinic Customer base path): the admin's
        # typed purpose is this call's agenda — render it ABOVE the prior-call
        # notes so the model treats it as the primary objective.
        admin_note = str(followup.get("admin_note") or "").strip()
        if admin_note:
            parts.append(
                "═══ REASON FOR THIS CALL — admin instruction ═══\n"
                f"The clinic admin asked you to: {admin_note}\n"
                "This is the purpose of the call. Deliver it warmly in your first "
                "substantive turn — do not bury it behind small talk.\n"
                "═══════════════════════════════════════════════"
            )

        if handoff_note:
            # The model reads prose 10× better than structured slots, so
            # this is the preferred path. Keep it short — the note itself
            # carries all the context.
            parts.append(
                "═══ FOLLOW-UP CALL — YOUR GOAL: BOOK A SITE VISIT ═══\n"
                f"This is attempt {attempt_n}. Notes from the last call:\n\n"
                f"{handoff_note}\n\n"
                "Open warmly by referencing these notes — DO NOT restart with "
                "\"Is this a good time to talk?\". If the notes mention an "
                "unresolved objection, address it FIRST.\n"
                "Your SINGLE goal on this call is to get them to commit to a SITE VISIT: "
                "re-engage briefly, then propose a specific day and time "
                "(e.g. \"this Saturday around 11?\") and lock it in. Don't just re-qualify "
                "or make small talk — keep steering every turn back toward booking the visit.\n"
                "══════════════════════"
            )
        else:
            # No note (lead never had a connected prior call, or the condenser
            # failed). Do NOT claim a previous conversation that didn't happen —
            # open about the PROJECT instead. The prior promise (the reason this
            # call was scheduled) is worth surfacing when present.
            prior_promise = str(followup.get("prior_promise") or "").strip()
            promise_clause = (
                f" They had asked to be called back: {prior_promise}."
                if prior_promise
                else ""
            )
            parts.append(
                "═══ FOLLOW-UP CALL — YOUR GOAL: BOOK A SITE VISIT ═══\n"
                f"This is attempt {attempt_n}. This is a follow-up to a lead who showed interest "
                f"in the project above.{promise_clause}\n"
                "Open warmly about the PROJECT — do NOT claim you spoke before if you have no notes "
                "for it, and do NOT restart cold with \"Is this a good time to talk?\".\n"
                "Your SINGLE goal on this call is to get them to commit to a SITE VISIT: spark "
                "interest with one specific benefit, then propose a specific day and time and lock "
                "it in. If they object, handle it once, then steer back to booking the visit.\n"
                "══════════════════════"
            )

    # A custom agent_prompt is the campaign's PERSONA + KNOWLEDGE, not its
    # delivery rules. Customers tend to paste a rigid "1. Introduction
    # 2. Qualification 3. Pitch…" call-flow, which makes the agent sound like a
    # form-filling telemarketer (observed in production). So we NO LONGER let a
    # custom prompt replace the human-delivery template — we layer it as
    # reference knowledge and ALWAYS render the tuned delivery scaffolding
    # (turn structure, opener variety, few-shot, name guardrail) on top, with
    # an explicit note that HOW-you-talk rules outrank any scripted sequence.
    if context.agent_prompt and context.agent_prompt != DEFAULT_AGENT_PROMPT:
        parts.append(
            "# OUTBOUND CAMPAIGN — CUSTOM PERSONA & KNOWLEDGE\n"
            "The block below is your product knowledge, persona, and tone reference. "
            "Use its FACTS and its VOICE. But if it contains a numbered call flow or "
            "step-by-step script, do NOT recite it in order like a checklist — that "
            "sounds robotic. HOW you actually talk (one beat per turn, listen first, "
            "vary your openers) is governed by the DELIVERY RULES below; those win "
            "over any scripted sequence here."
        )
        parts.append(context.agent_prompt)
    # The base template carries the human-delivery scaffolding (turn structure,
    # banned openers, banned standalone replies, no-stacking, few-shot). It is
    # rendered in BOTH branches now — for a custom persona it reinforces tone
    # and supplies the behavioural anchors the operator's script lacks.
    parts.append(
        _OUTBOUND_BASE_TEMPLATE.format(
            caller_name=context.caller_name or "Riya",
            company_name=context.company_name or "the company",
            objective_description=context.objective_description,
        )
    )
    parts.append(_OUTBOUND_UNIVERSAL_TURN_RULES)
    parts.append(_OUTBOUND_EXPLAIN_CALL_RULE.format(purpose_line=_call_purpose_line(context)))
    parts.append(
        _OUTBOUND_NAME_GUARDRAIL.format(caller_name=context.caller_name or "Riya")
    )

    # Outbound FSM mode block, branched on the org's business type:
    #   * clinics       → clinic outbound FSM (follow-up / booking / triage —
    #                     with the no-diagnosis guardrail).
    #   * anything else → real-estate outbound FSM (``sale`` / ``site_visit``
    #                     / ``outbound_lead``), preserving the legacy
    #                     always-render behaviour for callers that don't pass
    #                     ``business_type`` (outbound campaigns are
    #                     real-estate-only in practice).
    _bt = str(business_type or "").strip().lower()
    if _bt == "clinics":
        try:
            from app.services.clinic_outbound_agent_fsm import (
                current_mode as _clinic_outbound_mode,
                mode_block_for_prompt as _clinic_outbound_block,
            )

            _c_state = {"tool_flow": tool_flow_state or {}}
            parts.append(
                _clinic_outbound_block(
                    _clinic_outbound_mode(_c_state, latest_user_text=latest_user_text)
                )
            )
        except Exception:
            pass
        _outbound_mode = None  # type: ignore[assignment]
        _outbound_mode_block = None  # type: ignore[assignment]
    else:
        try:
            from app.services.real_estate_outbound_agent_fsm import (
                current_mode as _outbound_mode,
                mode_block_for_prompt as _outbound_mode_block,
            )
        except Exception:
            _outbound_mode = None  # type: ignore[assignment]
            _outbound_mode_block = None  # type: ignore[assignment]

    if _outbound_mode is not None and _outbound_mode_block is not None:
        _state_for_mode = {"tool_flow": tool_flow_state or {}}
        _mode = _outbound_mode(
            _state_for_mode,
            list(context.objectives or []),
            memory=conversational_memory,
        )
        _pending_label = None
        _pending_question = None
        if tool_flow_state and tool_flow_bundle is not None:
            _flow_key = str(tool_flow_state.get("flow_key") or "")
            _flow_def = ((tool_flow_bundle.get("flows") or {}).get(_flow_key) or {})
            _pending_slot_key = str(tool_flow_state.get("pending_slot") or "")
            for _slot in (_flow_def.get("slots") or []):
                if not isinstance(_slot, dict):
                    continue
                _skey = str(_slot.get("key") or "")
                if _pending_slot_key and _skey != _pending_slot_key:
                    continue
                if not _pending_slot_key and (tool_flow_state.get("collected") or {}).get(_skey):
                    continue
                _pending_label = str(_slot.get("label") or _skey)
                _questions = _slot.get("questions") or {}
                _pending_question = str(
                    _questions.get(language) or _questions.get("en") or ""
                )
                break
        parts.append(
            _outbound_mode_block(
                _mode,
                list(context.objectives or []),
                pending_slot_label=_pending_label,
                pending_slot_question=_pending_question,
                memory=conversational_memory,
            )
        )

    # Anti-loop reminder. The LLM, left to free-form, will sometimes re-ask
    # turn-1 framing questions ("Is this a good time to talk about X?") in
    # the middle of a call because the system prompt re-renders every turn.
    # By turn 3+ we explicitly forbid that pattern and nudge toward closure.
    if turn_index is not None and turn_index >= 3:
        loop_block = (
            "# TURN PROGRESS — DO NOT REWIND\n"
            f"You are on turn {turn_index}. The conversation is already underway.\n"
            "- NEVER re-ask opener-style framing questions: 'Is this a good time?', "
            "'Can I tell you about X?', 'May I share more?'. You already had permission. Move forward.\n"
            "- If you already explained the project once, do not re-explain it. Pivot to a qualifier or a close.\n"
            "- If you do not yet have name + phone, your next reply should aim for one of them."
        )
        if turn_index >= 6:
            loop_block += (
                "\n- You're past turn 5. Either land the next slot (name / phone / "
                "date / time) in this reply, or wrap the call politely. Do not "
                "open new discovery threads."
            )
        parts.append(loop_block)

    # Campaign overview always rendered so the model has goal + pitch even
    # when the custom-persona branch was taken.
    overview_parts: list[str] = []
    if context.pitch_summary:
        overview_parts.append(f"Pitch summary: {context.pitch_summary}")
    if context.goal:
        overview_parts.append(f"Goal: {context.goal}")
    overview_parts.append(f"Objective: {context.objective}")
    parts.append("# CAMPAIGN OVERVIEW\n" + "\n".join(overview_parts))
    if context.objectives:
        # Campaign objectives are now stored as structured codes ("site_visit",
        # "lead"); render them as human-readable labels for the LLM. Unknown
        # codes (legacy free-text rows from earlier campaigns) fall through
        # unchanged so existing campaigns keep working.
        try:
            from app.services.real_estate_outbound_agent_fsm import OBJECTIVE_LABELS as _OBJ_LABELS
        except Exception:
            _OBJ_LABELS = {}
        labeled_objectives = [
            _OBJ_LABELS.get(str(item).strip().lower(), str(item))
            for item in context.objectives
        ]
        objectives_render = "\n".join(f"  - {item}" for item in labeled_objectives)
        section = f"# OBJECTIVES (drive toward at least one)\n{objectives_render}"
        if remaining:
            remaining_render = "\n".join(
                f"  - {_OBJ_LABELS.get(str(item).strip().lower(), str(item))}"
                for item in remaining
            )
            section += f"\n\nStill pending this call:\n{remaining_render}"
        else:
            section += "\n\nAll objectives covered — confirm the next step and wrap the call politely."
        parts.append(section)
    memory_section = render_outbound_memory(outbound_memory)
    if memory_section:
        parts.append(memory_section)
    # Active booking-flow block. When the inbound-style tool_flow regex has
    # latched on (yes-after-offer or explicit booking intent), this surfaces
    # the slot state so the LLM drives toward filling the next one rather
    # than chatting around it. The block is INTENTIONALLY placed late so
    # recency bias keeps it top of mind for the next reply.
    if tool_flow_state:
        booking_block = render_booking_flow_state(
            tool_flow_state,
            tool_flow_bundle,
            language=language,
        )
        if booking_block:
            parts.append(booking_block)
    if context.exit_conditions:
        exit_render = "\n".join(f"  - {item}" for item in context.exit_conditions)
        parts.append(f"# EXIT CONDITIONS (when ANY is met, close the call warmly)\n{exit_render}")
    if context.tone:
        parts.append(f"Preferred tone: {context.tone}.")
    return "\n\n".join(parts)


def _opener_enquiry_phrase(facts: dict[str, Any], code: str) -> str:
    """A short, language-appropriate phrase naming what the lead is looking for
    (e.g. ``"3 BHK options in Gachibowli"``) from known facts. Empty when we
    don't know enough to personalise."""
    bhk = str(facts.get("bhk") or "").strip()
    location = str(facts.get("location") or "").strip()
    if not (bhk or location):
        return ""
    if code == "te":
        unit = f"{bhk} options" if bhk else "options"
        return f"{unit} {location} లో" if location else unit
    if code == "hi":
        unit = f"{bhk} options" if bhk else "options"
        return f"{location} में {unit}" if location else unit
    unit = f"{bhk} options" if bhk else "your requirement"
    return f"{unit} in {location}" if location else unit


def generate_outbound_opener_text(
    context: OutboundCampaignContext,
    *,
    language: str | None = None,
    known_facts: dict[str, Any] | None = None,
) -> str:
    """Deterministic, template-filled opener.

    Mirrors the reference's ``generate_opener_text`` — runs without an LLM
    call so the first audio is on the wire ~150ms faster than waiting for
    a stream. Includes prosody tags so :func:`stream_prosody_chunks` can
    render the line with proper tones.

    ``language`` switches the opener template so Telugu / Hindi campaigns
    don't start every call with English. ``known_facts`` (optional) carries
    what we already know about THIS lead — ``name`` plus any enquiry / prior-call
    facts (``bhk``, ``location``) and a ``returning`` flag — so the opener is
    personalised ("you'd enquired about 3 BHK in Gachibowli") instead of a cold
    one-size-fits-all line. Falls back to the campaign pitch when nothing is known.
    """
    caller = (context.caller_name or "Riya").strip() or "Riya"
    company = (context.company_name or "").strip()
    pitch = (context.pitch_summary or context.goal or "").strip()
    code = (language or "").strip().lower()[:2]

    facts = known_facts or {}
    lead_name = str(facts.get("name") or "").strip()
    returning = bool(facts.get("returning"))
    followup = bool(facts.get("followup"))
    project = str(facts.get("project") or "").strip()
    enquiry = _opener_enquiry_phrase(facts, code)

    if code == "te":
        name_part = f" {lead_name}" if lead_name else ""
        intro_te = (
            f"Hello{name_part}, నేను {caller}, {company} నుండి మాట్లాడుతున్నా."
            if company
            else f"Hello{name_part}, నేను {caller} మాట్లాడుతున్నా."
        )
        if returning:
            about_te = f"{project} గురించి " if project else "మన last conversation గురించి "
            return (
                f"[warm]{intro_te}[/warm] "
                f"[neutral]{about_te}follow up చేయడానికి call చేస్తున్నా.[/neutral] "
                "[question]ఇప్పుడు ఒక్క minute మాట్లాడగలరా?[/question]"
            )
        if followup:
            about_te = project or "మీరు interest చూపిన project"
            return (
                f"[warm]{intro_te}[/warm] "
                f"[neutral]{about_te} గురించి follow up చేయడానికి call చేస్తున్నా.[/neutral] "
                "[question]ఇప్పుడు ఒక్క minute మాట్లాడగలరా?[/question]"
            )
        if enquiry:
            reason = (
                f"Last time మీరు {enquiry} గురించి అడిగారు"
                if returning
                else f"మీరు {enquiry} గురించి enquiry చేశారు"
            )
            return (
                f"[warm]{intro_te}[/warm] "
                f"[neutral]{reason}.[/neutral] "
                "[question]ఇప్పుడు ఒక్క minute మాట్లాడగలరా?[/question]"
            )
        if pitch:
            return (
                f"[warm]{intro_te}[/warm] "
                f"[neutral]{pitch} గురించి call చేస్తున్నా.[/neutral] "
                "[question]ఇప్పుడు ఒక్క minute మాట్లాడగలరా?[/question]"
            )
        reason_te = (
            f"[neutral]{company} తరఫున మీ area లో ఒక project గురించి call చేస్తున్నా.[/neutral] "
            if company
            else ""
        )
        return (
            f"[warm]{intro_te}[/warm] "
            f"{reason_te}"
            "[question]ఇప్పుడు ఒక్క minute మాట్లాడగలరా?[/question]"
        )

    if code == "hi":
        name_part = f" {lead_name}" if lead_name else ""
        intro_hi = (
            f"नमस्ते{name_part}, मैं {caller} बोल रहा हूँ, {company} से."
            if company
            else f"नमस्ते{name_part}, मैं {caller} बोल रहा हूँ."
        )
        if returning:
            about_hi = f"{project} के बारे में " if project else "हमारी पिछली बात-चीत के "
            return (
                f"[warm]{intro_hi}[/warm] "
                f"[neutral]{about_hi}follow up के लिए call किया है.[/neutral] "
                "[question]क्या अभी एक minute बात कर सकते हैं?[/question]"
            )
        if followup:
            about_hi = project or "जिस project में आपकी interest थी उसके"
            return (
                f"[warm]{intro_hi}[/warm] "
                f"[neutral]{about_hi} के बारे में follow up के लिए call किया है.[/neutral] "
                "[question]क्या अभी एक minute बात कर सकते हैं?[/question]"
            )
        if enquiry:
            reason = (
                f"पिछली बार हमने {enquiry} के बारे में बात की थी"
                if returning
                else f"आपने {enquiry} के बारे में enquiry की थी"
            )
            return (
                f"[warm]{intro_hi}[/warm] "
                f"[neutral]{reason}.[/neutral] "
                "[question]क्या अभी एक minute बात कर सकते हैं?[/question]"
            )
        if pitch:
            return (
                f"[warm]{intro_hi}[/warm] "
                f"[neutral]{pitch} के लिए call किया है.[/neutral] "
                "[question]क्या अभी एक minute बात कर सकते हैं?[/question]"
            )
        reason_hi = (
            f"[neutral]मैं {company} की तरफ़ से आपके area के एक project के बारे में call कर रहा हूँ.[/neutral] "
            if company
            else ""
        )
        return (
            f"[warm]{intro_hi}[/warm] "
            f"{reason_hi}"
            "[question]क्या अभी एक minute बात कर सकते हैं?[/question]"
        )

    name_part = f" {lead_name}" if lead_name else ""
    intro = (
        f"Hi{name_part}, this is {caller} from {company}."
        if company
        else f"Hi{name_part}, this is {caller}."
    )
    if returning:
        # A real prior call happened (we have a note) — reference it, optionally
        # naming the project. The LLM drives the booking from the note next turn.
        about = f" about {project}" if project else ""
        return (
            f"[warm]{intro}[/warm] "
            f"[neutral]I'm calling to follow up on our last conversation{about}.[/neutral] "
            "[question]Is now a good time to talk for a minute?[/question]"
        )
    if followup:
        # Follow-up with NO prior conversation — do NOT invent one. Re-engage
        # grounded on the project; the LLM drives toward the site-visit booking.
        about = project or "the project you were interested in"
        return (
            f"[warm]{intro}[/warm] "
            f"[neutral]I'm following up about {about}.[/neutral] "
            "[question]Is now a good time to talk for a minute?[/question]"
        )
    if enquiry:
        reason = (
            f"we'd spoken about {enquiry} last time"
            if returning
            else f"you'd enquired about {enquiry}"
        )
        return (
            f"[warm]{intro}[/warm] "
            f"[neutral]I'm calling — {reason}.[/neutral] "
            "[question]Is now a good time to talk for a minute?[/question]"
        )
    if pitch:
        return (
            f"[warm]{intro}[/warm] "
            f"[neutral]I'm reaching out about {pitch}.[/neutral] "
            "[question]Is now a good time to talk for a minute?[/question]"
        )
    # No pitch summary: still give a reason so the prospect isn't left asking
    # "what is this?". Reference the company when we have it.
    reason_line = (
        f"[neutral]I'm reaching out on behalf of {company} about a project in your area.[/neutral] "
        if company
        else ""
    )
    return (
        f"[warm]{intro}[/warm] "
        f"{reason_line}"
        "[question]Is now a good time to talk for a minute?[/question]"
    )


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-zA-Z0-9]+", (text or "").lower())
        if len(token) > 2 and token not in _STOP_WORDS
    }


def infer_covered_objectives(
    context: OutboundCampaignContext | None,
    *,
    caller_text: str,
    agent_answer: str,
    already_covered: Iterable[str] | None = None,
) -> list[str]:
    """Best-effort objective progress from the latest caller + agent turn.

    This deliberately stays deterministic. It is not a source of truth for
    business state; it only reduces repeated proactive prompts by hiding
    objectives whose keywords were clearly discussed.
    """
    if context is None or not context.objectives:
        return list(already_covered or [])
    covered = [item for item in (already_covered or []) if item]
    covered_lower = {item.strip().lower() for item in covered}
    transcript_tokens = _tokens(f"{caller_text} {agent_answer}")
    for objective in context.objectives:
        normalized = objective.strip().lower()
        if not normalized or normalized in covered_lower:
            continue
        objective_tokens = _tokens(objective)
        if not objective_tokens:
            continue
        overlap = objective_tokens & transcript_tokens
        # Tightened next-step handling. Previously ANY single token overlap on a
        # "next step" objective marked it covered, so a stray "appointment" /
        # "callback" word in the objective text covered it even when no next step
        # was actually agreed. Now a next-step objective is covered only when an
        # actual next-step ACTION was discussed in the turn — not on incidental
        # token overlap.
        _action_tokens = {
            "appointment", "callback", "visit", "demo", "meeting",
            "book", "booking", "schedule",
        }
        is_next_step = bool(({"next", "step"} | _action_tokens) & objective_tokens)
        if is_next_step:
            covered_now = bool(overlap & _action_tokens)
        else:
            threshold = 1 if len(objective_tokens) <= 3 else 2
            covered_now = len(overlap) >= threshold
        if covered_now:
            covered.append(objective)
            covered_lower.add(normalized)
    return covered


_BHK_WORDS = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
}

_OUTBOUND_MEMORY_LABELS = {
    "name": "Name",
    "phone": "Phone",
    "purpose": "Buying purpose",
    "bhk": "BHK preference",
    "budget": "Budget",
    "timeline": "Timeline",
    "location_preference": "Location preference",
    "visit_preference": "Visit preference",
    "next_step": "Agreed next step",
    "requested_info": "Requested info",
    "objection": "Objection / constraint",
}


def _remember(memory: dict[str, str], key: str, value: str | None) -> None:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" .,:;")
    if not text:
        return
    memory[key] = text[:120]


def _extract_name(text: str) -> str | None:
    match = re.search(
        r"\b(?:my name is|this is|i am|i'm|call me)\s+([A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,2})",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    candidate = " ".join(match.group(1).split())
    first = candidate.split()[0].lower()
    if first in {
        "looking", "interested", "busy", "not", "calling", "thinking", "going",
        "planning", "buying", "searching", "available", "ok", "okay",
    }:
        return None
    return candidate.title()


def update_outbound_memory(
    existing: dict[str, Any] | None,
    *,
    caller_text: str,
    agent_answer: str = "",
) -> dict[str, str]:
    """Extract stable call facts for outbound turn memory.

    The LLM already sees raw transcript history, but important sales-call
    facts can be buried across turns. This lightweight memory is deliberately
    deterministic and conservative: it stores only explicit caller-provided
    details and a few common real-estate/outreach signals.
    """
    memory: dict[str, str] = {
        str(key): str(value)
        for key, value in (existing or {}).items()
        if key in _OUTBOUND_MEMORY_LABELS and value
    }
    text = re.sub(r"\s+", " ", caller_text or "").strip()
    lower = text.lower()
    if not text:
        return memory

    name = _extract_name(text)
    if name:
        _remember(memory, "name", name)

    phone_match = re.search(r"(?<!\d)(?:\+?91[\s-]?)?([6-9](?:[\s-]?\d){9})(?!\d)", text)
    if phone_match:
        digits = re.sub(r"\D", "", phone_match.group(0))
        _remember(memory, "phone", digits[-10:] if len(digits) >= 10 else digits)

    bhk_match = re.search(r"\b([1-6])\s*(?:bhk|bed(?:room)?s?)\b", lower)
    if not bhk_match:
        word_pattern = "|".join(_BHK_WORDS)
        bhk_match = re.search(rf"\b({word_pattern})\s*(?:bhk|bed(?:room)?s?)\b", lower)
    if bhk_match:
        raw = bhk_match.group(1)
        _remember(memory, "bhk", f"{_BHK_WORDS.get(raw, raw)} BHK")

    # Reuse the inbound budget extractor so outbound memory understands the
    # same spelled-out amounts ("half a crore", "fifty lakhs", "one and a half
    # crore") the inbound extractor was fixed to handle — not just digit+unit.
    try:
        from app.services.conversational_memory import MemoryExtractor as _ME

        budget_value = _ME._extract_budget(text)
    except Exception:
        budget_value = None
    if budget_value:
        _remember(memory, "budget", budget_value)

    if re.search(r"\b(self[-\s]?use|own use|end use|family|to live|for living)\b", lower):
        _remember(memory, "purpose", "self-use")
    elif re.search(r"\b(invest|investment|investor|rental|rent out|roi)\b", lower):
        _remember(memory, "purpose", "investment")

    timeline_match = re.search(
        r"\b(immediately|as soon as possible|this month|next month|this year|next year|"
        r"within\s+[0-9]+\s+(?:days|weeks|months)|in\s+[0-9]+\s+(?:days|weeks|months))\b",
        lower,
    )
    if timeline_match:
        _remember(memory, "timeline", timeline_match.group(1))

    if re.search(r"\b(weekday|weekdays|weekend|weekends|morning|afternoon|evening)\b", lower):
        _remember(memory, "visit_preference", re.search(r"\b(weekday|weekdays|weekend|weekends|morning|afternoon|evening)\b", lower).group(1))
    date_or_time_match = re.search(
        r"\b(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"[0-3]?\d(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|"
        r"(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:am|pm))\b",
        lower,
    )
    if date_or_time_match:
        current = memory.get("visit_preference")
        value = date_or_time_match.group(0)
        _remember(memory, "visit_preference", f"{current}; {value}" if current and value not in current else value)

    location_match = re.search(r"\b(?:near|around|in|at)\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3})\b", text)
    if location_match and location_match.group(1).lower() not in {"this", "that"}:
        _remember(memory, "location_preference", location_match.group(1))

    info_hits = []
    for label, pattern in (
        ("details", r"\b(details?|information|info)\b"),
        ("brochure", r"\bbrochure\b"),
        ("pricing", r"\b(price|pricing|cost|rate|quotation)\b"),
        ("RERA number", r"\brera\b"),
        ("floor plans", r"\bfloor\s*plans?\b"),
        ("callback", r"\b(call back|callback)\b"),
        ("WhatsApp details", r"\bwhatsapp\b"),
    ):
        if re.search(pattern, lower):
            info_hits.append(label)
    if info_hits:
        prior = [item.strip() for item in memory.get("requested_info", "").split(",") if item.strip()]
        combined = list(dict.fromkeys(prior + info_hits))
        _remember(memory, "requested_info", ", ".join(combined))

    # Next-step TYPE — distinguish a committed site visit from a callback
    # brush-off or a meeting, so the CRM and objective coverage don't conflate
    # them (a "call me back" is not the same outcome as "I'll come visit").
    if re.search(r"\b(site\s*visit|come (?:and )?(?:see|visit)|visit the (?:site|property|project|flat)|tour|in person)\b", lower):
        _remember(memory, "next_step", "site_visit")
    elif re.search(r"\b(appointment|meeting|demo|schedule a (?:call|meeting))\b", lower):
        _remember(memory, "next_step", "appointment")
    elif re.search(r"\b(call back|callback|call me (?:back|later)|ring me (?:back|later))\b", lower):
        _remember(memory, "next_step", "callback")

    # Objection parity with inbound: reuse the inbound objection patterns so
    # outbound captures competitor / long-horizon / timing brush-offs too — not
    # just the old binary not-interested / price split.
    try:
        from app.services.conversational_memory import _OBJECTION_PATTERNS as _OBJ
    except Exception:
        _OBJ = ()
    for pat, code in _OBJ:
        if pat.search(text):
            # do_not_call keeps the caller's verbatim words (the interest-signal
            # check greps them); the rest store a readable label of the code.
            _remember(memory, "objection", text if code == "do_not_call" else code.replace("_", " "))
            break

    return memory


def render_outbound_memory(memory: dict[str, Any] | None) -> str:
    items = []
    for key, label in _OUTBOUND_MEMORY_LABELS.items():
        value = str((memory or {}).get(key) or "").strip()
        if value:
            items.append(f"  - {label}: {value}")
    if not items:
        return ""
    return (
        "# CONVERSATION MEMORY — already known\n"
        "Use these facts as memory from this call. Do not ask for them again; "
        "build the next reply around them.\n"
        + "\n".join(items)
    )


def outbound_memory_as_facts(memory: dict[str, Any] | None) -> dict[str, Any]:
    """Map the lightweight outbound turn-memory dict onto canonical
    ConversationalMemory fact keys, so the structured-memory + strategy layer
    can be seeded with what the outbound extractor captured (lead scoring,
    objection playbook). Only the slot-shaped keys map; free-form ones
    (visit_preference, next_step, requested_info, objection) ride their own
    channels."""
    from app.services.conversational_memory import (
        FACT_BHK,
        FACT_BUDGET,
        FACT_LOCATION,
        FACT_NAME,
        FACT_PHONE,
        FACT_PURPOSE,
        FACT_TIMELINE,
    )

    mapping = {
        "name": FACT_NAME,
        "phone": FACT_PHONE,
        "bhk": FACT_BHK,
        "budget": FACT_BUDGET,
        "timeline": FACT_TIMELINE,
        "location_preference": FACT_LOCATION,
        "purpose": FACT_PURPOSE,
    }
    out: dict[str, Any] = {}
    for okey, fkey in mapping.items():
        value = str((memory or {}).get(okey) or "").strip()
        if value:
            out[fkey] = value
    return out


_FLOW_LABEL = {
    "real_estate_site_visit": "site-visit booking",
    "leads_create": "lead capture",
}


def render_booking_flow_state(
    flow_state: dict[str, Any] | None,
    bundle: dict[str, Any] | None,
    *,
    language: str | None = None,
) -> str:
    """Render the active tool_flow as a high-priority system block.

    When the inbound tool_flow regex starts a booking flow for an outbound
    call (e.g. "yeah" right after the agent offered a site visit), this
    block tells the LLM *what's been collected* and *what slot to ask
    next*. The deterministic slot question is included verbatim so the
    LLM has something concrete to paraphrase rather than drifting back
    to free-form chat.
    """
    if not isinstance(flow_state, dict) or not flow_state.get("active"):
        return ""
    if flow_state.get("completed"):
        return ""
    flow_key = str(flow_state.get("flow_key") or "")
    if not flow_key:
        return ""
    collected = dict(flow_state.get("collected") or {})
    pending_slot = str(flow_state.get("pending_slot") or "")

    slot_defs: list[dict[str, Any]] = []
    if bundle:
        flow_def = ((bundle.get("flows") or {}).get(flow_key) or {})
        slot_defs = [s for s in (flow_def.get("slots") or []) if isinstance(s, dict)]

    lang_code = (language or "en").split("-")[0].lower()

    captured_lines: list[str] = []
    missing_lines: list[str] = []
    next_slot_def: dict[str, Any] | None = None
    for slot in slot_defs:
        skey = str(slot.get("key") or "")
        if not skey:
            continue
        label = str(slot.get("label") or skey.replace("_", " ").title())
        value = collected.get(skey)
        if value not in (None, ""):
            captured_lines.append(f"  ✓ {label}: {value}")
            continue
        questions = slot.get("questions") or {}
        question = questions.get(lang_code) or questions.get("en") or ""
        missing_lines.append(f"  ✗ {label} — still needed")
        if next_slot_def is None and (not pending_slot or pending_slot == skey):
            next_slot_def = {"key": skey, "label": label, "question": question}

    # No slot definitions reached (bundle missing) — render a minimal block
    # from collected only so the LLM still sees what's captured.
    if not slot_defs:
        for key, value in collected.items():
            if value in (None, ""):
                continue
            captured_lines.append(f"  ✓ {key.replace('_', ' ').title()}: {value}")

    flow_label = _FLOW_LABEL.get(flow_key, flow_key.replace("_", " "))
    parts: list[str] = [
        f"# ACTIVE BOOKING FLOW — DRIVE TO COMPLETION",
        f"You're capturing a {flow_label}. The system records this once every required slot is filled.",
    ]
    if captured_lines:
        parts.append("Captured so far:")
        parts.extend(captured_lines)
    if missing_lines:
        parts.append("Still needed:")
        parts.extend(missing_lines)
    if next_slot_def:
        parts.append(
            "YOUR NEXT REPLY must move toward filling "
            f"**{next_slot_def['label']}** next."
        )
        if next_slot_def["question"]:
            parts.append(
                f'Reference question: "{next_slot_def["question"]}" '
                "— paraphrase in your persona; do NOT read it verbatim, and never stack two asks."
            )
        parts.append(
            "If the lead just answered a question of yours, take ONE acknowledgment "
            "(e.g. \"Got it.\") and then ask for the next slot. Do not pivot back to "
            "general project chat — that's the most common failure mode."
        )
    elif not missing_lines:
        parts.append(
            "All slots are filled — confirm the next step in one short sentence and wrap the call. "
            "The system will record this booking automatically."
        )
    return "\n".join(parts)


PROACTIVE_NUDGE_PROMPT = (
    "(no caller response — they went quiet. React like a real person would, not "
    "a script: do NOT repeat your last question word-for-word. Do ONE of these in "
    "one short, warm line — a soft connectivity check (\"Hi, are you still there?\"), "
    "a gentler re-phrase of the last question that lowers the friction, or a tiny "
    "reason-to-care hook from the brief. If they've now gone quiet twice, stop "
    "pushing and offer to follow up at a better time, then wrap politely.)"
)


PROACTIVE_OPENER_PROMPT = (
    "(call connected — you are the caller, the prospect just answered. "
    "Open the conversation in one short line that names the business, "
    "states the reason for the call from the campaign goal, and asks "
    "permission to continue. Do NOT greet as if you were answering an "
    "inbound call.)"
)


class ProactiveSilenceWatchdog:
    """Silence-triggered proactive nudge for outbound calls.

    The watchdog is armed after the agent finishes speaking. If the
    caller stays silent past ``timeout_seconds``, ``on_fire`` is invoked
    — the stream service uses this to run a proactive turn whose only
    "user input" is the :data:`PROACTIVE_NUDGE_PROMPT` sentinel. The
    LLM, given the proactive system prompt, interprets that as "drive
    the next objective".

    Strictly opt-in: inbound calls and outbound calls without a
    proactive ``OutboundCampaignContext`` should never instantiate one.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float,
        on_fire,
    ) -> None:
        self._timeout = max(0.5, float(timeout_seconds))
        self._on_fire = on_fire
        self._task: asyncio.Task[None] | None = None

    def arm(self) -> None:
        """(Re-)start the silence timer. Idempotent — cancels any prior
        in-flight timer first."""
        self.cancel()
        self._task = asyncio.create_task(self._run())

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    @property
    def armed(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run(self) -> None:
        try:
            await asyncio.sleep(self._timeout)
        except asyncio.CancelledError:
            return
        try:
            result = self._on_fire()
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"NOKVO-OUTBOUND: silence watchdog on_fire failed: {exc!r}")


__all__ = [
    "OutboundCampaignContext",
    "PROACTIVE_NUDGE_PROMPT",
    "ProactiveSilenceWatchdog",
    "build_agent_config",
    "compose_outbound_system_section",
    "infer_covered_objectives",
    "invalidate",
    "invalidate_all",
    "load_outbound_context",
    "render_outbound_memory",
    "update_outbound_memory",
]
