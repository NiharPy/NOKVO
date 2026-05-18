"""Tier-2 intent classification via a fast LLM call.

The regex-based ``FastIntentRouter`` (Tier 1) catches the obvious cases —
bare greetings, thanks, the cancellation verb, audio checks. It misses
everything else: paraphrases ("could you give my money back?"), code-switched
Indian-English ("nenu cancel cheyalanukunna"), STT mis-transcriptions ("can
sell my order"), and complaints whose phrasing is open-ended ("the rider
was rude to me").

This classifier closes that gap. One small Azure OpenAI call returns a
structured intent + retrieval hint + sentiment. Capped at 800ms with a
safe-default fallback so a slow or down classifier can never block a turn.

Outputs are CONSUMED by ``NokvoOneVoicePipeline._route_turn`` to decide:
  - Should we hit Qdrant? (needs_kb)
  - Should we use the deterministic policy engine? (intent ∈ cancel/refund + sensitive)
  - Should the LLM respond in conversational mode (no chunks) or factual mode?
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

from app.models.tenant_resources import TenantResources


# ── Intent vocabulary (superset of FastIntentRouter's, deliberately small) ──
INTENT_SMALLTALK = "smalltalk"
INTENT_KB_QUESTION = "kb_question"
INTENT_COMPLAINT = "complaint"
INTENT_CANCELLATION_REQUEST = "cancellation_request"
INTENT_REFUND_ELIGIBILITY = "refund_eligibility"
INTENT_ORDER_STATUS = "order_status"
INTENT_ESCALATION = "escalation"
INTENT_OUT_OF_SCOPE = "out_of_scope"
INTENT_UNCLEAR = "unclear"

_SENSITIVE = {
    INTENT_CANCELLATION_REQUEST,
    INTENT_REFUND_ELIGIBILITY,
    INTENT_COMPLAINT,
    INTENT_ORDER_STATUS,
}


@dataclass(slots=True)
class ClassifiedIntent:
    intent: str
    needs_kb: bool
    sensitive: bool
    sentiment: str  # positive | neutral | negative | frustrated | curious
    reason: str = ""
    fallback: bool = False  # set when the LLM call failed and we returned a safe default

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "needs_kb": self.needs_kb,
            "sensitive": self.sensitive,
            "sentiment": self.sentiment,
            "reason": self.reason,
            "fallback": self.fallback,
        }


_CLASSIFIER_SYSTEM = """You classify a single user voice-message into ONE category. The user is calling a customer-facing voice agent that ANSWERS QUESTIONS ABOUT A BUSINESS (a clinic, restaurant, store, service provider, etc.). The user may speak in English, Hindi, Telugu, Tamil, Bengali, Marathi, Gujarati, Kannada, Malayalam, Punjabi, or code-switched mixtures.

# IN-SCOPE topics (anything about THIS business or the caller's account/order with this business):
- Appointments, bookings, scheduling, availability — "can I get an appointment", "is the doctor available", "what time slots are open", "today's schedule"
- Policies, prices, hours, contact, location, services offered — "what are your timings", "where are you", "do you have X service", "how much is Y"
- The caller's order / account / past interactions — "where's my order", "I cancelled my order", "I got the wrong item"
- Refunds, cancellations, complaints, escalations
- Clinic / doctor / staff names IF the caller is asking about THIS business's staff
- ANYTHING that a frontline employee of this business would answer.

# OUT-OF-SCOPE topics (truly outside the business's domain):
- Weather, news, sports, geography, current events, general world facts
- Other businesses or competitors
- Personal opinions on unrelated topics
- "What's the capital of France?", "Who won the match?", "What's the temperature in Mumbai?"

# Categories
- "smalltalk": chitchat, greeting, gratitude, brief acknowledgment, casual remark, audio check, emotional reaction. NO factual question. Examples: "hi", "okay thanks", "yeah", "hello can you hear me", "I had a rough day", "alright bye".
- "kb_question": user asks for a fact about THIS business — policy, price, hours, availability, location, services, products, processes, staff names, anything an employee would answer. **DEFAULT to this category for anything operationally about the business.** Examples: "what's your refund policy?", "can I get an appointment today?", "which clinic is this?", "what doctors do you have?", "what's the timing?".
- "cancellation_request": user wants to cancel their order/booking. Examples: "cancel my order", "I want to cancel my appointment".
- "refund_eligibility": user is asking about getting a refund.
- "order_status": user is asking where their order is / when it arrives.
- "complaint": user is unhappy / frustrated about a specific incident with this business.
- "escalation": user wants a human agent. Examples: "transfer me to a human", "I want to speak to a manager".
- "out_of_scope": user is asking about something genuinely UNRELATED to this business (weather, news, sports, geography, other companies, world facts). **Be VERY conservative — only use this when the question has NO plausible connection to a business operation.** A clinic patient asking about appointment availability is NOT out-of-scope.
- "unclear": cannot confidently classify; default behavior should be to try kb_question retrieval.

