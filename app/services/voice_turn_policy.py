from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo


_APPOINTMENT_LOCAL_TZ = ZoneInfo("Asia/Kolkata")
_MONTH_LOOKUP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


# ── Telugu / Hindi relative date + time-of-day parity ────────────────────────
# Code-switched callers say the relative date / part-of-day in their language
# even when the rest of the utterance carries English loanwords. We map those
# (Devanagari + Telugu script AND common Latin transliterations) onto the
# canonical English tokens every downstream parser already understands, so a
# Telugu / Hindi booking turn parses exactly like the English one.
#
# NOTE: ``kal`` (Hindi) is yesterday *or* tomorrow by context; for a forward
# booking the only sensible reading is tomorrow, which is what we use.
_RELATIVE_DATE_ALIASES: dict[str, str] = {
    # today
    "aaj": "today", "आज": "today",
    "eeroju": "today", "iroju": "today", "ఈరోజు": "today", "ఈ రోజు": "today",
    # tomorrow
    "kal": "tomorrow", "कल": "tomorrow",
    "repu": "tomorrow", "rapu": "tomorrow", "రేపు": "tomorrow", "రెపు": "tomorrow",
    # day after tomorrow
    "parso": "day after tomorrow", "parson": "day after tomorrow",
    "परसों": "day after tomorrow", "परसो": "day after tomorrow",
    "ellundi": "day after tomorrow", "ఎల్లుండి": "day after tomorrow",
    "एल्लुंडी": "day after tomorrow",
}
_TIME_OF_DAY_ALIASES: dict[str, str] = {
    # morning
    "subah": "morning", "savere": "morning", "sawere": "morning",
    "सुबह": "morning", "सवेरे": "morning",
    "udayam": "morning", "poddunna": "morning", "poddunne": "morning",
    "ఉదయం": "morning", "పొద్దున": "morning", "పొద్దున్న": "morning",
    # afternoon
    "dopahar": "afternoon", "dopaher": "afternoon", "दोपहर": "afternoon",
    "madhyahnam": "afternoon", "madhyanam": "afternoon", "మధ్యాహ్నం": "afternoon",
    # evening
    "shaam": "evening", "शाम": "evening",
    "sayantram": "evening", "saayantram": "evening", "సాయంత్రం": "evening",
    # night
    "raat": "night", "रात": "night", "ratri": "night", "రాత్రి": "night",
}
# Built for regex alternation: longest first so "kal subah" beats "kal".
_RELATIVE_DATETIME_ALIAS_KEYS: list[str] = sorted(
    [*_RELATIVE_DATE_ALIASES, *_TIME_OF_DAY_ALIASES], key=len, reverse=True
)
_RELATIVE_DATETIME_ALIAS_RE = re.compile(
    "(?<!\\w)(" + "|".join(re.escape(k) for k in _RELATIVE_DATETIME_ALIAS_KEYS) + ")(?!\\w)",
    re.IGNORECASE,
)


def normalize_relative_datetime_text(text: str | None) -> str:
    """Replace hi/te relative-date and time-of-day tokens with their canonical
    English equivalents (``kal``→``tomorrow``, ``సాయంత్రం``→``evening``) so the
    English-native date/time parsers handle Telugu / Hindi turns unchanged.

    Latin transliterations are matched case-insensitively on word boundaries;
    Devanagari / Telugu script (which ``\\b`` doesn't bound reliably) is matched
    with explicit non-word-character lookarounds.
    """
    if not text:
        return text or ""

    def _sub(match: "re.Match[str]") -> str:
        key = match.group(0).lower()
        return (
            _RELATIVE_DATE_ALIASES.get(key)
            or _TIME_OF_DAY_ALIASES.get(key)
            or match.group(0)
        )

    return _RELATIVE_DATETIME_ALIAS_RE.sub(_sub, text)


_PHONE_HINT_RE = re.compile(
    r"\b(phone|mobile|contact|callback|call\s+back|reach\s+(?:me|you)|number|whatsapp)\b|"
    r"(ఫోన్|మొబైల్|నంబర్|నెంబర్|సంప్రదించ|फोन|मोबाइल|नंबर|नम्बर)",
    re.IGNORECASE,
)
_ORDER_HINT_RE = re.compile(
    r"\b(order|ticket|case|reference|booking|appointment)\s*(?:id|number|no\.?|#)?\b",
    re.IGNORECASE,
)
_APPOINTMENT_INTENT_RE = re.compile(
    r"\b(appointment|book|schedule|consultation|visit|checkup|check-up|reschedule|follow[-\s]?up)\b|"
    r"(అపాయింట్మెంట్|అపాయింట్‌మెంట్|అపాయింట్|బుక్|కన్సల్టేషన్|విజిట్|చెకప్|చెక్\s*అప్|"
    r"अपॉइंटमेंट|अपॉइन्टमेंट|बुक|कंसल्टेशन|विजिट|चेकअप)",
    re.IGNORECASE,
)
_URGENT_SYMPTOM_RE = re.compile(
    r"\b("
    r"sudden\s+(?:vision\s+loss|blurred\s+vision|blindness)|"
    r"vision\s+loss|severe\s+eye\s+pain|eye\s+injury|chemical\s+(?:splash|exposure|entered)|"
    r"object\s+stuck|blood\s+in\s+(?:my\s+)?eye|flashes\s+of\s+light|"
    r"sudden\s+(?:increase\s+in\s+)?floaters|curtain[-\s]?like\s+shadow|"
    r"swelling\s+around\s+(?:my\s+)?eye\s+with\s+fever|"
    r"contact\s+lens(?:es)?\s+.*(?:pain|redness|blurred)"
    r")\b",
    re.IGNORECASE,
)
_VISIT_REASON_HINT_RE = re.compile(
    r"\b(for|because|due\s+to|red|redness|pain|watering|itching|irritation|blurred|vision|dry|strain|checkup|check-up|spectacles|power)\b|"
    r"(కంటి|కన్ను|ఐ|చెకప్|చెక్\s*అప్|జనరల్|నొప్పి|నీరు|దురద|మంట|బ్లర్|విజన్|పవర్|"
    r"आंख|आँख|चेकअप|जनरल|दर्द|पानी|खुजली|जलन|ब्लर|विजन|पावर)",
    re.IGNORECASE,
)
_NAME_EXCLUSION_RE = re.compile(
    r"\b("
    # Negation / correction tokens that should never be mistaken for a name.
    # Without these, "no that's wrong" parses as a 3-word name candidate.
    r"no|nope|not|wrong|incorrect|right|correct|that(?:'s| is)|"
    r"yes|yeah|yep|yup|ya|yah|sure|ok|okay|alright|"
    # Existing clinical / appointment vocabulary.
    r"it(?:'s| is)|actually|appointment|consultation|visit|checkup|check-up|eye|eyes|"
    r"red|redness|pain|watering|itching|irritation|blurred|vision|dry|strain|"
    r"spectacles|power|problem|concern|issue|symptom"
    r")\b|"
    r"(అపాయింట్మెంట్|అపాయింట్|విజిట్|చెకప్|కంటి|కన్ను|ఐ|నొప్పి|సమస్య|లక్షణం|"
    r"अपॉइंटमेंट|विजिट|चेकअप|आंख|दर्द|समस्या|लक्षण)",
    re.IGNORECASE,
)
_TIME_RE = re.compile(
    r"\b(?:[01]?\d|2[0-3])(?::[0-5]\d|\s*(?:am|pm))\b|\b(?:morning|afternoon|evening|night|noon)\b",
    re.IGNORECASE,
)
_BARE_TIME_RE = re.compile(r"\b(?:[1-9]|1[0-2])\b")
_DATE_RE = re.compile(
    r"\b(?:today|tomorrow|day\s+after\s+tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?|\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*)\b",
    re.IGNORECASE,
)
_FIRST_VISIT_RE = re.compile(r"\b(first\s+visit|new\s+patient|first\s+time)\b", re.IGNORECASE)
_FOLLOW_UP_RE = re.compile(r"\b(follow[-\s]?up|review|already\s+visited|old\s+patient)\b", re.IGNORECASE)
_QUESTION_RE = re.compile(
    r"\b(what|where|when|why|how|can|could|do|does|is|are|tell|fee|timing|address)\b|"
    r"(ఏమి|ఎక్కడ|ఎప్పుడు|ఎలా|చెప్పగలరా|ఫీజు|టైమింగ్|అడ్రస్|क्या|कहाँ|कब|कैसे|फीस|टाइमिंग|पता)",
    re.IGNORECASE,
)
_CORRECTION_MARKER_RE = re.compile(
    r"\b("
    r"not|actually|correction|correct|wrong|misheard|misheard\s+me|"
    r"it(?:'s| is)\s+not|that(?:'s| is)\s+not|not\s+a|not\s+an"
    r")\b",
    re.IGNORECASE,
)
_NO_URGENT_RE = re.compile(
    r"\b(no|none|nothing|not now|nope|not really)\b|"
    r"(లేదు|లేవు|ఏం\s*లేవు|అలాంటివి\s*లేవు|కాదు|नहीं|नही|कुछ\s*नहीं|ऐसा\s*नहीं)",
    re.IGNORECASE,
)
# "Is X available?" / "can you book me at X?" / "when can you book?" — the
# caller wants the agent to consult its own calendar rather than just collect
# values. Two flavours:
#   1) explicit slot question containing "available/free/open/slot/booked/taken"
#   2) explicit booking request ("book me at 4 PM", "can you book me on …")
#   3) "when can you book", "next available", "earliest available", "any slot"
# The matcher is intentionally lax — false positives just trigger an extra
# scheduler lookup, which is cheap and self-correcting.
_AVAILABILITY_QUERY_RE = re.compile(
    r"\b("
    r"(?:available|availability|free\s+slot|open\s+slot|slot(?:s)?\s+(?:available|free|open)|"
    r"booked|taken|"
    r"(?:can|could|would|will)\s+you\s+book(?:\s+me)?\s+(?:at|on|for|in)\s+|"
    r"book\s+me\s+(?:at|on|for|in)\s+|"
    r"when\s+(?:can|could|will)\s+you\s+(?:book|fit\s+me|see\s+me)|"
    r"(?:next|earliest|first)\s+(?:available|free|open)|"
    r"any(?:thing)?\s+(?:available|free|open)|"
    r"any\s+(?:slot|opening|time)|"
    r"how\s+about\s+\d|"
    r"what(?:'s|\s+is)\s+(?:available|free|open|the\s+(?:next|earliest|first)))"
    r")\b",
    re.IGNORECASE,
)


