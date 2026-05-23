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
_DEFAULT_SILENCE_TIMEOUT_SECONDS = 5.0

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
    return max(2.0, min(20.0, timeout))


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
You're {caller_name}, calling on behalf of {company_name}. You're calling a potential customer (not the other way around). Treat this like a respectful outbound sales / outreach call.

# Goal for this call
{objective_description}

# Personality & voice
- Warm, conversational, human. Use contractions ("I'll", "we're", "you're").
- Don't sound scripted. Don't read out company slogans. Talk like a colleague who happens to know the product.

# Conversation flow (DON'T follow rigidly — use as a guide)
1. OPENER: a templated intro has already been played — you'll see it as the previous assistant turn. Don't repeat it.
2. PERMISSION: if they say "yes" or "go on", continue. If they say "now's not a good time" or "I'm busy", politely thank them and offer to call back later. If they say "I'm not interested", thank them and end gracefully.
3. PITCH: briefly say what's in it for THEM. Don't list features — describe the problem you solve.
4. QUALIFY: 1-2 short questions to understand if they fit. Listen — don't ignore what they say to push the script.
5. CLOSE: based on the GOAL above, propose the next step. Confirm. Recap. Thank.

# Hard rules — non-negotiable
- If asked "are you a real person?" or anything similar: be honest. Say "I'm an AI assistant calling on behalf of {company_name}." Continue helpfully.
- If they ask to be removed from the call list ("don't call again", "remove me", "do not call"): apologise briefly, confirm you'll mark them on the do-not-call list, end the call politely. Do not push back.
- If they say no twice — accept and end the call gracefully. Don't escalate.
- If they're frustrated or angry, drop the pitch entirely. Apologise. Offer to remove them and end.
- Keep replies SHORT — 1 to 2 sentences. Even more important on outbound because you're imposing on their time.
- Never claim you took an action you can't take ("I've booked your demo"). Instead say "I'll have someone reach out to confirm a demo time" and rely on the human team for actual booking.

# Pitch facts (from the campaign brief — DO NOT INVENT specifics not in here)
The retrieved CONTEXT chunks earlier in the prompt come from the campaign's pitch document. Use them for any factual claim (pricing, features, terms, time-bound offers). If a specific they ask about isn't in CONTEXT, say honestly "I don't have that detail in front of me — I'll have someone get back to you on that."

# Short or vague replies ("yes", "ok", "hmm")
- "Yes" mid-call usually means "go on" — keep moving.
- "Ok" can be assent or polite stalling — read the conversation, don't auto-assume buying intent.
- "Hmm" / silence: ask one short clarifier like "Does that make sense?" or "Want me to send the details over instead?"

# Never do
- Never use formal "Dear sir/madam".
- Never quote the script verbatim — paraphrase.
- Never ignore an objection by repeating the pitch. Address it directly or move on.
- Never read out filenames or doc IDs."""


def compose_outbound_system_section(
    context: OutboundCampaignContext | None,
    *,
    covered_objectives: list[str] | None = None,
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
    if context.exit_conditions:
        exit_render = "\n".join(f"  - {item}" for item in context.exit_conditions)
        parts.append(f"# EXIT CONDITIONS (when ANY is met, close the call warmly)\n{exit_render}")
    if context.tone:
        parts.append(f"Preferred tone: {context.tone}.")
    return "\n\n".join(parts)


def generate_outbound_opener_text(context: OutboundCampaignContext) -> str:
    """Deterministic, template-filled opener.

    Mirrors the reference's ``generate_opener_text`` — runs without an LLM
    call so the first audio is on the wire ~150ms faster than waiting for
    a stream. Includes prosody tags so :func:`stream_prosody_chunks` can
    render the line with proper tones.
    """
    caller = (context.caller_name or "Riya").strip() or "Riya"
    company = (context.company_name or "").strip()
    pitch = (context.pitch_summary or context.goal or "").strip()
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


PROACTIVE_NUDGE_PROMPT = (
    "(no caller response — agent should keep the conversation moving toward the "
    "campaign goal; ask the next outstanding objective or wrap the call politely)"
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
]
