"""Per-call conversational memory — single source of truth.

Why this module exists
----------------------
The voice agent had two parallel "remember things" systems before this:

  1. The Redis-backed ``AgentSessionStore`` (raw transcript history + a
     ``tool_flow.collected`` bag used by the structured booking flow).
  2. An outbound-only ``update_outbound_memory`` helper in
     :mod:`agent_outbound_context` that regex-mined sales-relevant
     facts from caller turns and was injected into the outbound prompt.

The inbound path got none of that "what's already known" prompt
augmentation, so it routinely re-asked questions the caller had just
answered. And neither system carried structured slots forward across
calls — every call started cold.

``ConversationalMemory`` unifies both: one typed bag of
:class:`MemoryFact` entries, populated on every turn (user **and**
agent) for **both inbound and outbound**, persisted via the existing
``AgentSessionStore`` state blob, surfaced into the LLM prompt with a
"don't re-ask these" directive, and consulted by ``tool_flow_policy``
so the structured booking flow auto-fills known slots and skips their
questions.

Design contract
---------------
- ``MemoryFact`` is the row. It carries the canonical key, the value,
  a confidence (0..1), the source turn index, the timestamp, the
  language at extraction, and the raw text it came from. Newer / higher
  confidence wins. Corrections explicitly bump confidence so
  "no actually 4BHK" overrides an earlier "3BHK".
- ``MemoryExtractor`` is the heuristic ladder. It is deliberately
  deterministic and conservative — no LLM call per turn. It handles
  English + Indian-language code-switched patterns (Hindi, Telugu,
  Tamil) for the slots that matter most: name, phone, email,
  bhk, budget, timeline, location, visit date/time, decisions,
  objections, preferences.
- ``ConversationalMemory`` is the container. It exposes ``has``,
  ``get``, ``snapshot``, ``merge_text``, ``compose_prompt_block``,
  ``known_slot_keys``, ``add_objection``, ``add_commitment``, and
  serialisation helpers.

Cross-call layer
----------------
At call-start, :func:`bootstrap_caller_memory` reads the latest
``outgoing_leads`` row (and any callable history) for the caller's
phone and seeds the memory with high-confidence facts. At call-end,
:func:`promote_to_caller_memory` writes the consolidated bag to a
phone-keyed Redis blob so the next call for the same number opens
warm. This is gated by phone availability — anonymous callers don't
participate.

Non-goals
---------
- This is **not** an LLM-driven NLU. It is heuristic + deterministic
  so it costs nothing per turn and can never make the call slower.
- It does not replace ``tool_flow.collected`` — that's still the
  authoritative bag for the active booking flow; memory feeds it on
  every turn but ``tool_flow`` writes back its own answer.
- It does not persist anything sensitive beyond what the existing
  ``outgoing_leads`` and ``nokvo_one_tool_records`` already capture.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from app.core.config import settings
from app.models.tenant_resources import TenantResources
from app.services.agent_session_store import AgentSessionStore


logger = logging.getLogger(__name__)


# ── Canonical fact keys ─────────────────────────────────────────────────────


# Identity
FACT_NAME = "name"
FACT_PHONE = "phone"
FACT_EMAIL = "email"

# Real-estate domain
FACT_BHK = "bhk"
FACT_BUDGET = "budget"
FACT_LOCATION = "location_preference"
FACT_PURPOSE = "purpose"  # self-use | investment
FACT_TIMELINE = "timeline"
FACT_PROPERTY = "property"

# Booking
FACT_VISIT_DATE = "visit_date"
FACT_VISIT_TIME = "visit_time"
FACT_URGENCY = "urgency"

# Generic
FACT_COMPANY = "company"
FACT_LANGUAGE_PREF = "language_preference"
FACT_FAMILY_SIZE = "family_size"
FACT_REQUESTED_INFO = "requested_info"

# Tracker-only buckets (lists, not single-valued slots)
BUCKET_OBJECTIONS = "objections"
BUCKET_COMMITMENTS = "commitments"
BUCKET_PREFERENCES = "preferences"
BUCKET_ASKED = "asked_questions"  # which question keys the agent has already asked


# Slot-key → human label used in the prompt preamble.
SLOT_LABELS: dict[str, str] = {
    FACT_NAME: "Name",
    FACT_PHONE: "Phone",
    FACT_EMAIL: "Email",
    FACT_BHK: "BHK preference",
    FACT_BUDGET: "Budget",
    FACT_LOCATION: "Location",
    FACT_PURPOSE: "Purpose",
    FACT_TIMELINE: "Timeline",
    FACT_PROPERTY: "Property",
    FACT_VISIT_DATE: "Visit date",
    FACT_VISIT_TIME: "Visit time",
    FACT_URGENCY: "Urgency",
    FACT_COMPANY: "Company",
    FACT_LANGUAGE_PREF: "Language preference",
    FACT_FAMILY_SIZE: "Family size",
    FACT_REQUESTED_INFO: "Requested info",
}


# Aliases the tool-flow uses. ``tool_flow_policy`` stores slots as e.g.
# "customer_name" / "contact_phone" / "preferred_date" — we map those
# onto our canonical keys so a known canonical fact satisfies the flow
# slot of the same meaning. (Direction is fact→flow_slot; the reverse
# is built from this at import time.)
FLOW_SLOT_TO_FACT: dict[str, str] = {
    "name": FACT_NAME,
    "customer_name": FACT_NAME,
    "full_name": FACT_NAME,
    "phone": FACT_PHONE,
    "mobile": FACT_PHONE,
    "contact_phone": FACT_PHONE,
    "email": FACT_EMAIL,
    "bhk": FACT_BHK,
    "budget": FACT_BUDGET,
    "location": FACT_LOCATION,
    "location_preference": FACT_LOCATION,
    "area": FACT_LOCATION,
    "purpose": FACT_PURPOSE,
    "timeline": FACT_TIMELINE,
    "visit_date": FACT_VISIT_DATE,
    "preferred_date": FACT_VISIT_DATE,
    "visit_time": FACT_VISIT_TIME,
    "preferred_time": FACT_VISIT_TIME,
    "property": FACT_PROPERTY,
    "urgency": FACT_URGENCY,
    "company": FACT_COMPANY,
    "family_size": FACT_FAMILY_SIZE,
}


# ── MemoryFact ──────────────────────────────────────────────────────────────


@dataclass
class MemoryFact:
    """One known fact about the conversation.

    Attributes
    ----------
    key:
        Canonical slot key (one of the ``FACT_*`` constants above).
    value:
        The value the agent should treat as authoritative.
    confidence:
        ``0.0..1.0``. The extractor assigns 0.85 for confident
        explicit-pattern matches, 0.6 for weaker ones, and bumps to
        0.95 when the user is explicitly correcting an earlier value
        (we detect "no", "actually", "I meant", or "wrong" prefixes).
    source_turn:
        Index in the conversation history where this fact was last
        observed. Used to break ties between facts of the same
        confidence — newer wins.
    timestamp:
        Wall-clock epoch seconds at extraction. Belt-and-braces for
        source_turn in case turn indexing drifts.
    language:
        Reply language at extraction. Useful for caller-memory
        bootstraps later — e.g., if every prior call was Telugu, lock
        the language at session-start.
    raw:
        The full caller utterance the fact was derived from. Optional;
        capped to 200 chars to keep Redis payloads bounded.
    """

    key: str
    value: Any
    confidence: float
    source_turn: int
    timestamp: float
    language: str | None = None
    raw: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "key": self.key,
            "value": self.value,
            "confidence": float(self.confidence),
            "source_turn": int(self.source_turn),
            "timestamp": float(self.timestamp),
        }
        if self.language:
            d["language"] = self.language
        if self.raw:
            d["raw"] = self.raw[:200]
        return d

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MemoryFact":
        return cls(
            key=str(payload.get("key") or ""),
            value=payload.get("value"),
            confidence=float(payload.get("confidence") or 0.0),
            source_turn=int(payload.get("source_turn") or 0),
            timestamp=float(payload.get("timestamp") or time.time()),
            language=(str(payload.get("language")) if payload.get("language") else None),
            raw=(str(payload.get("raw")) if payload.get("raw") else None),
        )


# ── Extractor ───────────────────────────────────────────────────────────────


# Indian language number words for BHK extraction.
_BHK_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    # Hindi
    "ek": "1", "एक": "1",
    "do": "2", "दो": "2",
    "teen": "3", "तीन": "3",
    "char": "4", "चार": "4",
    # Telugu
    "okati": "1", "ఒకటి": "1",
    "rendu": "2", "రెండు": "2",
    "moodu": "3", "మూడు": "3",
    "nalugu": "4", "నాలుగు": "4",
    # Tamil
    "onnu": "1", "ஒன்று": "1",
    "rendu_ta": "2",  # naming collision avoidance
    "moonu": "3", "மூன்று": "3",
    "naalu": "4", "நான்கு": "4",
}


_NAME_PATTERNS = (
    # English. Capture stops at the first sentence terminator (.,;!?)
    # or conjunction so "my name is Asha. Looking for 3BHK" doesn't
    # bleed into a 4-word "name" like "Asha. Looking For 3BHK".
    re.compile(
        r"\b(?:my name is|this is|i am|i'm|call me|name is)\s+"
        r"([A-Za-z][A-Za-z'-]*(?:\s+[A-Za-z][A-Za-z'-]*){0,2})"
        r"(?=[\s,.;!?]|$)",
        re.IGNORECASE,
    ),
    # Hindi (transliterated + script)
    re.compile(
        r"\b(?:mera naam|mera nam|mere ko bolte hain)\s+"
        r"([A-Za-z]+(?:\s+[A-Za-z]+){0,2})"
        r"(?=[\s,.;!?]|$)",
        re.IGNORECASE,
    ),
    re.compile(r"मेरा\s+नाम\s+([ऀ-ॿa-zA-Z]+(?:\s+[ऀ-ॿa-zA-Z]+){0,2})"),
    # Telugu (transliterated + script)
    re.compile(
        r"\b(?:naa peru|na peru|naa pēru|na pēru)\s+"
        r"([A-Za-z]+(?:\s+[A-Za-z]+){0,2})"
        r"(?=[\s,.;!?]|$)",
        re.IGNORECASE,
    ),
    re.compile(r"నా\s+పేరు\s+([ఀ-౿a-zA-Z]+(?:\s+[ఀ-౿a-zA-Z]+){0,2})"),
    # Tamil
    re.compile(r"என்\s+பெயர்\s+([஀-௿a-zA-Z]+(?:\s+[஀-௿a-zA-Z]+){0,2})"),
)

_NAME_REJECT_WORDS = {
    "looking", "interested", "busy", "not", "calling", "thinking", "going",
    "planning", "buying", "searching", "available", "ok", "okay", "yes", "no",
    "sure", "fine", "good", "great", "right",
    # Indic copulas / honorifics that often trail a self-introduction.
    "hai", "hain", "hu", "hoon", "ji",   # Hindi
    "ga", "garu", "andi", "ki", "ledu",  # Telugu
    "dha", "thaan", "than", "thaa",      # Tamil
}

_PHONE_RE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?([6-9](?:[\s-]?\d){9})(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_BHK_RE = re.compile(r"\b([1-6])\s*(?:bhk|bed(?:room)?s?)\b", re.IGNORECASE)
_BHK_WORD_RE = re.compile(
    rf"\b({'|'.join(re.escape(w) for w in _BHK_NUMBER_WORDS)})\s*(?:bhk|bed(?:room)?s?)\b",
    re.IGNORECASE,
)

_BUDGET_RE = re.compile(
    r"\b(?:budget(?:\s+is)?|around|about|upto|up to|under|within|near)?\s*"
    r"(?:rs\.?|inr|₹|rupees?)?\s*"
    r"([0-9]+(?:\.[0-9]+)?\s*(?:cr|crore|crores|lakh|lakhs|lac|lacs|k|thousand))\b",
    re.IGNORECASE,
)

_PURPOSE_SELF_RE = re.compile(
    r"\b(self[-\s]?use|own use|end use|family|to live|for living|own house|investment\s+nahi)\b",
    re.IGNORECASE,
)
_PURPOSE_INVEST_RE = re.compile(
    r"\b(invest|investment|investor|rental|rent out|roi|second home)\b",
    re.IGNORECASE,
)

_TIMELINE_RE = re.compile(
    r"\b(immediately|right away|asap|as soon as possible|this week|this month|"
    r"next month|this year|next year|"
    r"within\s+[0-9]+\s+(?:days?|weeks?|months?)|in\s+[0-9]+\s+(?:days?|weeks?|months?))\b",
    re.IGNORECASE,
)

_VISIT_TIME_RE = re.compile(
    r"\b((?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:am|pm|AM|PM))\b"
)
_VISIT_DATE_RE = re.compile(
    r"\b(today|tomorrow|day after tomorrow|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"this\s+(?:weekend|saturday|sunday)|next\s+(?:weekend|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"[0-3]?\d(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"
    r")\b",
    re.IGNORECASE,
)

_LOCATION_RE = re.compile(
    r"\b(?:near|around|in|at)\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3})\b"
)

# Correction-cue prefixes — when present, the value extracted from the
# same utterance should override existing memory at high confidence.
_CORRECTION_RE = re.compile(
    r"\b(no(?:t)?(?:\s+actually)?|actually|i meant|sorry|wrong|"
    r"not\s+\d|change(?:\s+it)?|update|correct(?:ion)?)\b",
    re.IGNORECASE,
)

# Objection / friction signals. These get pushed into the objections
# bucket (not a single-valued fact) so the agent's memory of "they're
# resistant on price" survives even after a later "ok, let me think".
_OBJECTION_PATTERNS = (
    (re.compile(r"\b(not interested|don't call|do not call|remove me|wrong number)\b", re.IGNORECASE), "do_not_call"),
    (re.compile(r"\b(busy|call later|later please|not (?:now|today))\b", re.IGNORECASE), "call_later"),
    (re.compile(r"\b(expensive|costly|too high|out of budget|over budget)\b", re.IGNORECASE), "price_concern"),
    (re.compile(r"\b(already (?:have|bought|booked)|other (?:agent|developer|broker))\b", re.IGNORECASE), "competitor"),
    (re.compile(r"\b(later this year|next year|after months?|in a few months)\b", re.IGNORECASE), "long_horizon"),
)

# Affirmative commitment / decision cues. We promote these to the
# commitments bucket so the agent stops re-pitching what's already
# agreed and instead drives to the next step.
_COMMITMENT_PATTERNS = (
    (re.compile(r"\b(yes|yeah|sure|okay|ok|definitely|interested|count me in|i'?m in)\b", re.IGNORECASE), "interested"),
    (re.compile(r"\b(go ahead|please book|book it|schedule it|let'?s do it)\b", re.IGNORECASE), "ready_to_book"),
    (re.compile(r"\b(send (?:me )?(?:details|brochure|info|info(?:rmation)?))\b", re.IGNORECASE), "info_requested"),
    (re.compile(r"\b(call (?:me )?(?:back|later))\b", re.IGNORECASE), "callback_requested"),
)

# Preferences (channel, contact time, contact mode). Stored as small
# dicts so multiple co-exist.
_CHANNEL_WHATSAPP_RE = re.compile(r"\bwhatsapp\b", re.IGNORECASE)
_CHANNEL_EMAIL_RE = re.compile(r"\b(?:email me|over email|by mail)\b", re.IGNORECASE)
_CHANNEL_VOICE_RE = re.compile(r"\b(?:call me|over (?:phone|call))\b", re.IGNORECASE)
_TIME_PREF_RE = re.compile(r"\b(morning|afternoon|evening|weekday|weekdays|weekend|weekends)\b", re.IGNORECASE)


# Strong language-hint patterns — when the user explicitly asks the
# agent to switch language, we remember it so subsequent calls can
# default to it.
_LANG_PREF_PATTERNS = (
    (re.compile(r"\b(hindi|हिंदी)\b", re.IGNORECASE), "hi"),
    (re.compile(r"\b(telugu|తెలుగు)\b", re.IGNORECASE), "te"),
    (re.compile(r"\b(tamil|தமிழ்)\b", re.IGNORECASE), "ta"),
    (re.compile(r"\b(english|angrezi)\b", re.IGNORECASE), "en"),
    (re.compile(r"\b(kannada|ಕನ್ನಡ)\b", re.IGNORECASE), "kn"),
    (re.compile(r"\b(marathi|मराठी)\b", re.IGNORECASE), "mr"),
)


def _clean_value(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip(" .,:;-")


class MemoryExtractor:
    """Stateless heuristic extractor.

    Given a single utterance, returns a list of :class:`MemoryFact`
    (canonical key) plus optional bucket signals (objection /
    commitment / preference / language-pref). Confidence is set per
    pattern; correction-cued utterances bump confidence to 0.95.
    """

    @staticmethod
    def _extract_name(text: str) -> str | None:
        for pat in _NAME_PATTERNS:
            match = pat.search(text)
            if not match:
                continue
            tokens = [t for t in match.group(1).split() if t]
            if not tokens:
                continue
            # Walk left-to-right, take tokens until we hit a reject word.
            # "name is Asha looking for 3BHK" → ["Asha"].
            kept: list[str] = []
            for tok in tokens:
                if tok.lower() in _NAME_REJECT_WORDS:
                    break
                kept.append(tok)
            if not kept:
                continue
            cleaned = _clean_value(" ".join(kept))
            if cleaned and len(cleaned) >= 2:
                return cleaned.title()
        return None

    @staticmethod
    def _extract_phone(text: str) -> str | None:
        match = _PHONE_RE.search(text)
        if not match:
            return None
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) >= 10:
            return digits[-10:]
        return digits or None

    @staticmethod
    def _extract_bhk(text: str) -> str | None:
        match = _BHK_RE.search(text)
        if match:
            return f"{match.group(1)} BHK"
        word_match = _BHK_WORD_RE.search(text)
        if word_match:
            digit = _BHK_NUMBER_WORDS.get(word_match.group(1).lower())
            if digit:
                return f"{digit} BHK"
        return None

    @staticmethod
    def _extract_budget(text: str) -> str | None:
        match = _BUDGET_RE.search(text)
        if not match:
            return None
        return _clean_value(match.group(1))

    @staticmethod
    def _extract_purpose(text: str) -> str | None:
        if _PURPOSE_SELF_RE.search(text):
            return "self-use"
        if _PURPOSE_INVEST_RE.search(text):
            return "investment"
        return None

    @staticmethod
    def _extract_timeline(text: str) -> str | None:
        match = _TIMELINE_RE.search(text)
        return _clean_value(match.group(0)) if match else None

    @staticmethod
    def _extract_visit_date(text: str) -> str | None:
        match = _VISIT_DATE_RE.search(text)
        return _clean_value(match.group(0)) if match else None

    @staticmethod
    def _extract_visit_time(text: str) -> str | None:
        match = _VISIT_TIME_RE.search(text)
        return _clean_value(match.group(0)) if match else None

    @staticmethod
    def _extract_location(text: str) -> str | None:
        match = _LOCATION_RE.search(text)
        if not match:
            return None
        value = _clean_value(match.group(1))
        # Filter out demonstratives that aren't actual locations.
        if value.lower() in {"this", "that", "here", "there"}:
            return None
        return value

    @staticmethod
    def _extract_language_pref(text: str) -> str | None:
        for pat, code in _LANG_PREF_PATTERNS:
            if pat.search(text):
                # Anchor only on "speak/talk/in X" patterns to avoid
                # firing on incidental mentions of a language name.
                if re.search(
                    rf"\b(speak|talk|in|switch to|reply in)\s+(?:[A-Za-z ]+\s+)?{pat.pattern.strip('\\b')}",
                    text,
                    re.IGNORECASE,
                ):
                    return code
        return None

    @classmethod
    def extract(
        cls,
        text: str,
        *,
        turn_index: int,
        language: str | None = None,
        role: str = "user",
    ) -> dict[str, Any]:
        """Return ``{"facts": [...], "objections": [...],
        "commitments": [...], "preferences": [...]}``.

        ``role`` controls how aggressively we trust the text.
        ``"user"`` is the primary source (names, phones, decisions).
        ``"assistant"`` text is mined too — the agent often confirms a
        slot ("Got it, 3BHK in Kompally") and we want that confirmation
        to lock the fact even if the user's earlier utterance was noisy.
        """
        clean_text = re.sub(r"\s+", " ", str(text or "")).strip()
        if not clean_text:
            return {"facts": [], "objections": [], "commitments": [], "preferences": []}

        ts = time.time()
        is_correction = bool(role == "user" and _CORRECTION_RE.search(clean_text))
        confidence_base = 0.85 if role == "user" else 0.75
        confidence = 0.95 if is_correction else confidence_base

        facts: list[MemoryFact] = []

        def _add(key: str, value: Any) -> None:
            if value in (None, "", []):
                return
            facts.append(
                MemoryFact(
                    key=key,
                    value=value,
                    confidence=confidence,
                    source_turn=turn_index,
                    timestamp=ts,
                    language=language,
                    raw=clean_text[:200],
                )
            )

        _add(FACT_NAME, cls._extract_name(clean_text))
        _add(FACT_PHONE, cls._extract_phone(clean_text))
        email_match = _EMAIL_RE.search(clean_text)
        _add(FACT_EMAIL, email_match.group(0) if email_match else None)
        _add(FACT_BHK, cls._extract_bhk(clean_text))
        _add(FACT_BUDGET, cls._extract_budget(clean_text))
        _add(FACT_PURPOSE, cls._extract_purpose(clean_text))
        _add(FACT_TIMELINE, cls._extract_timeline(clean_text))
        _add(FACT_VISIT_DATE, cls._extract_visit_date(clean_text))
        _add(FACT_VISIT_TIME, cls._extract_visit_time(clean_text))
        _add(FACT_LOCATION, cls._extract_location(clean_text))
        _add(FACT_LANGUAGE_PREF, cls._extract_language_pref(clean_text))

        objections: list[dict[str, Any]] = []
        if role == "user":
            for pat, code in _OBJECTION_PATTERNS:
                if pat.search(clean_text):
                    objections.append({"code": code, "text": clean_text[:200], "turn": turn_index, "ts": ts})
                    break  # one per turn is enough

        commitments: list[dict[str, Any]] = []
        if role == "user":
            for pat, code in _COMMITMENT_PATTERNS:
                if pat.search(clean_text):
                    commitments.append({"code": code, "text": clean_text[:200], "turn": turn_index, "ts": ts})

        preferences: list[dict[str, Any]] = []
        if _CHANNEL_WHATSAPP_RE.search(clean_text):
            preferences.append({"key": "contact_channel", "value": "whatsapp", "turn": turn_index})
        if _CHANNEL_EMAIL_RE.search(clean_text):
            preferences.append({"key": "contact_channel", "value": "email", "turn": turn_index})
        if _CHANNEL_VOICE_RE.search(clean_text):
            preferences.append({"key": "contact_channel", "value": "voice", "turn": turn_index})
        time_pref = _TIME_PREF_RE.search(clean_text)
        if time_pref:
            preferences.append({"key": "contact_time", "value": time_pref.group(0).lower(), "turn": turn_index})

        return {
            "facts": facts,
            "objections": objections,
            "commitments": commitments,
            "preferences": preferences,
        }


# ── ConversationalMemory ────────────────────────────────────────────────────


# Memory persists inside the existing session-state blob under this key.
_STATE_KEY = "memory"


@dataclass
class ConversationalMemory:
    """Per-call structured memory. Lives in session state under
    ``state['memory']`` and is the agent's single source of truth for
    "what do I already know about this caller / this conversation"."""

    facts: dict[str, MemoryFact] = field(default_factory=dict)
    objections: list[dict[str, Any]] = field(default_factory=list)
    commitments: list[dict[str, Any]] = field(default_factory=list)
    preferences: list[dict[str, Any]] = field(default_factory=list)
    asked_questions: list[dict[str, Any]] = field(default_factory=list)
    # Caller-memory bootstrap snapshot — facts loaded from prior calls
    # for the same phone. Kept separate so the in-call merge can prefer
    # current-call values over historical ones at equal confidence.
    bootstrap_keys: set[str] = field(default_factory=set)

    # ── Serialisation ────────────────────────────────────────────────

    def to_state_blob(self) -> dict[str, Any]:
        return {
            "facts": {k: v.to_dict() for k, v in self.facts.items()},
            "objections": list(self.objections),
            "commitments": list(self.commitments),
            "preferences": list(self.preferences),
            "asked_questions": list(self.asked_questions),
            "bootstrap_keys": sorted(self.bootstrap_keys),
        }

    @classmethod
    def from_state_blob(cls, blob: Any) -> "ConversationalMemory":
        data = blob if isinstance(blob, dict) else {}
        facts_raw = data.get("facts") or {}
        facts: dict[str, MemoryFact] = {}
        if isinstance(facts_raw, dict):
            for key, payload in facts_raw.items():
                if isinstance(payload, dict):
                    facts[str(key)] = MemoryFact.from_dict(payload)
        return cls(
            facts=facts,
            objections=list(data.get("objections") or []),
            commitments=list(data.get("commitments") or []),
            preferences=list(data.get("preferences") or []),
            asked_questions=list(data.get("asked_questions") or []),
            bootstrap_keys=set(data.get("bootstrap_keys") or []),
        )

    # ── Reads ────────────────────────────────────────────────────────

    def has(self, key: str) -> bool:
        fact = self.facts.get(key)
        return fact is not None and fact.value not in (None, "", [])

    def get(self, key: str) -> Any:
        fact = self.facts.get(key)
        return fact.value if fact else None

    def get_fact(self, key: str) -> MemoryFact | None:
        return self.facts.get(key)

    def snapshot(self) -> dict[str, Any]:
        """{slot_key: value} of every known fact. Convenience for the
        tool-flow integration so it can pre-populate ``collected``."""
        return {key: fact.value for key, fact in self.facts.items() if self.has(key)}

    def known_slot_keys(self) -> set[str]:
        return {key for key, fact in self.facts.items() if self.has(key)}

    # ── Writes ───────────────────────────────────────────────────────

    def _accept(self, fact: MemoryFact) -> None:
        """Merge a single fact respecting confidence + recency."""
        existing = self.facts.get(fact.key)
        if existing is None:
            self.facts[fact.key] = fact
            return
        # Newer fact wins if its confidence is at least as high.
        if fact.confidence >= existing.confidence:
            # Tie-breaker on equal confidence: prefer the later turn.
            if fact.confidence > existing.confidence or fact.source_turn >= existing.source_turn:
                self.facts[fact.key] = fact

    def merge_extracted(self, extracted: dict[str, Any]) -> None:
        for fact in extracted.get("facts") or []:
            if isinstance(fact, MemoryFact):
                self._accept(fact)
            elif isinstance(fact, dict):
                self._accept(MemoryFact.from_dict(fact))
        for o in extracted.get("objections") or []:
            self.objections.append(dict(o))
        for c in extracted.get("commitments") or []:
            self.commitments.append(dict(c))
        for p in extracted.get("preferences") or []:
            self.preferences.append(dict(p))
        # Keep the buckets bounded so a 50-turn call doesn't accumulate
        # 50 "interested" entries. Latest 16 of each is plenty.
        self.objections = self.objections[-16:]
        self.commitments = self.commitments[-16:]
        self.preferences = self.preferences[-16:]

    def merge_text(
        self,
        text: str,
        *,
        turn_index: int,
        language: str | None = None,
        role: str = "user",
    ) -> dict[str, Any]:
        extracted = MemoryExtractor.extract(
            text, turn_index=turn_index, language=language, role=role
        )
        self.merge_extracted(extracted)
        return extracted

    def mark_asked(self, slot_key: str, turn_index: int) -> None:
        """Record that the agent has asked for ``slot_key`` on this
        turn. The anti-repeat guard uses this to avoid asking the same
        question twice in a row even when extraction missed."""
        self.asked_questions.append({"slot": slot_key, "turn": turn_index})
        self.asked_questions = self.asked_questions[-32:]

    def has_asked_recently(self, slot_key: str, *, within_turns: int = 2) -> bool:
        if not self.asked_questions:
            return False
        last_turn = max(int(q.get("turn") or 0) for q in self.asked_questions)
        for item in reversed(self.asked_questions):
            if str(item.get("slot")) == slot_key and (last_turn - int(item.get("turn") or 0)) <= within_turns:
                return True
        return False

    def latest_objection(self) -> dict[str, Any] | None:
        return self.objections[-1] if self.objections else None

    def latest_commitment(self) -> dict[str, Any] | None:
        return self.commitments[-1] if self.commitments else None

    # ── Prompt block ─────────────────────────────────────────────────

    def compose_prompt_block(self, language: str | None = None) -> str:
        """Render the memory as a system-prompt fragment the LLM is
        told to honour. Empty string when nothing is known yet (the
        caller should drop the section entirely)."""
        lines: list[str] = []
        for key, label in SLOT_LABELS.items():
            fact = self.facts.get(key)
            if not fact or fact.value in (None, "", []):
                continue
            origin = "from a prior call" if key in self.bootstrap_keys else None
            value_text = str(fact.value)
            if origin:
                lines.append(f"  - {label}: {value_text} ({origin})")
            else:
                lines.append(f"  - {label}: {value_text}")

        if self.objections:
            latest = self.objections[-1]
            code = str(latest.get("code") or "objection")
            lines.append(f"  - Latest concern: {code}")
        if self.commitments:
            latest = self.commitments[-1]
            code = str(latest.get("code") or "interested")
            lines.append(f"  - Latest commitment: {code}")
        if self.preferences:
            # De-dup preferences by key — latest wins.
            seen: dict[str, str] = {}
            for p in self.preferences:
                k = str(p.get("key") or "")
                v = str(p.get("value") or "")
                if k and v:
                    seen[k] = v
            for k, v in seen.items():
                lines.append(f"  - Preference {k}: {v}")

        if not lines:
            return ""

        # Bilingual header — the LLM is more likely to honour the
        # directive when it's in the reply language.
        header = (
            "# CONVERSATIONAL MEMORY — already known from this call\n"
            "Treat the following as established facts. Do NOT ask the caller for them again. "
            "Build your next reply around what is known; if a fact contradicts what they just said, "
            "briefly acknowledge the correction ('Ah, my mistake') and update accordingly.\n"
        )
        return header + "\n".join(lines)


# ── Session-store load / save ───────────────────────────────────────────────


async def load_memory(
    tenant_res: TenantResources,
    call_id: str | None,
) -> ConversationalMemory:
    """Read the memory blob out of session state. Always returns a
    valid (possibly empty) instance — no Redis errors propagate."""
    if not call_id:
        return ConversationalMemory()
    try:
        state = await AgentSessionStore.get_state(tenant_res, call_id)
        blob = (state or {}).get(_STATE_KEY)
        return ConversationalMemory.from_state_blob(blob)
    except Exception:
        logger.debug("NOKVO-MEMORY: load_memory failed", exc_info=True)
        return ConversationalMemory()


async def save_memory(
    tenant_res: TenantResources,
    call_id: str | None,
    memory: ConversationalMemory,
) -> None:
    if not call_id or memory is None:
        return
    try:
        await AgentSessionStore.merge_state(
            tenant_res,
            call_id,
            {_STATE_KEY: memory.to_state_blob()},
        )
    except Exception:
        logger.debug("NOKVO-MEMORY: save_memory failed", exc_info=True)


# ── Cross-call caller memory ────────────────────────────────────────────────


_CALLER_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days
_CALLER_FACT_KEYS = (
    FACT_NAME,
    FACT_EMAIL,
    FACT_BHK,
    FACT_BUDGET,
    FACT_LOCATION,
    FACT_PURPOSE,
    FACT_TIMELINE,
    FACT_LANGUAGE_PREF,
)


def _normalise_phone(value: Any) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    if not digits:
        return None
    if digits.startswith("91") and len(digits) > 10:
        digits = digits[-10:]
    return digits[-10:] if len(digits) >= 10 else digits


def _caller_memory_key(tenant_res: TenantResources, phone: str) -> str:
    namespace = AgentSessionStore.namespace(tenant_res)
    return f"{namespace}:agent:caller_memory:v1:{phone}"


async def bootstrap_caller_memory(
    tenant_res: TenantResources,
    *,
    phone: Any,
    memory: ConversationalMemory,
) -> ConversationalMemory:
    """Seed ``memory`` with facts persisted from prior calls for the
    same caller phone. Idempotent and best-effort — failures are
    swallowed so the live call always proceeds."""
    norm = _normalise_phone(phone)
    if not norm:
        return memory
    try:
        client = AgentSessionStore.client()
        raw = await client.get(_caller_memory_key(tenant_res, norm))
        if not raw:
            return memory
        payload = json.loads(raw)
    except Exception:
        logger.debug("NOKVO-MEMORY: bootstrap_caller_memory load failed", exc_info=True)
        return memory
    if not isinstance(payload, dict):
        return memory
    facts_raw = payload.get("facts") or {}
    if not isinstance(facts_raw, dict):
        return memory
    for key, fact_payload in facts_raw.items():
        if key not in _CALLER_FACT_KEYS or not isinstance(fact_payload, dict):
            continue
        if memory.has(key):
            # Live-call fact already wins; bootstrap stays out of the
            # way. We still tag the key so the prompt knows it was
            # known historically (not silently corrected).
            memory.bootstrap_keys.add(key)
            continue
        # Bootstrap facts come in at slightly reduced confidence so an
        # in-call correction can override them without needing the
        # explicit "no actually" prefix.
        fact = MemoryFact.from_dict(fact_payload)
        fact.confidence = min(fact.confidence, 0.7)
        fact.source_turn = -1  # signal "from before this call"
        memory.facts[key] = fact
        memory.bootstrap_keys.add(key)
    return memory


async def promote_to_caller_memory(
    tenant_res: TenantResources,
    *,
    phone: Any,
    memory: ConversationalMemory,
) -> None:
    """Persist the durable subset of ``memory`` to the per-caller
    Redis blob so the next call for the same phone opens warm."""
    norm = _normalise_phone(phone)
    if not norm:
        return
    if not memory or not memory.facts:
        return
    payload_facts: dict[str, Any] = {}
    for key in _CALLER_FACT_KEYS:
        fact = memory.facts.get(key)
        if fact is None or fact.value in (None, "", []):
            continue
        payload_facts[key] = fact.to_dict()
    if not payload_facts:
        return
    payload = {
        "facts": payload_facts,
        "updated_at": time.time(),
    }
    try:
        client = AgentSessionStore.client()
        await client.setex(
            _caller_memory_key(tenant_res, norm),
            _CALLER_TTL_SECONDS,
            json.dumps(payload, ensure_ascii=False),
        )
    except Exception:
        logger.debug("NOKVO-MEMORY: promote_to_caller_memory failed", exc_info=True)


# ── Tool-flow integration helpers ───────────────────────────────────────────


def fact_for_flow_slot(slot_key: str) -> str | None:
    """Map a tool-flow slot key onto our canonical fact key."""
    return FLOW_SLOT_TO_FACT.get(str(slot_key or "").lower())


def hydrate_flow_collected(
    collected: dict[str, Any],
    memory: ConversationalMemory,
) -> dict[str, Any]:
    """Return ``collected`` plus any slots whose value the memory
    already knows. Existing collected entries take precedence — the
    flow's own writes are authoritative; we only fill the gaps."""
    merged = dict(collected or {})
    for slot_key, fact_key in FLOW_SLOT_TO_FACT.items():
        if slot_key in merged and merged.get(slot_key):
            continue
        fact = memory.facts.get(fact_key)
        if fact and fact.value not in (None, "", []):
            merged[slot_key] = fact.value
    return merged


__all__ = [
    "BUCKET_ASKED",
    "BUCKET_COMMITMENTS",
    "BUCKET_OBJECTIONS",
    "BUCKET_PREFERENCES",
    "ConversationalMemory",
    "FACT_BHK",
    "FACT_BUDGET",
    "FACT_COMPANY",
    "FACT_EMAIL",
    "FACT_FAMILY_SIZE",
    "FACT_LANGUAGE_PREF",
    "FACT_LOCATION",
    "FACT_NAME",
    "FACT_PHONE",
    "FACT_PROPERTY",
    "FACT_PURPOSE",
    "FACT_REQUESTED_INFO",
    "FACT_TIMELINE",
    "FACT_URGENCY",
    "FACT_VISIT_DATE",
    "FACT_VISIT_TIME",
    "FLOW_SLOT_TO_FACT",
    "MemoryExtractor",
    "MemoryFact",
    "SLOT_LABELS",
    "bootstrap_caller_memory",
    "fact_for_flow_slot",
    "hydrate_flow_collected",
    "load_memory",
    "promote_to_caller_memory",
    "save_memory",
]