_NAME_PREFIX_RE = re.compile(
    r"^(?:"
    # English: covers "the full name of the patient is X", "patient's name is X",
    # "patient name is X", "his/her name is X", "the name is X", "name is X",
    # "my name is X", "I am X", "I'm X", "this is X", "it is X", "call me X".
    r"the\s+(?:full\s+)?name\s+of\s+(?:the\s+)?(?:patient|person|caller|customer)\s+is|"
    r"(?:the\s+)?patient(?:'s|s)?\s+(?:full\s+)?name\s+is|"
    r"(?:the\s+)?caller(?:'s|s)?\s+(?:full\s+)?name\s+is|"
    r"(?:the\s+)?customer(?:'s|s)?\s+(?:full\s+)?name\s+is|"
    r"(?:the\s+)?person(?:'s|s)?\s+(?:full\s+)?name\s+is|"
    r"(?:his|her|their)\s+(?:full\s+)?name\s+is|"
    r"(?:the\s+)?(?:full\s+)?name\s+is|"
    r"my\s+name\s+is|"
    r"i(?:'m|\s+am)|"
    r"this\s+is|"
    r"call\s+me|"
    r"it(?:'s|\s+is)|"
    # Telugu / Hindi prefixes (kept from prior list, plus a couple of extras).
    r"నా\s+పేరు|పేరు|"
    r"पేషెంట్\s+పేరు|"
    r"मेरा\s+नाम|नाम\s+है|"
    r"मरीज(?:\s+का)?\s+नाम\s+है|"
    r"रोगी\s+का\s+नाम\s+है"
    r")\s+(.+)$",
    re.IGNORECASE,
)

# Mid-booking digression detector: pivot phrases that signal the caller wants
# to pause the active slot-fill flow and ask a side question instead. The
# canonical case: caller is mid-appointment-booking, agent has asked for the
# visit reason, caller says "Before that, could you list out your services?"
_SIDE_QUESTION_PIVOT_RE = re.compile(
    r"\b("
    r"before\s+(?:that|i\s+(?:book|schedule|continue|answer|decide|do)|"
    r"booking|scheduling|we\s+continue|moving\s+on)|"
    r"prior\s+to\s+(?:that|booking|scheduling)|"
    r"wait(?:\s+(?:a\s+(?:sec|second|minute|moment)|first))?|"
    r"hold\s+on|"
    r"actually\s+(?:wait|first|before|can|could|tell|i)|"
    r"but\s+first|just\s+first|"
    r"first(?:\s*(?:[,.]|off|of\s+all|up))|"
    r"first(?:\s+(?:can|could|would|tell|let|let\s+me|what|how|where|when|i))|"
    r"by\s+the\s+way|"
    r"one\s+(?:more\s+)?(?:thing|question)|"
    r"another\s+(?:thing|question)|"
    r"quick\s+question|"
    r"side\s+question|"
    r"(?:can|could|would|will)\s+you\s+(?:\w+\s+){0,3}?(?:tell|list|share|explain|describe|walk)|"
    r"list\s+out|"
    r"i\s+(?:have|wanted\s+to\s+ask|just\s+want\s+to\s+ask)\s+(?:a|one|some)?\s*(?:question|doubt|thing)"
    r")\b|"
    r"(దానికి\s+ముందు|బుక్\s+చేయటానికి\s+ముందు|మొదట(?:\s+చెప్పండి)?|వెయిట్|"
    r"ఒక\s+ప్రశ్న|ఒక\s+డౌట్|లిస్ట్\s+చేయండి|"
    r"उससे\s+पहले|बुक\s+करने\s+से\s+पहले|पहले|रुको|एक\s+सवाल|एक\s+और\s+बात|एक\s+डाउट)",
    re.IGNORECASE,
)

# Sensitive intent phrases that should also yield mid-booking (cancellation,
# refund). FastIntentRouter catches the obvious forms; this regex is the
# in-booking belt-and-braces. The downstream policy engine handles the
# actual cancellation/refund question once the FSM steps aside.
_SENSITIVE_PIVOT_RE = re.compile(
    r"\b("
    r"cancel(?:\s+(?:my|the|this|that))?\s+(?:order|booking|appointment|reservation|payment|subscription)|"
    r"(?:want|need|like|going)\s+to\s+cancel|"
    r"please\s+cancel|just\s+cancel|"
    r"refund(?:\s+(?:my|the|this|that))?(?:\s+(?:order|booking|payment|money|charges?))?|"
    r"(?:i\s+(?:want|need|would\s+like))\s+(?:a\s+)?refund|"
    r"get\s+my\s+money\s+back|"
    r"changed\s+my\s+mind"
    r")\b|"
    r"(క్యాన్సిల్|క్యాన్సల్|రద్దు|రీఫండ్|డబ్బు\s+వాపసు|"
    r"कैंसल|कैन्सल|रद्द|रिफंड|रिफ़ंड|पैसा\s+वापस|पैसे\s+वापस)",
    re.IGNORECASE,
)

# Topic keywords that mark a question as a knowledge-base query (services,
# pricing, location, etc.) rather than a slot-fill answer.
_KB_TOPIC_HINT_RE = re.compile(
    r"\b("
    r"services?(?:\s+offered)?|"
    r"specialit(?:y|ies)|treatments?|procedures?|"
    r"doctors?|specialists?|consultants?|staff|"
    r"timing(?:s)?|hours?|opening|closing|when\s+(?:are|do)\s+you\s+open|"
    r"location|address|directions|where\s+(?:are|is)\s+you|"
    r"fee(?:s)?|cost|price|charges?|pricing|how\s+much|"
    r"insurance|payment\s+(?:methods?|options?|mode)|"
    r"available\s+(?:doctors?|services?|times?|slots?)|"
    r"tell\s+me\s+(?:about|more)|what(?:'s| is)\s+(?:your|the)"
    r")\b|"
    r"(సర్వీసు(?:లు)?|ట్రీట్మెంట్|డాక్టర్(?:లు)?|స్పెషలిస్ట్|"
    r"టైమింగ్|లొకేషన్|అడ్రస్|ఫీజు|ధర|ఎంత|ఇన్సూరెన్స్|"
    r"सेवा(?:एं)?|डॉक्टर|स्पेशलिस्ट|टाइमिंग|पता|फीस|कीमत|कितना|बीमा)",
    re.IGNORECASE,
)

