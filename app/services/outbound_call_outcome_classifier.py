"""End-of-call outcome classifier for outbound test sessions.

The tester records a short transcript (user turn + agent reply pairs).
Once the call ends we ask a small LLM to classify the lead's interest
level into one of three buckets, which decides where the lead lands
in the Leads page:

  * ``interested``     → filed under that campaign's tab
  * ``partial``        → filed under the global "Uncategorized" tab
                         (covers "call me later", "send details", soft yes)
  * ``not_interested`` → not stored at all

A timeout, parse error, or LLM outage falls back to ``partial`` (the
safest non-destructive bucket: the operator can still see the call,
nothing is silently lost, and nothing is misfiled under a campaign).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.models.tenant_resources import TenantResources

logger = logging.getLogger(__name__)


OUTCOME_INTERESTED = "interested"
OUTCOME_PARTIAL = "partial"
OUTCOME_NOT_INTERESTED = "not_interested"

_VALID_OUTCOMES = {OUTCOME_INTERESTED, OUTCOME_PARTIAL, OUTCOME_NOT_INTERESTED}


_CLASSIFIER_SYSTEM = (
    "You classify the outcome of an outbound sales/outreach phone call.\n"
    "You will receive a short transcript of an admin rehearsing the call by "
    "playing the lead. Decide how interested the LEAD was in what the agent "
    "was pitching.\n\n"
    "Pick exactly one of three outcomes:\n"
    "  - interested: the lead asked to proceed, confirmed a next step, said yes, "
    "asked to book, requested a demo / site visit / meeting, gave their name "
    "and contact, or otherwise showed clear buying intent.\n"
    "  - partial: the lead is uncommitted but open. Examples: 'call me later', "
    "'send me details', 'maybe next week', 'let me think', soft yes, asked "
    "questions but didn't commit.\n"
    "  - not_interested: the lead refused, said no, asked not to be called, "
    "said wrong number, hung up early, or was openly hostile.\n\n"
    "Output STRICT JSON: {\"outcome\": \"interested|partial|not_interested\", "
    "\"reason\": \"<one short sentence quoting the deciding signal>\"}\n"
    "If the transcript is empty or unclear, output partial. "
    "Do not output anything other than the JSON."
)


@dataclass(slots=True)
class CallOutcome:
    outcome: str
    reason: str

    @property
    def should_create_lead(self) -> bool:
        return self.outcome in (OUTCOME_INTERESTED, OUTCOME_PARTIAL)

    @property
    def is_uncategorized(self) -> bool:
        return self.outcome == OUTCOME_PARTIAL


def _safe_default(reason: str) -> CallOutcome:
    return CallOutcome(outcome=OUTCOME_PARTIAL, reason=reason[:200])


def _parse_response(text: str) -> CallOutcome | None:
    raw = (text or "").strip()
    if not raw:
        return None
    # Some models wrap JSON in code fences or trailing prose; pull the first
    # JSON object out by brace-matching so a noisy reply still parses.
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if match:
        raw = match.group(0)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    outcome = str(data.get("outcome") or "").strip().lower()
    if outcome not in _VALID_OUTCOMES:
        return None
    return CallOutcome(
        outcome=outcome,
        reason=str(data.get("reason") or "")[:200],
    )


def _format_transcript(turns: list[dict[str, Any]] | None) -> str:
    if not turns:
        return ""
    lines: list[str] = []
    for turn in turns[-20:]:  # cap at 20 turn-pairs; older context is fluff
        if not isinstance(turn, dict):
            continue
        user_text = str(turn.get("query") or turn.get("user") or "").strip()
        agent_text = str(turn.get("answer") or turn.get("agent") or "").strip()
        if not user_text and not agent_text:
            continue
        if user_text:
            lines.append(f"lead: {user_text[:300]}")
        if agent_text:
            lines.append(f"agent: {agent_text[:300]}")
    return "\n".join(lines)


def _ls_chain(name: str):
    try:
        from langsmith.run_helpers import traceable
        return traceable(name=name, run_type="chain")
    except Exception:
        return lambda fn: fn


@_ls_chain("outcome_classifier")
async def classify_outbound_outcome(
    tenant_res: TenantResources,
    *,
    transcript_turns: list[dict[str, Any]] | None,
    campaign_name: str | None = None,
    campaign_goal: str | None = None,
    timeout_ms: int = 4000,
) -> CallOutcome:
    """Classify the outcome of a finished outbound test call.

    On any error / timeout / parse failure returns ``partial`` — the
    safe non-destructive bucket. The router translates that into an
    Uncategorized lead so the operator never silently loses a session.
    """
    transcript = _format_transcript(transcript_turns)
    if not transcript:
        return _safe_default("empty transcript")

    context_block = ""
    if campaign_name:
        context_block += f"Campaign: {campaign_name[:120]}\n"
    if campaign_goal:
        context_block += f"Goal: {campaign_goal[:300]}\n"
    if context_block:
        context_block += "\n"

    user_msg = (
        f"{context_block}Transcript (oldest first):\n{transcript[:4000]}"
    )

    from app.services.nokvo_one_voice_pipeline import (
        AzureGroundedLLM,
        NokvoOneAgentRuntimeError,
    )

    try:
        raw = await asyncio.wait_for(
            AzureGroundedLLM.complete(
                tenant_res,
                [
                    {"role": "system", "content": _CLASSIFIER_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=120,
            ),
            timeout=timeout_ms / 1000,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"NOKVO-OUTBOUND-OUTCOME: timeout after {timeout_ms}ms; defaulting partial"
        )
        return _safe_default("classifier timeout")
    except NokvoOneAgentRuntimeError as exc:
        logger.warning(f"NOKVO-OUTBOUND-OUTCOME: runtime error: {exc!r}")
        return _safe_default(f"runtime: {exc}"[:120])
    except Exception as exc:
        logger.warning(f"NOKVO-OUTBOUND-OUTCOME: unexpected error: {exc!r}")
        return _safe_default(f"error: {exc}"[:120])

    parsed = _parse_response(raw or "")
    if parsed is None:
        logger.warning(
            f"NOKVO-OUTBOUND-OUTCOME: unparseable response: {(raw or '')[:200]!r}"
        )
        return _safe_default("unparseable JSON")
    return parsed
