from __future__ import annotations

import re
from typing import Any

from app.services.nokvo_one_business_templates import normalize_business_type
from app.services.tool_flow_questions import build_tool_flow_questions, generated_questions_from_status
from app.services.voice_turn_policy import (
    _AVAILABILITY_QUERY_RE,
    _appointment_is_past,
    _coming_back_prefix,
    _email_confirmation_prompt,
    _id_confirmation_prompt,
    _is_high_confidence_phone,
    _is_side_question_during_booking,
    _language_code,
    _looks_affirmative,
    _looks_negative,
    _phone_confirmation_prompt,
    _shift_to_next_day_prompt,
    extract_turn_entities,
    normalize_phone_number,
)


# ── Domain-specific inference cues ────────────────────────────────────────
# When the caller volunteers slot data in any utterance (often the opener
# or a free-text "reason"), we pre-fill the corresponding slots so the FSM
# doesn't ask again. Same idea as the clinic visit_type / urgent_symptoms
# inference, generalised per business.
_BUDGET_INFER_RE = re.compile(
    r"\b(?:budget(?:\s+(?:is|of|around|under|upto|up\s+to))?\s*(?:₹|rs\.?|inr)?\s*"
    r"(\d+(?:[.,]\d+)?)\s*(?:lakh|lakhs|l|crore|crores|cr|k|thousand)?)\b|"
    r"\b(?:around|approx(?:imately)?|under|upto|up\s+to)\s+(?:₹|rs\.?|inr)?\s*"
    r"(\d+(?:[.,]\d+)?)\s*(?:lakh|lakhs|l|crore|crores|cr|k|thousand)\b",
    re.IGNORECASE,
)
_PROPERTY_TYPE_INFER_RE = re.compile(
    r"\b(\d\s*(?:bhk|bedroom|bed|rk)|"
    r"apartment|flat|villa|studio|penthouse|plot|land|"
    r"independent\s+house|row\s+house|commercial|office\s+space|shop)\b",
    re.IGNORECASE,
)
# Case-insensitive: voice STT emits lowercase ("in kokapet"), so the old
# capitalized-only pattern silently dropped every spoken location.
_LOCATION_INFER_RE = re.compile(
    r"\b(?:in|at|near|around)\s+([a-zA-Z][a-zA-Z]*(?:\s+[a-zA-Z]+){0,2})\b",
    re.IGNORECASE,
)
# Telugu / Hindi put the postposition AFTER the place ("Kokapet లో",
# "Kondapur mein", "Gachibowli ke paas"). The place name itself is almost
# always Latin even in code-switched te/hi STT, so we capture the Latin run
# immediately before a postposition. Bare "me"/"lo" are excluded — too
# collision-prone with English "me" / "lo".
_LOCATION_INFER_POST_RE = re.compile(
    r"([a-zA-Z][a-zA-Z]+(?:\s+[a-zA-Z]+){0,1})\s*"
    # Script postpositions need no trailing \b (combining marks aren't word-
    # boundaried); Latin ones keep \b so "mein" doesn't fire mid-word.
    r"(?:(?:లో|లోని|దగ్గర|వద్ద|में|मे|के\s*पास)|(?:ke\s+paas|ke\s+pass|mein)\b)",
    re.IGNORECASE,
)
# Tokens that follow "in/at/near" but aren't a place — keeps the inference
# from filling the location slot with filler like "in the area".
_LOCATION_INFER_STOPWORDS = {
    "the", "a", "an", "this", "that", "it", "your", "our", "any", "some",
    "area", "areas", "budget", "person", "future", "fact", "general", "mind",
    "touch", "case", "order",
}
_PARTY_SIZE_INFER_RE = re.compile(
    r"\b(?:for|of|table\s+for|party\s+of|group\s+of)\s+(\d{1,2})\s*"
    r"(?:people|persons|guests|adults|pax)?\b",
    re.IGNORECASE,
)
_ORDER_ID_INFER_RE = re.compile(
    r"\b(?:order\s*(?:id|number|no\.?|#)?|"
    r"ticket\s*(?:id|number|no\.?|#)?|"
    r"reference\s*(?:id|number|no\.?|#)?)\s*[:\-]?\s*"
    r"([A-Z0-9]{4,}[-A-Z0-9]*)\b",
    re.IGNORECASE,
)
_ISSUE_TYPE_INFER_RE = re.compile(
    r"\b(damaged|broken|wrong\s+(?:item|size|colour|color)|missing|defective|"
    r"not\s+working|delayed|never\s+(?:arrived|delivered)|"
    r"return(?:\s+request)?|refund\s+request)\b",
    re.IGNORECASE,
)


_BUSINESS_INFERENCE_KINDS = {
    "real_estate": {"budget", "property_type", "location"},
    "hospitality": {"party_size"},
    "ecommerce": {"order_id", "issue_type"},
}


def _infer_domain_slots(
    text: str,
    business_type: str | None,
    bundle: dict[str, Any],
    flow_key: str,
    collected: dict[str, Any],
) -> list[str]:
    """Mine the caller's utterance for slot values they volunteered. Pre-fill
    only when the slot is empty AND it exists in this flow's schema."""
    filled: list[str] = []
    if not text:
        return filled
    normalised = (business_type or "").lower()
    kinds = _BUSINESS_INFERENCE_KINDS.get(normalised, set())
    if not kinds:
        return filled
    available_slots = {str(slot.get("key")): str(slot.get("kind") or "") for slot in _flow_slots(bundle, flow_key)}

    def _try_fill(kind: str, value: Any) -> None:
        for key, slot_kind in available_slots.items():
            if slot_kind == kind and not collected.get(key):
                collected[key] = value
                filled.append(key)
                return
        # Fall back to matching by slot key when the schema uses descriptive
        # kinds (e.g., kind="generic" with key "budget").
        for key in available_slots:
            if kind in key and not collected.get(key):
                collected[key] = value
                filled.append(key)
                return

    if "budget" in kinds:
        m = _BUDGET_INFER_RE.search(text)
        if m:
            raw = m.group(1) or m.group(2)
            if raw:
                try:
                    _try_fill("budget", float(raw.replace(",", "")))
                except ValueError:
                    _try_fill("budget", raw)
    if "property_type" in kinds:
        m = _PROPERTY_TYPE_INFER_RE.search(text)
        if m:
            _try_fill("property_type", m.group(1).strip())
    if "location" in kinds:
        m = _LOCATION_INFER_RE.search(text) or _LOCATION_INFER_POST_RE.search(text)
        if m:
            place = m.group(1).strip()
            # Drop leading filler ("the area" -> "area" -> rejected) and skip
            # when nothing place-like remains.
            place_tokens = [t for t in place.split() if t.lower() not in _LOCATION_INFER_STOPWORDS]
            if place_tokens:
                _try_fill("location", " ".join(place_tokens))
    if "party_size" in kinds:
        m = _PARTY_SIZE_INFER_RE.search(text)
        if m:
            try:
                _try_fill("party_size", int(m.group(1)))
            except ValueError:
                pass
    if "order_id" in kinds:
        m = _ORDER_ID_INFER_RE.search(text)
        if m:
            _try_fill("order_id", m.group(1).strip())
    if "issue_type" in kinds:
        m = _ISSUE_TYPE_INFER_RE.search(text)
        if m:
            _try_fill("issue_type", m.group(1).lower().strip())
    return filled


# Kinds that warrant a read-back handshake before we persist. Names are
# already handled; this list covers high-stakes numeric/identifier fields
# where STT errors are common and silent corruption is bad (wrong phone,
# wrong order ID).
_CONFIRMATION_KINDS = {"phone", "email", "reference_number"}

# Names on a telephony line mis-transcribe ("Nihar" → "Nikhil" → "Lord") and
# re-confirming each fresh STT guess loops forever. Cap the read-backs: after this
# many confirmation prompts, accept the best-effort name (flagged unconfirmed) and
# move on — the auto-captured caller phone lets the team verify the spelling later.
_MAX_NAME_CONFIRM_ATTEMPTS = 2

# How many times we'll rigidly offer "same time on the next day" before giving
# up on the shift and surfacing the next actually-available slot via the
# scheduler. Caps the past-time loop: a caller who keeps naming times that have
# already passed today gets a concrete bookable option instead of the agent
# re-offering tomorrow forever.
_MAX_PAST_SHIFT_OFFERS = 1


_YES_RE = re.compile(r"\b(yes|yeah|yep|sure|ok|okay|please|book|schedule)\b|(అవును|సరే|చేయండి|బుక్|हाँ|हा|ठीक)", re.IGNORECASE)


def _name_confirmation_prompt(name: str, language: str | None) -> str:
    lang = _language_code(language)
    if lang == "te":
        return f"Just to confirm — పేరు {name} అని. Right ఆ?"
    if lang == "hi":
        return f"बस कन्फर्म करना है — नाम {name} है। सही है?"
    return f"Just to confirm — the name is {name}. Is that right?"