_APPOINTMENT_SLOTS = {
    "reason",
    "patient_name",
    "phone",
    "preferred_date",
    "preferred_time",
    "visit_type",
    "urgent_symptoms",
}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def _is_pure_numberish(text: str) -> bool:
    value = _clean(text)
    if not value:
        return False
    return bool(re.fullmatch(r"\+?[\d\s().-]{4,24}", value))


def normalize_phone_number(text: str, *, expected: bool = False) -> str | None:
    """Return a conservative phone-like value from a voice transcript.

    A bare Indian 10-digit mobile number is treated as a phone number. Shorter
    numeric IDs only become phone numbers when the surrounding text explicitly
    asks for contact details.
    """
    raw = _clean(text)
    if not raw:
        return None
    digits = _digits(raw)
    if len(digits) < 7 or len(digits) > 15:
        return None
    has_phone_hint = bool(_PHONE_HINT_RE.search(raw))
    pure_number = _is_pure_numberish(raw)
    if len(digits) == 10 and digits[0] in "6789" and (pure_number or has_phone_hint or expected):
        return digits
    if digits.startswith("91") and len(digits) == 12 and digits[2] in "6789":
        return f"+{digits}"
    if raw.startswith("+") and 8 <= len(digits) <= 15:
        return f"+{digits}"
    if expected or has_phone_hint:
        return digits
    return None


def infer_expected_slot(history: list[dict[str, str]], state: dict[str, Any] | None = None) -> str | None:
    appointment = ((state or {}).get("appointment") or {}) if isinstance(state, dict) else {}
    pending = appointment.get("pending_slot")
    if isinstance(pending, str) and pending:
        return pending

    for turn in reversed(history[-8:]):
        if turn.get("role") != "assistant":
            continue
        text = str(turn.get("content") or "").lower()
        if (
            "phone" in text
            or "mobile" in text
            or "contact number" in text
            or "callback" in text
            or "ఫోన్" in text
            or "మొబైల్" in text
            or "నంబర్" in text
            or "నెంబర్" in text
            or "फोन" in text
            or "मोबाइल" in text
            or "नंबर" in text
        ):
            return "phone"
        if "order number" in text or "order id" in text or "ticket number" in text or "reference number" in text:
            return "reference_number"
        if (
            ("patient" in text and "name" in text)
            or "full name" in text
            or "patient full name" in text
            or "పేషెంట్" in text
            or "పేరు" in text
            or "मरीज" in text
            or "नाम" in text
        ):
            return "patient_name"
        if (
            "reason" in text
            or "concern" in text
            or "problem" in text
            or "కారణం" in text
            or "సమస్య" in text
            or "eye concern" in text
            or "कारण" in text
            or "समस्या" in text
        ):
            return "reason"
        if "which date" in text or "preferred date" in text or "what date" in text or "తేదీ" in text or "date" in text or "तारीख" in text:
            return "preferred_date"
        if "what time" in text or "preferred time" in text or "which time" in text or "సమయం" in text or "time" in text or "समय" in text:
            return "preferred_time"
        if "first visit" in text or "follow-up" in text or "follow up" in text or "మొదటి" in text or "ఫాలో" in text or "पहली" in text:
            return "visit_type"
        if (
            "urgent symptom" in text
            or "severe pain" in text
            or "sudden vision" in text
            or "తీవ్రమైన" in text
            or "urgent symptoms" in text
            or "अर्जेंट" in text
            or "तेज दर्द" in text
        ):
            return "urgent_symptoms"
    return None


def extract_turn_entities(text: str, *, expected_slot: str | None = None) -> dict[str, Any]:
    value = _clean(text)
    if expected_slot == "reference_number" and _is_pure_numberish(value):
        digits = _digits(value)
        if 3 <= len(digits) <= 24:
            return {"reference_number": digits}
    phone = normalize_phone_number(value, expected=expected_slot == "phone")
    entities: dict[str, Any] = {}
    if phone:
        entities["phone"] = phone
    # Telugu / Hindi relative date + part-of-day get folded to canonical English
    # so the English-native _DATE_RE / _TIME_RE match a hi/te booking turn too.
    dt_value = normalize_relative_datetime_text(value)
    date_match = _DATE_RE.search(dt_value)
    if date_match:
        entities["date_text"] = date_match.group(0)
    time_match = _TIME_RE.search(dt_value)
    if time_match:
        entities["time_text"] = time_match.group(0)
    elif expected_slot == "preferred_time" and _BARE_TIME_RE.fullmatch(value):
        entities["time_text"] = value
    urgent = _URGENT_SYMPTOM_RE.search(value)
    if urgent:
        entities["urgent_symptom"] = urgent.group(0)
    if _FIRST_VISIT_RE.search(value) or re.search(r"(మొదటి|ఫస్ట్|पहली|पहला|नया)", value, re.IGNORECASE):
        entities["visit_type"] = "first_visit"
    elif _FOLLOW_UP_RE.search(value) or re.search(r"(ఫాలో|రివ్యూ|మళ్లీ|फॉलो|रिव्यू|दोबारा)", value, re.IGNORECASE):
        entities["visit_type"] = "follow_up"
    if _is_pure_numberish(value) and not phone:
        digits = _digits(value)
        if 3 <= len(digits) <= 24:
            entities["reference_number"] = digits
    return entities


def _unicode_lettersish(value: str) -> bool:
    has_letter = False
    for char in value:
        if char in " .'-":
            continue
        category = unicodedata.category(char)
        if category.startswith("L") or category.startswith("M"):
            has_letter = True
            continue
        return False
    return has_letter


_INDIC_TRAILING_PUNCT = "।॥।।…・「」（）"


# Discourse fillers that can precede a bare name answer ("you know Nihar",
# "well, Nihar"). Stripped repeatedly from the front before validation.
_NAME_DISCOURSE_PREFIX_RE = re.compile(
    r"^(?:\s*(?:well|so|um+|uh+|hmm+|oh|see|look|listen|actually|basically|"
    r"you\s+know|i\s+mean|it'?s|its)\b[\s,\.]*)+",
    re.IGNORECASE,
)


def _strip_name_suffix(value: str) -> str:
    value = _clean(value)
    # Peel leading discourse fillers so "You know Nihar" → "Nihar".
    value = _NAME_DISCOURSE_PREFIX_RE.sub("", value)
    value = re.sub(r"(?:గారు|అండి|ండి|sir|madam|ji|जी)\.?\s*$", "", value, flags=re.IGNORECASE)
    # Strip Indic sentence terminators (Devanagari ।, double-danda ॥) plus
    # ASCII punctuation. STT often appends a danda or full stop to the
    # spoken name; carrying it through would store the name as "निहार।"
    # and recite it back literally.
    stripped = value.strip(" ,.-" + _INDIC_TRAILING_PUNCT)
    return _clean(stripped)


def _extract_patient_name(text: str) -> str | None:
    name, _high_confidence = _extract_patient_name_with_confidence(text)
    return name


_LEADING_NEGATION_RE = re.compile(
    r"^\s*(?:no|nope|nah|not\s+really|actually|wait|sorry)\b[\s,]*",
    re.IGNORECASE,
)


def _extract_name_after_negation(text: str) -> str | None:
    """Strip a leading negation ("No,", "Actually,", "Sorry,") and re-run
    name extraction on the remainder. Lets utterances like "No, my name
    is Nihar" yield a corrected name during the confirmation handshake
    instead of being thrown away as a plain negative."""
    cleaned = _LEADING_NEGATION_RE.sub("", text or "")
    if not cleaned or cleaned == text:
        return None
    return _extract_patient_name(cleaned)


