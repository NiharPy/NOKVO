"""Extract structured policy-rule cards from ingested document text.

Output format (per the architectural spec):

    {
      "card_type": "policy_rule",
      "topic": "cancellation",
      "intent": "customer_cancel_order",
      "requires_live_status": bool,
      "sensitive": bool,
      "source_section_title": str,
      "source_text": str,        # raw section text for audit/debugging
      "conditions": [
        {
          "order_age_minutes_min": int | None,
          "order_age_minutes_max": int | None,
          "status": "any" | "restaurant_not_accepted" | ...,
          "outcome": "full_refund_original_payment" | ...,
          "customer_message": str,
          "source_line": str,
        },
        ...
      ],
      ...
    }

Generic across tenants. The extractor walks any section whose heading or body
mentions cancellation/refund, scans line-by-line for time-window and
outcome/status fragments, and emits one condition per matching line. If
structured extraction yields nothing, the extractor still emits a single card
with the full section text and ``requires_live_status=True`` so the decision
engine can at least produce a status-check response instead of leaking to the
LLM.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any


# ── Canonical outcome / status vocabulary (shared with PolicyDecisionEngine) ─
OUTCOME_FULL_REFUND_ORIGINAL = "full_refund_original_payment"
OUTCOME_FULL_REFUND_WALLET = "full_refund_wallet"
OUTCOME_EIGHTY_PERCENT_REFUND = "eighty_percent_refund"
OUTCOME_FIFTY_PERCENT_REFUND = "fifty_percent_refund"
OUTCOME_NO_CANCELLATION = "no_cancellation"
OUTCOME_ESCALATE = "escalate_to_support"

STATUS_ANY = "any"
STATUS_NOT_ACCEPTED = "restaurant_not_accepted"
STATUS_ACCEPTED_NOT_PREPARING = "accepted_not_preparing"
STATUS_PREPARING = "preparing"
STATUS_OUT_FOR_DELIVERY = "out_for_delivery"
STATUS_DELIVERED = "delivered"


INTENT_CUSTOMER_CANCEL = "customer_cancel_order"
INTENT_REFUND_ELIGIBILITY = "refund_eligibility"

TOPIC_CANCELLATION = "cancellation"
TOPIC_REFUND = "refund"


# ── Heading detection ────────────────────────────────────────────────────────
_POLICY_HEADING_RE = re.compile(
    r"^(?:#+\s*|\s*)?(cancellation(?:\s+policy)?|refund(?:\s+policy)?|cancellation\s*&\s*refund|cancel\s*/\s*refund)(?:\s*[:\-–])?\s*$",
    re.IGNORECASE,
)
_GENERIC_HEADING_RE = re.compile(r"^(?:#+\s*)?[A-Z][A-Za-z0-9 /&\-]{2,80}\s*[:\-]?\s*$")

# ── Window patterns ──
_WITHIN_RE = re.compile(r"\bwithin\s+(\d+)\s*(?:minute|min|m)s?\b", re.IGNORECASE)
_BETWEEN_RE = re.compile(
    r"\b(?:between\s+)?(\d+)\s*(?:[-–]|to|and|&)\s*(\d+)\s*(?:minute|min|m)s?\b",
    re.IGNORECASE,
)
_AFTER_RE = re.compile(r"\bafter\s+(\d+)\s*(?:minute|min|m)s?\b", re.IGNORECASE)

# ── Outcome patterns ──
_FULL_REFUND_ORIGINAL_RE = re.compile(
    r"full\s+refund[^.\n]*?(?:original|same\s+method|source|originating)\b[^.\n]*?(?:payment|method|account)?",
    re.IGNORECASE,
)
_FULL_REFUND_WALLET_RE = re.compile(
    r"(?:full\s+refund[^.\n]*?wallet|wallet\s+refund|refund(?:ed)?\s+to[^.\n]*?wallet)",
    re.IGNORECASE,
)
_EIGHTY_PCT_RE = re.compile(r"\b80\s*%|\b80\s*percent\b|\beighty\s*percent\b", re.IGNORECASE)
_FIFTY_PCT_RE = re.compile(r"\b50\s*%|\b50\s*percent\b|\bfifty\s*percent\b", re.IGNORECASE)
_NO_CANCEL_RE = re.compile(
    r"\b(?:no\s+cancell?ation|cannot\s+cancel|can[''’]?t\s+cancel|cancellation[^.\n]*?(?:not\s+allowed|disabled)|not\s+allowed)\b",
    re.IGNORECASE,
)
_CANCEL_FEE_RE = re.compile(
    r"\b(\d{1,2})\s*%[^.\n]*?(?:retained|cancellation\s+fee|non[\s-]?refundable|fee)\b",
    re.IGNORECASE,
)

# ── Status patterns ──
_STATUS_NOT_ACCEPTED_RE = re.compile(
    r"(?:restaurant[^.\n]*?(?:has\s+)?not\s+(?:yet\s+)?accept(?:ed)?|not\s+accepted(?:\s+yet)?)",
    re.IGNORECASE,
)
_STATUS_ACCEPTED_NOT_PREPARING_RE = re.compile(
    r"accept(?:ed)?[^.\n]*?(?:but\s+)?(?:has\s+)?not[^.\n]*?(?:start(?:ed)?\s+)?prepar(?:ing|ation)|accepted[^.\n]*?not\s+preparing",
    re.IGNORECASE,
)
_STATUS_PREPARING_RE = re.compile(r"\bprepar(?:ing|ation)\b", re.IGNORECASE)
_STATUS_OUT_FOR_DELIVERY_RE = re.compile(r"\bout\s+for\s+delivery\b|\bdispatched\b|\bon\s+the\s+way\b", re.IGNORECASE)
_STATUS_DELIVERED_RE = re.compile(r"\bdelivered\b|\border\s+complete(?:d)?\b", re.IGNORECASE)


@dataclass
class _Section:
    title: str
    body: str
    section_id: str


def _split_sections(text: str) -> list[_Section]:
    """Split the document into best-effort sections by heading lines."""
    cleaned = re.sub(r"\r\n?", "\n", text or "").strip()
    if not cleaned:
        return []
    lines = cleaned.split("\n")
    sections: list[_Section] = []
    current_title = "Document"
    current_lines: list[str] = []

    def flush() -> None:
        body = "\n".join(current_lines).strip()
        if body:
            sections.append(
                _Section(
                    title=current_title.strip()[:160],
                    body=body,
                    section_id=str(uuid.uuid4())[:12],
                )
            )

    for raw_line in lines:
        line = raw_line.rstrip()
        if not line.strip():
            current_lines.append("")
            continue
        if _POLICY_HEADING_RE.match(line) or (
            _GENERIC_HEADING_RE.match(line) and len(line.split()) <= 8 and not line.endswith(".")
        ):
            flush()
            current_title = re.sub(r"^#+\s*", "", line).strip(": -–")
            current_lines = []
            continue
        current_lines.append(raw_line)
    flush()
    return sections


def _is_policy_relevant(section: _Section, topic: str) -> bool:
    haystack = f"{section.title}\n{section.body}".lower()
    if topic == TOPIC_CANCELLATION:
        return ("cancel" in haystack or "cancellation" in haystack) and ("minute" in haystack or "refund" in haystack or "preparing" in haystack or "delivery" in haystack)
    if topic == TOPIC_REFUND:
        return "refund" in haystack and ("minute" in haystack or "cancel" in haystack or "wallet" in haystack or "original" in haystack)
    return False


def _detect_status(line: str) -> str:
    if _STATUS_NOT_ACCEPTED_RE.search(line):
        return STATUS_NOT_ACCEPTED
    if _STATUS_ACCEPTED_NOT_PREPARING_RE.search(line):
        return STATUS_ACCEPTED_NOT_PREPARING
    if _STATUS_OUT_FOR_DELIVERY_RE.search(line):
        return STATUS_OUT_FOR_DELIVERY
    if _STATUS_DELIVERED_RE.search(line):
        return STATUS_DELIVERED
    if _STATUS_PREPARING_RE.search(line):
        return STATUS_PREPARING
    return STATUS_ANY


def _detect_outcome(line: str) -> tuple[str | None, int | None]:
    """Return (outcome, fee_percent_if_partial)."""
    if _NO_CANCEL_RE.search(line):
        return OUTCOME_NO_CANCELLATION, None
    if _FULL_REFUND_WALLET_RE.search(line):
        return OUTCOME_FULL_REFUND_WALLET, None
    if _FULL_REFUND_ORIGINAL_RE.search(line):
        return OUTCOME_FULL_REFUND_ORIGINAL, None
    if _EIGHTY_PCT_RE.search(line):
        fee_match = _CANCEL_FEE_RE.search(line)
        fee = int(fee_match.group(1)) if fee_match else 20
        return OUTCOME_EIGHTY_PERCENT_REFUND, fee
    if _FIFTY_PCT_RE.search(line):
        return OUTCOME_FIFTY_PERCENT_REFUND, 50
    return None, None


def _detect_window(line: str) -> tuple[int | None, int | None]:
    """Return (min, max) inclusive minutes. None when the window is open-ended on a side."""
    match = _BETWEEN_RE.search(line)
    if match:
        lo, hi = int(match.group(1)), int(match.group(2))
        return min(lo, hi), max(lo, hi)
    match = _WITHIN_RE.search(line)
    if match:
        return 0, int(match.group(1))
    match = _AFTER_RE.search(line)
    if match:
        return int(match.group(1)), None
    return None, None


def _customer_message(
    *,
    window_min: int | None,
    window_max: int | None,
    status: str,
    outcome: str,
    fee_pct: int | None,
) -> str:
    """Build a precise, hedge-free customer-facing message."""
    if outcome == OUTCOME_NO_CANCELLATION:
        if status == STATUS_PREPARING:
            return "Your order is already marked Preparing, so cancellation is not allowed. I can escalate this to support if you still want help."
        if status == STATUS_OUT_FOR_DELIVERY:
            return "Once the order is out for delivery, cancellation is not allowed. If there is a missing item or quality issue after delivery, I can help raise that complaint."
        return "Cancellation is not allowed at this stage. I can escalate this to support if needed."

    if outcome == OUTCOME_FULL_REFUND_ORIGINAL:
        if window_max is not None and window_min in (0, None):
            return (
                f"You can cancel within {window_max} minutes of placing the order for a "
                "full refund to your original payment method."
            )
        return "You are eligible for a full refund to your original payment method."

    if outcome == OUTCOME_FULL_REFUND_WALLET:
        if window_min is not None and window_max is not None:
            return (
                f"Between {window_min} and {window_max} minutes, if the restaurant has not accepted the order, "
                "you can cancel for a full refund to ZapEats Wallet. Original payment refunds may take 5-7 days if applicable."
            )
        return "If the restaurant has not accepted the order, you can cancel for a full refund to your wallet."

    if outcome == OUTCOME_EIGHTY_PERCENT_REFUND:
        fee = fee_pct if fee_pct is not None else 20
        if window_min is not None and window_max is not None:
            return (
                f"Between {window_min} and {window_max} minutes, if the restaurant has accepted the order but has not started preparing it, "
                f"only 80% is refundable. {fee}% is retained as a cancellation fee."
            )
        return f"Only 80% is refundable; {fee}% is retained as a cancellation fee."

    if outcome == OUTCOME_FIFTY_PERCENT_REFUND:
        return "Only 50% is refundable at this stage; the remainder is retained as a cancellation fee."

    return "Please confirm your order status to determine the exact eligibility."


def _conditions_from_section(section: _Section) -> list[dict[str, Any]]:
    """Walk each non-empty line of the section and emit one condition per
    line that contains both a recognizable outcome AND either a window or a
    status. Order is preserved (most-permissive rules typically appear first).
    """
    conditions: list[dict[str, Any]] = []
    for raw_line in section.body.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        outcome, fee_pct = _detect_outcome(line)
        if not outcome:
            continue
        window_min, window_max = _detect_window(line)
        status = _detect_status(line)
        # Reject lines with neither a window nor an informative status — they
        # are likely policy summaries, not rule rows.
        if window_min is None and window_max is None and status == STATUS_ANY:
            # Allow when the outcome is "no_cancellation" with an explicit
            # status like "preparing" / "out for delivery".
            if not (outcome == OUTCOME_NO_CANCELLATION and status != STATUS_ANY):
                continue
        condition: dict[str, Any] = {
            "order_age_minutes_min": window_min,
            "order_age_minutes_max": window_max,
            "status": status,
            "outcome": outcome,
            "customer_message": _customer_message(
                window_min=window_min,
                window_max=window_max,
                status=status,
                outcome=outcome,
                fee_pct=fee_pct,
            ),
            "source_line": line[:280],
        }
        if fee_pct is not None:
            condition["cancellation_fee_percent"] = fee_pct
        conditions.append(condition)
    return conditions


def _fallback_card(section: _Section, topic: str) -> dict[str, Any]:
    """When structured extraction fails, still emit a card with the raw text
    so the decision engine can return a status-check answer instead of
    silently falling through to the LLM."""
    return {
        "card_type": "policy_rule",
        "topic": topic,
        "intent": INTENT_CUSTOMER_CANCEL if topic == TOPIC_CANCELLATION else INTENT_REFUND_ELIGIBILITY,
        "requires_live_status": True,
        "sensitive": True,
        "source_section_title": section.title,
        "source_section_id": section.section_id,
        "source_text": section.body[:4000],
        "conditions": [],
        "extraction_quality": "fallback_full_section",
    }


class PolicyCardExtractor:
    """Top-level entrypoint. Returns a list of policy-rule cards."""

    @staticmethod
    def extract(document_text: str) -> list[dict[str, Any]]:
        sections = _split_sections(document_text)
        cards: list[dict[str, Any]] = []
        for section in sections:
            for topic, intent in (
                (TOPIC_CANCELLATION, INTENT_CUSTOMER_CANCEL),
                (TOPIC_REFUND, INTENT_REFUND_ELIGIBILITY),
            ):
                if not _is_policy_relevant(section, topic):
                    continue
                conditions = _conditions_from_section(section)
                if conditions:
                    cards.append(
                        {
                            "card_type": "policy_rule",
                            "topic": topic,
                            "intent": intent,
                            "requires_live_status": True,
                            "sensitive": True,
                            "source_section_title": section.title,
                            "source_section_id": section.section_id,
                            "source_text": section.body[:4000],
                            "conditions": conditions,
                            "extraction_quality": "structured",
                        }
                    )
                else:
                    cards.append(_fallback_card(section, topic))
        return cards
