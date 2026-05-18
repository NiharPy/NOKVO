"""Deterministic policy decision engine.

Given a classified intent, the user's text, the tenant's active policy cards
and (optionally) live order context, produce an exact answer that preserves
all numeric conditions verbatim. No LLM. No retrieval. No hedge words.

This is the layer that prevents the "yes you can be refunded after 5
minutes" failure mode — it physically cannot say "yes" when the policy has
conditional windows. It either answers exactly per the matching condition,
or it asks for the missing status.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.services.fast_intent_router import (
    INTENT_CANCELLATION_REQUEST,
    INTENT_REFUND_ELIGIBILITY,
    TOPIC_CANCELLATION,
    TOPIC_REFUND,
)
from app.services.policy_card_extractor import (
    INTENT_CUSTOMER_CANCEL,
    OUTCOME_EIGHTY_PERCENT_REFUND,
    OUTCOME_FIFTY_PERCENT_REFUND,
    OUTCOME_FULL_REFUND_ORIGINAL,
    OUTCOME_FULL_REFUND_WALLET,
    OUTCOME_NO_CANCELLATION,
    STATUS_ACCEPTED_NOT_PREPARING,
    STATUS_ANY,
    STATUS_DELIVERED,
    STATUS_NOT_ACCEPTED,
    STATUS_OUT_FOR_DELIVERY,
    STATUS_PREPARING,
)


# ── Decision codes (stable for logging/analytics) ────────────────────────────
DEC_NO_POLICY_CARD = "no_policy_card"
DEC_MATRIX_RESPONSE = "matrix_response"
DEC_LIVE_STATUS_NEEDED = "live_status_needed"
DEC_EXACT_MATCH = "exact_match"
DEC_NO_MATCH = "no_matching_condition"


@dataclass
class PolicyDecision:
    answered: bool
    requires_live_status: bool
    needs_clarification: bool
    answer: str | None
    decision_code: str | None
    matched_card_id: str | None
    matched_condition: dict[str, Any] | None
    safe_to_cache: bool
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "answered": self.answered,
            "requires_live_status": self.requires_live_status,
            "needs_clarification": self.needs_clarification,
            "answer": self.answer,
            "decision_code": self.decision_code,
            "matched_card_id": self.matched_card_id,
            "matched_condition": self.matched_condition,
            "safe_to_cache": self.safe_to_cache,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


# ── Helpers ──────────────────────────────────────────────────────────────────
_CANCEL_CARD_INTENTS = {INTENT_CUSTOMER_CANCEL, INTENT_REFUND_ELIGIBILITY}


def _select_cards(
    cards: list[dict[str, Any]],
    *,
    intent: str,
    topic: str,
    current_policy_version: str | None,
) -> list[dict[str, Any]]:
    """Return active, approved cards relevant to this intent/topic on the
    current policy version (or unversioned legacy cards)."""
    out: list[dict[str, Any]] = []
    for card in cards or []:
        if card.get("card_type") != "policy_rule":
            continue
        if card.get("approval_status") not in (None, "approved"):
            continue
        if card.get("status") not in (None, "active", "ok"):
            continue
        card_pv = card.get("policy_version")
        if current_policy_version and card_pv and card_pv != current_policy_version:
            continue
        # Topic must match (cancellation/refund cards both serve cancellation+refund intents).
        if topic in (TOPIC_CANCELLATION, TOPIC_REFUND) and card.get("topic") not in (TOPIC_CANCELLATION, TOPIC_REFUND):
            continue
        if intent in (INTENT_CANCELLATION_REQUEST, INTENT_REFUND_ELIGIBILITY) and card.get("intent") not in _CANCEL_CARD_INTENTS:
            continue
        out.append(card)
    return out


def _condition_matches(condition: dict[str, Any], live: dict[str, Any]) -> tuple[bool, int]:
    """Return (matches, specificity_score). Higher specificity wins on tiebreaks.

    Critically: a card with NO window AND status=ANY is a catch-all that
    would beat any well-specified rule with specificity 0. Never let that
    happen — those cards are almost always extraction errors (the policy
    doc said something like "Cancellation is not allowed" without context).
    Treat them as non-matching so the route falls through to the LLM, which
    can reason from the actual source text instead of giving a wrong canned
    response.
    """
    age = live.get("order_age_minutes")
    status = (live.get("status") or "").strip() or None

    cond_min = condition.get("order_age_minutes_min")
    cond_max = condition.get("order_age_minutes_max")
    cond_status = (condition.get("status") or STATUS_ANY)

    # Reject catch-all (no window AND status=ANY). It's almost certainly an
    # extraction artifact; letting it match anything produces canned
    # wrong-answer responses like "Cancellation is not allowed at this stage"
    # for "I cancelled within 5 minutes, can I get a refund?".
    if cond_min is None and cond_max is None and cond_status == STATUS_ANY:
        return False, 0

    specificity = 0

    if cond_min is not None or cond_max is not None:
        if age is None:
            return False, 0
        lo = cond_min if cond_min is not None else 0
        hi = cond_max if cond_max is not None else float("inf")
        # Half-open at the upper bound to avoid double-matching at boundaries
        # (e.g. age=5 belongs to the 5-10 window, not the 0-5 one).
        if not (lo <= age < hi if hi != float("inf") else age >= lo):
            return False, 0
        specificity += 2

    if cond_status != STATUS_ANY:
        if status is None:
            # A status-specific condition cannot match without a status.
            return False, 0
        if status != cond_status:
            return False, 0
        specificity += 2

    return True, specificity


def _format_matrix_summary(cards: list[dict[str, Any]]) -> str:
    """When no live context is available, render a precise summary that
    covers every condition without hedging."""
    parts: list[str] = []
    seen: set[str] = set()
    for card in cards:
        for cond in card.get("conditions") or []:
            msg = (cond.get("customer_message") or "").strip()
            if not msg or msg in seen:
                continue
            seen.add(msg)
            parts.append(msg)
    if not parts:
        return ""
    return " ".join(parts)


def _phrase_outcome(condition: dict[str, Any]) -> str:
    """Compose a definite answer for an exact-matched condition."""
    msg = (condition.get("customer_message") or "").strip()
    if msg:
        return msg
    # Last-resort canonical mapping if a card lacked a message.
    outcome = condition.get("outcome")
    return {
        OUTCOME_FULL_REFUND_ORIGINAL: "You are eligible for a full refund to your original payment method.",
        OUTCOME_FULL_REFUND_WALLET: "You are eligible for a full refund to your wallet.",
        OUTCOME_EIGHTY_PERCENT_REFUND: "80% is refundable; 20% is retained as a cancellation fee.",
        OUTCOME_FIFTY_PERCENT_REFUND: "Only 50% is refundable at this stage.",
        OUTCOME_NO_CANCELLATION: "Cancellation is not allowed at this stage.",
    }.get(outcome, "Please contact support to confirm your eligibility.")


def _user_asked_general_policy(user_text: str) -> bool:
    """Heuristic: 'What's the cancellation policy?' vs 'Cancel my order'."""
    text = (user_text or "").lower()
    return bool(
        re.search(r"\b(policy|rules?|terms|how\s+(?:do|does|long|much)|window|when|can\s+i|am\s+i|after|within|before|eligibility)\b", text)
    )