def _extract_patient_name_with_confidence(text: str) -> tuple[str | None, bool]:
    """Return (name, high_confidence).

    ``high_confidence`` is True when the caller introduced the name with an
    explicit prefix ("my name is X", "నా పేరు X", "मेरा नाम X"). Callers can
    use the flag to skip the name-confirmation handshake, which exists to
    catch STT errors on bare-name utterances and would otherwise stall a
    happy path where the caller already framed the name unambiguously.
    """
    value = _strip_name_suffix(text)
    if not value:
        return None, False
    match = _NAME_PREFIX_RE.search(value)
    if match:
        # The caller explicitly introduced the name ("the full name of the
        # patient is …", "my name is …"). Validate the *candidate* (post
        # prefix), so utterances like "It's actually about eye pain" still
        # get filtered by the exclusion regex.
        candidate = _strip_name_suffix(match.group(1))
        if not candidate:
            return None, False
        if (
            _QUESTION_RE.search(candidate)
            or _NAME_EXCLUSION_RE.search(candidate)
            or any(c.isdigit() for c in candidate)
        ):
            return None, False
        words = candidate.split()
        if not (1 <= len(words) <= 6) or len(candidate) > 120:
            return None, False
        if not _unicode_lettersish(candidate):
            return None, False
        return candidate, True
    if _looks_like_name(value):
        return value, False
    return None, False


def _looks_like_name(text: str) -> bool:
    value = _strip_name_suffix(text)
    if (
        not value
        or _QUESTION_RE.search(value)
        or _NAME_EXCLUSION_RE.search(value)
        or any(char.isdigit() for char in value)
    ):
        return False
    words = value.split()
    if not (1 <= len(words) <= 5):
        return False
    if len(value) > 120:
        return False
    return _unicode_lettersish(value)


def _looks_like_reason(text: str) -> bool:
    value = _strip_polite_suffix(text)
    if not value or _QUESTION_RE.search(value):
        return False
    if normalize_phone_number(value) or _DATE_RE.search(value) or _TIME_RE.search(value):
        return False
    return 2 <= len(value.split()) <= 28


def _parse_slot_date(raw: str | None) -> date | None:
    """Best-effort parse of slot-fill date strings.

    Mirrors the lighter cases handled by ``NokvoOneVoicePipeline._parse_appointment_date``
    so the policy layer can detect "appointment is in the past" *before* the
    tool round-trip, instead of letting the caller finish four more questions
    and only then be told to start over.
    """
    if not raw:
        return None
    text = re.sub(r"\s+", " ", normalize_relative_datetime_text(str(raw)).strip().lower())
    if not text:
        return None
    today = datetime.now(_APPOINTMENT_LOCAL_TZ).date()
    if text in {"today"}:
        return today
    if text in {"tomorrow"}:
        return today + timedelta(days=1)
    if text in {"day after tomorrow"}:
        return today + timedelta(days=2)
    # 20 may 2026 / 20th may / may 20 / 20-05-2026 / 20/05
    match = re.match(
        r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,9})(?:\s+(\d{2,4}))?$",
        text,
    )
    if match:
        day = int(match.group(1))
        month = _MONTH_LOOKUP.get(match.group(2)[:3])
        year = int(match.group(3)) if match.group(3) else today.year
        if year < 100:
            year += 2000
        if month and 1 <= day <= 31:
            try:
                candidate = date(year, month, day)
                if candidate < today and not match.group(3):
                    candidate = date(year + 1, month, day)
                return candidate
            except ValueError:
                return None
    match = re.match(r"([a-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s+(\d{2,4}))?$", text)
    if match:
        month = _MONTH_LOOKUP.get(match.group(1)[:3])
        day = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else today.year
        if year < 100:
            year += 2000
        if month and 1 <= day <= 31:
            try:
                candidate = date(year, month, day)
                if candidate < today and not match.group(3):
                    candidate = date(year + 1, month, day)
                return candidate
            except ValueError:
                return None
    match = re.match(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?$", text)
    if match:
        day = int(match.group(1))
        month = int(match.group(2))
        year = int(match.group(3)) if match.group(3) else today.year
        if year < 100:
            year += 2000
        if 1 <= month <= 12 and 1 <= day <= 31:
            try:
                return date(year, month, day)
            except ValueError:
                return None
    return None


def _parse_slot_time(raw: str | None) -> time | None:
    if not raw:
        return None
    text = re.sub(r"\s+", " ", normalize_relative_datetime_text(str(raw)).strip().lower())
    if not text:
        return None
    named = {
        "morning": time(9, 0), "afternoon": time(14, 0),
        "evening": time(17, 0), "night": time(19, 0), "noon": time(12, 0),
    }
    for label, value in named.items():
        if label in text:
            return value
    ampm = re.search(r"\b(\d{1,2})(?::([0-5]\d))?\s*(am|pm)\b", text)
    if ampm:
        hour = int(ampm.group(1))
        minute = int(ampm.group(2) or 0)
        suffix = ampm.group(3)
        if 1 <= hour <= 12:
            if suffix == "pm" and hour != 12:
                hour += 12
            if suffix == "am" and hour == 12:
                hour = 0
            return time(hour, minute)
    twenty_four = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text)
    if twenty_four:
        return time(int(twenty_four.group(1)), int(twenty_four.group(2)))
    return None


def _appointment_is_past(date_text: str | None, time_text: str | None) -> bool:
    parsed_date = _parse_slot_date(date_text)
    parsed_time = _parse_slot_time(time_text)
    if not parsed_date or not parsed_time:
        return False
    local_dt = datetime.combine(parsed_date, parsed_time, tzinfo=_APPOINTMENT_LOCAL_TZ)
    return local_dt <= datetime.now(_APPOINTMENT_LOCAL_TZ)


def _name_confirmation_prompt(name: str, language: str | None) -> str:
    lang = _language_code(language)
    if lang == "te":
        return f"Just to confirm — patient పేరు {name} అని. Right ఆ?"
    if lang == "hi":
        return f"बस कन्फर्म करना है — मरीज़ का नाम {name} है। सही है?"
    return f"Just to confirm — the patient's name is {name}. Is that right?"


def _is_high_confidence_phone(phone: str) -> bool:
    """Phones in canonical formats are skip-confirmation candidates: 10 digits
    starting with [6-9] for Indian mobiles, or +91 / 91 prefixed equivalents.
    Anything weird (too short, leading zero, repeated digit, etc.) keeps
    triggering the confirmation handshake."""
    digits = _digits(phone or "")
    if not digits:
        return False
    if digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    if len(digits) != 10 or digits[0] not in "6789":
        return False
    # Reject obvious STT garbage like "9999999999" / "8888888888".
    if len(set(digits)) <= 2:
        return False
    return True


def _format_phone_for_speech(phone: str) -> str:
    """Render a phone number as space-separated digits so TTS reads it
    digit-by-digit (avoids "nine billion one hundred seventy-seven million…")."""
    digits = _digits(phone or "")
    if not digits:
        return phone or ""
    # Group last 10 digits in 5-5 for Indian numbers, prefix with country code if any.
    if len(digits) > 10:
        prefix = digits[:-10]
        body = digits[-10:]
        return " ".join(list(prefix)) + " " + " ".join(list(body[:5])) + " " + " ".join(list(body[5:]))
    if len(digits) == 10:
        return " ".join(list(digits[:5])) + " " + " ".join(list(digits[5:]))
    return " ".join(list(digits))


def _phone_confirmation_prompt(phone: str, language: str | None) -> str:
    spoken = _format_phone_for_speech(phone)
    lang = _language_code(language)
    if lang == "te":
        return f"Just to confirm — {spoken}. Right ఆ?"
    if lang == "hi":
        return f"बस कन्फर्म करना है — {spoken}. सही है?"
    return f"Just to confirm — {spoken}. Is that the right number?"


def _email_confirmation_prompt(email: str, language: str | None) -> str:
    lang = _language_code(language)
    if lang == "te":
        return f"Just to confirm — email {email} అని. Right ఆ?"
    if lang == "hi":
        return f"कन्फर्म करना है — ईमेल {email} है। सही है?"
    return f"Just to confirm — {email}. Is that the right email?"


def _id_confirmation_prompt(value: str, slot_label: str, language: str | None) -> str:
    spoken = " ".join(list(str(value))) if str(value).isdigit() else str(value)
    lang = _language_code(language)
    if lang == "te":
        return f"Just to confirm — {slot_label} {spoken}. Right ఆ?"
    if lang == "hi":
        return f"कन्फर्म करना है — {slot_label} {spoken}. सही है?"
    return f"Just to confirm — {slot_label} {spoken}. Is that right?"


