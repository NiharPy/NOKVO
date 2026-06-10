"""In-call rolling summary — keeps the agent aware of the whole conversation.

As the call grows past the verbatim window, the oldest exchanges are folded into
a compact running summary by a cheap model (gpt-4.1-nano). The summary is injected
into the agent's prompt (the dynamic suffix) so it stays aware of the entire call
without re-sending the full transcript every turn.

This runs OFF the latency path (fired as a background task after the reply). On
any failure it returns the prior summary unchanged — never blocks or breaks a turn.
"""
from __future__ import annotations

import logging

from app.core.config import settings

logger = logging.getLogger("nokvo_one.in_call_summary")

_SYSTEM = (
    "You maintain a running summary of a live phone call for the agent's own awareness "
    "(internal context, not spoken to the caller). Rewrite the summary so it incorporates "
    "the new exchange lines. Keep it tight and factual.\n"
    "Capture: the caller's intent, mood / objections, what the agent has offered / decided / "
    "promised, key details given, and any open threads or unresolved questions.\n"
    "Summarize ONLY what is stated in the lines — never invent. Write in English (it is "
    "internal context). Output ONLY the updated summary, no preamble."
)


async def fold(prior_summary: str, evicted_turns: list[dict[str, str]], *, language: str = "en") -> str:
    """Fold ``evicted_turns`` into ``prior_summary`` and return the updated summary.

    Best-effort: returns ``prior_summary`` unchanged on any error / no input.
    """
    prior = (prior_summary or "").strip()
    turns = [t for t in (evicted_turns or []) if isinstance(t, dict) and t.get("content")]
    if not turns:
        return prior
    if not settings.IN_CALL_SUMMARY_ENABLED:
        return prior
    try:
        from app.services.call_condenser_service import _render_transcript
        from app.services.nokvo_one_voice_pipeline import AzureGroundedLLM

        new_lines = _render_transcript(turns)
        user = (
            (f"Running summary so far:\n{prior}\n\n" if prior else "No summary yet.\n\n")
            + f"New exchange lines to fold in:\n{new_lines}\n\n"
            + f"Return the updated summary in at most {settings.IN_CALL_SUMMARY_MAX_TOKENS} tokens."
        )
        messages = [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]
        updated = await AzureGroundedLLM.complete_nano(
            messages, max_tokens=settings.IN_CALL_SUMMARY_MAX_TOKENS
        )
        updated = (updated or "").strip()
        return updated or prior
    except Exception:
        logger.debug("in-call summary fold failed; keeping prior summary", exc_info=True)
        return prior


async def maybe_update(tenant_res, call_id, *, language: str = "en") -> None:
    """Fire-and-forget after a turn: if the call has grown past the condense
    window, fold the newly-evicted messages into the rolling summary and persist.

    No-op until there are messages to condense. A race with the main turn's
    memory save is self-healing: a clobber leaves ``summarized_turns`` unadvanced,
    so the next call re-folds the same messages (idempotent). Never raises.
    """
    if not settings.IN_CALL_SUMMARY_ENABLED or not call_id:
        return
    try:
        from app.services.agent_session_store import AgentSessionStore
        from app.services.conversational_memory import load_memory, save_memory

        history = await AgentSessionStore.get_history(tenant_res, call_id) or []
        boundary = len(history) - int(settings.IN_CALL_SUMMARY_CONDENSE_WINDOW)
        if boundary <= 0:
            return
        memory = await load_memory(tenant_res, call_id)
        start = max(0, int(getattr(memory, "summarized_turns", 0)))
        if boundary <= start:
            return  # nothing newly evicted
        evicted = history[start:boundary]
        if not evicted:
            return
        memory.rolling_summary = await fold(memory.rolling_summary, evicted, language=language)
        memory.summarized_turns = boundary
        await save_memory(tenant_res, call_id, memory)
    except Exception:
        logger.debug("in-call summary maybe_update failed", exc_info=True)