# ── Engine ───────────────────────────────────────────────────────────────────
class PolicyDecisionEngine:
    @staticmethod
    def evaluate(
        intent: str,
        topic: str,
        user_text: str,
        policy_cards: list[dict[str, Any]],
        live_context: dict[str, Any] | None = None,
        *,
        current_policy_version: str | None = None,
    ) -> PolicyDecision:
        if intent not in (INTENT_CANCELLATION_REQUEST, INTENT_REFUND_ELIGIBILITY):
            return PolicyDecision(
                answered=False,
                requires_live_status=False,
                needs_clarification=False,
                answer=None,
                decision_code=None,
                matched_card_id=None,
                matched_condition=None,
                safe_to_cache=False,
                reason="intent not handled by decision engine",
            )

        cards = _select_cards(
            policy_cards or [],
            intent=intent,
            topic=topic,
            current_policy_version=current_policy_version,
        )
        if not cards:
            # No structured policy data for this tenant yet — likely a tenant
            # whose KB was ingested before policy_card extraction existed, or
            # one that hasn't uploaded a cancellation/refund doc. We
            # deliberately ANSWER here (answered=True) with a status-check
            # response so the turn does not fall through to a generic
            # "I do not have enough information" refusal. The bootstrap path
            # is to call AgentKnowledgeService.rechunk_documents on this
            # tenant, which will (re)generate policy cards from existing
            # chunks.
            return PolicyDecision(
                answered=True,
                requires_live_status=True,
                needs_clarification=True,
                answer=(
                    "I need to check your order status to confirm cancellation or refund "
                    "eligibility. Could you share your order number? Otherwise I can "
                    "transfer you to support."
                ),
                decision_code=DEC_NO_POLICY_CARD,
                matched_card_id=None,
                matched_condition=None,
                safe_to_cache=False,
                reason="no active policy cards for this tenant/version",
            )

        # ── 1) Exact match on live context ───────────────────────────────
        if live_context and (live_context.get("order_age_minutes") is not None or live_context.get("status")):
            best: tuple[int, dict[str, Any], dict[str, Any]] | None = None
            for card in cards:
                for cond in card.get("conditions") or []:
                    matched, score = _condition_matches(cond, live_context)
                    if not matched:
                        continue
                    if best is None or score > best[0]:
                        best = (score, card, cond)
            if best:
                _, card, cond = best
                return PolicyDecision(
                    answered=True,
                    requires_live_status=False,
                    needs_clarification=False,
                    answer=_phrase_outcome(cond),
                    decision_code=DEC_EXACT_MATCH,
                    matched_card_id=card.get("id") or card.get("source_section_id"),
                    matched_condition=cond,
                    safe_to_cache=False,  # tied to a specific order
                    reason="live context matched a specific condition",
                )
            # Live context provided but no condition matched — return matrix
            # so the agent doesn't fabricate.
            summary = _format_matrix_summary(cards)
            if summary:
                return PolicyDecision(
                    answered=True,
                    requires_live_status=False,
                    needs_clarification=False,
                    answer=summary + " Please contact support to confirm the exact eligibility for your order.",
                    decision_code=DEC_NO_MATCH,
                    matched_card_id=None,
                    matched_condition=None,
                    safe_to_cache=False,
                    reason="no condition matched live context; returned full matrix",
                )

        # ── 2) No live context but general policy question → return matrix ──
        if _user_asked_general_policy(user_text):
            summary = _format_matrix_summary(cards)
            if summary:
                return PolicyDecision(
                    answered=True,
                    requires_live_status=False,
                    needs_clarification=False,
                    answer=summary,
                    decision_code=DEC_MATRIX_RESPONSE,
                    matched_card_id=cards[0].get("id") or cards[0].get("source_section_id"),
                    matched_condition=None,
                    safe_to_cache=False,  # policy answers are version-pinned; stale risk
                    reason="general policy question without live context",
                )

        # ── 3) Personal action without live context → ask for status ──
        return PolicyDecision(
            answered=True,
            requires_live_status=True,
            needs_clarification=True,
            answer=(
                "I need to check your order status first because cancellation depends on both "
                "how long ago the order was placed and whether the restaurant has accepted or "
                "started preparing it. Could you share your order number?"
            ),
            decision_code=DEC_LIVE_STATUS_NEEDED,
            matched_card_id=None,
            matched_condition=None,
            safe_to_cache=False,
            reason="personal cancellation/refund without live context",
        )