def _past_appointment_prompt(language: str | None) -> str:
    lang = _language_code(language)
    if lang == "te":
        return "ఆ time అప్పటికే past లో ఉంది. ఏ future date మరియు time note చేయాలి?"
    if lang == "hi":
        return "वो समय बीत चुका है। कौन सी आगे की तारीख और समय नोट करूं?"
    return "That appointment time is already past. Which future date and time should I note?"


def _format_friendly_date(parsed: date, language: str | None) -> str:
    lang = _language_code(language)
    return parsed.strftime("%d %b").lstrip("0")


def _shift_to_next_day_prompt(
    original_date: str | None,
    original_time: str | None,
    language: str | None,
) -> tuple[str, str, str] | None:
    """When the requested datetime has passed today, suggest the SAME time
    on the next valid day instead of clearing both slots. Returns
    (next_date_iso, next_date_label, prompt) or None if we can't infer a
    sensible suggestion."""
    parsed_time = _parse_slot_time(original_time)
    parsed_date = _parse_slot_date(original_date)
    if not parsed_time or not parsed_date:
        return None
    next_date = parsed_date + timedelta(days=1)
    # Surface the suggestion in IST-friendly form (e.g. "21 May at 11 AM").
    nice_date = _format_friendly_date(next_date, language)
    nice_time = original_time.strip()
    lang = _language_code(language)
    if lang == "te":
        prompt = (
            f"{original_time} ఇవాళ అప్పటికే past లో ఉంది. "
            f"Same time on {nice_date} ({nice_time}) book చేయనా?"
        )
    elif lang == "hi":
        prompt = (
            f"{original_time} आज बीत चुका है। "
            f"{nice_date} को उसी समय ({nice_time}) बुक करूं?"
        )
    else:
        prompt = (
            f"{original_time} today has already passed. "
            f"Shall I book the same time on {nice_date} ({nice_time}) instead?"
        )
    return next_date.isoformat(), nice_date, prompt


# ASCII affirmatives carry \b so "yes please" matches but "yesterday" doesn't.
# Indic affirmatives use (?=\s|$|[.,!?]) lookahead instead — Python's \b only
# considers ASCII word chars, so the Devanagari/Telugu branches in the old
# combined regex never fired at all.
#
# Voice STT regularly emits informal yes-equivalents ("ya", "yah", "mhm",
# "uh huh", "right", "correct") — keeping the alternation conservative
# misclassifies these as new names / corrections / unrelated input. The
# expanded set captures realistic call audio without colliding with words
# like "right now" / "right away" (we anchor at start-of-utterance).
_AFFIRMATIVE_ASCII_RE = re.compile(
    r"^\s*(?:"
    r"yes|yea|yeah|yeahh?|yep+|yup+|ya|yah|yass+|yess+|"
    r"sure|ok(?:ay)?|alright|"
    r"please|please\s+do|"
    r"go\s+ahead|sounds\s+good|that\s+works|"
    r"that(?:'s|\s+is)\s+(?:fine|correct|right)|"
    r"correct|right|absolutely|definitely|of\s+course|"
    r"mm[\- ]?hmm?|uh[\- ]?huh|mhm+|"
    r"do\s+(?:it|that)|book\s+it|book\s+that|confirm(?:ed)?|"
    r"lock\s+(?:it|that)\s+in"
    r")\b",
    re.IGNORECASE,
)
_AFFIRMATIVE_INDIC_RE = re.compile(
    r"^\s*(అవును|సరే|ఓకే|"
    r"हाँ|हां|जी|ठीक है|कर दो)(?=\s|$|[.,!?])",
    re.IGNORECASE,
)


_NEGATIVE_ASCII_RE = re.compile(
    r"^\s*(?:"
    r"no|nope|nah|naw|"
    r"not\s+(?:really|correct|right|quite)|"
    r"that(?:'s|\s+is)\s+(?:not|wrong|incorrect)|"
    r"wrong|incorrect|change|"
    r"actually(?:[,]?\s+(?:no|not))?"
    r")\b",
    re.IGNORECASE,
)
_NEGATIVE_INDIC_RE = re.compile(
    r"^\s*(కాదు|లేదు|"
    r"नहीं|नही|गलत|बदलो)(?=\s|$|[.,!?])",
    re.IGNORECASE,
)


def _looks_affirmative(text: str) -> bool:
    cleaned = _clean(text)
    return bool(_AFFIRMATIVE_ASCII_RE.match(cleaned) or _AFFIRMATIVE_INDIC_RE.match(cleaned))


def _looks_negative(text: str) -> bool:
    cleaned = _clean(text)
    return bool(_NEGATIVE_ASCII_RE.match(cleaned) or _NEGATIVE_INDIC_RE.match(cleaned))


# Cues volunteered in the reason text that tell us this is a first visit /
# follow-up / has no urgent symptoms, so we don't need to ritually re-ask.
_REASON_FIRST_VISIT_RE = re.compile(
    r"\b(first\s+(?:visit|time|appointment|consult)|new\s+patient|never\s+been\s+here\s+before|"
    r"haven['']?t\s+been\s+here\s+before)\b|"
    r"(మొదటి\s+(?:విజిట్|సారి)|మొదటిసారి|"
    r"पहली\s+(?:बार|विजिट)|नया\s+मरीज)",
    re.IGNORECASE,
)
_REASON_FOLLOW_UP_RE = re.compile(
    r"\b(follow[-\s]?up|review|coming\s+back|already\s+been\s+here|"
    r"already\s+visited|old\s+patient|return\s+visit)\b|"
    r"(ఫాలో|రివ్యూ|మళ్లీ|"
    r"फॉलो|रिव्यू|दोबारा|पुराना\s+मरीज)",
    re.IGNORECASE,
)
_REASON_NO_URGENT_RE = re.compile(
    r"\b(routine|regular|annual|yearly|just\s+a\s+)?check[-\s]?up\b|"
    r"\b(general\s+(?:check|review)|nothing\s+(?:urgent|major|serious|critical)|"
    r"not\s+(?:urgent|emergency|emergencies?)|no\s+(?:emergency|urgency|pain))\b|"
    r"(రెగ్యులర్\s+చెకప్|జనరల్\s+చెకప్|అత్యవసరం\s+కాదు|"
    r"रेगुलर\s+चेकअप|जनरल\s+चेकअप|कोई\s+इमरजेंसी\s+नहीं)",
    re.IGNORECASE,
)


def _infer_implicit_slots(text: str, appointment: dict[str, Any]) -> list[str]:
    """Pre-fill visit_type and urgent_symptoms when the caller volunteered
    them in the same utterance. Returns the slot names we filled, so the
    caller can audit which questions we skipped."""
    filled: list[str] = []
    if not text:
        return filled
    cleaned = _clean(text).lower()
    if not appointment.get("visit_type"):
        if _REASON_FIRST_VISIT_RE.search(cleaned):
            appointment["visit_type"] = "first_visit"
            filled.append("visit_type")
        elif _REASON_FOLLOW_UP_RE.search(cleaned):
            appointment["visit_type"] = "follow_up"
            filled.append("visit_type")
    if not appointment.get("urgent_symptoms"):
        if _REASON_NO_URGENT_RE.search(cleaned) and not _URGENT_SYMPTOM_RE.search(cleaned):
            appointment["urgent_symptoms"] = "none_reported"
            filled.append("urgent_symptoms")
    return filled


def _next_appointment_slot(appointment: dict[str, Any]) -> str | None:
    for slot in (
        "reason",
        "patient_name",
        "phone",
        "preferred_date",
        "preferred_time",
        "visit_type",
        "urgent_symptoms",
    ):
        if not appointment.get(slot):
            return slot
    return None


def _language_code(language: str | None) -> str:
    return (language or "en").split("-")[0].lower()


