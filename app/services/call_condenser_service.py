"""Post-call condenser — turn a full transcript into a 3-sentence handoff note.

Runs as a fire-and-forget background task at the very end of every outbound
call (see ``app/services/nokvo_one_voice_stream_service.py``, right after
``promote_to_caller_memory``). One cheap LLM call against the gpt-4.1-nano
pool (``complete_nano``; falls back to the gpt-5-mini pool when no nano
deployment is configured) produces a short, human-readable summary that
the next follow-up call's preamble can quote verbatim — and that a clinic
manager can scan in the leads tab without opening transcripts.

This is purely additive to :mod:`app.services.conversational_memory`.
ConversationalMemory still drives mid-call recall (slot tracking across
20+ turns); the condenser only fires once after the call ends. The two
artefacts coexist: the condenser writes ``OutgoingLead.handoff_note``,
ConversationalMemory writes the durable-fact bag in Redis.

Returns ``None`` on:
  - Empty / too-short transcript (< 3 turns) — nothing to summarise.
  - LLM error or timeout — the caller treats None as "no condensed note;
    follow-up preamble falls back to the structured-facts block".
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

from app.models.tenant_resources import TenantResources
from app.services.agent_session_store import AgentSessionStore

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = (
    "You are a sales-call note-taker for an Indian outbound voice agent. "
    "Read the transcript and write EXACTLY three sentences for the next "
    "agent who will follow up with this lead. Focus strictly on: "
    "(1) what the customer wanted, "
    "(2) why they didn't book or what hesitation they raised, "
    "(3) what we promised them or what they asked us to do. "
    "Do NOT invent details. Do NOT use bullet points, JSON, or headings — "
    "plain prose only. If a section has nothing to say, say so plainly. "
    "Use the lead's name when known. Write in the same language the call "
    "was conducted in (English, Hindi, Telugu, or mixed)."
)

# Transcripts shorter than this aren't worth condensing — the model would
# either invent details or produce useless filler ("The lead picked up and
# the call ended"). A 3-turn floor catches greeting + first reply + hangup.
_MIN_TURNS = 3

# Cap per-turn excerpt length to keep the prompt small. Most utterances are
# under 200 chars; a hard cap stops a runaway transcript from blowing the
# context window.
_MAX_TURN_CHARS = 320
_MAX_TURNS = 24


def _render_transcript(history: list[dict[str, str]]) -> str:
    """Format the role/content list as the LLM expects.

    Mirrors the convention used by
    :func:`outbound_call_outcome_classifier._format_transcript` so any
    operator reading both prompts sees the same shape.
    """
    lines: list[str] = []
    for turn in history[-_MAX_TURNS:]:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "").strip().lower()
        content = str(turn.get("content") or "").strip()
        if not content:
            continue
        speaker = "lead" if role == "user" else "agent"
        lines.append(f"{speaker}: {content[:_MAX_TURN_CHARS]}")
    return "\n".join(lines)


def _cap_sentences(text: str, *, n: int = 3) -> str:
    """Trim an over-eager model to N sentences.

    Uses a simple terminal-punctuation split. Handles common abbreviations
    (Mr., Dr., Mrs.) by keeping the trailing period attached to the
    sentence before splitting on the NEXT period. Not regex-perfect but
    plenty for the condenser's "guarantee 3-ish sentences" budget.
    """
    s = (text or "").strip()
    if not s:
        return ""
    # Defang common abbreviations so they don't trigger a split.
    protected = re.sub(
        r"\b(Mr|Mrs|Ms|Dr|Sr|Jr|St|Rs|No)\.",
        r"\1__DOT__",
        s,
    )
    parts = re.split(r"(?<=[.!?])\s+", protected)
    kept = [p.replace("__DOT__", ".").strip() for p in parts[:n] if p.strip()]
    return " ".join(kept)


def _ls_chain(name: str):
    try:
        from langsmith.run_helpers import traceable
        return traceable(name=name, run_type="chain")
    except Exception:
        return lambda fn: fn


@_ls_chain("post_call.condenser")
async def condense_call(
    *,
    tenant_res: TenantResources,
    call_id: str | None,
    lead_name: str | None = None,
    campaign_name: str | None = None,
    transcript: list[dict[str, str]] | None = None,
    timeout_s: float = 6.0,
) -> Optional[str]:
    """Produce a 3-sentence handoff note for the lead.

    :param tenant_res: TenantResources, used to scope the Redis history
        lookup. The LLM itself runs against the shared nano pool, so
        ``tenant_res`` does NOT pick the model.
    :param call_id: The just-ended call's ID. We read its history from
        Redis if ``transcript`` is not supplied.
    :param lead_name: Caller's name when known. Prepended to the user
        message so the model writes "Priya asked about…" instead of
        "the customer asked about…".
    :param campaign_name: Campaign label, e.g. "Diwali outreach".
        Prepended for the same reason.
    :param transcript: Optional pre-fetched history. Mostly for tests.
    :param timeout_s: Hard ceiling on the LLM call.

    :return: The handoff-note string (3 sentences, plain prose), or
        ``None`` on transcript-too-short / LLM-error / timeout. Callers
        treat None as "no condensed note available".
    """
    if not call_id:
        return None

    history = transcript
    if history is None:
        try:
            history = await AgentSessionStore.get_history(tenant_res, call_id)
        except Exception:
            logger.debug("NOKVO-CONDENSE: get_history failed", exc_info=True)
            return None

    if not history or len(history) < _MIN_TURNS:
        return None

    rendered = _render_transcript(history)
    if not rendered:
        return None

    user_msg = (
        f"Lead: {(lead_name or 'Unknown')}. "
        f"Campaign: {(campaign_name or 'Outbound call')}. "
        f"Transcript:\n\n{rendered}"
    )

    # Lazy import — avoids a hard dep on the voice pipeline when this
    # module is imported by code paths that don't need the LLM (tests,
    # for example).
    from app.services.nokvo_one_voice_pipeline import AzureGroundedLLM

    try:
        # Summaries run on the cheap gpt-4.1-nano pool (round-robin, no
        # stickiness) — a 3-sentence note doesn't justify the gpt-5-mini pool.
        # complete_nano transparently falls back to the mini pool when no nano
        # deployment is configured.
        raw = await asyncio.wait_for(
            AzureGroundedLLM.complete_nano(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=200,
            ),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        logger.debug("NOKVO-CONDENSE: timeout after %.1fs", timeout_s)
        return None
    except Exception:
        # Any LLM-side error (rate-limit, network, malformed response)
        # silently falls back to "no condensed note". Don't propagate —
        # we're a best-effort artefact, not a blocking dependency.
        logger.debug("NOKVO-CONDENSE: LLM call failed", exc_info=True)
        return None

    text = (raw or "").strip()
    if not text:
        return None

    return _cap_sentences(text, n=3) or None