# ── Live-context fetcher stub ────────────────────────────────────────────────
async def fetch_live_order_context(
    tenant_id: str,
    session_id: str | None,
    user_text: str,
) -> dict[str, Any] | None:
    """Stub for CRM/order-lookup integration.

    TODO: connect to the tenant's CRM / order service via the integration
    layer (see app/services/crm_integration_service.py and
    app/services/erp_integration_service.py). For now, returns None so the
    decision engine falls back to the matrix or status-check response.
    """
    return None


# ── Conversation-history extractor ───────────────────────────────────────────
# Without a CRM lookup, the next-best signal is what the caller already SAID
# earlier in the call. If the agent asked "how long ago did you place it?" and
# the caller answered "3 minutes ago", we want the policy engine to use that
# on the next turn ("can I cancel?") without re-asking. The patterns here are
# deliberately tolerant — they have to fire on STT-produced text which is
# often missing punctuation, lower-cased, with Indian-English variations.

# Speakers say numbers as WORDS far more often than digits in voice. STT
# usually transcribes "five minutes" as "five minutes", not "5 minutes".
# These maps cover the values that appear in cancellation windows (0-30 min).
_WORD_NUMBERS: dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "thirty": 30,
    # Casual quantifiers
    "a": 1, "an": 1, "couple": 2, "few": 3,
}
# Number pattern usable inside the age regexes — captures either digits or a
# spelled-out word number.
_NUM = r"(?:\d{1,3}|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|a|an|couple|few)"
_INDIC_MINUTE = (
    r"(?:"
    r"మినిట్[ఀ-౿]*|మినిట్స్[ఀ-౿]*|నిమిష[ఀ-౿]*|"  # Telugu
    r"मिनट[ऀ-ॿ]*|"                                  # Hindi / Marathi
    r"நிமிட[஀-௿]*|"                                  # Tamil
    r"ನಿಮಿಷ[ಀ-೿]*|"                                  # Kannada
    r"മിനിറ്റ്[ഀ-ൿ]*|"                                # Malayalam
    r"মিনিট[ঀ-৿]*|"                                  # Bengali
    r"મિનિટ[઀-૿]*|"                                  # Gujarati
    r"ਮਿੰਟ[਀-੿]*"                                    # Punjabi
    r")"
)