def _appointment_question(slot: str, language: str | None = None) -> str:
    lang = _language_code(language)
    if lang == "te":
        return {
            "reason": "Visit reason లేదా eye concern ఏమిటి?",
            "patient_name": "Patient full name చెప్పండి.",
            "phone": "Confirmation కోసం phone number చెప్పండి.",
            "preferred_date": "Preferred date ఏది?",
            "preferred_time": "Preferred time ఏది?",
            "visit_type": "First visit ఆ, follow-up ఆ?",
            "urgent_symptoms": (
                "Severe eye pain, sudden vision loss, injury, chemical exposure, "
                "లేదా sudden blurry vision లాంటివి ఏమైనా ఉన్నాయా?"
            ),
        }.get(slot, "ఇంకా ఏ detail note చేయాలి?")
    if lang == "hi":
        return {
            "reason": "विजिट के लिए आंख की समस्या या कारण क्या नोट करूं?",
            "patient_name": "कृपया मरीज का पूरा नाम बताएंगे?",
            "phone": "कन्फर्मेशन के लिए सबसे अच्छा फोन नंबर क्या है?",
            "preferred_date": "आप कौन सी तारीख पसंद करेंगे?",
            "preferred_time": "आप कौन सा समय पसंद करेंगे?",
            "visit_type": "यह पहली विजिट है या फॉलो-अप?",
            "urgent_symptoms": (
                "क्या तेज दर्द, अचानक दिखना कम होना, चोट, केमिकल एक्सपोजर, "
                "या अचानक धुंधला दिखना जैसे कोई अर्जेंट लक्षण हैं?"
            ),
        }.get(slot, "और क्या नोट करूं?")
    return {
        "reason": "What eye concern or reason should I note for the visit?",
        "patient_name": "Could I have the patient's full name?",
        "phone": "What is the best phone number for confirmation?",
        "preferred_date": "Which date would you prefer?",
        "preferred_time": "What time would you prefer?",
        "visit_type": "Is this a first visit or a follow-up?",
        "urgent_symptoms": (
            "Are there any urgent symptoms like severe pain, sudden vision loss, "
            "injury, chemical exposure, or sudden blurred vision?"
        ),
    }.get(slot, "What else should I note?")


def _coming_back_prefix(language: str | None) -> str:
    """Prefix prepended to a slot question when the FSM resumes after a
    knowledge-base digression. Set by callers that yield to RAG mid-booking
    (regex detector OR LLM digression check) via ``appointment.deferred_for_kb``.
    """
    lang = _language_code(language)
    if lang == "te":
        return "మీ బుకింగ్‌కి తిరిగి వద్దాం — "
    if lang == "hi":
        return "आपकी बुकिंग पर वापस आते हैं — "
    return "Coming back to your booking — "


def _appointment_summary(appointment: dict[str, Any], language: str | None = None) -> str:
    parts = []
    if appointment.get("patient_name"):
        parts.append(f"patient {appointment['patient_name']}")
    if appointment.get("phone"):
        parts.append(f"phone {appointment['phone']}")
    if appointment.get("reason"):
        parts.append(f"reason {appointment['reason']}")
    if appointment.get("preferred_date") or appointment.get("preferred_time"):
        when = " ".join(
            str(appointment.get(key) or "")
            for key in ("preferred_date", "preferred_time")
            if appointment.get(key)
        )
        parts.append(f"preferred time {when}")
    summary = ", ".join(parts)
    lang = _language_code(language)
    if lang == "te":
        return (
            f"సరే, నేను {summary} అని నమోదు చేశాను. క్లినిక్ టీమ్ ఖచ్చితమైన అందుబాటును కన్ఫర్మ్ చేస్తారు."
            if summary
            else "సరే, అపాయింట్మెంట్ రిక్వెస్ట్ నమోదు చేశాను. క్లినిక్ టీమ్ ఖచ్చితమైన అందుబాటును కన్ఫర్మ్ చేస్తారు."
        )
    if lang == "hi":
        return (
            f"ठीक है, मैंने {summary} नोट कर लिया है। क्लिनिक टीम सही उपलब्धता कन्फर्म करेगी."
            if summary
            else "ठीक है, मैंने अपॉइंटमेंट रिक्वेस्ट नोट कर ली है। क्लिनिक टीम सही उपलब्धता कन्फर्म करेगी."
        )
    return (
        f"Thanks, I have noted {summary}. The clinic team can confirm exact availability."
        if summary
        else "Thanks, I have noted the appointment request. The clinic team can confirm exact availability."
    )


def _strip_polite_suffix(value: str) -> str:
    value = _clean(value)
    value = re.sub(r"\b(?:please|actually|it(?:'s| is)|that(?:'s| is))\b", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:andi|sir|madam)\b", "", value, flags=re.IGNORECASE)
    value = re.sub(r"(?:అండి|ండి|గారు|जी)\.?\s*$", "", value, flags=re.IGNORECASE)
    return _clean(value.strip(" ,.-"))


def _extract_reason_correction(text: str) -> str | None:
    value = _clean(text)
    if not value or not _CORRECTION_MARKER_RE.search(value):
        return None
    segments = [
        _strip_polite_suffix(part)
        for part in re.split(r"[,.;!?]+|\bbut\b|\binstead\b", value, flags=re.IGNORECASE)
    ]
    candidates = [
        segment
        for segment in segments
        if segment
        and not _CORRECTION_MARKER_RE.search(segment)
        and _looks_like_reason(segment)
    ]
    if candidates:
        return candidates[0]
    cleaned = _CORRECTION_MARKER_RE.sub(" ", value)
    cleaned = _strip_polite_suffix(cleaned)
    return cleaned if _looks_like_reason(cleaned) else None


_KB_REQUEST_VERB_RE = re.compile(
    r"\b("
    r"list|know|share|explain|describe|show|give|tell|provide|mention|"
    r"want\s+to\s+know|like\s+to\s+know|need\s+to\s+know|"
    r"i'?d\s+like\s+to|i\s+would\s+like\s+to|"
    r"is\s+(?:it\s+)?(?:possible|there)|are\s+(?:there|you|the)|"
    r"do\s+you\s+(?:have|offer|provide)|does\s+(?:the|your)|"
    r"can\s+you|could\s+you|would\s+you"
    r")\b|"
    r"(చెప్పండి|చెప్పగలరా|లిస్ట్|వివరించండి|बताइए|बताओ|बता|बतायें|लिस्ट|दीजिए|दे)",
    re.IGNORECASE,
)


def _is_side_question_during_booking(value: str, *, entities: dict[str, Any]) -> bool:
    """Detect a caller pivoting mid-booking to a knowledge-base question.

    Conservative: returns True only when there is strong evidence the caller
    is pausing the slot-fill flow. The previous-turn appointment state is
    preserved by the caller (returning ``None`` leaves Redis untouched) so
    the next user turn naturally resumes the pending slot.
    """
    if (
        entities.get("phone")
        or entities.get("date_text")
        or entities.get("time_text")
        or entities.get("visit_type")
    ):
        return False
    if _SIDE_QUESTION_PIVOT_RE.search(value):
        return True
    if _SENSITIVE_PIVOT_RE.search(value):
        return True
    # Any explicit KB topic hint (services, fees, doctors, timing, etc.) during
    # an active booking is treated as a pivot. The slot-fill FSM only ever asks
    # for personal details (name/phone/reason/date/time) — none of those slots
    # have a legitimate answer that contains "services", "doctors", "fees" and
    # the like. So a mention of those keywords is by definition a side query.
    if _KB_TOPIC_HINT_RE.search(value):
        return True
    if _QUESTION_RE.search(value) and _KB_REQUEST_VERB_RE.search(value):
        return True
    return False