# Decision bias
- When uncertain between kb_question and out_of_scope, choose **kb_question**. The retrieval layer will surface relevant info if it's there, and refuse cleanly if not.
- When uncertain between smalltalk and kb_question, choose **kb_question** if there's any substantive ask in the utterance.
- Reserve out_of_scope ONLY for clearly external topics (weather, news, world facts).

Return STRICT JSON, no extra text, no markdown:
{"intent":"...","needs_kb":true|false,"sensitive":true|false,"sentiment":"positive|neutral|negative|frustrated|curious","reason":"≤20 words"}

# Rules
- needs_kb = true for kb_question, complaint, unclear, AND anything you're not sure about. Better to do unnecessary retrieval than miss a real query.
- sensitive = true for cancellation_request, refund_eligibility, order_status, complaint.
- For smalltalk: needs_kb=false, sensitive=false.
- For out_of_scope: needs_kb=false, sensitive=false."""


def _safe_default(reason: str) -> ClassifiedIntent:
    """Conservative default when the classifier fails. We try retrieval
    (so we don't accidentally drop a real question), but we DON'T flag it
    sensitive (so we don't trigger the policy engine on garbage)."""
    return ClassifiedIntent(
        intent=INTENT_UNCLEAR,
        needs_kb=True,
        sensitive=False,
        sentiment="neutral",
        reason=reason,
        fallback=True,
    )


_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*?\}")


def _parse_response(text: str) -> ClassifiedIntent | None:
    """Tolerant JSON parser — strips code fences, finds the first JSON object."""
    if not text:
        return None
    cleaned = text.strip()
    # Strip code fences if the model wrapped its JSON in them
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    m = _JSON_OBJECT_RE.search(cleaned)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    intent = str(data.get("intent") or "").strip().lower()
    if not intent:
        return None
    sensitive = bool(data.get("sensitive", intent in _SENSITIVE))
    needs_kb = bool(data.get("needs_kb", intent == INTENT_KB_QUESTION))
    sentiment = str(data.get("sentiment") or "neutral").strip().lower()
    if sentiment not in {"positive", "neutral", "negative", "frustrated", "curious"}:
        sentiment = "neutral"
    return ClassifiedIntent(
        intent=intent,
        needs_kb=needs_kb,
        sensitive=sensitive or (intent in _SENSITIVE),
        sentiment=sentiment,
        reason=str(data.get("reason") or "")[:200],
    )


class LLMIntentClassifier:
    @staticmethod
    async def classify(
        text: str,
        *,
        tenant_res: TenantResources,
        history: list[dict[str, str]] | None = None,
        timeout_ms: int = 800,
    ) -> ClassifiedIntent:
        cleaned = (text or "").strip()
        if not cleaned:
            return ClassifiedIntent(
                intent=INTENT_UNCLEAR,
                needs_kb=False,
                sensitive=False,
                sentiment="neutral",
                reason="empty input",
            )

        # Build a tight conversation snippet so the classifier sees pronoun
        # references / what the agent just asked. Last 4 turns is plenty.
        history_block = ""
        if history:
            recent = [h for h in history[-4:] if h.get("content")]
            if recent:
                history_block = "Recent conversation (oldest first):\n" + "\n".join(
                    f"{(h.get('role') or 'user')}: {str(h.get('content'))[:200]}" for h in recent
                ) + "\n\n"

        user_msg = f"{history_block}Classify THIS new user message:\n{cleaned[:1500]}"

        # Local import — avoid a circular import at module load (the pipeline
        # imports us; we import the LLM client).
        from app.services.nokvo_one_voice_pipeline import AzureGroundedLLM, NokvoOneAgentRuntimeError

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
            print(f"[NOKVO-CLASSIFIER] timeout after {timeout_ms}ms; using safe default")
            return _safe_default("classifier timeout")
        except NokvoOneAgentRuntimeError as exc:
            print(f"[NOKVO-CLASSIFIER] runtime error: {exc!r}")
            return _safe_default(f"runtime: {exc}"[:120])
        except Exception as exc:
            print(f"[NOKVO-CLASSIFIER] unexpected error: {exc!r}")
            return _safe_default(f"error: {exc}"[:120])

        parsed = _parse_response(raw or "")
        if parsed is None:
            print(f"[NOKVO-CLASSIFIER] unparseable response: {(raw or '')[:200]!r}")
            return _safe_default("unparseable JSON")
        return parsed