def _date_kind_keys(bundle: dict[str, Any], flow_key: str) -> list[str]:
    return [
        str(slot.get("key"))
        for slot in _flow_slots(bundle, flow_key)
        if str(slot.get("kind") or "") == "date"
    ]


def _time_kind_keys(bundle: dict[str, Any], flow_key: str) -> list[str]:
    return [
        str(slot.get("key"))
        for slot in _flow_slots(bundle, flow_key)
        if str(slot.get("kind") or "") == "time"
    ]

_VISIT_INTENT_RE = re.compile(
    r"\b(site\s+visit|visit|"
    r"see\s+(?:it|the\s+)?(?:property|flat|house|place|project|apartment|villa|model|sample)|"
    r"view(?:ing)?|schedule\s+a\s+visit|book\s+a\s+(?:visit|tour|viewing)|"
    r"come\s+(?:by|over|in|down|and\s+(?:see|look|visit)|to\s+(?:see|visit|view))|"
    r"drop\s+(?:by|in)|stop\s+by|walk[\s-]?in|"
    r"take\s+a\s+(?:tour|look)|tour|"
    r"show\s+me\s+(?:around|the)|look\s+at\s+(?:it|the|your)|"
    r"check\s+(?:it|the\s+\w+)\s+out|in\s+person|"
    # Transliterated Telugu / Hindi visit cues (Latin, so \b + IGNORECASE work).
    r"chuda(?:li|lani|dam|ne)|choodal(?:i|ani)|chupinch(?:andi|u)|chupistara|"
    r"raav?ali|raav?aalani|vasta(?:nu|m|ru)|vacchi\s+chuda|"
    r"dekhna|dekhne(?:\s+(?:aana|aaunga|aaun))?|aana\s+(?:hai|chahta|chahti|chahte)|"
    r"dikh(?:ao|aao|aiye|ayein|wado)|ghoom(?:na|ne)?)\b|"
    # Devanagari / Telugu script visit cues.
    r"(విజిట్|చూడాలి|చూడాలని|చూడటానికి|చూడొచ్చా|చూపించండి|రావాలి|రావాలని|టూర్|"
    r"సైట్\s*విజిట్|ప్రాపర్టీ\s*చూడ|"
    r"देखना|देखने|देखने\s*आना|आना\s*(?:है|चाहता|चाहती)|दिखाइए|दिखाओ|घूम|"
    r"विजिट|साइट\s*विजिट|आकर\s*देख)",
    re.IGNORECASE,
)
_LEAD_INTENT_RE = re.compile(
    r"\b(interested|looking\s+for|"
    r"need\s+(?:details|info(?:rmation)?|a\s+quote)|"
    r"more\s+(?:details|info(?:rmation)?)|"
    r"send\s+(?:me\s+)?(?:details|brochure|info(?:rmation)?|the\s+\w+)|"
    r"contact\s+me|call\s+me\s+back|call\s+me|reach\s+(?:out\s+to\s+)?me|get\s+back\s+to\s+me|"
    r"enquir(?:y|e|ing)|inquir(?:y|e|ing)|"
    r"want\s+to\s+know|tell\s+me\s+(?:more|about)|"
    r"price(?:\s+list)?|pricing|cost|rate|how\s+much|kitna|enta|"
    r"quote|details?\s+about|"
    # Transliterated Telugu / Hindi enquiry cues.
    r"vivar(?:alu|aalu|am)|ja+n?kari|"
    r"details?\s+kavali|brochure\s+kavali|"
    r"call\s+chey(?:andi|u)|call\s+kar(?:ein|o|na|iye)|phone\s+chey(?:andi|u)|"
    r"sampradinch(?:andi|u)|aasakti)\b|"
    # Devanagari / Telugu script enquiry cues.
    r"(ఆసక్తి|డీటెయిల్స్|వివరాలు|వివరం|ధర|రేటు|బ్రోషర్|కావాలి|"
    r"కాల్\s*చేయండి|సంప్రదించండి|కాంటాక్ట్|"
    r"संपर्क|जानकारी|डिटेल्स|दिलचस्पी|कीमत|रेट|ब्रोशर|कॉल\s*कर|फ़ोन\s*कर|चाहिए)",
    re.IGNORECASE,
)
# Brochure-on-WhatsApp request → triggers the real-estate whatsapp_mode FSM. Kept
# focused (explicit "brochure", or send/share/whatsapp + a document noun) so a
# generic enquiry ("what's the price?") does NOT flip the agent into send mode.
_BROCHURE_REQUEST_RE = re.compile(
    r"\bbrochure\b"
    r"|(?:send|share|whatsapp|forward|mail|e-?mail|text|drop)\b[^.?!]{0,40}"
    r"\b(?:details?|info(?:rmation)?|floor\s*plan|price\s*list|pdf|document|catalogue?|catalog)\b"
    r"|brochure\s+kavali|brochure\s+bhej(?:o|iye|na)?|ब्रोशर|బ్రోషర్",
    re.IGNORECASE,
)


def detect_brochure_request(text: str | None) -> bool:
    """True when the caller asks for the brochure / project details to be sent to
    them (e.g. on WhatsApp). Drives the ``whatsapp_mode`` FSM trigger; the actual
    send happens via the ``request_brochure`` tool."""
    return bool(text and _BROCHURE_REQUEST_RE.search(str(text)))


def brochure_intent_active(
    text: str | None,
    history: list[dict[str, str]] | None = None,
    *,
    lookback: int = 3,
) -> bool:
    """Sticky brochure intent. Stays True for a few turns after the caller first
    asks for the brochure, so a follow-up like "yeah" or reading out a number
    doesn't drop the agent out of whatsapp_mode mid-exchange. Once a brochure
    send is logged (``request_brochure``), the agent's turn carries
    ``brochure sent`` / ``on its way`` wording — but we key off the request side
    only, capped by ``lookback`` recent USER turns so it self-expires."""
    if detect_brochure_request(text):
        return True
    recent_user_turns = [
        str(t.get("content") or "")
        for t in (history or [])
        if str(t.get("role") or "").lower() == "user"
    ][-lookback:]
    return any(detect_brochure_request(t) for t in recent_user_turns)