def evaluate_voice_turn_policy(
    text: str,
    *,
    history: list[dict[str, str]] | None = None,
    state: dict[str, Any] | None = None,
    language: str | None = None,
) -> dict[str, Any] | None:
    value = _clean(text)
    if not value:
        return None
    history = history or []
    state = dict(state or {})
    expected_slot = infer_expected_slot(history, state)
    entities = extract_turn_entities(value, expected_slot=expected_slot)
    appointment = dict(state.get("appointment") or {})
    appointment_closed = bool(appointment.get("completed")) and not appointment.get("pending_slot")

    urgent = entities.get("urgent_symptom")
    if urgent:
        return {
            "answer": (
                "This may need urgent medical attention. Please do not delay. "
                "If possible, visit the clinic immediately, or go to the nearest emergency hospital."
            ),
            "intent": "urgent_symptom",
            "entities": entities,
            "state_patch": {"last_urgent_symptom": urgent},
            "reason": "urgent symptom detected",
        }

    active_appointment = (bool(appointment.get("active")) and not appointment_closed) or bool(
        _APPOINTMENT_INTENT_RE.search(value)
    ) or (expected_slot in _APPOINTMENT_SLOTS and not appointment_closed)

    # Availability lookup: caller is asking the agent to check its own
    # calendar ("is 11:30 AM available?", "when can you book?", "any slot
    # tomorrow?"). Return an intent the pipeline will resolve against the
    # scheduler instead of falling through to a slow RAG path.
    if active_appointment and _AVAILABILITY_QUERY_RE.search(value):
        availability_entities: dict[str, Any] = {}
        if entities.get("date_text"):
            availability_entities["date_text"] = entities["date_text"]
        if entities.get("time_text"):
            availability_entities["time_text"] = entities["time_text"]
        # Carry the in-progress appointment so the handler can offer to slot
        # the matched time straight into preferred_date/preferred_time on
        # caller confirmation.
        return {
            "answer": None,  # filled in by the pipeline handler
            "intent": "availability_check",
            "entities": availability_entities,
            "language": _language_code(language),
            "state_patch": {"appointment": appointment},
            "state_slot": "availability_check",
            "reason": "caller asked about specific or next-available slot",
        }

    # Yield to RAG when the caller pivots to a KB question. This must run on
    # FIRST-turn pivots ("Before I book, what services do you offer?") as well
    # as mid-booking digressions — the only condition is that the booking
    # intent was detected (active_appointment) AND the caller's message looks
    # like a side question rather than slot data. Returning None leaves any
    # existing appointment state untouched; the route layer attaches the
    # `deferred_for_kb` marker only when there was a pre-existing booking,
    # so a first-turn pivot doesn't get an inappropriate resumption prefix.
    if active_appointment and _is_side_question_during_booking(value, entities=entities):
        return None

    # Caller answering "yes/no" to a slot we just offered ("11:30 is taken,
    # next free is 12:00 — want me to book that?").
    if appointment.get("awaiting_slot_confirm"):
        if _looks_affirmative(value):
            proposed_utc = appointment.pop("proposed_slot_utc", None)
            proposed_label = appointment.pop("proposed_slot_label", None)
            appointment["awaiting_slot_confirm"] = False
            if proposed_utc:
                try:
                    parsed = datetime.fromisoformat(proposed_utc.replace("Z", "+00:00"))
                    local = parsed.astimezone(_APPOINTMENT_LOCAL_TZ)
                    # Store the date in a format the appointment-date parser
                    # actually accepts ("22 May 2026"), not bare ISO — the
                    # parser regex doesn't match `YYYY-MM-DD` and we'd lose
                    # the locked slot to a "That date does not look valid"
                    # rejection later in the flow.
                    appointment["preferred_date"] = local.strftime("%d %b %Y")
                    appointment["preferred_time"] = local.strftime("%I:%M %p").lstrip("0")
                    # And persist the canonical ISO so the tool path can
                    # short-circuit re-parsing entirely.
                    appointment["appointment_time"] = proposed_utc
                    from app.services.flow_session import stamp_proposed_slot_accepted
                    stamp_proposed_slot_accepted(
                        appointment, slot_utc_iso=proposed_utc, member_name=proposed_label,
                    )
                except Exception:
                    pass
            appointment["active"] = True
            # Continue down to next_slot computation so the agent moves on
            # to whichever field is still missing (or completes the booking).
        elif _looks_negative(value):
            appointment["awaiting_slot_confirm"] = False
            appointment.pop("proposed_slot_utc", None)
            appointment.pop("proposed_slot_label", None)
            appointment["pending_slot"] = "preferred_date"
            return {
                "answer": _appointment_question("preferred_date", language),
                "intent": "appointment_flow",
                "entities": entities,
                "language": _language_code(language),
                "state_patch": {"appointment": appointment},
                "state_slot": "preferred_date",
                "reason": "caller declined offered slot",
            }
        # If the caller said something else, fall through — the rest of the
        # block may pick up new entities from the utterance.

    if active_appointment:
        appointment["active"] = True
        # Defence-in-depth: even if the regex pivot detector above somehow
        # missed it, never store a value that mentions a KB topic (services,
        # doctors, fees, …) as the reason slot. None of those words belong in
        # a legitimate "eye concern" answer; if they appear, the caller is
        # asking a side question, not naming a visit reason.
        value_is_kb_query = bool(_KB_TOPIC_HINT_RE.search(value)) or (
            _QUESTION_RE.search(value) is not None
            and _KB_REQUEST_VERB_RE.search(value) is not None
        )
        if value_is_kb_query:
            return None
        corrected_reason = _extract_reason_correction(value)
        if corrected_reason and appointment.get("reason"):
            appointment["reason"] = corrected_reason
            entities["corrected_reason"] = corrected_reason
        if expected_slot == "reason" and _looks_like_reason(value):
            appointment["reason"] = _strip_polite_suffix(value)
        elif (
            not appointment.get("reason")
            and _APPOINTMENT_INTENT_RE.search(value)
            and _VISIT_REASON_HINT_RE.search(value)
        ):
            appointment["reason"] = _strip_polite_suffix(value)
        # Name confirmation handshake: if we just captured the name on the
        # previous turn and asked the caller to confirm, treat this turn as
        # the answer to that confirmation. We accept "yes" verbatim, take any
        # spelled-out replacement as the corrected name, and reject explicit
        # "no" by re-asking for the name.
        if appointment.get("awaiting_name_confirmation"):
            appointment["awaiting_name_confirmation"] = False
            # Priority order is important:
            #   1. Affirmative ("yes", "yes that's right") → confirm as-is.
            #   2. Negative with embedded correction ("No, my name is Nihar")
            #      → use the corrected name and re-confirm, instead of
            #      throwing away the correction and re-asking from scratch.
            #   3. Plain negative ("no") → clear and re-ask.
            #   4. Anything else → try to extract a corrected name; if none,
            #      re-ask.
            if _looks_affirmative(value):
                # Name stays as-is, proceed to next slot below.
                from app.services.flow_session import record_confirmation, append_audit_trail
                record_confirmation(appointment, "patient_name", appointment.get("patient_name"))
                append_audit_trail(appointment, "name_confirmed", detail=appointment.get("patient_name"))
            elif _looks_negative(value):
                # Look for an embedded correction first so "No, my name is
                # Nihar" still keeps the conversation moving. The plain
                # ``_extract_patient_name`` won't match because the
                # leading "No" trips the exclusion list — use the
                # negation-aware helper.
                embedded_correction = _extract_name_after_negation(value)
                if embedded_correction:
                    appointment["patient_name"] = embedded_correction
                    appointment["awaiting_name_confirmation"] = True
                    return {
                        "answer": _name_confirmation_prompt(embedded_correction, language),
                        "intent": "appointment_flow",
                        "entities": entities,
                        "language": _language_code(language),
                        "state_patch": {"appointment": appointment},
                        "state_slot": "patient_name_confirm",
                        "reason": "name corrected, awaiting confirmation",
                    }
                appointment["patient_name"] = None
                appointment["pending_slot"] = "patient_name"
                return {
                    "answer": _appointment_question("patient_name", language),
                    "intent": "appointment_flow",
                    "entities": entities,
                    "language": _language_code(language),
                    "state_patch": {"appointment": appointment},
                    "state_slot": "patient_name",
                    "reason": "name confirmation rejected",
                }
            else:
                # Treat the new utterance as the correction when it parses
                # as a name.
                replacement = _extract_patient_name(value)
                if replacement:
                    appointment["patient_name"] = replacement
                    appointment["awaiting_name_confirmation"] = True
                    return {
                        "answer": _name_confirmation_prompt(replacement, language),
                        "intent": "appointment_flow",
                        "entities": entities,
                        "language": _language_code(language),
                        "state_patch": {"appointment": appointment},
                        "state_slot": "patient_name_confirm",
                        "reason": "name corrected, awaiting confirmation",
                    }
        elif expected_slot == "patient_name":
            patient_name = _extract_patient_name(value)
            if patient_name:
                appointment["patient_name"] = patient_name
                appointment["awaiting_name_confirmation"] = True
                appointment["pending_slot"] = "patient_name_confirm"
                return {
                    "answer": _name_confirmation_prompt(patient_name, language),
                    "intent": "appointment_flow",
                    "entities": entities,
                    "language": _language_code(language),
                    "state_patch": {"appointment": appointment},
                    "state_slot": "patient_name_confirm",
                    "reason": "name captured, awaiting confirmation",
                }
        # Phone confirmation handshake — STT mis-hears digits often, so we read
        # the captured number back and require a yes before persisting it.
        if appointment.get("awaiting_phone_confirmation"):
            appointment["awaiting_phone_confirmation"] = False
            if _looks_affirmative(value):
                from app.services.flow_session import record_confirmation, append_audit_trail
                record_confirmation(appointment, "phone", appointment.get("phone"))
                append_audit_trail(appointment, "phone_confirmed", detail=appointment.get("phone"))
            elif _looks_negative(value):
                appointment["phone"] = None
                appointment["pending_slot"] = "phone"
                return {
                    "answer": _appointment_question("phone", language),
                    "intent": "appointment_flow",
                    "entities": entities,
                    "language": _language_code(language),
                    "state_patch": {"appointment": appointment},
                    "state_slot": "phone",
                    "reason": "phone confirmation rejected",
                }
            elif entities.get("phone"):
                appointment["phone"] = entities["phone"]
                appointment["awaiting_phone_confirmation"] = True
                appointment["pending_slot"] = "phone_confirm"
                return {
                    "answer": _phone_confirmation_prompt(entities["phone"], language),
                    "intent": "appointment_flow",
                    "entities": entities,
                    "language": _language_code(language),
                    "state_patch": {"appointment": appointment},
                    "state_slot": "phone_confirm",
                    "reason": "phone corrected, awaiting confirmation",
                }
        elif expected_slot == "phone" and entities.get("phone") and not appointment.get("phone"):
            appointment["phone"] = entities["phone"]
            # Spec gate: skip confirmation only when the policy allows the
            # high-confidence shortcut AND the value waives it.
            from app.services.agent_spec import CONFIRMATION_POLICY

            allow_skip = (
                CONFIRMATION_POLICY.confirm_phone_unless_high_confidence
                and _is_high_confidence_phone(entities["phone"])
            )
            if allow_skip:
                pass
            else:
                appointment["awaiting_phone_confirmation"] = True
                appointment["pending_slot"] = "phone_confirm"
                return {
                    "answer": _phone_confirmation_prompt(entities["phone"], language),
                    "intent": "appointment_flow",
                    "entities": entities,
                    "language": _language_code(language),
                    "state_patch": {"appointment": appointment},
                    "state_slot": "phone_confirm",
                    "reason": "phone captured, awaiting confirmation",
                }
        elif entities.get("phone"):
            appointment["phone"] = entities["phone"]
        if entities.get("date_text"):
            appointment["preferred_date"] = entities["date_text"]
        if entities.get("time_text"):
            appointment["preferred_time"] = entities["time_text"]
        if entities.get("visit_type"):
            appointment["visit_type"] = entities["visit_type"]
        if expected_slot == "urgent_symptoms" and _NO_URGENT_RE.search(value):
            appointment["urgent_symptoms"] = "none_reported"

        # Mine the same utterance for visit_type / urgent_symptoms cues the
        # caller volunteered (e.g. reason was "routine checkup, first visit,
        # nothing urgent"). Stops the agent from ritually re-asking later.
        inferred_slots = _infer_implicit_slots(value, appointment)
        if inferred_slots:
            entities["inferred_slots"] = inferred_slots

        # If both date and time are now filled, check immediately whether the
        # combined datetime is in the past. Instead of clearing both, we keep
        # the time and offer the same time on the next day — closer to what
        # a real receptionist would say.
        if appointment.get("preferred_date") and appointment.get("preferred_time"):
            if _appointment_is_past(appointment.get("preferred_date"), appointment.get("preferred_time")):
                shift = _shift_to_next_day_prompt(
                    appointment.get("preferred_date"),
                    appointment.get("preferred_time"),
                    language,
                )
                if shift is not None:
                    next_iso, _label, prompt = shift
                    appointment["proposed_date"] = next_iso
                    appointment["awaiting_past_time_shift"] = True
                    appointment["pending_slot"] = "preferred_date_shift_confirm"
                    return {
                        "answer": prompt,
                        "intent": "appointment_flow",
                        "entities": entities,
                        "language": _language_code(language),
                        "state_patch": {"appointment": appointment},
                        "state_slot": "preferred_date_shift_confirm",
                        "reason": "appointment time past — offered same time next day",
                    }
                # Fallback (couldn't parse): clear both and ask again.
                appointment["preferred_date"] = None
                appointment["preferred_time"] = None
                appointment["pending_slot"] = "preferred_date"
                return {
                    "answer": _past_appointment_prompt(language),
                    "intent": "appointment_flow",
                    "entities": entities,
                    "language": _language_code(language),
                    "state_patch": {"appointment": appointment},
                    "state_slot": "preferred_date",
                    "reason": "appointment time in the past",
                }

        # Handle the user's answer to the "same time tomorrow?" question.
        if appointment.get("awaiting_past_time_shift"):
            appointment["awaiting_past_time_shift"] = False
            proposed = appointment.pop("proposed_date", None)
            if _looks_affirmative(value) and proposed:
                appointment["preferred_date"] = proposed
                # preferred_time stays as the caller originally said it
            else:
                # Caller declined — either gave a new date/time in this turn,
                # or wants to pick fresh. Honour the new entities if any,
                # else clear and ask again.
                if not entities.get("date_text") and not entities.get("time_text"):
                    appointment["preferred_date"] = None
                    appointment["preferred_time"] = None
                    appointment["pending_slot"] = "preferred_date"
                    return {
                        "answer": _past_appointment_prompt(language),
                        "intent": "appointment_flow",
                        "entities": entities,
                        "language": _language_code(language),
                        "state_patch": {"appointment": appointment},
                        "state_slot": "preferred_date",
                        "reason": "shift declined, re-asking",
                    }
                if entities.get("date_text"):
                    appointment["preferred_date"] = entities["date_text"]
                if entities.get("time_text"):
                    appointment["preferred_time"] = entities["time_text"]

        next_slot = _next_appointment_slot(appointment)
        appointment["pending_slot"] = next_slot
        # Consume the digression marker (if any) so the resumption is
        # acknowledged exactly once and the flag is cleared from state.
        resumed_from_kb = bool(appointment.pop("deferred_for_kb", False))
        patch = {"appointment": appointment}
        if next_slot:
            # Adaptive disambiguation: when the caller has given a date but
            # not a time yet, query the scheduler for the earliest free slot
            # on that date instead of asking "what time?" in a vacuum.
            if (
                next_slot == "preferred_time"
                and appointment.get("preferred_date")
                and not appointment.get("preferred_time")
                and not appointment.get("offered_disambiguation")
            ):
                appointment["offered_disambiguation"] = True
                return {
                    "answer": None,
                    "intent": "availability_check",
                    "entities": {"date_text": appointment["preferred_date"]},
                    "language": _language_code(language),
                    "state_patch": {"appointment": appointment},
                    "state_slot": "availability_disambiguation",
                    "reason": "date given without time — offering concrete slot",
                }
            prefix = _coming_back_prefix(language) if resumed_from_kb else ""
            return {
                "answer": prefix + _appointment_question(next_slot, language),
                "intent": "appointment_flow",
                "entities": entities,
                "language": _language_code(language),
                "state_patch": patch,
                "state_slot": next_slot,
                "reason": "appointment slot collection",
            }
        appointment["completed"] = True
        appointment["active"] = False
        appointment["pending_slot"] = None
        return {
            "answer": _appointment_summary(appointment, language),
            "intent": "appointment_flow",
            "entities": entities,
            "language": _language_code(language),
            "state_patch": {"appointment": appointment},
            "state_slot": "complete",
            "reason": "appointment slots complete",
        }

    if expected_slot == "reference_number" and entities.get("reference_number"):
        return {
            "answer": "Thanks, I have that reference number. What would you like me to check?",
            "intent": "reference_number_capture",
            "entities": entities,
            "state_patch": {"last_reference_number": entities["reference_number"]},
            "reason": "expected reference number captured",
        }

    if entities.get("phone"):
        if expected_slot == "phone" or _PHONE_HINT_RE.search(value):
            answer = "Thanks, I have your phone number. What should I help you with next?"
            reason = "expected phone number captured"
        elif _ORDER_HINT_RE.search(value):
            return None
        else:
            answer = "Is that the best phone number to use?"
            reason = "bare phone number needs confirmation"
        return {
            "answer": answer,
            "intent": "phone_number_capture",
            "entities": entities,
            "state_patch": {"last_phone_number": entities["phone"]},
            "reason": reason,
        }

    return None
