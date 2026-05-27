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
    return {
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
- Do not push past their answer. A respectful outbound call sounds like a conversation, not a script."""


def compose_outbound_system_section(
    context: OutboundCampaignContext | None,
    *,
    covered_objectives: list[str] | None = None,
    outbound_memory: dict[str, Any] | None = None,
) -> str:
    """Build the system-prompt fragment for an outbound turn.

    Empty when no proactive config is present. Sales-persona prompt is
    ported from the reference implementation (``agent_lab/voice-rag-agent``)
    — it's the template that's been tuned to produce humane outbound
    behaviour: respect the prospect's time, accept "no" twice and exit,
    disclose AI on ask, never claim actions the system can't take.
    """
    if context is None or not context.is_proactive:
        return ""
    remaining = context.remaining_objectives(covered_objectives or [])
    parts: list[str] = []
    # If the operator provided a custom agent_prompt, prefer it but still
    # append the hard rules so AI-disclosure / "no twice = end" stay locked.
    if context.agent_prompt and context.agent_prompt != DEFAULT_AGENT_PROMPT:
        parts.append("# OUTBOUND CAMPAIGN — CUSTOM PERSONA")
        parts.append(context.agent_prompt)
    else:
        parts.append(
            _OUTBOUND_BASE_TEMPLATE.format(
                caller_name=context.caller_name or "Riya",
                company_name=context.company_name or "the company",
                objective_description=context.objective_description,
            )
        )
    parts.append(_OUTBOUND_UNIVERSAL_TURN_RULES)
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
        objectives_render = "\n".join(f"  - {item}" for item in context.objectives)
        section = f"# OBJECTIVES (ask in order until covered)\n{objectives_render}"
        if remaining:
            remaining_render = "\n".join(f"  - {item}" for item in remaining)
            section += f"\n\nStill pending this call:\n{remaining_render}"
        else:
            section += "\n\nAll objectives covered — confirm the next step and wrap the call politely."
        parts.append(section)
    memory_section = render_outbound_memory(outbound_memory)
    if memory_section:
        parts.append(memory_section)
    if context.exit_conditions:
        exit_render = "\n".join(f"  - {item}" for item in context.exit_conditions)
        parts.append(f"# EXIT CONDITIONS (when ANY is met, close the call warmly)\n{exit_render}")
    if context.tone:
        parts.append(f"Preferred tone: {context.tone}.")
    return "\n\n".join(parts)


def generate_outbound_opener_text(
    context: OutboundCampaignContext,
    *,
    language: str | None = None,
) -> str:
    """Deterministic, template-filled opener.

    Mirrors the reference's ``generate_opener_text`` — runs without an LLM
    call so the first audio is on the wire ~150ms faster than waiting for
    a stream. Includes prosody tags so :func:`stream_prosody_chunks` can
    render the line with proper tones.

    ``language`` switches the opener template so Telugu / Hindi campaigns
    don't start every call with English. The opener uses the same natural
    code-switching style the system prompt mandates downstream (English
    loanwords + native particles), so the first impression matches the
    rest of the call's register.
    """
    caller = (context.caller_name or "Riya").strip() or "Riya"
    company = (context.company_name or "").strip()
    pitch = (context.pitch_summary or context.goal or "").strip()
    code = (language or "").strip().lower()[:2]

    if code == "te":
        intro_te = (
            f"Hello, nenu {caller}, {company} nundi maatalaadutunna."
            if company
            else f"Hello, nenu {caller} maatalaadutunna."
        )
        if pitch:
            return (
                f"[warm]{intro_te}[/warm] "
                f"[neutral]{pitch} gurinchi call chesthunna.[/neutral] "
                "[question]Ippudu okka minute maatalaadagalara?[/question]"
            )
        return (
            f"[warm]{intro_te}[/warm] "
            "[question]Ippudu okka minute maatalaadagalara?[/question]"
        )

    if code == "hi":
        intro_hi = (
            f"Namaste, main {caller} bol raha hoon, {company} se."
            if company
            else f"Namaste, main {caller} bol raha hoon."
        )
        if pitch:
            return (
                f"[warm]{intro_hi}[/warm] "
                f"[neutral]{pitch} ke liye call kiya hai.[/neutral] "
                "[question]Kya abhi ek minute baat kar sakte hain?[/question]"
            )
        return (
            f"[warm]{intro_hi}[/warm] "
            "[question]Kya abhi ek minute baat kar sakte hain?[/question]"
        )

    intro = (
        f"Hi, this is {caller} from {company}."
        if company
        else f"Hi, this is {caller}."
    )
    if pitch:
        return (
            f"[warm]{intro}[/warm] "
            f"[neutral]I'm calling about {pitch}.[/neutral] "
            "[question]Is now a good time to talk for a minute?[/question]"
        )
    return (
        f"[warm]{intro}[/warm] "
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
        next_step_objective = bool({"next", "step", "appointment", "callback", "demo", "visit"} & objective_tokens)
        threshold = 1 if len(objective_tokens) <= 3 or next_step_objective else 2
        if len(overlap) >= threshold:
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

    budget_match = re.search(
        r"\b(?:budget(?:\s+is)?|around|about|upto|up to|under|within|near)?\s*"
        r"(?:rs\.?|inr|₹)?\s*([0-9]+(?:\.[0-9]+)?\s*(?:cr|crore|crores|lakh|lakhs|lac|lacs))\b",
        lower,
    )
    if budget_match:
        _remember(memory, "budget", budget_match.group(1))

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

    if re.search(r"\b(not interested|don't call|do not call|remove me|wrong number|busy|call later)\b", lower):
        _remember(memory, "objection", text)
    elif re.search(r"\b(expensive|costly|too high|out of budget)\b", lower):
        _remember(memory, "objection", "price concern")

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


PROACTIVE_NUDGE_PROMPT = (
    "(no caller response — do not monologue. Give one brief nudge tied to the "
    "last question, ask only the next outstanding objective, or wrap politely)"
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