def _parse_number(token: str) -> int | None:
    """Map either a digit string or a spelled-out word to int."""
    if not token:
        return None
    token = token.strip().lower()
    if token.isdigit():
        try:
            return int(token)
        except ValueError:
            return None
    return _WORD_NUMBERS.get(token)


_AGE_PATTERNS = [
    # "5 minutes ago", "five mins back", "ten minute before"
    re.compile(rf"\b({_NUM})\s*(?:minute|min)s?\s*(?:ago|back|before)\b", re.IGNORECASE),
    # "after 5 minutes I cancelled" / "after about five mins"
    re.compile(rf"\bafter\s+(?:about\s+)?({_NUM})\s*(?:minute|min)s?\b", re.IGNORECASE),
    # "5 minutes later/after/passed/in"
    re.compile(rf"\b({_NUM})\s*(?:minute|min)s?\s*(?:later|after|passed|in)\b", re.IGNORECASE),
    # "ordered 3 mins ago" / "placed five minutes ago"
    re.compile(
        rf"\b(?:order(?:ed)?|place(?:d)?|put|made)\s+(?:it|the\s+order)?\s*(?:about\s+)?({_NUM})\s*(?:minute|min)s?\b",
        re.IGNORECASE,
    ),
    # "I cancelled (it) after/in/within X minutes" — cancel verb is the anchor
    re.compile(
        rf"\bcancel(?:led|ed)?\s+(?:it\s+)?(?:after|in|within)\s+({_NUM})\s*(?:minute|min)s?\b",
        re.IGNORECASE,
    ),
    # "within X minutes" — strong signal in a customer-service voice context,
    # so we no longer require a cancel/refund/order anchor nearby. STT splits
    # sentences with periods which would defeat the lookahead anyway.
    re.compile(rf"\bwithin\s+({_NUM})\s*(?:minute|min)s?\b", re.IGNORECASE),
    # "couple of minutes" / "a couple minutes" / "few minutes"
    re.compile(
        rf"\b(?:a\s+)?(couple|few)\s+(?:of\s+)?(?:minute|min)s?\b",
        re.IGNORECASE,
    ),
    # bare "just five minutes"
    re.compile(rf"\bjust\s+({_NUM})\s*(?:minute|min)s?\b", re.IGNORECASE),
    # Native-script STT often preserves the digit but renders "minutes" in the
    # caller's language: "5 మినిట్స్కే", "5 मिनट", etc.
    re.compile(rf"({_NUM})\s*{_INDIC_MINUTE}", re.IGNORECASE),
]
_JUST_NOW_RE = re.compile(
    r"\b(?:just\s+now|right\s+now|literally\s+now|seconds?\s+ago|moments?\s+ago)\b",
    re.IGNORECASE,
)
_HOUR_RE = re.compile(r"\b(\d{1,2})\s*(?:hour|hr)s?\s*(?:ago|back)?\b", re.IGNORECASE)

