from __future__ import annotations

import re
import unicodedata
from typing import Any


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
_NAME_PREFIX_RE = re.compile(
    r"^(?:my\s+name\s+is|name\s+is|i\s+am|this\s+is|నా\s+పేరు|పేరు|मेरा\s+नाम|नाम\s+है)\s+(.+)$",
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
    if _DATE_RE.search(value):
        entities["date_text"] = _DATE_RE.search(value).group(0)
    time_match = _TIME_RE.search(value)
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


def _strip_name_suffix(value: str) -> str:
    value = _clean(value)
    value = re.sub(r"(?:గారు|అండి|ండి|sir|madam|ji|जी)\.?\s*$", "", value, flags=re.IGNORECASE)
    return _clean(value.strip(" ,.-"))


def _extract_patient_name(text: str) -> str | None:
    value = _strip_name_suffix(text)
    if not value:
        return None
    match = _NAME_PREFIX_RE.search(value)
    if match:
        value = _strip_name_suffix(match.group(1))
    return value if _looks_like_name(value) else None


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
        if expected_slot == "patient_name":
            patient_name = _extract_patient_name(value)
            if patient_name:
                appointment["patient_name"] = patient_name
        if entities.get("phone"):
            appointment["phone"] = entities["phone"]
        if entities.get("date_text"):
            appointment["preferred_date"] = entities["date_text"]
        if entities.get("time_text"):
            appointment["preferred_time"] = entities["time_text"]
        if entities.get("visit_type"):
            appointment["visit_type"] = entities["visit_type"]
        if expected_slot == "urgent_symptoms" and _NO_URGENT_RE.search(value):
            appointment["urgent_symptoms"] = "none_reported"

        next_slot = _next_appointment_slot(appointment)
        appointment["pending_slot"] = next_slot
        # Consume the digression marker (if any) so the resumption is
        # acknowledged exactly once and the flag is cleared from state.
        resumed_from_kb = bool(appointment.pop("deferred_for_kb", False))
        patch = {"appointment": appointment}
        if next_slot:
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