# Clinic appointment-booking intent (starts the clinic_appointment flow).
_APPOINTMENT_INTENT_RE = re.compile(
    r"\b(?:book|schedule|make|fix|get|need|want|take)\b.{0,30}\b"
    r"(?:appointment|appt|consultation|consult|checkup|check[\s-]?up|sitting|booking|slot)\b|"
    r"\b(?:see|meet|consult|visit)\b.{0,20}\b(?:doctor|dr\.?|physician|specialist|dentist|consultant)\b|"
    # Transliterated Hindi / Telugu.
    r"appointment\s*(?:chahiye|kavali)|doctor\s*ko?\s*dikha|"
    # Devanagari / Telugu script.
    r"(?:अपॉइंटमेंट|अपॉइंटमेण्ट|डॉक्टर\s*को\s*दिखा|अपॉइंटमेंट\s*चाहिए|"
    r"అపాయింట్‌మెంట్|డాక్టర్\s*ని\s*చూడ|అపాయింట్‌మెంట్\s*కావాలి)",
    re.IGNORECASE,
)
_APPOINTMENT_OFFER_RE = re.compile(
    r"\b(?:book|schedule|set\s+up|arrange)\b.{0,30}\b(?:appointment|consultation|slot|visit)\b|"
    r"\bshall\s+i\s+(?:book|schedule)\b|\bwould\s+you\s+like\s+to\s+book\b",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b[^@\s]+@[^@\s]+\.[^@\s]+\b")
_NUMBER_RE = re.compile(r"(?:₹|rs\.?|inr)?\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE)

# Indian budget phrasing → rupees. "1.5 cr" -> 15000000, "50 lakhs" -> 5000000.
_BUDGET_UNIT_RE = re.compile(
    r"(?:₹|rs\.?|inr)?\s*(\d+(?:\.\d+)?|a|an|one)\s*(crores?|cr|lakhs?|lacs?|thousand|k)\b",
    re.IGNORECASE,
)
_BUDGET_UNIT_MULT = {
    "crore": 1e7, "crores": 1e7, "cr": 1e7,
    "lakh": 1e5, "lakhs": 1e5, "lac": 1e5, "lacs": 1e5,
    "thousand": 1e3, "k": 1e3,
}


def _parse_budget_amount(text: str):
    """Normalise a spoken budget to rupees. Returns the MAX amount mentioned
    (the affordability ceiling), else a bare number, else the raw text so a
    vague answer like "not too much" is preserved rather than dropped."""
    lowered = (text or "").lower().replace(",", "")
    amounts: list[float] = []
    for m in _BUDGET_UNIT_RE.finditer(lowered):
        qty_raw, unit = m.group(1), m.group(2).lower()
        try:
            qty = 1.0 if qty_raw in ("a", "an", "one") else float(qty_raw)
        except ValueError:
            continue
        mult = _BUDGET_UNIT_MULT.get(unit)
        if mult:
            amounts.append(qty * mult)
    if amounts:
        return max(amounts)
    bare = _NUMBER_RE.search(lowered)
    if bare:
        try:
            return float(bare.group(1))
        except ValueError:
            return text
    return text

# Free-text slot kinds where the user might inadvertently dictate a question
# (e.g., "what services do you offer?") that we must NOT accept as the slot
# value. Strict slot kinds like phone/email/date/time have their own narrow
# extractors and reject question-shaped input naturally.
_FREE_TEXT_SLOT_KINDS = {"name", "reason", "location", "property_type", "generic"}
_QUESTION_SHAPED_RE = re.compile(
    r"\?\s*$|"
    r"\b(what|where|when|why|how|can|could|would|will|do|does|did|is|are|tell|explain|list|share)\b\s+(?:you|i|me|us|the|your|me\s+about)|"
    r"\b(is\s+it\s+possible|could\s+you|can\s+you|would\s+you)\b|"
    r"(ఏమి|ఎక్కడ|ఎప్పుడు|ఎలా|చెప్పగలరా|క్या|कहाँ|कब|कैसे|बताइए|बताओ)",
    re.IGNORECASE,
)

# Word-based question detection (everything in _QUESTION_SHAPED_RE EXCEPT the
# bare trailing "?"). A hesitant name answer often ends with rising-intonation
# "?", which isn't the caller asking a question — so for names we only bail on
# a genuine question word, not a stray "?".
_QUESTION_WORD_RE = re.compile(
    r"\b(what|where|when|why|how|can|could|would|will|do|does|did|is|are|tell|explain|list|share)\b\s+(?:you|i|me|us|the|your|me\s+about)|"
    r"\b(is\s+it\s+possible|could\s+you|can\s+you|would\s+you)\b|"
    # Contraction questions: "what's the price", "how's the location", "where's…".
    r"\b(?:what|how|where|when|who)'?s\b|"
    r"(ఏమి|ఎక్కడ|ఎప్పుడు|ఎలా|చెప్పగలరా|క్या|कहाँ|कब|कैसे|बताइए|बताओ)",
    re.IGNORECASE,
)

# A bare affirmation / negation / filler is NEVER a valid DATA-slot value
# (the yes/no confirmation handshake is handled separately, before slot
# extraction). Rejecting these stops a confirmation "yes" — or a stray "ok" —
# from being stored as a name / project / reason and skipping the real prompt.
_AFFIRMATION_ONLY_RE = re.compile(
    r"^(?:(?:yes|yeah|yep|yup|ya|yah|yaa|sure|ok|okay|okey|fine|right|correct|"
    r"exactly|absolutely|definitely|alright|no|nope|nah|negative|please|"
    r"that'?s|thats|go|ahead|do)\b[\s,.!?]*)+$",
    re.IGNORECASE,
)


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


# ── Name extraction ────────────────────────────────────────────────────────
# The name slot is the most abused: callers answer it with whole sentences
# ("Yeah, I'd like to book a site visit"), prepend discourse markers
# ("You know Nihar"), or restate the question ("the name is Nihar"). A naive
# "take the whole utterance" approach then confirms garbage as the name. The
# extractor below peels discourse fillers + lead-in phrases, drops honorifics,
# and walks tokens until a non-name word — rejecting request/filler utterances
# outright so the FSM re-asks instead of booking under a bogus name.

# Discourse fillers / titles that can precede a real name answer; stripped
# repeatedly so "well, you know, it's Nihar" peels down to "Nihar".
_NAME_DISCOURSE_PREFIX_RE = re.compile(
    r"^(?:\s*(?:yeah|yes|yep|yup|ya|yah|yaa|no|nope|nah|ok|okay|fine|well|so|umm?|uhh?|hmm+|"
    r"oh|hi+|hello|hey|sure|right|alright|actually|see|look|listen|please|"
    r"you\s+know|i\s+mean|it'?s|its|that'?s|"
    r"i\s+think|i\s+guess|i\s+believe|i\s+suppose|let\s+me\s+think|think|maybe|probably|"
    r"mr\.?|mrs\.?|ms\.?|dr\.?)\b[\s,\.]*)+",
    re.IGNORECASE,
)

# Lead-in phrases that explicitly introduce the speaker's name.
_NAME_LEADIN_RE = re.compile(
    r"^(?:"
    r"(?:the\s+)?(?:my\s+)?name'?s|"
    r"(?:the\s+)?(?:my\s+)?name\s+(?:is|would\s+be)|"
    r"i\s+am|i'?m|this\s+is|myself|call\s+me|you\s+can\s+call\s+me|"
    # Transliterated Hindi / Telugu lead-ins ("mera naam (hai)", "naa peru").
    r"mera\s+naam(?:\s+hai)?|naam\s+hai|naam|naa\s+peru|naa\s+pesaru|naa\s+peeru|"
    r"నా\s+పేరు|పేరు|మీ\s+పేరు|मेरा\s+नाम|नाम"
    # An explicit separator (not \b): Indic combining vowel marks aren't
    # treated as word chars, so \b fails after e.g. "పేరు".
    r")[\s,:]+",
    re.IGNORECASE,
)

# Honorifics / trailing discourse to drop from the tail of a name answer.
_NAME_HONORIFIC_SUFFIX_RE = re.compile(
    r"\s*(?:గారు|అండి|ండి|garu|sir|madam|ma'?am|ji|जी|here|speaking|calling|"
    r"this\s+side)\.?\s*$",
    re.IGNORECASE,
)

# Tokens that are never part of a personal name. Used to trim trailing words
# ("Nihar speaking") and, when they appear first, to reject the whole
# utterance as a request/filler rather than a name.
_NAME_STOP_WORDS = {
    "yeah", "yes", "yep", "yup", "no", "nope", "nah", "not", "really",
    "ok", "okay", "well",
    # Affirmation / discourse fillers (incl. transliterated Hindi "yes") — a
    # caller saying "aha" / "haan" is acknowledging, not giving their name.
    "aha", "ah", "aah", "huh", "uhhuh", "uh-huh", "haan", "han", "haa", "haa",
    "so", "um", "uh", "hmm", "oh", "hi", "hello", "hey", "sure", "right",
    "alright", "actually", "please", "thanks", "thank", "you", "your",
    "yours", "my", "me", "mine", "i", "im", "i'm", "i'd", "id", "we", "they",
    "it", "its", "it's", "the", "a", "an", "this", "that", "these", "those",
    "there", "here", "like", "liked", "want", "wanna", "would", "could",
    "can", "cannot", "need", "needed", "looking", "look", "book", "booking",
    "schedule", "scheduling", "visit", "reserve", "reservation", "check",
    "checking", "interested", "have", "has", "had", "do", "does", "did",
    "know", "tell", "give", "get", "got", "see", "speak", "speaking", "talk",
    "talking", "calling", "site", "appointment", "slot", "details", "detail",
    "info", "information", "bhk", "flat", "apartment", "villa", "project",
    "property", "price", "budget", "name", "names", "and", "or", "but", "to",
    "for", "of", "with", "in", "at", "on", "about", "is", "are", "was",
    "were", "be", "guys", "help", "trying",
    # Additions surfaced by the eval framework: "Yeah, just wanted to know
    # about your projects" was extracting as "Just Wanted". The leading
    # adverbs / intent verbs are never part of a name.
    "just", "wanted", "wants", "wanting", "could", "should", "might", "may",
    "asking", "asked",
}


# Polite decline phrases the caller might say when they DON'T want to book a
# visit or hand over personal info. These should never be confirmed as names
# even though they contain no individual stop word at index 0 ("not" was added
# to the stop set above, but a tokenwise rule misses multi-word forms like
# "no thank you" or "not at the moment").
_NAME_DECLINE_PHRASES_RE = re.compile(
    r"^\s*(?:"
    r"not\s+(?:really|interested|at\s+(?:the\s+)?(?:moment|time)|right\s+now|now|today)|"
    r"no\s+(?:thanks|thank\s+you|thank\s+you\s+for|don'?t|not\s+now)|"
    r"i\s+(?:don'?t|do\s+not)\s+want|"
    r"maybe\s+(?:later|next\s+time|some\s+other\s+time)|"
    r"some\s+other\s+time"
    r")\b",
    re.IGNORECASE,
)


def _extract_person_name(raw: str) -> str | None:
    value = _clean(raw)
    if not value:
        return None
    # Polite decline ("Not really, but thank you...", "No thanks") is never a
    # name — reject it BEFORE peeling discourse prefixes, otherwise tokens like
    # "really" survive the strip and get title-cased to "Not Really".
    if _NAME_DECLINE_PHRASES_RE.search(value):
        return None
    # A question is never a name — catches "what's your timing", "how much is
    # it" even without a trailing "?" (which the slot-level guard keys on).
    if _QUESTION_WORD_RE.search(value):
        return None
    # Peel discourse fillers and a name lead-in, alternating until stable so
    # "well, you know, the name is Nihar" reduces to "Nihar".
    prev: str | None = None
    while prev != value and value:
        prev = value
        value = _NAME_DISCOURSE_PREFIX_RE.sub("", value)
        value = _NAME_LEADIN_RE.sub("", value)
    value = _NAME_HONORIFIC_SUFFIX_RE.sub("", value)
    value = value.strip(" ,.-।॥…・「」（）'\"?!")
    cleaned = _clean(value)
    if not cleaned:
        return None
    lowered = cleaned.lower()
    # Pure digits / punctuation can't be names.
    if re.fullmatch(r"[\d\s\W]+", cleaned):
        return None
    # Day-of-week / relative-day answers belong to a date slot, not a name.
    if re.search(
        r"\b(mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|"
        r"friday|saturday|sunday|today|tomorrow|tonight|yesterday|next\s+\w+)\b",
        lowered,
    ):
        return None
    # Time-of-day phrases belong to a time slot.
    if re.search(
        r"\b(\d{1,2}\s*(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.))\b|"
        r"\b(noon|midnight|morning|afternoon|evening|night)\b",
        lowered,
    ):
        return None
    # Timeline / intent / discovery answers that arrive as one-word replies to
    # "buy immediately or exploring?" / "self-use or investment?" / "weekday or
    # weekend?" must NEVER be confirmed as a name. Without this, "Immediately"
    # title-cases to a valid-looking name and the agent confirms "the name is
    # Immediately." Single-utterance match only so multi-word names containing
    # these tokens still parse.
    if lowered in {
        "immediately", "asap", "soon", "later", "eventually", "sometime",
        "urgent", "urgently", "exploring", "browsing", "casually", "browse",
        "away",  # "right away" peels to "away" after discourse-prefix strip
        "investment", "investor", "rental", "rent",
        # Affirmation / acknowledgment particles — common single-word replies
        # to a yes/no question that must NOT be confirmed as a name.
        "yes", "yeah", "yep", "yup", "ya", "yah", "yaa", "uh-huh", "uhuh", "mhm", "mmhm",
        "no", "nope", "nah", "maybe", "definitely", "probably",
        "sure", "okay", "ok", "alright", "fine",
        "weekday", "weekdays", "weekend", "weekends",
    }:
        return None
    if re.fullmatch(
        r"right\s+away|in\s+a\s+few\s+(?:days|weeks|months)|"
        r"this\s+(?:week|month|year)|next\s+(?:week|month|year)|"
        r"within\s+\d+\s+(?:days|weeks|months)|"
        r"self[\s-]?use|own\s+use|end\s+use|for\s+(?:self|investment|family|living)",
        lowered,
    ):
        return None
    # Walk tokens, stopping at the first non-name word. A stop word in first
    # position means the utterance is a request/filler ("I'd like to book…"),
    # so reject it entirely.
    kept: list[str] = []
    for tok in re.split(r"\s+", cleaned):
        bare = re.sub(r"[^A-Za-zऀ-෿']", "", tok)
        if not bare:
            break
        if bare.lower() in _NAME_STOP_WORDS:
            break
        kept.append(bare)
        if len(kept) >= 3:
            break
    if not kept:
        return None
    name = " ".join(kept).strip(" '")
    # Need at least two alphabetic characters total (rejects stray "K", "Hi").
    if len(re.sub(r"[^A-Za-zऀ-෿]", "", name)) < 2:
        return None
    # Title-case pure-ASCII names; leave Indic-script names untouched.
    if re.fullmatch(r"[A-Za-z' ]+", name):
        name = name.title()
    return name


def _language(language: str | None) -> str:
    return (language or "en").split("-")[0].lower()


def _last_assistant_offered_visit(history: list[dict[str, str]], *, lookback: int = 4) -> bool:
    for turn in reversed((history or [])[-lookback:]):
        if turn.get("role") != "assistant":
            continue
        text = str(turn.get("content") or "")
        if _VISIT_INTENT_RE.search(text) or re.search(
            r"\b(schedule|book|arrange|set\s+up|fix).{0,30}\bvisit\b", text, re.IGNORECASE
        ):
            return True
    return False


# Non-committal / declining replies that must NOT be read as visit acceptance
# even when they carry a date ("maybe later") or a time. Without this guard a
# hesitant enquiry caller who happens to mention a month would be misfiled as a
# booked site visit. Only gates the weaker date/time acceptance signal below —
# an explicit "yes" after a visit offer is still honoured.
_VISIT_NONCOMMIT_RE = re.compile(
    r"\b("
    r"just\s+(?:exploring|looking|browsing|checking)|"
    r"not\s+sure|don'?t\s+know|haven'?t\s+decided|"
    r"maybe\s+(?:later|some\s+other|another\s+time|next\s+time)|some\s+other\s+time|"
    r"thinking\s+about\s+it|need\s+to\s+(?:think|check|discuss|talk)|"
    r"no\s+(?:thanks?|thank\s+you)|"
    r"not\s+(?:now|today|interested|right\s+now|at\s+the\s+(?:moment|time))|"
    r"can'?t\s+(?:come|make\s+it)|won'?t\s+be\s+able|"
    # Telugu / Hindi declines.
    r"వద్దు|ఇప్పుడు\s*కాదు|रहने\s*दो|अभी\s*नहीं|नहीं\s*चाहिए"
    r")\b",
    re.IGNORECASE,
)


def _turn_proposes_datetime(text: str) -> bool:
    """True when the caller's turn carries a concrete date or time — the natural
    way to accept "when would you like to come?" without saying "yes". Reuses the
    shared (multilingual) entity extractor so te/hi booking turns count too."""
    try:
        entities = extract_turn_entities(text, expected_slot=None)
    except Exception:
        return False
    return bool(entities.get("date_text") or entities.get("time_text"))


def caller_agreed_to_site_visit(history: list[dict[str, str]]) -> bool:
    """True when a caller turn signals intent to come for a site visit — an
    explicit visit phrase, OR an acceptance of a visit the agent offered. The
    acceptance can be a "yes"/affirmation OR a concrete date/time ("Saturday
    around 4"), since a caller naturally accepts "when would you like to come?"
    by naming a slot rather than saying "yes". Without the date/time path the
    booking was being misfiled as an enquiry lead instead of a Site Visit."""
    turns = history or []
    for idx, turn in enumerate(turns):
        if turn.get("role") != "user":
            continue
        text = str(turn.get("content") or "")
        if not text.strip():
            continue
        # (a) The caller names a visit themselves — agreement regardless of offer.
        if _VISIT_INTENT_RE.search(text):
            return True
        # (b) The caller accepts a visit the agent offered in the run-up to this
        # turn. A wider look-back than the live flow-start (6 vs 4) tolerates a
        # clarifying exchange between the offer and the acceptance.
        if not _last_assistant_offered_visit(turns[:idx], lookback=6):
            continue
        if _YES_RE.search(text):
            return True
        # A concrete date/time IS the acceptance, unless the same turn reads as
        # non-committal / a deferral.
        if _turn_proposes_datetime(text) and not _VISIT_NONCOMMIT_RE.search(text):
            return True
    return False


# Phrases the agent uses when it offers to share more info / details / brochure
# / a callback. When the lead replies "yeah" to one of these, we should start
# the leads_create flow so the slot-filling kicks in (name/phone first).
# Mirrors :func:`_last_assistant_offered_visit` but for lead-capture offers.
_LEAD_OFFER_RE = re.compile(
    r"\b("
    r"want\s+(?:to\s+know\s+)?(?:more|details?|info(?:rmation)?)|"
    r"(?:share|send|give)\s+(?:you\s+)?(?:more\s+)?(?:details?|info(?:rmation)?|the\s+brochure)|"
    r"(?:would\s+you\s+like|interested\s+in)\s+(?:more\s+)?(?:details?|info(?:rmation)?|amenities|location|brochure|features|pricing|price)|"
    r"need\s+(?:more\s+)?(?:details?|info(?:rmation)?|a\s+quote)|"
    r"(?:can|may)\s+(?:i|we)\s+(?:share|send)|"
    r"call\s+(?:you\s+)?back|reach\s+(?:back|out\s+to\s+you)|get\s+(?:back|in\s+touch)\s+(?:to|with)\s+you|"
    r"explain\s+(?:more|further|the\s+\w+)|tell\s+you\s+(?:more|about)"
    r")\b",
    re.IGNORECASE,
)


def _last_assistant_offered_lead_capture(history: list[dict[str, str]]) -> bool:
    """True when one of the last 4 assistant turns offered to share details /
    info / brochure / a callback — i.e. anything that a "yes" from the lead
    should plausibly turn into a lead-capture flow."""
    for turn in reversed((history or [])[-4:]):
        if turn.get("role") != "assistant":
            continue
        text = str(turn.get("content") or "")
        if _LEAD_OFFER_RE.search(text):
            return True
    return False


def _last_assistant_offered_appointment(history: list[dict[str, str]]) -> bool:
    """True when a recent assistant turn offered to book an appointment, so a
    bare "yes" starts the clinic_appointment flow."""
    for turn in reversed((history or [])[-4:]):
        if turn.get("role") != "assistant":
            continue
        if _APPOINTMENT_OFFER_RE.search(str(turn.get("content") or "")):
            return True
    return False


def _question_for_slot(bundle: dict[str, Any], flow_key: str, slot_key: str, language: str | None) -> str:
    lang = _language(language)
    flow = ((bundle.get("flows") or {}).get(flow_key) or {})
    for slot in flow.get("slots") or []:
        if slot.get("key") == slot_key:
            questions = slot.get("questions") or {}
            return str(questions.get(lang) or questions.get("en") or f"Please share {slot.get('label') or slot_key}.")
    return {
        "hi": f"कृपया {slot_key.replace('_', ' ')} बताइए.",
        "te": f"{slot_key.replace('_', ' ')} చెప్పండి.",
    }.get(lang, f"Please share {slot_key.replace('_', ' ')}.")


def _field_kind_for_slot(bundle: dict[str, Any], flow_key: str, slot_key: str) -> str:
    flow = ((bundle.get("flows") or {}).get(flow_key) or {})
    for slot in flow.get("slots") or []:
        if slot.get("key") == slot_key:
            return str(slot.get("kind") or "generic")
    return "generic"


def _flow_slots(bundle: dict[str, Any], flow_key: str) -> list[dict[str, Any]]:
    flow = ((bundle.get("flows") or {}).get(flow_key) or {})
    return [slot for slot in (flow.get("slots") or []) if isinstance(slot, dict)]


def _next_slot(flow_state: dict[str, Any], bundle: dict[str, Any]) -> str | None:
    collected = dict(flow_state.get("collected") or {})
    for slot in _flow_slots(bundle, str(flow_state.get("flow_key") or "")):
        if slot.get("required", True) and not collected.get(slot.get("key")):
            return str(slot.get("key"))
    return None


def _extract_value(text: str, slot_key: str, kind: str) -> Any:
    value = _clean(text).strip(" ,.-")
    if not value:
        return None
    # A bare affirmation/negation/filler is never a valid data-slot value — drop
    # it so a confirmation token can't become the next slot's value.
    if _AFFIRMATION_ONLY_RE.match(value):
        return None
    if kind == "phone" or slot_key in {"phone", "mobile", "contact_phone"}:
        return normalize_phone_number(value, expected=True)
    if kind == "email" or slot_key == "email":
        match = _EMAIL_RE.search(value)
        return match.group(0) if match else None
    if kind in {"date", "time"} or slot_key in {"visit_date", "visit_time"}:
        entities = extract_turn_entities(value, expected_slot="preferred_date" if kind == "date" else "preferred_time")
        return entities.get("date_text") if kind == "date" else entities.get("time_text")
    if kind == "datetime":
        # A combined "Date and Time" field (one configured slot). The question
        # asks for both, so a firm answer carries both ("tomorrow at 11"). We
        # only fill when BOTH are present — matching the "a firm booking needs a
        # date AND time" rule in _site_visit_args_from_call_state. A partial
        # answer leaves the slot pending so the FSM re-asks (the combined
        # question primes the caller to give both); the conversational-memory
        # facts still accumulate, so the end-of-call safety net can upgrade an
        # enquiry to a site visit if both later land.
        entities = extract_turn_entities(value)
        date_text = entities.get("date_text")
        time_text = entities.get("time_text")
        if date_text and time_text:
            return f"{date_text} {time_text}"
        return None
    if kind == "budget":
        return _parse_budget_amount(value)
    if (
        kind in _FREE_TEXT_SLOT_KINDS
        and _QUESTION_SHAPED_RE.search(value)
        and not (kind == "name" and not _QUESTION_WORD_RE.search(value))
    ):
        # The caller dictated a question instead of answering this slot. Don't
        # consume the text — let the route layer yield to RAG and resume the
        # slot on the next turn.
        return None
    if kind == "name" or slot_key in {"name", "customer_name", "full_name"}:
        # Strip Indic sentence terminators / STT noise up front, then hand to
        # the dedicated name extractor which peels discourse + lead-in phrases
        # and rejects request/filler utterances ("I'd like to book a visit").
        return _extract_person_name(value.strip(" ,.-।॥…・「」（）"))
    if kind == "project" or slot_key in {"project", "project_name"}:
        # Strip lead-in phrasing like "interested in", "I'd like", "the".
        cleaned = re.sub(
            r"^(?:i(?:'m| am)?\s+(?:interested in|looking at|considering)|interested in|the|in)\s+",
            "",
            value,
            flags=re.IGNORECASE,
        ).strip(" ,.-।॥")
        return _clean(cleaned) or None
    return value


def _start_flow_key(text: str, business_type: str | None, history: list[dict[str, str]]) -> str | None:
    if normalize_business_type(business_type) == "real_estate" and (
        _VISIT_INTENT_RE.search(text) or (_YES_RE.search(text) and _last_assistant_offered_visit(history))
    ):
        return "real_estate_site_visit"
    if normalize_business_type(business_type) in ("clinics", "services") and (
        _APPOINTMENT_INTENT_RE.search(text) or (_YES_RE.search(text) and _last_assistant_offered_appointment(history))
    ):
        return "clinic_appointment"
    # Lead-capture trigger. Either an explicit interest phrase from the lead
    # ("send me details", "I'm interested") OR a plain "yes/yeah" right after
    # the agent offered to share details / a brochure / a callback.
    #
    # Real estate is EXCLUDED: it never runs a mid-call lead slot-fill. An
    # enquiry just stays conversational (QUERY mode) and, if no site visit is
    # booked, a lead is created automatically at end-of-call from the ANI +
    # call summary (see maybe_create_real_estate_lead_from_call). Interrogating
    # an enquiry caller for name/phone was the "going dumb" behaviour.
    # Clinics are EXCLUDED for the same reason: clinics have no leads at all —
    # every caller is captured in the Customer base automatically (ANI +
    # post-call notes), so there is no leads_create flow to start.
    if normalize_business_type(business_type) not in ("real_estate", "clinics") and (
        _LEAD_INTENT_RE.search(text)
        or (_YES_RE.search(text) and _last_assistant_offered_lead_capture(history))
    ):
        return "leads_create"
    return None


def _flow_action(flow_state: dict[str, Any]) -> dict[str, Any] | None:
    flow_key = str(flow_state.get("flow_key") or "")
    collected = dict(flow_state.get("collected") or {})
    if flow_key == "real_estate_site_visit":
        return {
            "tool_key": "qualify_lead_and_schedule_visit",
            "flow_key": flow_key,
            "arguments": collected,
        }
    if flow_key == "leads_create":
        return {
            "tool_key": "leads_create",
            "flow_key": flow_key,
            "arguments": collected,
        }
    if flow_key == "clinic_appointment":
        return {
            "tool_key": "book_appointment_with_lead_capture",
            "flow_key": flow_key,
            "arguments": _clinic_appointment_args(collected),
        }
    return None


def _clinic_appointment_args(collected: dict[str, Any]) -> dict[str, Any]:
    """Map collected clinic slots → ``book_appointment_with_lead_capture`` args.

    patient_name/phone/service/reason pass through; the date+time slot(s) are
    parsed (via the canonical parser) and combined into an ISO ``appointment_time``
    so the tool gets a clean timestamp. The tool then resolves the service to a
    doctor + assigns the slot.
    """
    args: dict[str, Any] = {}
    for src, dst in (("patient_name", "patient_name"), ("name", "patient_name"),
                     ("phone", "phone"), ("service", "service"), ("reason", "reason")):
        value = collected.get(src)
        if value and dst not in args:
            args[dst] = value

    # Find a date + a time anywhere in the collected values (a combined datetime
    # slot like "tomorrow 11 am", or separate date/time slots).
    date_txt = time_txt = None
    for value in collected.values():
        if not isinstance(value, str):
            continue
        ent = extract_turn_entities(value)
        date_txt = date_txt or ent.get("date_text")
        time_txt = time_txt or ent.get("time_text")
    iso = _combine_date_time_to_iso(date_txt, time_txt)
    if iso:
        args["appointment_time"] = iso
    else:
        # Fall back to the raw datetime text; the tool's own parser gets a shot.
        raw = collected.get("appointment_time") or collected.get("appointment_date")
        if raw:
            args["appointment_time"] = str(raw)
    return args


def _combine_date_time_to_iso(date_txt: str | None, time_txt: str | None) -> str | None:
    if not (date_txt and time_txt):
        return None
    try:
        from datetime import datetime as _dt, timezone as _tz
        from zoneinfo import ZoneInfo
        from app.services.datetime_parse import parse_date, parse_time

        local_tz = ZoneInfo("Asia/Kolkata")
        d = parse_date(date_txt)
        t = parse_time(time_txt)
        return _dt.combine(d, t, tzinfo=local_tz).astimezone(_tz.utc).isoformat()
    except Exception:
        return None


def evaluate_tool_flow_policy(
    text: str,
    *,
    business_type: str | None,
    schema_overrides: dict[str, Any] | None = None,
    custom_tabs: list[dict[str, Any]] | None = None,
    provider_status: dict[str, Any] | None = None,
    history: list[dict[str, str]] | None = None,
    state: dict[str, Any] | None = None,
    language: str | None = None,
    allowed_flow_keys: list[str] | None = None,
) -> dict[str, Any] | None:
    """Evaluate the tool-flow policy for a turn.

    ``allowed_flow_keys`` is an optional gate used by the outbound
    pipeline to restrict which flows can START during this call based
    on the campaign's selected objectives. ``None`` = all flows allowed
    (inbound default). Once a flow is already active, this filter is
    NOT enforced — pulling the rug mid-conversation would be worse than
    completing a flow that wasn't strictly part of the campaign goals.
    """
    value = _clean(text)
    if not value:
        return None
    history = history or []
    state = dict(state or {})
    persisted = generated_questions_from_status(provider_status)
    expected = build_tool_flow_questions(business_type, schema_overrides, custom_tabs)
    bundle = persisted if persisted.get("schema_hash") == expected.get("schema_hash") else expected

    # Anti-repeat: any slot we already know from the conversational
    # memory (snapshot lives in ``state['memory']['facts']`` keyed by
    # the canonical fact name) should pre-populate ``collected`` so
    # the flow skips asking. The hydrator only fills gaps — explicit
    # flow writes win.
    try:
        # Local import to avoid the import-time cycle with services
        # that themselves consume tool_flow_policy.
        from app.services.conversational_memory import (
            ConversationalMemory as _CM,
            hydrate_flow_collected as _hydrate,
        )
        memory_obj = _CM.from_state_blob((state or {}).get("memory") or {})
    except Exception:
        memory_obj = None
        _hydrate = None  # type: ignore[assignment]

    flow_state = dict(state.get("tool_flow") or {})
    newly_started = False
    if not flow_state.get("active"):
        flow_key = _start_flow_key(value, business_type, history)
        if not flow_key or flow_key not in (bundle.get("flows") or {}):
            return None
        # Inbound real-estate no longer books a site visit live. The agent stays
        # conversational (QUERY mode) and the brochure + project location are sent
        # to the caller's WhatsApp at call end (maybe_create_real_estate_lead_from_call),
        # so we never start the site-visit slot-fill that used to interrogate an
        # inbound caller for name/phone — the phone is the ANI we already have.
        # Gated on the explicit inbound surface (set at call start in
        # nokvo_one_voice_stream_service); outbound campaigns keep this flow.
        if (
            str((state or {}).get("call_surface") or "") == "voice_inbound"
            and normalize_business_type(business_type) == "real_estate"
            and flow_key == "real_estate_site_visit"
        ):
            return None
        # Outbound campaign objective gate. If the operator selected only
        # "Book a site visit" for this campaign, ``leads_create`` must NOT
        # start automatically — and vice-versa. ``allowed_flow_keys=None``
        # means "all flows allowed" (inbound default).
        if allowed_flow_keys is not None and flow_key not in allowed_flow_keys:
            return None
        # The caller asked a side question on the same turn that signals intent
        # ("Before I book, what services do you offer?"). Let the route fall
        # through to RAG; the pipeline will mark the tool_flow as deferred so
        # the next turn resumes with a "Coming back to your booking — " prefix.
        entities_for_pivot = extract_turn_entities(value, expected_slot=None)
        if _is_side_question_during_booking(value, entities=entities_for_pivot):
            return None
        newly_started = True
        flow_state = {
            "active": True,
            "flow_key": flow_key,
            "tool_key": ((bundle.get("flows") or {}).get(flow_key) or {}).get("tool_key"),
            "collected": {},
        }

    # Memory hydration — fill in any slot the caller has already told
    # us about (this or a prior call) so ``_next_slot`` won't ask for
    # things we know. Done once per evaluation pass; subsequent
    # writes by the flow itself still take precedence.
    if memory_obj is not None and _hydrate is not None:
        try:
            flow_state["collected"] = _hydrate(
                flow_state.get("collected") or {},
                memory_obj,
            )
        except Exception:
            pass

    flow_key = str(flow_state.get("flow_key") or "")

    # ── ANI auto-fill: phone slot from the caller's number ─────────────────
    # ``state['caller_phone']`` is the number we're talking to (from the call
    # signaling, set at call start). Fill the phone slot directly so the agent
    # never asks the caller to recite digits — telephony STT mangles spoken
    # numbers. Only fills an EMPTY slot (a number the caller speaks/corrects still
    # wins, via the extraction path), and skips the read-back since there's no STT
    # error to guard against. The marker lives on flow_state, not collected, so it
    # never leaks into the stored record.
    _caller_phone = str((state.get("caller_phone") or "")).strip()
    if _caller_phone:
        _coll = dict(flow_state.get("collected") or {})
        _flow_slots = ((bundle.get("flows") or {}).get(flow_key) or {}).get("slots") or []
        _filled = False
        for _s in _flow_slots:
            if isinstance(_s, dict) and _s.get("kind") == "phone":
                _pk = str(_s.get("key") or "")
                if _pk and not _coll.get(_pk):
                    _coll[_pk] = _caller_phone
                    _filled = True
        if _filled:
            flow_state["collected"] = _coll
            flow_state["phone_from_ani"] = True

    # ── 1) Availability-lookup intent ─────────────────────────────────────
    # When the caller asks "is X available?" / "when can you book me?" / "any
    # slot tomorrow?" while a flow is in progress, return an intent the
    # pipeline can resolve against the scheduler instead of falling through
    # to a slow RAG path. Works for site_visit and reservation flows the
    # same way it does for clinic appointments.
    if flow_state.get("active") and _AVAILABILITY_QUERY_RE.search(value):
        availability_entities: dict[str, Any] = {}
        date_entities = extract_turn_entities(value, expected_slot=None)
        if date_entities.get("date_text"):
            availability_entities["date_text"] = date_entities["date_text"]
        if date_entities.get("time_text"):
            availability_entities["time_text"] = date_entities["time_text"]
        return {
            "answer": None,
            "intent": "availability_check",
            "entities": availability_entities,
            "language": _language_code(language),
            "state_patch": {"tool_flow": flow_state},
            "state_slot": "availability_check",
            "reason": "tool_flow caller asked about availability",
            "flow_key": flow_key,
        }

    # ── 2) Caller answering "yes/no" to a slot we just offered ──────────
    if flow_state.get("awaiting_slot_confirm"):
        if _looks_affirmative(value):
            proposed_utc = flow_state.pop("proposed_slot_utc", None)
            _label = flow_state.pop("proposed_slot_label", None)
            flow_state["awaiting_slot_confirm"] = False
            if proposed_utc:
                # Snap the offered slot into the appropriate date/time slots.
                from datetime import datetime as _dt
                try:
                    parsed = _dt.fromisoformat(proposed_utc.replace("Z", "+00:00"))
                    local_iso = parsed.astimezone().date().isoformat()
                    local_time = parsed.astimezone().strftime("%I:%M %p").lstrip("0")
                    collected = dict(flow_state.get("collected") or {})
                    for key in _date_kind_keys(bundle, flow_key):
                        collected[key] = local_iso
                    for key in _time_kind_keys(bundle, flow_key):
                        collected[key] = local_time
                    flow_state["collected"] = collected
                    from app.services.flow_session import stamp_proposed_slot_accepted
                    stamp_proposed_slot_accepted(
                        flow_state, slot_utc_iso=proposed_utc, member_name=None,
                    )
                except Exception:
                    pass
            # Fall through so the next missing slot (or completion) fires.
        elif _looks_negative(value):
            flow_state["awaiting_slot_confirm"] = False
            flow_state.pop("proposed_slot_utc", None)
            flow_state.pop("proposed_slot_label", None)
            # Clear date/time slots so the flow asks again.
            collected = dict(flow_state.get("collected") or {})
            for key in _date_kind_keys(bundle, flow_key) + _time_kind_keys(bundle, flow_key):
                collected.pop(key, None)
            flow_state["collected"] = collected

    # ── 2b) Caller responding to a past-time "same slot tomorrow?" offer ──
    # Handled here, BEFORE slot extraction, so the reply is read as an answer
    # to the offer — not re-scraped as a fresh time that re-triggers the very
    # same offer (the bug where the agent looped "10 AM has passed…" while the
    # caller kept proposing new times and even said "yeah"). Three outcomes:
    #   • a different time/date in the reply → adopt it; the past-time guard
    #     below re-evaluates and either books it or re-offers a shift,
    #   • yes  → book the proposed next-day slot,
    #   • no   → drop the slot and re-ask.
    # An unparseable reply re-states the offer once. The competing
    # availability ``awaiting_slot_confirm`` is cleared when the offer is made,
    # so the two confirmation states never coexist and fight over the answer.
    if flow_state.get("awaiting_past_time_shift"):
        proposed = flow_state.get("proposed_date")
        original_time = flow_state.get("original_time")
        shift_entities = extract_turn_entities(value, expected_slot=None)
        new_date = shift_entities.get("date_text")
        new_time = shift_entities.get("time_text")
        collected = dict(flow_state.get("collected") or {})
        if new_date or new_time:
            flow_state["awaiting_past_time_shift"] = False
            flow_state.pop("proposed_date", None)
            flow_state.pop("original_time", None)
            if new_date:
                for key in _date_kind_keys(bundle, flow_key):
                    collected[key] = new_date
            if new_time:
                for key in _time_kind_keys(bundle, flow_key):
                    collected[key] = new_time
            flow_state["collected"] = collected
            # Fall through: the past-time guard re-checks the new value.
        elif _looks_affirmative(value) and proposed:
            flow_state["awaiting_past_time_shift"] = False
            flow_state.pop("proposed_date", None)
            flow_state.pop("original_time", None)
            for key in _date_kind_keys(bundle, flow_key):
                collected[key] = proposed
            if original_time:
                for key in _time_kind_keys(bundle, flow_key):
                    collected[key] = original_time
            flow_state["collected"] = collected
        elif _looks_negative(value):
            flow_state["awaiting_past_time_shift"] = False
            flow_state.pop("proposed_date", None)
            flow_state.pop("original_time", None)
            for key in _date_kind_keys(bundle, flow_key) + _time_kind_keys(bundle, flow_key):
                collected.pop(key, None)
            flow_state["collected"] = collected
        else:
            # Unparseable ("hmm"): keep the offer open and re-state it once.
            # Returning here (rather than falling through) prevents the flow
            # from completing with a slot we already flagged as in the past.
            reask = _shift_to_next_day_prompt(
                next((collected.get(k) for k in _date_kind_keys(bundle, flow_key) if collected.get(k)), None),
                original_time,
                language,
            )
            if reask is not None:
                _next_iso, _nice, reask_prompt = reask
                return {
                    "answer": reask_prompt,
                    "intent": "tool_flow",
                    "flow_key": flow_key,
                    "state_patch": {"tool_flow": flow_state},
                    "state_slot": "date_shift_confirm",
                    "reason": "past-time offer awaiting a clear yes/no/time",
                }

    # ── 3a) Caller answering phone/email/id confirmation ───────────────
    if flow_state.get("awaiting_id_confirmation"):
        confirmation_key = str(flow_state.get("id_confirmation_slot") or "")
        confirmation_kind = str(flow_state.get("id_confirmation_kind") or "")
        flow_state["awaiting_id_confirmation"] = False
        flow_state.pop("id_confirmation_slot", None)
        flow_state.pop("id_confirmation_kind", None)
        if _looks_affirmative(value):
            from app.services.flow_session import record_confirmation, append_audit_trail
            collected_now = dict(flow_state.get("collected") or {})
            record_confirmation(flow_state, confirmation_key, collected_now.get(confirmation_key))
            append_audit_trail(flow_state, f"{confirmation_kind}_confirmed", detail=collected_now.get(confirmation_key))
            # Clear ``pending_slot`` so the subsequent slot loop calls
            # ``_next_slot`` and advances to the next real slot. Without
            # this clear the FSM stayed stuck on the ``*_confirm`` pseudo
            # slot — and when the caller volunteered the next slot's
            # value in the same breath ("Yes, that's the phone. The time
            # is 11am."), it never got captured and the LLM ad-libbed an
            # uncommitted "before I confirm?" recap.
            if flow_state.get("pending_slot", "").endswith("_confirm"):
                flow_state["pending_slot"] = None
        elif _looks_negative(value):
            collected = dict(flow_state.get("collected") or {})
            if confirmation_key:
                collected.pop(confirmation_key, None)
            flow_state["collected"] = collected
            flow_state["pending_slot"] = confirmation_key
            return {
                "answer": _question_for_slot(bundle, flow_key, confirmation_key, language),
                "intent": "tool_flow",
                "flow_key": flow_key,
                "state_patch": {"tool_flow": flow_state},
                "state_slot": confirmation_key,
                "reason": f"{confirmation_kind} confirmation rejected",
            }
        else:
            replacement = _extract_value(value, confirmation_key, confirmation_kind)
            if replacement:
                collected = dict(flow_state.get("collected") or {})
                collected[confirmation_key] = replacement
                flow_state["collected"] = collected
                flow_state["awaiting_id_confirmation"] = True
                flow_state["id_confirmation_slot"] = confirmation_key
                flow_state["id_confirmation_kind"] = confirmation_kind
                if confirmation_kind == "phone":
                    prompt = _phone_confirmation_prompt(str(replacement), language)
                elif confirmation_kind == "email":
                    prompt = _email_confirmation_prompt(str(replacement), language)
                else:
                    label = confirmation_key.replace("_", " ")
                    prompt = _id_confirmation_prompt(str(replacement), label, language)
                return {
                    "answer": prompt,
                    "intent": "tool_flow",
                    "flow_key": flow_key,
                    "state_patch": {"tool_flow": flow_state},
                    "state_slot": f"{confirmation_key}_confirm",
                    "reason": f"{confirmation_kind} corrected, awaiting confirmation",
                }

    # ── 3b) Caller answering name confirmation ─────────────────────────
    if flow_state.get("awaiting_name_confirmation"):
        from app.services.flow_session import record_confirmation, append_audit_trail
        confirmation_key = str(flow_state.get("name_confirmation_slot") or "")
        flow_state["awaiting_name_confirmation"] = False
        flow_state.pop("name_confirmation_slot", None)
        attempts = int(flow_state.get("name_confirm_attempts") or 1)

        def _advance_past_name() -> None:
            if str(flow_state.get("pending_slot") or "").endswith("_confirm"):
                flow_state["pending_slot"] = None

        if _looks_affirmative(value):
            collected_now = dict(flow_state.get("collected") or {})
            record_confirmation(flow_state, confirmation_key, collected_now.get(confirmation_key))
            append_audit_trail(flow_state, "name_confirmed", detail=collected_now.get(confirmation_key))
            flow_state.pop("name_confirm_attempts", None)
            _advance_past_name()
            # fall through to the main slot logic (asks the next slot / completes)
        else:
            # Caller didn't say a clean "yes". Find their intended name — an
            # embedded correction after "no, it's X", or a bare re-statement.
            stripped = re.sub(
                r"^\s*(?:no|nope|nah|not\s+really|actually|wait|sorry)\b[\s,]*",
                "",
                value or "",
                flags=re.IGNORECASE,
            )
            new_name = None
            if confirmation_key:
                new_name = (
                    (_extract_value(stripped, confirmation_key, "name") if stripped and stripped != value else None)
                    or _extract_value(value, confirmation_key, "name")
                )
            if new_name:
                collected = dict(flow_state.get("collected") or {})
                collected[confirmation_key] = new_name
                flow_state["collected"] = collected

            if attempts >= _MAX_NAME_CONFIRM_ATTEMPTS:
                # Stop the re-confirm loop: accept the best-effort name (flagged
                # unconfirmed for the team to verify against the captured phone) and
                # advance — telephony STT will never nail every name, and looping is
                # far worse than a one-line "we'll double-check the spelling".
                collected = dict(flow_state.get("collected") or {})
                record_confirmation(flow_state, confirmation_key, collected.get(confirmation_key))
                append_audit_trail(flow_state, "name_unconfirmed", detail=collected.get(confirmation_key))
                flow_state.pop("name_confirm_attempts", None)
                _advance_past_name()
                # fall through to the main slot logic
            elif new_name:
                flow_state["name_confirm_attempts"] = attempts + 1
                flow_state["awaiting_name_confirmation"] = True
                flow_state["name_confirmation_slot"] = confirmation_key
                return {
                    "answer": _name_confirmation_prompt(new_name, language),
                    "intent": "tool_flow",
                    "flow_key": flow_key,
                    "state_patch": {"tool_flow": flow_state},
                    "state_slot": f"{confirmation_key}_confirm",
                    "reason": "name corrected, awaiting confirmation",
                }
            elif _looks_negative(value):
                # "No" without a clear correction → re-ask the name (under the cap).
                flow_state["name_confirm_attempts"] = attempts + 1
                collected = dict(flow_state.get("collected") or {})
                if confirmation_key:
                    collected.pop(confirmation_key, None)
                flow_state["collected"] = collected
                flow_state["pending_slot"] = confirmation_key
                return {
                    "answer": _question_for_slot(bundle, flow_key, confirmation_key, language),
                    "intent": "tool_flow",
                    "flow_key": flow_key,
                    "state_patch": {"tool_flow": flow_state},
                    "state_slot": confirmation_key,
                    "reason": "name confirmation rejected",
                }
            # else: ambiguous, no new name, not negative, under cap → fall through

    pending = str(flow_state.get("pending_slot") or "") or _next_slot(flow_state, bundle)
    collected = dict(flow_state.get("collected") or {})
    if pending and not newly_started:
        # Mid-flow digression check: if the caller pivoted to a KB question
        # rather than answering the pending slot, return None so the route
        # layer yields to RAG. The unchanged tool_flow state in Redis keeps
        # the pending slot intact; the next turn resumes the same question
        # (with a "Coming back" prefix once the pipeline marks deferred).
        entities_for_pivot = extract_turn_entities(value, expected_slot=None)
        if _is_side_question_during_booking(value, entities=entities_for_pivot):
            return None
        kind = _field_kind_for_slot(bundle, flow_key, pending)
        extracted = _extract_value(value, pending, kind)
        if extracted:
            collected[pending] = extracted
            flow_state["collected"] = collected
        elif kind == "name":
            # The caller's reply didn't pass the name extractor (likely
            # they answered a later slot like "Tuesday" or "10 AM" while
            # the FSM was still on name). Try to opportunistically capture
            # it into the appropriate date/time slot so the agent isn't
            # forced to re-ask the same question that triggered the bad
            # reply in the first place. The name slot stays pending.
            stray_entities = extract_turn_entities(value, expected_slot="preferred_date")
            if stray_entities.get("date_text"):
                for key in _date_kind_keys(bundle, flow_key):
                    if not collected.get(key):
                        collected[key] = stray_entities["date_text"]
                        flow_state["collected"] = collected
                        break
            stray_time = extract_turn_entities(value, expected_slot="preferred_time")
            if stray_time.get("time_text"):
                for key in _time_kind_keys(bundle, flow_key):
                    if not collected.get(key):
                        collected[key] = stray_time["time_text"]
                        flow_state["collected"] = collected
                        break

        # Domain inference: even when this slot didn't capture, the caller
        # may have volunteered budget/property_type/party_size/order_id etc.
        # in the same utterance. Pre-fill matching slots in the flow schema.
        inferred = _infer_domain_slots(value, business_type, bundle, flow_key, collected)
        if inferred:
            flow_state["collected"] = collected

        if extracted:
            # ── 4) Name-kind slot → confirm before moving on ─────────────
            if kind == "name":
                # When the slot was deferred for a side question, the
                # "Coming back to your booking" prefix must precede the
                # confirmation prompt — otherwise the caller hears the
                # confirmation without acknowledgment of the detour.
                resumed_from_kb = bool(flow_state.pop("deferred_for_kb", False))
                prefix = _coming_back_prefix(language) if resumed_from_kb else ""
                flow_state["awaiting_name_confirmation"] = True
                flow_state["name_confirmation_slot"] = pending
                flow_state["name_confirm_attempts"] = 1
                flow_state["pending_slot"] = f"{pending}_confirm"
                return {
                    "answer": prefix + _name_confirmation_prompt(extracted, language),
                    "intent": "tool_flow",
                    "flow_key": flow_key,
                    "state_patch": {"tool_flow": flow_state},
                    "state_slot": f"{pending}_confirm",
                    "reason": "name captured, awaiting confirmation",
                }

            # ── 4b) Other high-stakes kinds → confirm too ─────────────────
            if kind in _CONFIRMATION_KINDS:
                # We always read back phone / email / reference numbers. The
                # previous "skip if 10-digit Indian mobile" shortcut was
                # tempting but masked a real failure mode: a single STT digit
                # swap (7 vs 8 vs 9) still produces a "high-confidence" 10-
                # digit number — and the wrong record then gets the callback.
                # A 2-second read-back is worth that guarantee.
                skip = False
                if not skip:
                    if kind == "phone":
                        prompt = _phone_confirmation_prompt(str(extracted), language)
                    elif kind == "email":
                        prompt = _email_confirmation_prompt(str(extracted), language)
                    else:
                        label = pending.replace("_", " ")
                        prompt = _id_confirmation_prompt(str(extracted), label, language)
                    flow_state["awaiting_id_confirmation"] = True
                    flow_state["id_confirmation_slot"] = pending
                    flow_state["id_confirmation_kind"] = kind
                    flow_state["pending_slot"] = f"{pending}_confirm"
                    return {
                        "answer": prompt,
                        "intent": "tool_flow",
                        "flow_key": flow_key,
                        "state_patch": {"tool_flow": flow_state},
                        "state_slot": f"{pending}_confirm",
                        "reason": f"{kind} captured, awaiting confirmation",
                    }

            # ── 5) Past-time guard when both date+time now present ───────
            date_slots = _date_kind_keys(bundle, flow_key)
            time_slots = _time_kind_keys(bundle, flow_key)
            date_value = next((collected.get(k) for k in date_slots if collected.get(k)), None)
            time_value = next((collected.get(k) for k in time_slots if collected.get(k)), None)
            if date_value and time_value and _appointment_is_past(date_value, time_value):
                shift_count = int(flow_state.get("past_shift_count") or 0)
                if shift_count >= _MAX_PAST_SHIFT_OFFERS:
                    # The caller keeps naming times that have already passed
                    # today. Stop re-offering "same time tomorrow" and let the
                    # scheduler surface the next actually-available slot. Drop
                    # the past date/time so the availability lookup anchors on
                    # "now" (proposing today's next free slot if any remains,
                    # else the soonest upcoming one).
                    for key in date_slots + time_slots:
                        collected.pop(key, None)
                    flow_state["collected"] = collected
                    flow_state["awaiting_past_time_shift"] = False
                    flow_state.pop("proposed_date", None)
                    flow_state.pop("original_time", None)
                    flow_state["offered_disambiguation"] = True
                    return {
                        "answer": None,
                        "intent": "availability_check",
                        "entities": {},
                        "language": _language_code(language),
                        "state_patch": {"tool_flow": flow_state},
                        "state_slot": "availability_after_past",
                        "reason": "repeated past times — offering next available slot",
                        "flow_key": flow_key,
                    }
                shift = _shift_to_next_day_prompt(date_value, time_value, language)
                if shift is not None:
                    next_iso, _nice_date, prompt = shift
                    flow_state["proposed_date"] = next_iso
                    flow_state["original_time"] = time_value
                    flow_state["awaiting_past_time_shift"] = True
                    flow_state["past_shift_count"] = shift_count + 1
                    # A past-time offer is the sole live confirmation now —
                    # clear any availability slot-confirm so the next reply
                    # routes unambiguously to the past-time handler above.
                    flow_state["awaiting_slot_confirm"] = False
                    flow_state.pop("proposed_slot_utc", None)
                    flow_state.pop("proposed_slot_label", None)
                    return {
                        "answer": prompt,
                        "intent": "tool_flow",
                        "flow_key": flow_key,
                        "state_patch": {"tool_flow": flow_state},
                        "state_slot": "date_shift_confirm",
                        "reason": "tool_flow time in the past — offered same time next day",
                    }

    next_slot = _next_slot(flow_state, bundle)
    flow_state["pending_slot"] = next_slot
    if next_slot:
        # Adaptive disambiguation: caller gave a date but no time for this
        # flow. Trigger the scheduler so we offer a concrete free slot.
        collected_now = dict(flow_state.get("collected") or {})
        next_kind = _field_kind_for_slot(bundle, flow_key, next_slot)
        date_filled = any(collected_now.get(k) for k in _date_kind_keys(bundle, flow_key))
        if (
            next_kind == "time"
            and date_filled
            and not flow_state.get("offered_disambiguation")
        ):
            flow_state["offered_disambiguation"] = True
            date_text = next((collected_now.get(k) for k in _date_kind_keys(bundle, flow_key) if collected_now.get(k)), None)
            return {
                "answer": None,
                "intent": "availability_check",
                "entities": {"date_text": date_text} if date_text else {},
                "language": _language_code(language),
                "state_patch": {"tool_flow": flow_state},
                "state_slot": "availability_disambiguation",
                "reason": "date given without time — offering concrete slot",
                "flow_key": flow_key,
            }
        # Consume the deferred-for-kb marker exactly once when resuming a flow
        # that paused mid-slot for a side question. The prefix acknowledges the
        # detour ("Coming back to your booking — ") before the slot question.
        resumed_from_kb = bool(flow_state.pop("deferred_for_kb", False))
        prefix = _coming_back_prefix(language) if resumed_from_kb else ""
        return {
            "answer": prefix + _question_for_slot(bundle, flow_key, next_slot, language),
            "intent": "tool_flow",
            "flow_key": flow_key,
            "state_patch": {"tool_flow": flow_state},
            "state_slot": next_slot,
            "reason": f"{flow_key} slot collection",
        }

    flow_state["active"] = False
    flow_state["completed"] = True
    action = _flow_action(flow_state)
    return {
        "answer": "One moment, I’ll record that.",
        "intent": "tool_flow",
        "flow_key": flow_key,
        "state_patch": {"tool_flow": flow_state},
        "state_slot": "complete",
        "reason": f"{flow_key} slots complete",
        "action": action,
    }