_STATUS_NOT_ACCEPTED_HISTORY_RE = re.compile(
    r"\b(?:restaurant|store|shop|they)?\s*(?:has(?:n['’]?t)?|hasn['’]?t|not\s+yet|did(?:n['’]?t)?\s+(?:yet\s+)?)\s*accept(?:ed)?\b",
    re.IGNORECASE,
)
_STATUS_ACCEPTED_NOT_PREPARING_HISTORY_RE = re.compile(
    r"\baccept(?:ed)?\b[^.\n]{0,40}\b(?:not|haven['’]?t|hasn['’]?t|wasn['’]?t|didn['’]?t)\b[^.\n]{0,15}\b(?:start(?:ed)?\s+)?prepar",
    re.IGNORECASE,
)
_STATUS_PREPARING_HISTORY_RE = re.compile(
    r"\b(?:it[' ]?s\s+|is\s+|was\s+|currently\s+|being\s+|are\s+|started\s+)prepar(?:ing|ation)\b",
    re.IGNORECASE,
)
_STATUS_OUT_FOR_DELIVERY_HISTORY_RE = re.compile(
    r"\b(?:out\s+for\s+delivery|on\s+the\s+way|dispatched|rider\s+(?:has\s+)?(?:picked|got)|already\s+left)\b",
    re.IGNORECASE,
)
_STATUS_DELIVERED_HISTORY_RE = re.compile(
    r"\b(?:already\s+delivered|received\s+(?:it|the\s+order)|got\s+(?:the\s+)?(?:food|order|package))\b",
    re.IGNORECASE,
)
# Positive accepted — caller said "the restaurant accepted it" / "they accepted
# my order" / "accepted, accepted" without any "not"/"haven't" near it. We
# treat plain accepted as accepted_not_preparing because that's the user-most-
# favourable interpretation; if the user separately mentions preparing, the
# preparing pattern above wins on the priority ladder in extract_*.
_STATUS_ACCEPTED_POSITIVE_HISTORY_RE = re.compile(
    r"\b(?:restaurant|they|store|shop|order)?\s*(?:had|has|was|is|already\s+)?\s*accept(?:ed)?\b",
    re.IGNORECASE,
)


def _scan_user_turns(history: Iterable[dict[str, Any]]) -> list[str]:
    """Return user-turn text from history, most recent first.

    Past assistant turns are ignored — we want what the CALLER said, not what
    we asked. Most-recent-first because newer information should win on conflict
    (e.g. caller corrects an earlier mis-statement).
    """
    rows: list[str] = []
    for entry in history or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("role") != "user":
            continue
        text = str(entry.get("content") or "").strip()
        if text:
            rows.append(text)
    return list(reversed(rows))


def extract_live_context_from_history(
    history: Iterable[dict[str, Any]] | None,
    *,
    current_user_text: str | None = None,
) -> dict[str, Any] | None:
    """Best-effort extraction of ``{order_age_minutes, status}`` from prior
    user turns (and the current turn if provided). Returns ``None`` when
    nothing actionable is detected so callers can fall back to "ask the
    caller" rather than running the matrix on garbage data.
    """
    user_turns = _scan_user_turns(history or [])
    if current_user_text:
        # The current turn isn't yet appended to history; check it first
        # because it's the freshest signal.
        user_turns = [current_user_text] + user_turns

    age: int | None = None
    status: str | None = None

    for text in user_turns:
        if age is None:
            if _JUST_NOW_RE.search(text):
                age = 0
            else:
                for pattern in _AGE_PATTERNS:
                    m = pattern.search(text)
                    if not m:
                        continue
                    parsed = _parse_number(m.group(1))
                    if parsed is not None:
                        age = parsed
                        break
            if age is None:
                m = _HOUR_RE.search(text)
                if m:
                    try:
                        # Hours far exceed any cancellation window; surfacing
                        # it lets the engine match a "no_cancellation" rule.
                        age = int(m.group(1)) * 60
                    except (TypeError, ValueError):
                        pass

        if status is None:
            if _STATUS_OUT_FOR_DELIVERY_HISTORY_RE.search(text):
                status = "out_for_delivery"
            elif _STATUS_DELIVERED_HISTORY_RE.search(text):
                status = "delivered"
            elif _STATUS_ACCEPTED_NOT_PREPARING_HISTORY_RE.search(text):
                status = "accepted_not_preparing"
            elif _STATUS_PREPARING_HISTORY_RE.search(text):
                status = "preparing"
            elif _STATUS_NOT_ACCEPTED_HISTORY_RE.search(text):
                status = "restaurant_not_accepted"
            elif _STATUS_ACCEPTED_POSITIVE_HISTORY_RE.search(text):
                # Caller said the restaurant accepted, no preparing mention.
                # User-favourable default: accepted but not preparing yet.
                # Sanity check — make sure no "not"/"hasn't" sneaks in via
                # informal contractions the negation pattern missed.
                if not re.search(r"\b(not|never|no)\s+accept", text, re.IGNORECASE):
                    status = "accepted_not_preparing"

        if age is not None and status is not None:
            break

    if age is None and status is None:
        return None
    out: dict[str, Any] = {}
    if age is not None:
        out["order_age_minutes"] = age
    if status is not None:
        out["status"] = status
    out["source"] = "conversation_history"
    return out
