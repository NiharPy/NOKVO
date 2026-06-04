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
  Tamil). Extraction is **business-type aware**: a universal set
  (name, phone, email, language, appointment date/time, timeline) runs
  for everyone, and a per-domain set runs on top —

    * real_estate → bhk, budget, location, purpose
    * clinics     → symptoms, appointment type, doctor, age, gender,
                    insurance, prior visit
    * ecommerce   → order id, tracking, issue type, item, address,
                    payment method
    * hospitality → party size, check-in/out, room/table, occasion,
                    dietary
    * other / unknown → the full superset (we can't predict the domain)

  Alongside slots it captures *salient notes* — free-form must-remember
  statements (allergies, named family, deadlines, "please remember…")
  that aren't single-valued slots, giving the agent strong recall of
  the specifics of *this* conversation.
- ``ConversationalMemory`` is the container. It exposes ``has``,
  ``get``, ``snapshot``, ``merge_text``, ``compose_prompt_block``,
  ``known_slot_keys``, ``mark_asked``, and serialisation helpers.

Cross-call layer
----------------
At call-start, :func:`bootstrap_caller_memory` reads the per-(tenant,
phone) Redis blob written by a prior call and seeds the memory with the
durable facts + salient notes relevant to the current business type. At
call-end, :func:`promote_to_caller_memory` writes the consolidated bag
back so the next call for the same number opens warm. Both are gated by
phone availability — anonymous callers don't participate — and the
Redis key is namespaced per tenant so memory never leaks across
tenants.

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
FACT_INCOME = "monthly_income"
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

# Clinics / healthcare
FACT_SYMPTOMS = "symptoms"
FACT_APPOINTMENT_TYPE = "appointment_type"  # consultation, follow-up, lab test
FACT_DOCTOR_PREFERENCE = "doctor_preference"
FACT_PATIENT_AGE = "patient_age"
FACT_PATIENT_GENDER = "patient_gender"
FACT_INSURANCE = "insurance"
FACT_PRIOR_VISIT = "prior_visit"

# E-commerce / support
FACT_ORDER_ID = "order_id"
FACT_ITEM = "item"
FACT_ISSUE_TYPE = "issue_type"  # refund, exchange, delivery, defect, query
FACT_SHIPPING_ADDRESS = "shipping_address"
FACT_TRACKING_NUMBER = "tracking_number"
FACT_PAYMENT_METHOD = "payment_method"

# Hospitality (hotels / restaurants / events)
FACT_PARTY_SIZE = "party_size"
FACT_CHECK_IN = "check_in"
FACT_CHECK_OUT = "check_out"
FACT_ROOM_TYPE = "room_type"
FACT_OCCASION = "occasion"
FACT_DIETARY = "dietary"
FACT_SEATING_PREFERENCE = "seating_preference"

# Follow-up agent signals
# - promised_callback_at: caller asked to be called back at a specific time.
#   Drives the highest-priority branch of the follow-up decision tree.
#   Value: ISO-8601 string with TZ (UTC). Source: heuristic parser below.
# - opted_out: caller asked to never be contacted again. Triggers the legal
#   kill switch in the follow-up service (consent_status='revoked' + cancel
#   pending follow-ups). Value: True (only present when detected).
FACT_PROMISED_CALLBACK_AT = "promised_callback_at"
FACT_OPTED_OUT = "opted_out"

# Tracker-only buckets (lists, not single-valued slots)
BUCKET_OBJECTIONS = "objections"
BUCKET_COMMITMENTS = "commitments"
BUCKET_PREFERENCES = "preferences"
BUCKET_ASKED = "asked_questions"  # which question keys the agent has already asked


# Slot-key → human label used in the prompt preamble. ``compose_prompt_block``
# filters this map by business type so a clinic call doesn't see BHK lines and a
# real-estate call doesn't see check-in date lines.
SLOT_LABELS: dict[str, str] = {
    # Universal
    FACT_NAME: "Name",
    FACT_PHONE: "Phone",
    FACT_EMAIL: "Email",
    FACT_COMPANY: "Company",
    FACT_LANGUAGE_PREF: "Language preference",
    FACT_FAMILY_SIZE: "Family size",
    FACT_REQUESTED_INFO: "Requested info",
    FACT_URGENCY: "Urgency",
    FACT_TIMELINE: "Timeline",
    FACT_VISIT_DATE: "Visit / appointment date",
    FACT_VISIT_TIME: "Visit / appointment time",
    # Real estate
    FACT_BHK: "BHK preference",
    FACT_BUDGET: "Budget",
    FACT_INCOME: "Monthly income",
    FACT_LOCATION: "Location",
    FACT_PURPOSE: "Purpose",
    FACT_PROPERTY: "Property",
    # Clinics
    FACT_SYMPTOMS: "Symptoms / reason",
    FACT_APPOINTMENT_TYPE: "Appointment type",
    FACT_DOCTOR_PREFERENCE: "Doctor preference",
    FACT_PATIENT_AGE: "Patient age",
    FACT_PATIENT_GENDER: "Patient gender",
    FACT_INSURANCE: "Insurance",
    FACT_PRIOR_VISIT: "Prior visit",
    # E-commerce
    FACT_ORDER_ID: "Order ID",
    FACT_ITEM: "Item",
    FACT_ISSUE_TYPE: "Issue type",
    FACT_SHIPPING_ADDRESS: "Shipping address",
    FACT_TRACKING_NUMBER: "Tracking number",
    FACT_PAYMENT_METHOD: "Payment method",
    # Hospitality
    FACT_PARTY_SIZE: "Party size",
    FACT_CHECK_IN: "Check-in date",
    FACT_CHECK_OUT: "Check-out date",
    FACT_ROOM_TYPE: "Room / table type",
    FACT_OCCASION: "Occasion",
    FACT_DIETARY: "Dietary",
    FACT_SEATING_PREFERENCE: "Seating preference",
}


# Aliases the tool-flow uses. ``tool_flow_policy`` stores slots as e.g.
# "customer_name" / "contact_phone" / "preferred_date" — we map those
# onto our canonical keys so a known canonical fact satisfies the flow
# slot of the same meaning. (Direction is fact→flow_slot; the reverse
# is built from this at import time.)
FLOW_SLOT_TO_FACT: dict[str, str] = {
    # Universal identity
    "name": FACT_NAME,
    "customer_name": FACT_NAME,
    "patient_name": FACT_NAME,
    "guest_name": FACT_NAME,
    "full_name": FACT_NAME,
    "phone": FACT_PHONE,
    "mobile": FACT_PHONE,
    "contact_phone": FACT_PHONE,
    "patient_phone": FACT_PHONE,
    "email": FACT_EMAIL,
    "company": FACT_COMPANY,
    "family_size": FACT_FAMILY_SIZE,
    # Real estate
    "bhk": FACT_BHK,
    "budget": FACT_BUDGET,
    "monthly_income": FACT_INCOME,
    "income": FACT_INCOME,
    "salary": FACT_INCOME,
    "location": FACT_LOCATION,
    "location_preference": FACT_LOCATION,
    "area": FACT_LOCATION,
    "purpose": FACT_PURPOSE,
    "timeline": FACT_TIMELINE,
    "property": FACT_PROPERTY,
    "property_name": FACT_PROPERTY,
    "project": FACT_PROPERTY,
    "project_name": FACT_PROPERTY,
    "urgency": FACT_URGENCY,
    # Universal appointment date/time aliases (also used by clinics)
    "visit_date": FACT_VISIT_DATE,
    "preferred_date": FACT_VISIT_DATE,
    "appointment_date": FACT_VISIT_DATE,
    "visit_time": FACT_VISIT_TIME,
    "preferred_time": FACT_VISIT_TIME,
    "appointment_time": FACT_VISIT_TIME,
    # Clinics
    "symptoms": FACT_SYMPTOMS,
    "reason_for_visit": FACT_SYMPTOMS,
    "complaint": FACT_SYMPTOMS,
    "appointment_type": FACT_APPOINTMENT_TYPE,
    "consultation_type": FACT_APPOINTMENT_TYPE,
    "doctor": FACT_DOCTOR_PREFERENCE,
    "doctor_preference": FACT_DOCTOR_PREFERENCE,
    "preferred_doctor": FACT_DOCTOR_PREFERENCE,
    "patient_age": FACT_PATIENT_AGE,
    "age": FACT_PATIENT_AGE,
    "patient_gender": FACT_PATIENT_GENDER,
    "gender": FACT_PATIENT_GENDER,
    "insurance": FACT_INSURANCE,
    "insurance_provider": FACT_INSURANCE,
    "prior_visit": FACT_PRIOR_VISIT,
    # E-commerce
    "order_id": FACT_ORDER_ID,
    "order_number": FACT_ORDER_ID,
    "order": FACT_ORDER_ID,
    "item": FACT_ITEM,
    "product": FACT_ITEM,
    "issue_type": FACT_ISSUE_TYPE,
    "issue": FACT_ISSUE_TYPE,
    "shipping_address": FACT_SHIPPING_ADDRESS,
    "delivery_address": FACT_SHIPPING_ADDRESS,
    "address": FACT_SHIPPING_ADDRESS,
    "tracking_number": FACT_TRACKING_NUMBER,
    "awb": FACT_TRACKING_NUMBER,
    "payment_method": FACT_PAYMENT_METHOD,
    # Hospitality
    "party_size": FACT_PARTY_SIZE,
    "guest_count": FACT_PARTY_SIZE,
    "pax": FACT_PARTY_SIZE,
    "check_in": FACT_CHECK_IN,
    "checkin": FACT_CHECK_IN,
    "check_in_date": FACT_CHECK_IN,
    "arrival_date": FACT_CHECK_IN,
    "check_out": FACT_CHECK_OUT,
    "checkout": FACT_CHECK_OUT,
    "check_out_date": FACT_CHECK_OUT,
    "departure_date": FACT_CHECK_OUT,
    "room_type": FACT_ROOM_TYPE,
    "table_type": FACT_ROOM_TYPE,
    "occasion": FACT_OCCASION,
    "dietary": FACT_DIETARY,
    "dietary_preference": FACT_DIETARY,
    "seating": FACT_SEATING_PREFERENCE,
    "seating_preference": FACT_SEATING_PREFERENCE,
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
    # Set True when the fact was restored from the per-phone caller-memory
    # blob at session start (replaces the parallel ``bootstrap_keys`` set that
    # ConversationalMemory used to track separately — same meaning, expressed
    # on the fact itself for consistency with bucket entries which already
    # carry an equivalent ``from_prior_call`` flag).
    from_prior_call: bool = False

    @property
    def turn(self) -> int:
        """Alias for ``source_turn``. Read-only.

        Bucket entries (objections / commitments / salient_notes) historically
        used ``turn`` while facts used ``source_turn``. Exposing both names
        lets the FSM / strategy layer reference signals uniformly without
        having to remember which bucket they came from. The storage name stays
        ``source_turn`` until a follow-up rename PR.
        """
        return self.source_turn

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
        if self.from_prior_call:
            d["from_prior_call"] = True
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
            from_prior_call=bool(payload.get("from_prior_call")),
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
    # Verbs/adjectives that commonly follow "I am" / "I'm" in
    # non-introduction sentences, so a clinic caller's "I'm allergic to
    # penicillin" or "I am feeling feverish" doesn't become a name.
    "feeling", "suffering", "having", "experiencing", "here", "trying",
    "waiting", "wondering", "hoping", "from", "allergic", "unable",
    "worried", "upset", "happy", "unhappy",
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

# Spelled-out amounts the digit regex above misses: "half a crore", "a crore",
# "fifty lakhs", "one and a half crore". Maps a leading word-number phrase onto
# a float multiplier, then the matched unit gives the canonical value.
_NUMBER_WORDS = {
    "a": 1.0, "an": 1.0, "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0,
    "five": 5.0, "six": 6.0, "seven": 7.0, "eight": 8.0, "nine": 9.0,
    "ten": 10.0, "eleven": 11.0, "twelve": 12.0, "fifteen": 15.0,
    "twenty": 20.0, "thirty": 30.0, "forty": 40.0, "fifty": 50.0,
    "sixty": 60.0, "seventy": 70.0, "eighty": 80.0, "ninety": 90.0,
    "hundred": 100.0, "couple": 2.0, "few": 3.0,
}
_FRACTION_WORDS = {"half": 0.5, "quarter": 0.25}
_BUDGET_UNIT_CANON = {
    "cr": "crore", "crore": "crore", "crores": "crore",
    "lakh": "lakh", "lakhs": "lakh", "lac": "lakh", "lacs": "lakh",
    "k": "k", "thousand": "k",
}
# A run of number/fraction words (and the fillers "and"/"of") followed by a unit.
_BUDGET_WORD_RE = re.compile(
    r"\b((?:(?:" + "|".join(_NUMBER_WORDS) + r"|" + "|".join(_FRACTION_WORDS) + r"|and|of)\s+){1,4})"
    r"(cr|crore|crores|lakh|lakhs|lac|lacs|k|thousand)\b",
    re.IGNORECASE,
)

# Income statements must NOT land in the budget slot. "I earn 1.5 lakhs a
# month" describes income, not what they'll spend on a home.
_INCOME_CUE_RE = re.compile(
    r"\b(earn|earning|salary|income|take[\s-]?home|per\s+month|/\s*month|monthly|p\.?m\.?|in[\s-]?hand)\b",
    re.IGNORECASE,
)
_INCOME_AMOUNT_RE = re.compile(
    r"([0-9]+(?:\.[0-9]+)?\s*(?:cr|crore|crores|lakhs|lakh|lacs|lac|k|thousand))\b",
    re.IGNORECASE,
)

_PURPOSE_SELF_RE = re.compile(
    r"\b(self[-\s]?use|own use|end use|family|to live|for living|own house|investment\s+nahi|"
    # Transliterated Telugu / Hindi self-use cues.
    r"sontham|sontha|nivasam|undatani(?:ki)?|"
    r"khud\s+ke\s+liye|rehne\s+ke\s+liye|ghar\s+ke\s+liye)\b|"
    r"(సొంత|సొంతం|నివాసం|ఉండటానికి|खुद\s*के\s*लिए|रहने\s*के\s*लिए|घर\s*के\s*लिए)",
    re.IGNORECASE,
)
_PURPOSE_INVEST_RE = re.compile(
    r"\b(invest|investment|investor|rental|rent out|roi|second home|"
    # Transliterated Telugu / Hindi investment cues.
    r"pettubadi|adde|addhe|nivesh|kiray[ae])\b|"
    r"(పెట్టుబడి|అద్దె|अद्दे|निवेश|किराया|किराये|किराए)",
    re.IGNORECASE,
)

_TIMELINE_RE = re.compile(
    r"\b(immediately|right away|asap|as soon as possible|this week|this month|"
    r"next month|this year|next year|"
    r"within\s+[0-9]+\s+(?:days?|weeks?|months?)|in\s+[0-9]+\s+(?:days?|weeks?|months?)|"
    # Transliterated Telugu / Hindi timeline cues.
    r"abhi|turant|ippud[ue]|ventane|ee\s+nela|vacche\s+nela|is\s+mahine|agle\s+mahine)\b|"
    r"(అభీ|ఇప్పుడే|వెంటనే|ఈ\s*నెల|వచ్చే\s*నెల|अभी|तुरंत|इस\s*महीने|अगले\s*महीने)",
    re.IGNORECASE,
)

_VISIT_TIME_RE = re.compile(
    r"\b((?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:am|pm|AM|PM))\b"
)
_VISIT_DATE_RE = re.compile(
    r"\b(today|tomorrow|day after tomorrow|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"this\s+(?:weekend|saturday|sunday)|next\s+(?:weekend|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"[0-3]?\d(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|"
    # Transliterated Telugu / Hindi relative dates.
    r"aaj|kal|parso|parson|repu|rapu|eeroju|ellundi"
    r")\b|"
    r"(ఈరోజు|రేపు|రెపు|ఎల్లుండి|आज|कल|परसों|परसो)",
    re.IGNORECASE,
)

_LOCATION_RE = re.compile(
    r"\b(?:near|around|in|at)\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3})\b"
)
# Telugu / Hindi put the postposition after the (Latin-script) place:
# "Kokapet లో", "Kondapur mein", "Gachibowli ke paas".
_LOCATION_POST_RE = re.compile(
    r"([A-Za-z][A-Za-z]+(?:\s+[A-Za-z]+){0,2})\s*"
    r"(?:(?:లో|లోని|దగ్గర|వద్ద|में|मे|के\s*पास)|(?:ke\s+paas|ke\s+pass|mein)\b)",
    re.IGNORECASE,
)
_LOCATION_REJECT_WORDS = {"this", "that", "here", "there", "interested", "looking", "budget"}

# Project / development the caller names. Two cues: an explicit "<Name>
# project" (case-insensitive, the strongest signal) and a Title-cased proper
# noun after an interest verb. Kept conservative — the booking macro's fuzzy
# ``find_project_match`` is the authority, so we only need a usable hint here.
_PROPERTY_PROJECT_RE = re.compile(
    r"\b([A-Za-z][\w&'.-]*(?:\s+[A-Za-z][\w&'.-]*){0,4})\s+project\b",
    re.IGNORECASE,
)
_PROPERTY_INTEREST_RE = re.compile(
    r"\b(?:interested\s+in|looking\s+(?:at|into)|enquir(?:e|ing)\s+about|"
    r"asking\s+about|about|regarding|visit|see)\s+"
    r"([A-Z][\w&'.-]*(?:\s+[A-Z][\w&'.-]*){0,4})\b"
)
# Telugu / Hindi place the project name BEFORE the cue: "Skyline gurinchi"
# (about Skyline), "Green Meadows ke baare mein". The name is Latin-script even
# in code-switched te/hi STT.
_PROPERTY_POST_RE = re.compile(
    r"([A-Za-z][\w&'.-]*(?:\s+[A-Za-z][\w&'.-]*){0,4})\s+"
    r"(?:gurinchi|gurinci|gurinchii|ke\s+baare\s+mein|ke\s+bare\s+me|"
    r"గురించి|के\s*बारे\s*में)\b",
    re.IGNORECASE,
)
# Filler/lead-in tokens peeled off the edges of a captured project phrase so
# "tell me about the green meadows" reduces to "green meadows".
_PROPERTY_LEAD_FILLER = {
    "tell", "me", "us", "about", "the", "a", "an", "i", "we", "you", "is",
    "are", "am", "im", "want", "wanted", "know", "more", "please", "can",
    "could", "would", "should", "share", "send", "give", "get", "interested",
    "in", "at", "looking", "into", "regarding", "enquire", "enquiring",
    "asking", "like", "to", "visit", "see", "details", "detail", "info",
    "information", "your", "and",
}
# Words that are never a project name even when they follow an interest cue.
_PROPERTY_REJECT_WORDS = {
    "this", "that", "it", "the", "a", "an", "you", "your", "them", "us", "me",
    "details", "detail", "price", "pricing", "cost", "brochure", "more", "info",
    "information", "something", "anything", "property", "properties", "flat",
    "flats", "apartment", "apartments", "home", "homes", "house", "villa",
    "villas", "plot", "plots", "options", "option", "area", "areas", "budget",
    "loan", "emi", "possession", "site", "visit", "booking",
}

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
    (re.compile(r"\b(you (?:keep|already) ask(?:ing|ed)|already (?:told|said|gave) (?:you|that)|same (?:thing|question)( again)?|asked me (?:that|this)( already)?|i just (?:told|said)|stop asking)\b", re.IGNORECASE), "repetition_complaint"),
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

# Resolution / acceptance cues. Multi-token phrases only — a bare "ok" or
# "yeah" is too broad (it could be a cold-open filler or a permission-to-
# continue reply). A match here, combined with the existence of a live
# objection from a PRIOR turn, means the caller accepted the rebuttal and
# the FSM should drop out of objection_handling cleanly.
_RESOLUTION_PATTERNS = (
    re.compile(
        r"\b("
        r"(?:ok|okay|alright|fine|sure|right)\s+(?:makes\s+sense|that\s+works|got\s+it|understood|fair\s+enough)|"
        r"makes?\s+sense|fair\s+enough|got\s+it|understood|i\s+see|noted|"
        r"that\s+(?:makes\s+sense|helps|works|sounds\s+(?:good|fine|fair|reasonable))|"
        r"thanks?\s+for\s+(?:clarifying|explaining|the\s+info(?:rmation)?)|"
        r"good\s+point|that'?s\s+(?:a\s+)?(?:good|fair)\s+point|"
        r"i\s+(?:can|might|could)\s+(?:work|live)\s+with\s+that|"
        r"sounds\s+(?:good|fair|reasonable)"
        r")\b",
        re.IGNORECASE,
    ),
)

# Preferences (channel, contact time, contact mode). Stored as small
# dicts so multiple co-exist.
_CHANNEL_WHATSAPP_RE = re.compile(r"\bwhatsapp\b", re.IGNORECASE)
_CHANNEL_EMAIL_RE = re.compile(r"\b(?:email me|over email|by mail)\b", re.IGNORECASE)
_CHANNEL_VOICE_RE = re.compile(r"\b(?:call me|over (?:phone|call))\b", re.IGNORECASE)
_TIME_PREF_RE = re.compile(r"\b(morning|afternoon|evening|weekday|weekdays|weekend|weekends)\b", re.IGNORECASE)

# ── Follow-up agent: callback-time + opt-out extraction ────────────────────

# Strong opt-out cues. Deliberately narrow — a bare "not interested" is an
# objection (handled by the FSM), not a legal opt-out. We need explicit
# "stop calling" / "don't contact" / "remove me" language to flip
# consent_status='revoked', because that decision is irreversible from the
# system's perspective.
_OPT_OUT_RE = re.compile(
    r"\b("
    r"don'?t (?:ever )?(?:call|contact|phone|ring|reach out to) (?:me|us)(?: again)?|"
    r"stop (?:calling|contacting|phoning) (?:me|us)|"
    r"do not (?:call|contact|phone) (?:me|us)(?: again)?|"
    r"remove me from (?:your|the|this) (?:list|database|records)|"
    r"take me off (?:your|the|this) (?:list|database|records)|"
    r"unsubscribe me|opt me out|"
    r"no more calls|never call (?:me|us)(?: again)?|"
    r"leave me alone|stop bothering me|"
    r"add (?:me|us) to (?:the )?do(?:-| )not(?:-| )call(?: list| registry)?"
    r")\b",
    re.IGNORECASE,
)

# Callback-time trigger phrases. The caller MUST be asking to be called back
# (not stating they'll call back themselves). We look for one of these
# request frames first, then parse a time anchor in the same utterance.
_CALLBACK_REQUEST_RE = re.compile(
    r"\b("
    r"call (?:me |us )?back|ring (?:me |us )?back|phone (?:me |us )?back|"
    r"call (?:me|us)|ring (?:me|us)|phone (?:me|us)|reach (?:me|us)|"
    r"try (?:me|us)|hit (?:me|us) up"
    r")\b",
    re.IGNORECASE,
)

# Caller-driven false-positive guard: "I'll call you back" is the caller
# saying THEY will reach out, not a request for us to call them. Skip these.
_CALLER_INITIATED_RE = re.compile(
    r"\b(i'?ll|i will|let me) (?:call|ring|phone|reach) (?:you|back)\b",
    re.IGNORECASE,
)

# Time-anchor patterns. Each compiled regex pairs with a parser that turns
# the matched span into a (delta_kind, delta_value) tuple consumed by
# :func:`_resolve_callback_anchor`. Order matters — match the most specific
# (full date+time) first; least specific ("tomorrow") last.
_CALLBACK_AT_HHMM_RE = re.compile(
    r"\b(?:at|around|by)\s+(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?",
    re.IGNORECASE,
)
_CALLBACK_IN_DURATION_RE = re.compile(
    r"\bin\s+(\d+|a|an|half an|one|two|three|four|five|six)\s+(minute|minutes|min|mins|"
    r"hour|hours|hr|hrs|day|days|week|weeks)\b",
    re.IGNORECASE,
)
_CALLBACK_AFTER_DURATION_RE = re.compile(
    r"\bafter\s+(\d+|a|an|one|two|three|four|five|six)\s+(minute|minutes|min|mins|"
    r"hour|hours|hr|hrs|day|days|week|weeks)\b",
    re.IGNORECASE,
)
_CALLBACK_TOMORROW_RE = re.compile(r"\btomorrow\b", re.IGNORECASE)
_CALLBACK_TODAY_RE = re.compile(r"\b(?:today|this (?:afternoon|evening|morning))\b", re.IGNORECASE)
_CALLBACK_NEXT_WEEK_RE = re.compile(r"\bnext week\b", re.IGNORECASE)
_CALLBACK_WEEKDAY_RE = re.compile(
    r"\b(?:on |this |next |coming )?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
_CALLBACK_PART_OF_DAY_RE = re.compile(
    r"\b(?:in the |this )?(morning|afternoon|evening|noon|night)\b",
    re.IGNORECASE,
)

# Number-word → int for "in two hours" style.
_DURATION_NUMBER_WORDS: dict[str, int] = {
    "a": 1, "an": 1, "half an": 1,  # "half an hour" handled specially below
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
}

# Part-of-day → default local HH:MM. Used when caller says "tomorrow morning"
# with no explicit clock time.
_PART_OF_DAY_HOUR: dict[str, tuple[int, int]] = {
    "morning": (10, 0),
    "afternoon": (14, 0),
    "noon": (12, 0),
    "evening": (17, 0),
    "night": (18, 0),  # cap at end of TRAI window; clamp will push further if needed
}

# Weekday name → ISO weekday number (Monday=1). The parser computes "next
# <weekday>" by walking forward to the next occurrence (today excluded if
# explicitly "next", included if just "<weekday>").
_WEEKDAY_NUM: dict[str, int] = {
    "monday": 1, "tuesday": 2, "wednesday": 3, "thursday": 4,
    "friday": 5, "saturday": 6, "sunday": 7,
}


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


# ── Clinics / healthcare patterns ────────────────────────────────────────────

_SYMPTOM_LEADIN_RE = re.compile(
    r"\b(?:having|feeling|suffering from|been having|i have|i've got|i've had|"
    r"complaining of|down with|got|with)\s+"
    r"((?:[a-z][a-z'-]+\s*){1,6})"
    r"(?=[\s,.;!?]|$)",
    re.IGNORECASE,
)
_SYMPTOM_KEYWORDS = (
    "fever", "cough", "cold", "headache", "migraine", "back pain", "chest pain",
    "stomach pain", "nausea", "vomiting", "diarrhea", "rash", "allergy",
    "sore throat", "body ache", "fatigue", "tired", "dizzy", "dizziness",
    "shortness of breath", "breathlessness", "blood pressure", "bp", "sugar",
    "diabetes", "asthma", "anxiety", "depression", "insomnia", "swelling",
    "infection", "burning", "itching", "tooth pain", "ear pain", "eye pain",
    "knee pain", "joint pain", "period pain", "cramps", "constipation",
)
_APPOINTMENT_TYPE_RE = re.compile(
    r"\b(consultation|follow[\s-]?up|follow up|first visit|new patient|"
    r"second opinion|lab test|blood test|scan|x[\s-]?ray|mri|ct scan|"
    r"checkup|check[\s-]?up|review|vaccination|teleconsult|tele consult|video consult)\b",
    re.IGNORECASE,
)
# "see Dr. Sharma", "doctor Mehta", "with Dr Rao". The "with" lead-in only
# fires when followed by dr/doctor so it doesn't grab "with fever".
_DOCTOR_RE = re.compile(
    r"\b(?:(?:doctor|dr\.?|see)\s+(?:dr\.?\s+|doctor\s+)?|with\s+(?:dr\.?|doctor)\s+)"
    r"([A-Za-z][A-Za-z'-]+(?:\s+[A-Za-z][A-Za-z'-]+){0,2})",
    re.IGNORECASE,
)
# Trailing tokens that aren't part of a doctor's name ("Dr Mehta tomorrow").
_DOCTOR_STOP_WORDS = {
    "tomorrow", "today", "yesterday", "next", "last", "this", "on", "at",
    "in", "for", "please", "morning", "afternoon", "evening", "tonight",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "and", "or", "about", "regarding",
}
_AGE_RE = re.compile(
    r"\b(?:age(?:d)?(?:\s+is)?|i am|i'm|my age is|patient is|(?:she|he) is)\s+"
    r"(\d{1,3})\s*(?:years?(?:\s*old)?|yrs?(?:\s*old)?|yo)?\b",
    re.IGNORECASE,
)
_AGE_SIMPLE_RE = re.compile(r"\b(\d{1,3})\s*(?:years?\s*old|yrs?\s*old|yo)\b", re.IGNORECASE)
_GENDER_RE = re.compile(
    r"\b(?:patient\s+is\s+a?|for\s+a?|my\s+(?:son|daughter|wife|husband|"
    r"mother|father|brother|sister))\s+(male|female|man|woman|boy|girl)\b",
    re.IGNORECASE,
)
_INSURANCE_RE = re.compile(
    r"\b(?:insurance|cover(?:ed)?\s+by|tpa|health\s+plan)\s+(?:is\s+|with\s+|by\s+)?"
    r"([A-Z][A-Za-z&\- ]{2,40}?)(?=[,.;!?]|$)",
    re.IGNORECASE,
)
_PRIOR_VISIT_RE = re.compile(
    r"\b(visited (?:before|last|earlier)|been here (?:before|earlier)|"
    r"existing patient|regular patient|came (?:last|earlier|previously)|"
    r"my last visit (?:was )?(?:in )?[a-z]+)\b",
    re.IGNORECASE,
)


# ── E-commerce / support patterns ────────────────────────────────────────────

_ORDER_ID_RE = re.compile(
    r"\b(?:order\s*(?:no\.?|number|id|#)?\s*(?:is\s+|was\s+|=\s*)?[:#]?\s*|#)"
    r"([A-Z0-9][A-Z0-9-]{4,24})\b",
    re.IGNORECASE,
)
_TRACKING_RE = re.compile(
    r"\b(?:tracking|awb|waybill|consignment)\s*(?:no\.?|number|id|#)?\s*"
    r"(?:is\s+|was\s+|=\s*)?[:#]?\s*"
    r"([A-Z0-9][A-Z0-9-]{5,24})\b",
    re.IGNORECASE,
)
_ISSUE_TYPE_PATTERNS = (
    (re.compile(r"\b(refund|money back|reimburse|return the money)\b", re.IGNORECASE), "refund"),
    (re.compile(r"\b(return|send (?:it )?back|pick(?:\s*up)? return)\b", re.IGNORECASE), "return"),
    (re.compile(r"\b(exchange|replace(?:ment)?|swap)\b", re.IGNORECASE), "exchange"),
    (re.compile(r"\b(damaged|broken|defect(?:ive)?|not working|faulty|spoilt)\b", re.IGNORECASE), "damaged"),
    (re.compile(r"\b(wrong (?:item|product|size|colou?r)|missing item|not (?:as|what) (?:described|ordered))\b", re.IGNORECASE), "wrong_item"),
    (re.compile(r"\b(delivery|shipping|courier|tracking|where is my order|not delivered|late)\b", re.IGNORECASE), "delivery"),
    (re.compile(r"\b(cancel(?:lation)?|cancel my order)\b", re.IGNORECASE), "cancel"),
    (re.compile(r"\b(invoice|gst bill|receipt)\b", re.IGNORECASE), "invoice"),
)
_PAYMENT_METHOD_RE = re.compile(
    r"\b(cod|cash on delivery|upi|gpay|google pay|phonepe|paytm|credit card|debit card|"
    r"net\s*banking|bank transfer|wallet)\b",
    re.IGNORECASE,
)
_ADDRESS_LEADIN_RE = re.compile(
    r"\b(?:ship(?:ping)? (?:address|to)|deliver(?:y)? (?:address|to)|address is)\s+"
    r"(.+?)(?=[.;!?]|$)",
    re.IGNORECASE,
)
# "ordered a blue kettle", "bought the running shoes" — deliberately NOT
# "item is …" / "product is …" because those usually precede a *state*
# ("the item is damaged"), not the item itself.
_ITEM_LEADIN_RE = re.compile(
    r"\b(?:ordered|bought|purchased|received)\s+(?:a|an|the|some|my)?\s*"
    r"((?:[A-Za-z0-9][A-Za-z0-9'-]*\s*){1,5})"
    r"(?=[\s,.;!?]|$)",
    re.IGNORECASE,
)
# Words that describe an issue/state, not a product — reject as item values.
_ITEM_REJECT_WORDS = {
    "damaged", "broken", "defective", "faulty", "wrong", "missing", "late",
    "refund", "return", "exchange", "it", "this", "that", "them", "back",
}
# Tokens that end the item phrase — "ordered a blue kettle but received…"
# should yield "blue kettle", not the whole tail.
_ITEM_STOP_WORDS = {
    "but", "and", "however", "though", "received", "is", "was", "that",
    "which", "from", "with", "because", "since", "yesterday", "today",
    "on", "in", "to", "however,", "last", "next", "this", "ago",
    "week", "month", "weeks", "months", "days", "day",
}


# ── Hospitality patterns ─────────────────────────────────────────────────────

_PARTY_SIZE_RE = re.compile(
    r"\b(?:for|table for|booking for|reservation for|party of|group of|we are|"
    r"there are|there'll be)\s+"
    r"(\d{1,3}|two|three|four|five|six|seven|eight|nine|ten|"
    r"do|teen|char|paanch|rendu|moodu|nalugu|aidu)"
    r"(?:\s+(?:people|adults?|guests?|pax|persons?|of us))?\b",
    re.IGNORECASE,
)
_PARTY_NUMBER_WORDS = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10,
    "do": 2, "teen": 3, "char": 4, "paanch": 5,
    "rendu": 2, "moodu": 3, "nalugu": 4, "aidu": 5,
}
_CHECKIN_RE = re.compile(
    r"\b(?:check[\s-]?in|arriv(?:e|al)|coming on)\s+"
    r"(?:on\s+)?([A-Za-z0-9 ,/-]{3,30}?)(?=[.;!?]|$|\s+(?:and|for|with|to))",
    re.IGNORECASE,
)
_CHECKOUT_RE = re.compile(
    r"\b(?:check[\s-]?out|leav(?:e|ing)|depart(?:ing|ure)|until)\s+"
    r"(?:on\s+)?([A-Za-z0-9 ,/-]{3,30}?)(?=[.;!?]|$|\s+(?:and|for|with))",
    re.IGNORECASE,
)
_ROOM_TYPE_RE = re.compile(
    r"\b(single|double|twin|triple|suite|deluxe|standard|family|king|queen|"
    r"superior|executive|presidential|villa|cottage)\s*(?:room|suite)?\b",
    re.IGNORECASE,
)
_TABLE_TYPE_RE = re.compile(
    r"\b(booth|window (?:seat|table)|outdoor|patio|private dining|bar (?:seat|table))\b",
    re.IGNORECASE,
)
_OCCASION_RE = re.compile(
    r"\b(birthday|anniversary|wedding|honeymoon|business meeting|"
    r"corporate|family (?:gathering|reunion)|date night|engagement|"
    r"baby shower|farewell)\b",
    re.IGNORECASE,
)
_DIETARY_RE = re.compile(
    r"\b(vegetarian|vegan|jain|non[\s-]?veg|halal|kosher|gluten[\s-]?free|"
    r"lactose[\s-]?free|nut[\s-]?free|diabetic[\s-]?friendly|low[\s-]?sodium|"
    r"no onion(?:\s+no garlic)?)\b",
    re.IGNORECASE,
)


# ── Salient-detail capture ───────────────────────────────────────────────────
#
# Things a caller says that aren't a single-valued slot but the agent must
# remember for the rest of the call (and ideally future calls): allergies,
# named family members, callback-time promises, "important" / "please remember"
# statements, specific numbers + units, emotional flags ("very upset"). The
# heuristic deliberately captures *short* utterance fragments — not the whole
# turn — so the prompt block stays compact.

_SALIENT_CUE_PATTERNS = (
    (re.compile(r"\b(?:allergic to|allergy to|cannot (?:eat|have|take)|can'?t (?:eat|have|take))\s+[a-z][a-z\s]{1,40}", re.IGNORECASE), "allergy"),
    (re.compile(r"\b(?:please remember|don'?t forget|make sure (?:that|to)|by the way|fyi|important|note that|keep in mind|remember that)\s+[^.;!?]{3,140}", re.IGNORECASE), "note"),
    (re.compile(r"\b(?:my (?:son|daughter|wife|husband|mother|father|brother|sister|partner|kid|child))\s+[a-z][^.;!?]{2,80}", re.IGNORECASE), "family"),
    (re.compile(r"\b(?:call me|call back|contact me)\s+(?:at|on|around|after|before)\s+[^.;!?]{2,40}", re.IGNORECASE), "callback_time"),
    (re.compile(r"\b(?:very|really|extremely|completely)\s+(?:upset|angry|frustrated|disappointed|unhappy|worried|anxious|stressed)\b", re.IGNORECASE), "emotional_flag"),
    (re.compile(r"\b(?:waiting (?:for|since)|haven'?t (?:received|got))\s+[^.;!?]{3,80}", re.IGNORECASE), "complaint"),
    (re.compile(r"\b(?:my (?:case|complaint|ticket|reference)\s*(?:number|no|id)?\s*(?:is)?)\s*[:#]?\s*[A-Z0-9-]{3,24}", re.IGNORECASE), "reference"),
    (re.compile(r"\b(?:by|before|after|within|in)\s+(?:next\s+)?(?:\d+\s+(?:days?|weeks?|months?|hours?)|(?:january|february|march|april|may|june|july|august|september|october|november|december))\b", re.IGNORECASE), "deadline"),
    (re.compile(r"\b(?:must|need to|have to)\s+(?:be|have|get|finish|complete|deliver(?:ed)?|arrive)\s+[^.;!?]{3,80}", re.IGNORECASE), "requirement"),
)

_SALIENT_MAX_LEN = 160


def _parse_duration_qty(raw: str) -> float | None:
    """Convert the duration quantifier from a callback-time phrase into a
    numeric multiplier. Handles digit literals, the article "a"/"an", and
    "half an" (returns 0.5)."""
    raw = raw.strip().lower()
    if raw == "half an":
        return 0.5
    if raw in _DURATION_NUMBER_WORDS:
        return float(_DURATION_NUMBER_WORDS[raw])
    try:
        n = float(raw)
        return n if n > 0 else None
    except ValueError:
        return None


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
        # Scan *all* matches of each pattern, not just the first. "I'm
        # allergic to penicillin, my name is Ravi" trips the "i'm" lead-in
        # first (rejected on "allergic"); we must keep scanning so the real
        # "my name is Ravi" later in the turn still wins.
        for pat in _NAME_PATTERNS:
            for match in pat.finditer(text):
                tokens = [t for t in match.group(1).split() if t]
                if not tokens:
                    continue
                # Walk left-to-right, take tokens until we hit a reject
                # word. "name is Asha looking for 3BHK" → ["Asha"].
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

    # ── Follow-up agent: opt-out + promised_callback_at ──────────────────

    @staticmethod
    def _extract_opted_out(text: str) -> bool | None:
        """Return True iff the caller used an unambiguous opt-out phrase.

        Returns None (not False) on no match so the existing ``_add`` helper
        skips emitting the fact. Only present when triggered.
        """
        if _OPT_OUT_RE.search(text):
            return True
        return None

    @staticmethod
    def _extract_promised_callback_at(text: str) -> str | None:
        """Extract a callback-time promise from the caller's utterance.

        Two-stage:
          1. Confirm the caller is *asking us to call back* (not the inverse
             "I'll call you back"). Without this guard, "I'll call back
             tomorrow" would schedule a follow-up call we never asked for.
          2. Parse one of several time anchors (in N hours, tomorrow at 4,
             next Tuesday, this evening, etc.) into a TZ-aware ISO string.

        Returns the ISO-8601 string (UTC) the follow-up service can parse, or
        None on no match / no parseable anchor. Always heuristic — confidence
        is implied by the ``MemoryFact.confidence`` set by the caller in
        :meth:`extract_turn`.
        """
        if _CALLER_INITIATED_RE.search(text):
            # Caller said THEY will call — not a request.
            return None
        if not _CALLBACK_REQUEST_RE.search(text):
            return None

        from datetime import datetime, timedelta, time as _t, timezone as _tz
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("Asia/Kolkata")
        except Exception:
            tz = _tz.utc

        now_local = datetime.now(tz)
        target: datetime | None = None
        explicit_hhmm: tuple[int, int] | None = None
        part_of_day: tuple[int, int] | None = None

        # "at 4pm" / "at 4:30" / "by 11"
        hm = _CALLBACK_AT_HHMM_RE.search(text)
        if hm:
            hour = int(hm.group(1))
            minute = int(hm.group(2) or 0)
            meridiem = (hm.group(3) or "").lower().replace(".", "")
            if "pm" in meridiem and hour < 12:
                hour += 12
            elif "am" in meridiem and hour == 12:
                hour = 0
            elif not meridiem and 1 <= hour <= 7:
                # Bare "at 4" in a business-day context: assume PM (16:00),
                # because nobody books a callback for 4 AM.
                hour += 12
            if 0 <= hour <= 23 and 0 <= minute <= 59:
                explicit_hhmm = (hour, minute)

        # Part-of-day fallback ("this evening", "tomorrow morning")
        pod = _CALLBACK_PART_OF_DAY_RE.search(text)
        if pod:
            part_of_day = _PART_OF_DAY_HOUR.get(pod.group(1).lower())

        default_hm = explicit_hhmm or part_of_day or (10, 0)

        # ── Anchor: "in N (hours|minutes|days)" ──
        in_dur = _CALLBACK_IN_DURATION_RE.search(text)
        if in_dur:
            raw_num = in_dur.group(1).lower()
            unit = in_dur.group(2).lower()
            qty = _parse_duration_qty(raw_num)
            if qty is not None:
                if unit.startswith("min"):
                    target = now_local + timedelta(minutes=qty)
                elif unit.startswith("hour") or unit.startswith("hr"):
                    target = now_local + timedelta(hours=qty)
                elif unit.startswith("day"):
                    target = now_local + timedelta(days=qty)
                    target = target.replace(
                        hour=default_hm[0], minute=default_hm[1],
                        second=0, microsecond=0,
                    )
                elif unit.startswith("week"):
                    target = now_local + timedelta(weeks=qty)
                    target = target.replace(
                        hour=default_hm[0], minute=default_hm[1],
                        second=0, microsecond=0,
                    )

        # ── Anchor: "after N hours" (same as "in N hours") ──
        if target is None:
            after_dur = _CALLBACK_AFTER_DURATION_RE.search(text)
            if after_dur:
                raw_num = after_dur.group(1).lower()
                unit = after_dur.group(2).lower()
                qty = _parse_duration_qty(raw_num)
                if qty is not None:
                    if unit.startswith("min"):
                        target = now_local + timedelta(minutes=qty)
                    elif unit.startswith("hour") or unit.startswith("hr"):
                        target = now_local + timedelta(hours=qty)
                    elif unit.startswith("day"):
                        target = now_local + timedelta(days=qty)
                        target = target.replace(
                            hour=default_hm[0], minute=default_hm[1],
                            second=0, microsecond=0,
                        )

        # ── Anchor: "tomorrow" + optional time ──
        if target is None and _CALLBACK_TOMORROW_RE.search(text):
            tomorrow = (now_local + timedelta(days=1)).date()
            target = datetime.combine(
                tomorrow, _t(hour=default_hm[0], minute=default_hm[1]), tzinfo=tz,
            )

        # ── Anchor: "today" / "this afternoon|evening|morning" ──
        if target is None and _CALLBACK_TODAY_RE.search(text):
            target = now_local.replace(
                hour=default_hm[0], minute=default_hm[1], second=0, microsecond=0,
            )
            if target <= now_local:
                # "Today at 4pm" already passed — bump to tomorrow same time.
                target = target + timedelta(days=1)

        # ── Anchor: "next week" ──
        if target is None and _CALLBACK_NEXT_WEEK_RE.search(text):
            target = (now_local + timedelta(days=7)).replace(
                hour=default_hm[0], minute=default_hm[1], second=0, microsecond=0,
            )

        # ── Anchor: weekday name ──
        if target is None:
            wd_match = _CALLBACK_WEEKDAY_RE.search(text)
            if wd_match:
                target_wd = _WEEKDAY_NUM[wd_match.group(1).lower()]
                today_wd = now_local.isoweekday()
                days_ahead = (target_wd - today_wd) % 7
                # If the spoken weekday is today's weekday, assume "next week"
                # — saying "call me Monday" on a Monday almost always means
                # the upcoming Monday, not this exact moment + 1 week.
                if days_ahead == 0:
                    days_ahead = 7
                target = (now_local + timedelta(days=days_ahead)).replace(
                    hour=default_hm[0], minute=default_hm[1], second=0, microsecond=0,
                )

        # ── Anchor: bare "at 4pm" or part-of-day with no day reference ──
        if target is None and (explicit_hhmm or part_of_day):
            target = now_local.replace(
                hour=default_hm[0], minute=default_hm[1], second=0, microsecond=0,
            )
            if target <= now_local:
                target = target + timedelta(days=1)

        if target is None:
            return None
        return target.astimezone(_tz.utc).isoformat()

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
        # An income statement ("I earn 1.5 lakhs a month") is not a budget.
        # Let _extract_income claim those so they don't pollute the budget slot.
        if _INCOME_CUE_RE.search(text):
            return None
        match = _BUDGET_RE.search(text)
        if match:
            return _clean_value(match.group(1))
        return MemoryExtractor._spelled_amount(text)

    @staticmethod
    def _spelled_amount(text: str) -> str | None:
        """Parse a spelled-out amount like 'half a crore' / 'fifty lakhs' /
        'one and a half crore' into a canonical '<n> <unit>' string.

        Semantics: integer words sum into the whole part, fraction words into
        the fractional part. The article 'a'/'an' counts as 1 only when it is
        the sole quantity ('a crore' → 1); in 'half a crore' / 'one and a half
        crore' it's an article, not a number, so it must not add 1."""
        match = _BUDGET_WORD_RE.search(text)
        if not match:
            return None
        tokens = [w for w in match.group(1).lower().split() if w and w not in ("and", "of")]
        unit = _BUDGET_UNIT_CANON.get(match.group(2).lower())
        if not tokens or not unit:
            return None
        integers = [t for t in tokens if t in _NUMBER_WORDS and t not in ("a", "an")]
        fractions = [t for t in tokens if t in _FRACTION_WORDS]
        has_article = any(t in ("a", "an") for t in tokens)
        if integers:
            whole = sum(_NUMBER_WORDS[t] for t in integers)
        elif fractions:
            whole = 0.0  # "half a crore" — the 'a' is an article
        elif has_article:
            whole = 1.0  # "a crore"
        else:
            return None
        total = whole + sum(_FRACTION_WORDS[t] for t in fractions)
        if total <= 0:
            return None
        # Render 1.0 → "1", 0.5 → "0.5", 1.5 → "1.5".
        return f"{total:g} {unit}"

    @staticmethod
    def _extract_income(text: str) -> str | None:
        if not _INCOME_CUE_RE.search(text):
            return None
        match = _INCOME_AMOUNT_RE.search(text)
        if match:
            return _clean_value(match.group(1))
        spelled = MemoryExtractor._spelled_amount(text)
        return spelled

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
        # English "in/near <Place>" first; then the hi/te "<Place> లో/mein/ke paas"
        # postposition order so a Telugu / Hindi turn captures the area too.
        match = _LOCATION_RE.search(text) or _LOCATION_POST_RE.search(text)
        if not match:
            return None
        value = _clean_value(match.group(1))
        tokens = [t for t in value.split() if t.lower() not in _LOCATION_REJECT_WORDS]
        if not tokens:
            return None
        return " ".join(tokens)

    @staticmethod
    def _extract_property(text: str) -> str | None:
        def _usable(value: str) -> str | None:
            tokens = _clean_value(value).strip(" .,-").split()
            # Peel leading/trailing filler so "me about the green meadows"
            # reduces to "green meadows".
            while tokens and tokens[0].lower() in _PROPERTY_LEAD_FILLER:
                tokens.pop(0)
            while tokens and tokens[-1].lower() in _PROPERTY_LEAD_FILLER:
                tokens.pop()
            if not tokens:
                return None
            if all(t.lower() in _PROPERTY_REJECT_WORDS for t in tokens):
                return None
            candidate = " ".join(tokens)
            return candidate if len(candidate) >= 3 else None

        match = _PROPERTY_PROJECT_RE.search(text)
        if match:
            usable = _usable(match.group(1))
            if usable:
                return usable.title()
        match = _PROPERTY_INTEREST_RE.search(text)
        if match:
            usable = _usable(match.group(1))
            if usable:
                return usable
        match = _PROPERTY_POST_RE.search(text)
        if match:
            usable = _usable(match.group(1))
            if usable:
                return usable.title()
        return None

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

    # ── Clinics ──────────────────────────────────────────────────────

    @staticmethod
    def _extract_symptoms(text: str) -> str | None:
        lowered = text.lower()
        hits = [kw for kw in _SYMPTOM_KEYWORDS if kw in lowered]
        if hits:
            # Drop substrings of a longer hit ("pain" when "chest pain"
            # is present), keep declaration order, cap at three.
            hits.sort(key=len, reverse=True)
            kept: list[str] = []
            for kw in hits:
                if any(kw != other and kw in other for other in kept):
                    continue
                kept.append(kw)
            return ", ".join(kept[:3]) if kept else None
        match = _SYMPTOM_LEADIN_RE.search(text)
        if match:
            value = _clean_value(match.group(1))
            if value and len(value) >= 3 and value.lower() not in _NAME_REJECT_WORDS:
                return value.lower()
        return None

    @staticmethod
    def _extract_appointment_type(text: str) -> str | None:
        match = _APPOINTMENT_TYPE_RE.search(text)
        return _clean_value(match.group(1)).lower() if match else None

    @staticmethod
    def _extract_doctor(text: str) -> str | None:
        match = _DOCTOR_RE.search(text)
        if not match:
            return None
        tokens = _clean_value(match.group(1)).split()
        kept: list[str] = []
        for tok in tokens:
            if tok.lower() in _DOCTOR_STOP_WORDS:
                break
            kept.append(tok)
        value = " ".join(kept).strip()
        first = value.split()[0].lower() if value else ""
        if not value or first in {"dr", "doctor"} or first in _NAME_REJECT_WORDS:
            return None
        if len(value) >= 2:
            return f"Dr. {value.title()}"
        return None

    @staticmethod
    def _extract_age(text: str) -> str | None:
        match = _AGE_SIMPLE_RE.search(text) or _AGE_RE.search(text)
        if not match:
            return None
        try:
            age = int(match.group(1))
        except (TypeError, ValueError):
            return None
        if 0 < age <= 120:
            return str(age)
        return None

    @staticmethod
    def _extract_gender(text: str) -> str | None:
        match = _GENDER_RE.search(text)
        if not match:
            return None
        raw = match.group(1).lower()
        if raw in {"male", "man", "boy"}:
            return "male"
        if raw in {"female", "woman", "girl"}:
            return "female"
        return None

    @staticmethod
    def _extract_insurance(text: str) -> str | None:
        match = _INSURANCE_RE.search(text)
        if not match:
            return None
        value = _clean_value(match.group(1))
        return value if value and len(value) >= 3 else None

    @staticmethod
    def _extract_prior_visit(text: str) -> str | None:
        return "yes" if _PRIOR_VISIT_RE.search(text) else None

    # ── E-commerce ───────────────────────────────────────────────────

    @staticmethod
    def _extract_order_id(text: str) -> str | None:
        match = _ORDER_ID_RE.search(text)
        if not match:
            return None
        value = _clean_value(match.group(1)).upper()
        # Reject pure-word captures ("ORDER", "NUMBER") — require a digit.
        if value and any(c.isdigit() for c in value):
            return value
        return None

    @staticmethod
    def _extract_tracking(text: str) -> str | None:
        match = _TRACKING_RE.search(text)
        if not match:
            return None
        value = _clean_value(match.group(1)).upper()
        return value if value and any(c.isdigit() for c in value) else None

    @staticmethod
    def _extract_issue_type(text: str) -> str | None:
        for pat, code in _ISSUE_TYPE_PATTERNS:
            if pat.search(text):
                return code
        return None

    @staticmethod
    def _extract_payment_method(text: str) -> str | None:
        match = _PAYMENT_METHOD_RE.search(text)
        return _clean_value(match.group(1)).lower() if match else None

    @staticmethod
    def _extract_shipping_address(text: str) -> str | None:
        match = _ADDRESS_LEADIN_RE.search(text)
        if not match:
            return None
        value = _clean_value(match.group(1))
        return value if value and len(value) >= 6 else None

    @staticmethod
    def _extract_item(text: str) -> str | None:
        match = _ITEM_LEADIN_RE.search(text)
        if not match:
            return None
        tokens = _clean_value(match.group(1)).split()
        kept: list[str] = []
        for tok in tokens:
            if tok.lower() in _ITEM_STOP_WORDS:
                break
            kept.append(tok)
        value = " ".join(kept).strip()
        if not value or len(value) < 3:
            return None
        first = value.split()[0].lower()
        if first in _NAME_REJECT_WORDS or first in _ITEM_REJECT_WORDS:
            return None
        return value

    # ── Hospitality ──────────────────────────────────────────────────

    @staticmethod
    def _extract_party_size(text: str) -> str | None:
        match = _PARTY_SIZE_RE.search(text)
        if not match:
            return None
        token = match.group(1).lower()
        if token.isdigit():
            n = int(token)
        else:
            n = _PARTY_NUMBER_WORDS.get(token)
        if n and 0 < n <= 100:
            return str(n)
        return None

    @staticmethod
    def _extract_check_in(text: str) -> str | None:
        match = _CHECKIN_RE.search(text)
        if match:
            value = _clean_value(match.group(1))
            if value and len(value) >= 3:
                return value
        # Fall back to a bare date phrase ("arriving tomorrow").
        date_match = _VISIT_DATE_RE.search(text)
        if date_match and re.search(r"\b(check[\s-]?in|arriv|coming)\b", text, re.IGNORECASE):
            return _clean_value(date_match.group(0))
        return None

    @staticmethod
    def _extract_check_out(text: str) -> str | None:
        match = _CHECKOUT_RE.search(text)
        if match:
            value = _clean_value(match.group(1))
            if value and len(value) >= 3:
                return value
        return None

    @staticmethod
    def _extract_room_type(text: str) -> str | None:
        match = _ROOM_TYPE_RE.search(text)
        if match:
            return _clean_value(match.group(0)).lower()
        table_match = _TABLE_TYPE_RE.search(text)
        if table_match:
            return _clean_value(table_match.group(0)).lower()
        return None

    @staticmethod
    def _extract_occasion(text: str) -> str | None:
        match = _OCCASION_RE.search(text)
        return _clean_value(match.group(1)).lower() if match else None

    @staticmethod
    def _extract_dietary(text: str) -> str | None:
        match = _DIETARY_RE.search(text)
        return _clean_value(match.group(1)).lower() if match else None

    # ── Salient details ──────────────────────────────────────────────

    @staticmethod
    def _extract_salient(text: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen_codes: set[str] = set()
        for pat, code in _SALIENT_CUE_PATTERNS:
            match = pat.search(text)
            if not match:
                continue
            # One note per code per turn keeps the bucket from flooding
            # when a long utterance trips several sub-patterns.
            if code in seen_codes:
                continue
            seen_codes.add(code)
            snippet = _clean_value(match.group(0))
            if snippet:
                out.append({"code": code, "text": snippet[:_SALIENT_MAX_LEN]})
        return out

    @classmethod
    def _extractors_for(cls, business_type: str | None) -> tuple[tuple[str, str], ...]:
        """Return the ``(fact_key, method_name)`` extractor list to run.

        Known business type → universal slots + that domain's slots.
        ``None`` / ``"other"`` / unknown → universal + the superset of
        every domain (we don't know which facts matter, so capture
        broadly). The superset is also what keeps legacy callers that
        pass no business type extracting real-estate slots as before.
        """
        bt = str(business_type or "").strip().lower()
        if bt and bt != "other" and bt in _BUSINESS_EXTRACTORS:
            return _UNIVERSAL_EXTRACTORS + _BUSINESS_EXTRACTORS[bt]
        superset: tuple[tuple[str, str], ...] = _UNIVERSAL_EXTRACTORS
        for key in ("real_estate", "clinics", "ecommerce", "hospitality"):
            superset = superset + _BUSINESS_EXTRACTORS[key]
        return superset

    @classmethod
    def extract(
        cls,
        text: str,
        *,
        turn_index: int,
        language: str | None = None,
        role: str = "user",
        business_type: str | None = None,
    ) -> dict[str, Any]:
        """Return ``{"facts": [...], "objections": [...],
        "commitments": [...], "preferences": [...], "salient": [...]}``.

        ``role`` controls how aggressively we trust the text.
        ``"user"`` is the primary source (names, phones, decisions).
        ``"assistant"`` text is mined too — the agent often confirms a
        slot ("Got it, 3BHK in Kompally") and we want that confirmation
        to lock the fact even if the user's earlier utterance was noisy.

        ``business_type`` selects which domain extractors run. When it is
        a known type only that domain's slots are mined (a clinic call
        never extracts BHK); otherwise the full superset runs.
        """
        empty = {"facts": [], "objections": [], "commitments": [], "preferences": [], "salient": []}
        clean_text = re.sub(r"\s+", " ", str(text or "")).strip()
        if not clean_text:
            return empty

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

        # Email is universal and cheap — handle inline.
        email_match = _EMAIL_RE.search(clean_text)
        _add(FACT_EMAIL, email_match.group(0) if email_match else None)

        for fact_key, method_name in cls._extractors_for(business_type):
            extractor = getattr(cls, method_name, None)
            if extractor is None:
                continue
            _add(fact_key, extractor(clean_text))

        # Follow-up agent signals — only mined from caller utterances. The
        # agent's own template "shall I call you back tomorrow?" must not
        # schedule a follow-up; only the human's reply does.
        if role == "user":
            _add(FACT_OPTED_OUT, cls._extract_opted_out(clean_text))
            _add(
                FACT_PROMISED_CALLBACK_AT,
                cls._extract_promised_callback_at(clean_text),
            )

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

        # Salient details — only mined from caller turns. The agent's own
        # phrasing ("please remember to bring your ID") would otherwise be
        # captured as if the caller said it.
        salient: list[dict[str, Any]] = []
        if role == "user":
            for note in cls._extract_salient(clean_text):
                salient.append({**note, "turn": turn_index, "ts": ts})

        return {
            "facts": facts,
            "objections": objections,
            "commitments": commitments,
            "preferences": preferences,
            "salient": salient,
        }


# Extractor registries. ``_UNIVERSAL_EXTRACTORS`` run for every business
# type; the per-type tuples add domain slots. Keep these *after* the class
# so the method references resolve, and reference them by name (string) so
# the registry stays a plain data structure.
_UNIVERSAL_EXTRACTORS: tuple[tuple[str, str], ...] = (
    (FACT_NAME, "_extract_name"),
    (FACT_PHONE, "_extract_phone"),
    (FACT_LANGUAGE_PREF, "_extract_language_pref"),
    (FACT_VISIT_DATE, "_extract_visit_date"),
    (FACT_VISIT_TIME, "_extract_visit_time"),
    (FACT_TIMELINE, "_extract_timeline"),
)

_BUSINESS_EXTRACTORS: dict[str, tuple[tuple[str, str], ...]] = {
    "real_estate": (
        (FACT_BHK, "_extract_bhk"),
        (FACT_BUDGET, "_extract_budget"),
        (FACT_INCOME, "_extract_income"),
        (FACT_PURPOSE, "_extract_purpose"),
        (FACT_LOCATION, "_extract_location"),
        (FACT_PROPERTY, "_extract_property"),
    ),
    "clinics": (
        (FACT_SYMPTOMS, "_extract_symptoms"),
        (FACT_APPOINTMENT_TYPE, "_extract_appointment_type"),
        (FACT_DOCTOR_PREFERENCE, "_extract_doctor"),
        (FACT_PATIENT_AGE, "_extract_age"),
        (FACT_PATIENT_GENDER, "_extract_gender"),
        (FACT_INSURANCE, "_extract_insurance"),
        (FACT_PRIOR_VISIT, "_extract_prior_visit"),
    ),
    "ecommerce": (
        (FACT_ORDER_ID, "_extract_order_id"),
        (FACT_TRACKING_NUMBER, "_extract_tracking"),
        (FACT_ISSUE_TYPE, "_extract_issue_type"),
        (FACT_ITEM, "_extract_item"),
        (FACT_SHIPPING_ADDRESS, "_extract_shipping_address"),
        (FACT_PAYMENT_METHOD, "_extract_payment_method"),
    ),
    "hospitality": (
        (FACT_PARTY_SIZE, "_extract_party_size"),
        (FACT_CHECK_IN, "_extract_check_in"),
        (FACT_CHECK_OUT, "_extract_check_out"),
        (FACT_ROOM_TYPE, "_extract_room_type"),
        (FACT_OCCASION, "_extract_occasion"),
        (FACT_DIETARY, "_extract_dietary"),
    ),
    "other": (),
}


# Which slot labels to surface in the prompt block per business type. The
# universal keys appear for everyone; domain keys only for their domain.
# ``None`` / "other" shows the universal set plus anything that was actually
# captured (handled in ``compose_prompt_block``).
_UNIVERSAL_PROMPT_KEYS: tuple[str, ...] = (
    FACT_NAME, FACT_PHONE, FACT_EMAIL, FACT_COMPANY, FACT_LANGUAGE_PREF,
    FACT_FAMILY_SIZE, FACT_REQUESTED_INFO, FACT_URGENCY, FACT_TIMELINE,
    FACT_VISIT_DATE, FACT_VISIT_TIME,
)
_BUSINESS_PROMPT_KEYS: dict[str, tuple[str, ...]] = {
    "real_estate": (FACT_BHK, FACT_BUDGET, FACT_INCOME, FACT_LOCATION, FACT_PURPOSE, FACT_PROPERTY),
    "clinics": (
        FACT_SYMPTOMS, FACT_APPOINTMENT_TYPE, FACT_DOCTOR_PREFERENCE,
        FACT_PATIENT_AGE, FACT_PATIENT_GENDER, FACT_INSURANCE, FACT_PRIOR_VISIT,
    ),
    "ecommerce": (
        FACT_ORDER_ID, FACT_ITEM, FACT_ISSUE_TYPE, FACT_SHIPPING_ADDRESS,
        FACT_TRACKING_NUMBER, FACT_PAYMENT_METHOD,
    ),
    "hospitality": (
        FACT_PARTY_SIZE, FACT_CHECK_IN, FACT_CHECK_OUT, FACT_ROOM_TYPE,
        FACT_OCCASION, FACT_DIETARY, FACT_SEATING_PREFERENCE,
    ),
    "other": (),
}


def _prompt_keys_for(business_type: str | None) -> tuple[str, ...] | None:
    """Ordered slot keys to render in the prompt block for this business
    type. ``None`` return means "render every known fact" (used for the
    ``other`` / unknown case where we can't predict the relevant slots)."""
    bt = str(business_type or "").strip().lower()
    if bt and bt != "other" and bt in _BUSINESS_PROMPT_KEYS:
        return _UNIVERSAL_PROMPT_KEYS + _BUSINESS_PROMPT_KEYS[bt]
    return None


def _durable_fact_keys_for(business_type: str | None) -> tuple[str, ...]:
    """Subset of facts worth persisting across calls for this business
    type. Identity + stable preferences always; domain facts that stay
    true between calls (budget, insurance, dietary) when known.

    Follow-up facts (``promised_callback_at``, ``opted_out``) are always
    durable — they're the contract the follow-up scheduler reads at call
    end, and they must survive the gap between call and next call (when the
    call ends prematurely without the scheduler being driven).
    """
    # Follow-up signals are universal — every business type needs them so
    # the opt-out kill switch and promise-extraction loops fire regardless
    # of whether the org is a clinic, real-estate brokerage, etc.
    followup = (FACT_PROMISED_CALLBACK_AT, FACT_OPTED_OUT)
    base = (FACT_NAME, FACT_EMAIL, FACT_COMPANY, FACT_LANGUAGE_PREF, FACT_FAMILY_SIZE)
    bt = str(business_type or "").strip().lower()
    domain: dict[str, tuple[str, ...]] = {
        "real_estate": (FACT_BHK, FACT_BUDGET, FACT_INCOME, FACT_LOCATION, FACT_PURPOSE, FACT_TIMELINE),
        "clinics": (FACT_DOCTOR_PREFERENCE, FACT_PATIENT_AGE, FACT_PATIENT_GENDER, FACT_INSURANCE, FACT_PRIOR_VISIT),
        "ecommerce": (FACT_SHIPPING_ADDRESS, FACT_PAYMENT_METHOD),
        "hospitality": (FACT_ROOM_TYPE, FACT_DIETARY, FACT_SEATING_PREFERENCE, FACT_PARTY_SIZE),
    }
    if bt and bt != "other" and bt in domain:
        return followup + base + domain[bt]
    # Unknown / other → persist identity plus every domain's durable keys.
    combined = followup + base
    for keys in domain.values():
        combined = combined + keys
    return combined


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
    # Free-form important caller statements (allergies, deadlines, named
    # family, "please remember…") that aren't single-valued slots but the
    # agent must keep front-of-mind for the rest of the call.
    salient_notes: list[dict[str, Any]] = field(default_factory=list)
    asked_questions: list[dict[str, Any]] = field(default_factory=list)
    # Buying-journey stage the caller reached on their previous call (set
    # by ``bootstrap_caller_memory``). Lets the strategy layer open a
    # returning caller where they left off instead of from discovery.
    prior_stage: str | None = None

    @property
    def bootstrap_keys(self) -> set[str]:
        """Computed view of which facts came from a prior call.

        Historically tracked as a parallel ``set[str]`` instance field on
        the dataclass; consolidated onto :attr:`MemoryFact.from_prior_call`
        during the session-state unification so there's a single source of
        truth. Existing callers that iterated over ``bootstrap_keys`` (e.g.
        the prompt-block renderer) keep working unchanged. Mutating callers
        (``bootstrap_caller_memory``) now stamp the per-fact flag directly.
        """
        return {key for key, fact in self.facts.items() if fact.from_prior_call}

    # ── Serialisation ────────────────────────────────────────────────

    def to_state_blob(self) -> dict[str, Any]:
        return {
            "facts": {k: v.to_dict() for k, v in self.facts.items()},
            "objections": list(self.objections),
            "commitments": list(self.commitments),
            "preferences": list(self.preferences),
            "salient_notes": list(self.salient_notes),
            "asked_questions": list(self.asked_questions),
            # ``bootstrap_keys`` is computed from per-fact flags now, but
            # we keep emitting it in the serialised shape so an older code
            # path that reads this blob during a deploy rollover still
            # sees the prior-call markers it expects.
            "bootstrap_keys": sorted(self.bootstrap_keys),
            "prior_stage": self.prior_stage,
        }

    @classmethod
    def from_state_blob(cls, blob: Any) -> "ConversationalMemory":
        data = blob if isinstance(blob, dict) else {}
        facts_raw = data.get("facts") or {}
        legacy_bootstrap_keys: set[str] = set(data.get("bootstrap_keys") or [])
        facts: dict[str, MemoryFact] = {}
        if isinstance(facts_raw, dict):
            for key, payload in facts_raw.items():
                if not isinstance(payload, dict):
                    continue
                fact = MemoryFact.from_dict(payload)
                # Legacy migration: an older blob carried prior-call provenance
                # in the parallel ``bootstrap_keys`` list. Re-stamp each fact
                # so the new computed property answers correctly on the very
                # next read.
                if not fact.from_prior_call and fact.key in legacy_bootstrap_keys:
                    fact.from_prior_call = True
                facts[str(key)] = fact
        return cls(
            facts=facts,
            objections=list(data.get("objections") or []),
            commitments=list(data.get("commitments") or []),
            preferences=list(data.get("preferences") or []),
            salient_notes=list(data.get("salient_notes") or []),
            asked_questions=list(data.get("asked_questions") or []),
            prior_stage=(str(data["prior_stage"]) if data.get("prior_stage") else None),
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
        for note in extracted.get("salient") or []:
            self._accept_salient(dict(note))
        # Keep the buckets bounded so a 50-turn call doesn't accumulate
        # 50 "interested" entries. Latest 16 of each is plenty.
        self.objections = self.objections[-16:]
        self.commitments = self.commitments[-16:]
        self.preferences = self.preferences[-16:]
        self.salient_notes = self.salient_notes[-16:]

    def _accept_salient(self, note: dict[str, Any]) -> None:
        """Append a salient note, de-duping on the captured text so the
        same allergy mentioned three times doesn't appear three times."""
        text = str(note.get("text") or "").strip().lower()
        if not text:
            return
        for existing in self.salient_notes:
            if str(existing.get("text") or "").strip().lower() == text:
                return
        self.salient_notes.append(note)

    def merge_text(
        self,
        text: str,
        *,
        turn_index: int,
        language: str | None = None,
        role: str = "user",
        business_type: str | None = None,
    ) -> dict[str, Any]:
        extracted = MemoryExtractor.extract(
            text,
            turn_index=turn_index,
            language=language,
            role=role,
            business_type=business_type,
        )
        self.merge_extracted(extracted)
        if role == "user":
            self._resolve_prior_objections(turn_index, text)
        return extracted

    def _resolve_prior_objections(self, turn_index: int, text: str) -> None:
        """Stamp ``resolved_at_turn`` on every prior-turn live objection when
        the caller's current turn signals acceptance.

        Two signals trigger resolution:

          * a :data:`_RESOLUTION_PATTERNS` match in the caller's text
            ("ok makes sense", "fair enough", "got it", …); or
          * any commitment captured on the CURRENT turn (already in
            ``self.commitments`` after :meth:`merge_extracted` ran) — a
            soft "interested" / "ready_to_book" after an objection means
            the rebuttal landed.

        When resolution fires, every objection from a turn ``< turn_index``
        that wasn't already resolved or restored from a prior call gets
        ``resolved_at_turn=<turn_index>``. We also append a synthetic
        ``objection_resolved`` commitment so :func:`conversation_strategy.latest_turn`
        advances — without that, the FSM's "latest signal is an objection"
        check would still see the old objection as live.
        """
        if not self.objections:
            return
        resolution_match = any(pat.search(text or "") for pat in _RESOLUTION_PATTERNS)
        has_current_commitment = any(
            isinstance(c, dict)
            and c.get("turn") == turn_index
            and not c.get("from_prior_call")
            for c in self.commitments
        )
        if not (resolution_match or has_current_commitment):
            return
        changed = False
        for obj in self.objections:
            if not isinstance(obj, dict):
                continue
            if obj.get("from_prior_call"):
                continue
            if obj.get("resolved_at_turn") is not None:
                continue
            turn = obj.get("turn")
            if isinstance(turn, int) and turn < turn_index:
                obj["resolved_at_turn"] = turn_index
                changed = True
        if changed and resolution_match and not has_current_commitment:
            # Synthetic commitment carries the latest_turn forward so the FSM
            # exits objection_handling this turn even when the caller didn't
            # use any of the canonical commitment phrases. ``ts`` follows the
            # rest of this module's convention — ``time.time()`` float, not
            # an ISO string.
            self.commitments.append({
                "code": "objection_resolved",
                "text": (text or "")[:200],
                "turn": turn_index,
                "ts": time.time(),
            })
            self.commitments = self.commitments[-16:]

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

    def compose_prompt_block(
        self,
        language: str | None = None,
        *,
        business_type: str | None = None,
    ) -> str:
        """Render the memory as a system-prompt fragment the LLM is
        told to honour. Empty string when nothing is known yet (the
        caller should drop the section entirely).

        ``business_type`` restricts which slots are rendered so a clinic
        call doesn't show BHK lines. When it is ``None`` / ``"other"`` /
        unknown, every known fact is rendered (we can't predict which
        slots matter for an unclassified business)."""
        prompt_keys = _prompt_keys_for(business_type)
        if prompt_keys is None:
            # Unknown business: render whatever was captured, in the
            # canonical label order.
            ordered_keys = [k for k in SLOT_LABELS if self.has(k)]
        else:
            ordered_keys = [k for k in prompt_keys if k in SLOT_LABELS]

        lines: list[str] = []
        for key in ordered_keys:
            fact = self.facts.get(key)
            if not fact or fact.value in (None, "", []):
                continue
            label = SLOT_LABELS.get(key, key)
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

        # Salient details get their own labelled sub-block so the LLM
        # treats them as must-remember context, not just slots.
        salient_lines: list[str] = []
        for note in self.salient_notes[-8:]:
            text = str(note.get("text") or "").strip()
            if text:
                salient_lines.append(f"  - {text}")

        if not lines and not salient_lines:
            return ""

        header = (
            "# CONVERSATIONAL MEMORY — already known about this caller\n"
            "Treat the following as established facts. Do NOT ask the caller for them again. "
            "Build your next reply around what is known; if a fact contradicts what they just said, "
            "briefly acknowledge the correction ('Ah, my mistake') and update accordingly. "
            "Facts marked '(from a prior call)' come from an earlier conversation — reference them "
            "naturally to show continuity, but confirm rather than assume if acting on them.\n"
        )
        block = header
        if lines:
            block += "\n".join(lines)
        if salient_lines:
            if lines:
                block += "\n"
            block += "Key details to remember:\n" + "\n".join(salient_lines)
        return block


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

# Salient-note codes that stay true between calls (an allergy doesn't expire
# at hang-up). Call-specific codes — emotional_flag, complaint, callback_time,
# deadline — are deliberately excluded so the next call doesn't re-surface a
# stale "very upset" or a deadline that has since passed.
_DURABLE_SALIENT_CODES = frozenset({"allergy", "family", "note", "requirement", "reference"})
_CALLER_SALIENT_MAX = 6

# Objection / commitment codes worth carrying across calls. Transient ones
# (``call_later``, ``repetition_complaint``) describe a moment, not a standing
# position, so they're excluded — only durable buying-journey signals persist.
_DURABLE_OBJECTION_CODES = frozenset({"price_concern", "competitor", "long_horizon"})
_DURABLE_COMMITMENT_CODES = frozenset({"ready_to_book", "info_requested", "callback_requested", "interested"})
_CALLER_BUCKET_MAX = 4


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
    business_type: str | None = None,
) -> ConversationalMemory:
    """Seed ``memory`` with facts persisted from prior calls for the
    same (tenant, caller phone) pair. Idempotent and best-effort —
    failures are swallowed so the live call always proceeds."""
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
    allowed = set(_durable_fact_keys_for(business_type))
    # Always allow language preference and core identity even if the
    # stored business type differs from the current one.
    allowed.update({FACT_NAME, FACT_EMAIL, FACT_LANGUAGE_PREF, FACT_COMPANY})
    facts_raw = payload.get("facts") or {}
    if isinstance(facts_raw, dict):
        for key, fact_payload in facts_raw.items():
            if key not in allowed or not isinstance(fact_payload, dict):
                continue
            if memory.has(key):
                # Live-call fact already wins; bootstrap stays out of the
                # way. We still tag the existing fact so the prompt knows
                # the slot was known historically (not silently corrected).
                existing_fact = memory.facts.get(key)
                if existing_fact is not None:
                    existing_fact.from_prior_call = True
                continue
            # Bootstrap facts come in at slightly reduced confidence so an
            # in-call correction can override them without needing the
            # explicit "no actually" prefix.
            fact = MemoryFact.from_dict(fact_payload)
            fact.confidence = min(fact.confidence, 0.7)
            fact.source_turn = -1  # signal "from before this call"
            fact.from_prior_call = True
            memory.facts[key] = fact

    # Restore durable salient notes (allergies, named family, standing
    # requirements) tagged so the prompt block flags them as prior-call.
    notes_raw = payload.get("salient_notes") or []
    if isinstance(notes_raw, list):
        for note in notes_raw:
            if not isinstance(note, dict):
                continue
            restored = dict(note)
            restored["from_prior_call"] = True
            memory._accept_salient(restored)
        memory.salient_notes = memory.salient_notes[-16:]

    # Restore the buying-journey context: which objections the caller raised,
    # what they committed to, and the stage they reached. Tagged
    # ``from_prior_call`` so the strategy layer treats them as history (not a
    # fresh signal) and the lead score is driven by the current call.
    # Objections persist as ``{code, text}`` now, but blobs written before that
    # change stored bare code strings — accept both shapes.
    prior_objections: list[dict[str, str]] = []
    for raw in (payload.get("objections") or []):
        if isinstance(raw, str):
            code, text = raw, ""
        elif isinstance(raw, dict):
            code = str(raw.get("code") or "")
            text = str(raw.get("text") or "").strip()
        else:
            continue
        if not code:
            continue
        entry: dict[str, str] = {"code": code}
        if text:
            entry["text"] = text
        prior_objections.append(entry)
    for entry in prior_objections:
        restored: dict[str, Any] = {"code": entry["code"], "from_prior_call": True}
        if entry.get("text"):
            restored["text"] = entry["text"]
        memory.objections.append(restored)
    memory.objections = memory.objections[-16:]

    prior_commitments = [
        str(c) for c in (payload.get("commitments") or []) if isinstance(c, str)
    ]
    for code in prior_commitments:
        memory.commitments.append({"code": code, "from_prior_call": True})
    memory.commitments = memory.commitments[-16:]

    stage = payload.get("stage")
    memory.prior_stage = str(stage) if stage else None

    # One human-readable continuity line so the agent opens warm and can
    # reference the prior arc ("last time we were arranging a site visit").
    continuity_bits: list[str] = []
    if memory.prior_stage:
        continuity_bits.append(f"reached the {memory.prior_stage.replace('_', ' ')} stage")
    if prior_objections:
        obj_bits: list[str] = []
        for entry in prior_objections[:2]:
            label = entry["code"].replace("_", " ")
            text = entry.get("text")
            obj_bits.append(f'{label} ("{text}")' if text else label)
        continuity_bits.append("raised " + ", ".join(obj_bits))
    if prior_commitments:
        continuity_bits.append("committed: " + ", ".join(c.replace("_", " ") for c in prior_commitments[:2]))
    if continuity_bits:
        memory._accept_salient(
            {
                "code": "note",
                "text": "Returning caller — last call: " + "; ".join(continuity_bits) + ".",
                "from_prior_call": True,
            }
        )
        memory.salient_notes = memory.salient_notes[-16:]
    return memory


async def promote_to_caller_memory(
    tenant_res: TenantResources,
    *,
    phone: Any,
    memory: ConversationalMemory,
    business_type: str | None = None,
    call_id: str | None = None,
) -> None:
    """Persist the durable subset of ``memory`` to the per-caller
    Redis blob so the next call for the same phone opens warm. Keyed by
    the per-tenant namespace + normalised phone, so memory never leaks
    across tenants.

    Idempotency: when ``call_id`` is supplied, the session state is
    checked for a ``caller_memory_promoted_at`` marker. If already set,
    the promote is a no-op. This defends against the failure mode where
    the unified-store TTL extension (history 600s → unified 900s) lets a
    delayed post-call hook (re-handled hangup webhook, manually
    retriggered outcome classifier) pick up an alive-but-stale blob and
    double-promote — which would clobber the caller-memory entry with
    duplicate data and reset its 30-day TTL twice for the same call.
    """
    norm = _normalise_phone(phone)
    if not norm:
        return
    if not memory:
        return
    # Idempotency check — best-effort. A Redis hiccup is treated as
    # "no marker", so we err on the side of doing the promote (preferable
    # to silently dropping caller-memory updates).
    if call_id:
        try:
            state = await AgentSessionStore.get_state(tenant_res, call_id)
            if isinstance(state, dict) and state.get("caller_memory_promoted_at"):
                logger.debug(
                    "NOKVO-MEMORY: promote_to_caller_memory skipped for %s — already promoted",
                    call_id,
                )
                return
        except Exception:
            logger.debug(
                "NOKVO-MEMORY: idempotency probe failed for %s; proceeding with promote",
                call_id,
                exc_info=True,
            )
    payload_facts: dict[str, Any] = {}
    for key in _durable_fact_keys_for(business_type):
        fact = memory.facts.get(key)
        if fact is None or fact.value in (None, "", []):
            continue
        payload_facts[key] = fact.to_dict()

    payload_notes: list[dict[str, Any]] = []
    for note in memory.salient_notes:
        # Don't re-persist a continuity note synthesized from a prior call —
        # only genuinely durable facts the caller stated this call.
        if note.get("from_prior_call"):
            continue
        if str(note.get("code") or "") in _DURABLE_SALIENT_CODES:
            payload_notes.append({k: note[k] for k in ("code", "text") if k in note})
        if len(payload_notes) >= _CALLER_SALIENT_MAX:
            break

    # Durable buying-journey signals raised on THIS call (de-duped, latest few).
    def _durable_codes(bucket: list[dict[str, Any]], allowed: frozenset[str]) -> list[str]:
        out: list[str] = []
        for entry in bucket:
            if entry.get("from_prior_call"):
                continue
            code = str(entry.get("code") or "")
            if code in allowed and code not in out:
                out.append(code)
        return out[-_CALLER_BUCKET_MAX:]

    # Objections carry the caller's actual wording so a returning caller can be
    # met addressing the concern by name. Keep the LATEST text per code.
    def _durable_objections(bucket: list[dict[str, Any]]) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        seen: set[str] = set()
        for entry in reversed(bucket):  # latest first
            if entry.get("from_prior_call"):
                continue
            code = str(entry.get("code") or "")
            if code not in _DURABLE_OBJECTION_CODES or code in seen:
                continue
            seen.add(code)
            item: dict[str, str] = {"code": code}
            text = str(entry.get("text") or "").strip()
            if text:
                item["text"] = text[:120]
            out.append(item)
        out.reverse()  # restore chronological order
        return out[-_CALLER_BUCKET_MAX:]

    payload_objections = _durable_objections(memory.objections)
    payload_commitments = _durable_codes(memory.commitments, _DURABLE_COMMITMENT_CODES)

    stage: str | None = None
    try:
        from app.services.conversation_strategy import journey_stage

        stage = journey_stage(memory, business_type=business_type)
    except Exception:
        logger.debug("NOKVO-MEMORY: journey_stage failed during promote", exc_info=True)

    if not (payload_facts or payload_notes or payload_objections or payload_commitments or stage):
        return
    payload = {
        "facts": payload_facts,
        "salient_notes": payload_notes,
        "objections": payload_objections,
        "commitments": payload_commitments,
        "stage": stage,
        "business_type": str(business_type or "").strip().lower() or None,
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
        return
    # Stamp the idempotency marker AFTER the durable write succeeded so a
    # transient Redis error during promotion doesn't permanently block
    # a retry. Best-effort — failure to write the marker only costs one
    # extra duplicate-promote if the WS reconnects, which is bounded by
    # the same 30-day TTL anyway.
    if call_id:
        try:
            await AgentSessionStore.merge_state(
                tenant_res,
                call_id,
                {"caller_memory_promoted_at": time.time()},
            )
        except Exception:
            logger.debug(
                "NOKVO-MEMORY: failed to stamp caller_memory_promoted_at marker",
                exc_info=True,
            )


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


def seed_facts(
    memory: ConversationalMemory,
    facts: dict[str, Any],
    *,
    confidence: float = 0.55,
) -> None:
    """Inject externally-known facts (a CRM lead form, the outbound turn-memory
    dict) into ``memory`` as low-confidence, gap-filling :class:`MemoryFact`s.

    This is how the outbound path unifies its lightweight ``outbound_memory``
    dict with the richer structured memory the strategy layer reads: the
    strategy block and the "already known" prompt block then see the same
    facts. Confidence is deliberately low so :meth:`_accept` never lets a seed
    override a higher-confidence in-call extraction of the same slot."""
    payload: list[MemoryFact] = []
    for key, value in (facts or {}).items():
        if value in (None, "", []):
            continue
        payload.append(
            MemoryFact(
                key=str(key),
                value=value,
                confidence=confidence,
                source_turn=-1,
                timestamp=time.time(),
            )
        )
    if payload:
        memory.merge_extracted({"facts": payload})


__all__ = [
    "BUCKET_ASKED",
    "BUCKET_COMMITMENTS",
    "BUCKET_OBJECTIONS",
    "BUCKET_PREFERENCES",
    "ConversationalMemory",
    # Universal
    "FACT_COMPANY",
    "FACT_EMAIL",
    "FACT_FAMILY_SIZE",
    "FACT_LANGUAGE_PREF",
    "FACT_NAME",
    "FACT_PHONE",
    "FACT_REQUESTED_INFO",
    "FACT_TIMELINE",
    "FACT_URGENCY",
    "FACT_VISIT_DATE",
    "FACT_VISIT_TIME",
    # Real estate
    "FACT_BHK",
    "FACT_BUDGET",
    "FACT_INCOME",
    "FACT_LOCATION",
    "FACT_PROPERTY",
    "FACT_PURPOSE",
    # Clinics
    "FACT_APPOINTMENT_TYPE",
    "FACT_DOCTOR_PREFERENCE",
    "FACT_INSURANCE",
    "FACT_PATIENT_AGE",
    "FACT_PATIENT_GENDER",
    "FACT_PRIOR_VISIT",
    "FACT_SYMPTOMS",
    # E-commerce
    "FACT_ISSUE_TYPE",
    "FACT_ITEM",
    "FACT_ORDER_ID",
    "FACT_PAYMENT_METHOD",
    "FACT_SHIPPING_ADDRESS",
    "FACT_TRACKING_NUMBER",
    # Hospitality
    "FACT_CHECK_IN",
    "FACT_CHECK_OUT",
    "FACT_DIETARY",
    "FACT_OCCASION",
    "FACT_PARTY_SIZE",
    "FACT_ROOM_TYPE",
    "FACT_SEATING_PREFERENCE",
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
    "seed_facts",
]
