"""Fast regex-based intent router for the Nokvo One voice agent.

Sits before the semantic cache, embedding, and Qdrant retrieval on the hot
path. Pure-functional — no I/O, no LLM, no Redis. Intended to terminate the
turn early for greetings / thanks / goodbye, and to route sensitive policy
intents (cancellation, refund, payment, …) to the deterministic
PolicyDecisionEngine instead of the generic RAG path.

Keep it conservative: when in doubt, return ``unknown_general`` and let the
existing RAG fallback handle it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ── Intents ──────────────────────────────────────────────────────────────────
INTENT_GREETING = "greeting"
INTENT_THANKS = "thanks"
INTENT_GOODBYE = "goodbye"
INTENT_SMALLTALK = "smalltalk"
INTENT_AUDIO_CHECK = "audio_check"
INTENT_CANCELLATION_REQUEST = "cancellation_request"
INTENT_REFUND_ELIGIBILITY = "refund_eligibility"
INTENT_PAYMENT_ISSUE = "payment_issue"
INTENT_ORDER_STATUS = "order_status"
INTENT_DELIVERY_DELAY = "delivery_delay"
INTENT_FOOD_QUALITY = "food_quality"
INTENT_MISSING_ITEM = "missing_item"
INTENT_ADDRESS_CHANGE = "address_change"
INTENT_ACCOUNT_ISSUE = "account_issue"
INTENT_UNKNOWN_GENERAL = "unknown_general"


# ── Topic taxonomy (matches AgentKnowledgeService._topic_for_text) ───────────
TOPIC_CANCELLATION = "cancellation"
TOPIC_REFUND = "refund"
TOPIC_PAYMENT = "payment"
TOPIC_DELIVERY = "delivery"
TOPIC_FOOD_QUALITY = "food_quality"
TOPIC_ACCOUNT = "account"
TOPIC_GENERAL = "general"


_SENSITIVE_INTENTS = {
    INTENT_CANCELLATION_REQUEST,
    INTENT_REFUND_ELIGIBILITY,
    INTENT_PAYMENT_ISSUE,
    INTENT_FOOD_QUALITY,
    INTENT_ACCOUNT_ISSUE,
}

# Intents whose answer depends on a specific customer order / account
_LIVE_STATUS_INTENTS = {
    INTENT_CANCELLATION_REQUEST,
    INTENT_REFUND_ELIGIBILITY,
    INTENT_ORDER_STATUS,
    INTENT_DELIVERY_DELAY,
    INTENT_PAYMENT_ISSUE,
    INTENT_MISSING_ITEM,
    INTENT_ADDRESS_CHANGE,
}


@dataclass
class IntentResult:
    intent: str
    topic: str
    confidence: float
    sensitive: bool
    requires_live_status: bool
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "topic": self.topic,
            "confidence": self.confidence,
            "sensitive": self.sensitive,
            "requires_live_status": self.requires_live_status,
            "reason": self.reason,
            "metadata": dict(self.metadata),
        }


# ── Patterns (compiled once) ─────────────────────────────────────────────────
_GREETING_RE = re.compile(
    r"^\s*(hi|hello|hey|namaste|namaskar|good\s+(?:morning|afternoon|evening))[\s!.?,]*$",
    re.IGNORECASE,
)
_THANKS_RE = re.compile(
    r"^\s*(thanks?(?:\s+you)?|thank\s+you|ty|appreciated|much\s+appreciated)[\s!.?,]*$",
    re.IGNORECASE,
)
_GOODBYE_RE = re.compile(
    r"^\s*(bye|goodbye|see\s+you|see\s+ya|talk\s+later|that('?| i)s\s+all)[\s!.?,]*$",
    re.IGNORECASE,
)
_SMALLTALK_RE = re.compile(
    r"^\s*(ok(?:ay)?|alright|got\s+it|cool|sure|fine|yes|no|hmm+|uh+huh?)[\s!.?,]*$",
    re.IGNORECASE,
)
# Audio-check / presence-probe phrases — the caller is testing the line, not
# asking a knowledge question. These should NEVER route to RAG.
_AUDIO_CHECK_RE = re.compile(
    r"^\s*("
    r"can\s+you\s+hear\s+me|"
    r"do\s+you\s+hear\s+me|"
    r"are\s+you\s+(?:there|listening)|"
    r"you\s+there|"
    r"hello\s+(?:are\s+you\s+there|can\s+you\s+hear\s+me)|"
    r"is\s+(?:this|anyone|anybody)\s+(?:working|there|on)|"
    r"is\s+this\s+thing\s+(?:on|working)|"
    r"test(?:ing)?|"
    r"(?:check|mic|microphone)\s+check|"
    r"sound\s+check"
    r")[\s!.?,]*$",
    re.IGNORECASE,
)

# Verb-led cancel phrases (avoid false-positive on "I want to know cancellation policy")
_CANCEL_VERB_RE = re.compile(
    r"\b(cancel|cancelling|cancelled|cancelation|cancellation)\b",
    re.IGNORECASE,
)
_REFUND_RE = re.compile(r"\b(refund(?:ed|able)?|money\s*back|chargeback)\b", re.IGNORECASE)
_PAYMENT_RE = re.compile(
    r"\b(payment|charged|charge|upi|card|wallet|deducted|transaction|paid|paying|debit|credit\s+card|gpay|phonepe|paytm)\b",
    re.IGNORECASE,
)
_PAYMENT_ISSUE_HINT_RE = re.compile(
    r"\b(failed|fail|error|stuck|pending|double|twice|wrong\s+amount|extra|not\s+received|did\s*n[o']?t\s+go|did\s*n[o']?t\s+work|issue|problem|trouble)\b",
    re.IGNORECASE,
)
_ORDER_STATUS_RE = re.compile(
    r"\b(where\s+is\s+my\s+order|track\s+my\s+order|order\s+status|status\s+of\s+(?:my\s+)?order|when\s+will\s+(?:my\s+)?order\s+(?:arrive|come))\b",
    re.IGNORECASE,
)
_DELIVERY_DELAY_RE = re.compile(
    r"\b(late|delayed|delay|taking\s+(?:too\s+)?long|not\s+(?:yet\s+)?delivered|still\s+not\s+(?:here|come|arrived)|haven['’]?t\s+received)\b",
    re.IGNORECASE,
)
_FOOD_QUALITY_RE = re.compile(
    r"\b(cold|stale|spoiled|bad\s+(?:taste|food)|burn(?:ed|t)|raw|undercooked|overcooked|smell(?:s|ed)?\s+bad|hair\s+in|insect|moldy|expired)\b",
    re.IGNORECASE,
)
_MISSING_ITEM_RE = re.compile(
    r"\b(missing|did\s*n[o']?t\s+(?:get|receive)|wasn['’]?t\s+(?:there|in\s+the\s+order)|forgot(?:ten)?|left\s+out|item\s+missing|wrong\s+item)\b",
    re.IGNORECASE,
)
_ADDRESS_CHANGE_RE = re.compile(
    r"\b(change\s+(?:my\s+)?address|update\s+(?:my\s+)?address|wrong\s+address|new\s+address|deliver\s+(?:to|at)\s+a?\s*different)\b",
    re.IGNORECASE,
)
_ACCOUNT_RE = re.compile(
    r"\b(account|login|log\s*in|sign\s*in|password|otp|reset|locked|blocked|kyc|verification|verify\s+(?:my\s+)?account|profile)\b",
    re.IGNORECASE,
)

# Refund-eligibility (vs cancellation-request) hint: asks about "when/can/window"
_ELIGIBILITY_HINT_RE = re.compile(
    r"\b(can\s+i|am\s+i\s+(?:eligible|able)|will\s+i\s+(?:get|be)|window|policy|how\s+(?:long|much\s+time)|within|after|before|eligibility|eligible|am\s+i\s+entitled)\b",
    re.IGNORECASE,
)

# Multilingual policy-keyword detector. Indians regularly use English loanwords
# ("cancel", "refund") in Telugu / Hindi / Tamil utterances, with the loanwords
# written either in Latin script (after STT) or in the local script. This
# regex tries to catch both. Used by the pipeline to FORCE policy_card chunk
# injection on any utterance that smells like a cancellation/refund question,
# regardless of what the LLM classifier returned (Sarvam translate can time
# out, the classifier defaults to "unclear", and we miss real policy intents).
_POLICY_KEYWORD_LATIN_RE = re.compile(
    r"\b(?:cancel|cancelled|cancelling|cancellation|refund|refunded|refunding|"
    r"return|returning|replace|replacement|money\s*back|chargeback)\b",
    re.IGNORECASE,
)
# Common transliterations into Devanagari (Hindi/Marathi), Telugu, Tamil,
# Kannada, Malayalam, Bengali, Gujarati. STT may produce native-script
# spellings of these English loanwords.
_CANCEL_NATIVE_TERMS = [
    # Telugu
    "క్యాన్సిల్", "క్యాన్సల్",
    # Hindi / Marathi (Devanagari)
    "कैंसल", "कैन्सल",
    # Tamil
    "கேன்சல்",
    # Kannada
    "ಕ್ಯಾನ್ಸಲ್",
    # Malayalam
    "ക്യാൻസൽ",
    # Bengali
    "ক্যানসেল",
    # Gujarati
    "કેન્સલ",
    # Punjabi
    "ਕੈਂਸਲ",
]
_REFUND_NATIVE_TERMS = [
    # Telugu. Sarvam/STT sometimes hears "refund" as డిఫెండ్/డిఫండ్.
    "రీఫండ్", "రిఫండ్", "డిఫెండ్", "డిఫండ్", "డెఫండ్", "రిటర్న్", "డబ్బులు తిరిగి",
    # Hindi / Marathi (Devanagari)
    "रिफंड", "रिफ़ंड", "रिटर्न", "वापसी", "पैसे वापस",
    # Tamil
    "ரீஃபண்ட்", "ரிட்டர்ன்", "பணம் திரும்ப",
    # Kannada
    "ರೀಫಂಡ್", "ರಿಟರ್ನ್", "ಹಣ ಹಿಂದಿರುಗ",
    # Malayalam
    "റീഫണ്ട്", "റിട്ടേൺ", "പണം തിരികെ",
    # Bengali
    "রিফান্ড", "রিটার্ন", "টাকা ফেরত",
    # Gujarati
    "રિફંડ", "રિટર્ન", "પૈસા પાછા",
    # Punjabi
    "ਰਿਫੰਡ", "ਰਿਟਰਨ", "ਪੈਸੇ ਵਾਪਸ",
]
_CANCEL_NATIVE_RE = re.compile("|".join(map(re.escape, _CANCEL_NATIVE_TERMS)))
_REFUND_NATIVE_RE = re.compile("|".join(map(re.escape, _REFUND_NATIVE_TERMS)))
_POLICY_KEYWORD_NATIVE_RE = re.compile(
    "|".join(map(re.escape, _CANCEL_NATIVE_TERMS + _REFUND_NATIVE_TERMS))
)


def detect_policy_keyword(text: str) -> str | None:
    """Return the matched keyword (or `None`). Useful as a force-trigger for
    policy_card injection when the classifier missed the intent."""
    if not text:
        return None
    m = _POLICY_KEYWORD_LATIN_RE.search(text)
    if m:
        return m.group(0)
    m = _POLICY_KEYWORD_NATIVE_RE.search(text)
    if m:
        return m.group(0)
    return None


# Pure-politeness combo detection. We want to recognise a multi-clause
# utterance like "Thank you for the help. I'll call you later. Thank you."
# as a goodbye, but NOT "Thanks, but I have one more question" — that pivots
# to substantive content. Heuristic: if the utterance contains a politeness
# token AND no substantive markers, it's pure politeness.
# Thanks tokens — Latin English, common transliterations, AND native scripts.
# STT often produces the native-script form of the English loanword
# ("థాంక్యూ" in Telugu, "थैंक्यू" in Hindi). The same English word can be
# transliterated multiple ways per script, so we use loose patterns that
# match the recognizable consonant skeleton (th + nk) plus exact native
# words for gratitude.
_THANK_TOKEN_RE = re.compile(
    "|".join([
        # English + Romanized transliterations
        r"\bthank(?:s|\s+you)?\b",
        r"\bappreciate(?:d)?\b",
        r"\bcheers\b",
        r"\bgracias\b", r"\bmuchas\b",
        r"\bdhanyavad(?:a|am|amulu|aalu)?\b",
        r"\bdhanyawad\b",
        r"\bshukriya\b", r"\bshukran\b",
        r"\bnanri\b",
        # ── Telugu: matches థాంక్యూ, థ్యాంక్యూ, థాంక్స్, థ్యాంక్ యూ, etc. ──
        # th-vowels-nk skeleton: థ then 0-3 Telugu chars then ంక
        r"థ[ఀ-౿]{0,3}ంక",
        # Native Telugu words
        r"ధన్యవాద(?:ాలు|ం)?",
        # ── Devanagari (Hindi / Marathi): थैंक्यू, थैंक्स, थंक्स, थैंक यू ──
        r"थ[ऀ-ॿ]{0,3}ंक",
        r"धन्यवाद",
        r"शुक्रिया",
        r"आभार",
        # ── Tamil: தாங்க்ஸ், தாங்க், தங்க்ஸ் ──
        r"த[஀-௿]{0,3}[ங்க]+",
        r"நன்றி",
        # ── Kannada ──
        r"ಥ[ಀ-೿]{0,3}ಂಕ",
        r"ಧನ್ಯವಾದ(?:ಗಳು|ಂ)?",
        # ── Malayalam ──
        r"ത[ഀ-ൿ]{0,3}ങ്ക",
        r"നന്ദി",
        # ── Bengali ──
        r"থ[ঀ-৿]{0,3}ংক",
        r"ধন্যবাদ",
        # ── Gujarati ──
        r"થ[઀-૿]{0,3}ંક",
        r"આભાર",
        # ── Punjabi (Gurmukhi) ──
        r"ਥ[਀-੿]{0,3}ੰਕ",
        r"ਧੰਨਵਾਦ",
    ]),
    re.IGNORECASE,
)
_GOODBYE_TOKEN_RE = re.compile(
    "|".join([
        r"\b(?:bye|goodbye|see\s+(?:you|ya)|see\s+ya|talk\s+(?:to\s+you\s+)?later|catch\s+(?:you|ya)\s+later|call\s+(?:you\s+)?later|chat\s+later|good\s*night|alvida|phir\s+milenge|tata)\b",
        # Native-script farewells
        "बाय", "अलविदा", "फिर मिलेंगे",     # Hindi
        "బై", "మళ్ళీ", "మళ్లీ కలుద్దాం",     # Telugu
        "வணக்கம் (போய் வா)", "பின்னர்",     # Tamil
        "বিদায়",                              # Bengali
    ]),
    re.IGNORECASE,
)
_GREETING_TOKEN_RE = re.compile(
    "|".join([
        r"\b(?:hello|hi|hey|namaste|namaskar|good\s+(?:morning|afternoon|evening))\b",
        # Native scripts
        "नमस्ते", "नमस्कार",     # Hindi
        "నమస్తే", "నమస్కారం",   # Telugu
        "வணக்கம்",                # Tamil
        "নমস্কার",                 # Bengali
    ]),
    re.IGNORECASE,
)
# Words that indicate the user is asking for something / pivoting to a real
# request. If ANY of these appears, the utterance is NOT pure politeness.
# Deliberately EXCLUDES "help" (appears in "thanks for the help") and "wait"
# / "deliver" / "later" (too ambiguous against gratitude).
_SUBSTANTIVE_RE = re.compile(
    r"\b(?:but|however|though|why|how\s+(?:do|does|can|long|much)|"
    r"what|when|where|which|who|"
    r"cancel|refund|order|payment|account|issue|problem|wrong|"
    r"complain|complaint|hour|status|food|item|missing|broken|stuck|"
    r"charge|charged|error|fault|fix|update|change|need|require|want\s+to|"
    r"could\s+you|can\s+you|would\s+you|tell\s+me|let\s+me\s+know)\b",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _topic_for_intent(intent: str) -> str:
    return {
        INTENT_CANCELLATION_REQUEST: TOPIC_CANCELLATION,
        INTENT_REFUND_ELIGIBILITY: TOPIC_REFUND,
        INTENT_PAYMENT_ISSUE: TOPIC_PAYMENT,
        INTENT_ORDER_STATUS: TOPIC_DELIVERY,
        INTENT_DELIVERY_DELAY: TOPIC_DELIVERY,
        INTENT_FOOD_QUALITY: TOPIC_FOOD_QUALITY,
        INTENT_MISSING_ITEM: TOPIC_FOOD_QUALITY,
        INTENT_ADDRESS_CHANGE: TOPIC_DELIVERY,
        INTENT_ACCOUNT_ISSUE: TOPIC_ACCOUNT,
    }.get(intent, TOPIC_GENERAL)


class FastIntentRouter:
    """Cheap, deterministic intent classifier."""

    @staticmethod
    def classify(text: str, language: str | None = None) -> IntentResult:
        normalized = _normalize(text)
        if not normalized:
            return IntentResult(
                intent=INTENT_UNKNOWN_GENERAL,
                topic=TOPIC_GENERAL,
                confidence=1.0,
                sensitive=False,
                requires_live_status=False,
                reason="empty input",
            )

        # ── Fast template paths ──
        if _GREETING_RE.match(normalized):
            return IntentResult(INTENT_GREETING, TOPIC_GENERAL, 0.99, False, False, "matched greeting regex")
        if _THANKS_RE.match(normalized):
            return IntentResult(INTENT_THANKS, TOPIC_GENERAL, 0.99, False, False, "matched thanks regex")
        if _GOODBYE_RE.match(normalized):
            return IntentResult(INTENT_GOODBYE, TOPIC_GENERAL, 0.98, False, False, "matched goodbye regex")
        if _SMALLTALK_RE.match(normalized) and len(normalized.split()) <= 2:
            return IntentResult(INTENT_SMALLTALK, TOPIC_GENERAL, 0.95, False, False, "matched smalltalk regex")
        if _AUDIO_CHECK_RE.match(normalized):
            return IntentResult(INTENT_AUDIO_CHECK, TOPIC_GENERAL, 0.98, False, False, "matched audio-check regex")

        # ── Pure politeness combo detector ──
        # Anchored regexes (above) catch bare "thanks" / "hi" / "bye". Real
        # speech often combines them with farewell or filler ("okay, thank
        # you", "Thank you for the help. I'll call you later. Thank you."),
        # which can easily exceed any word-count threshold. Instead of
        # length-gating, check whether the utterance is PURE politeness:
        #   - contains a politeness token (thank / bye / hi)
        #   - contains NO substantive content (no question, no business
        #     keyword, no pivot like "but"/"however")
        # If so, route to the corresponding template. Priority: goodbye
        # beats thanks beats greeting, so "thanks, bye" goes to goodbye.
        if _SUBSTANTIVE_RE.search(normalized) or "?" in text:
            substantive = True
        else:
            substantive = False

        if not substantive:
            has_thanks = bool(_THANK_TOKEN_RE.search(normalized))
            has_goodbye = bool(_GOODBYE_TOKEN_RE.search(normalized))
            has_greeting = bool(_GREETING_TOKEN_RE.search(normalized))
            if has_thanks and has_goodbye:
                # "thank you, see you later" / "thanks, bye"
                return IntentResult(INTENT_GOODBYE, TOPIC_GENERAL, 0.92, False, False, "pure thanks+goodbye combo")
            if has_goodbye:
                return IntentResult(INTENT_GOODBYE, TOPIC_GENERAL, 0.9, False, False, "pure goodbye combo")
            if has_thanks:
                return IntentResult(INTENT_THANKS, TOPIC_GENERAL, 0.92, False, False, "pure thanks combo")
            if has_greeting:
                return IntentResult(INTENT_GREETING, TOPIC_GENERAL, 0.85, False, False, "pure greeting combo")

        # ── Sensitive intents (specificity-ordered) ──
        cancel_hit = bool(_CANCEL_VERB_RE.search(normalized) or _CANCEL_NATIVE_RE.search(normalized))
        refund_hit = bool(_REFUND_RE.search(normalized) or _REFUND_NATIVE_RE.search(normalized))
        eligibility_hit = bool(_ELIGIBILITY_HINT_RE.search(normalized))

        if refund_hit:
            # If user asks an eligibility/policy question we route to refund_eligibility,
            # which the decision engine also serves from the cancellation matrix when
            # appropriate (refund windows usually mirror cancellation windows).
            intent = INTENT_REFUND_ELIGIBILITY if eligibility_hit else INTENT_REFUND_ELIGIBILITY
            return FastIntentRouter._build(intent, confidence=0.9, reason="refund term present")

        if cancel_hit:
            # "Can I cancel after 5 minutes" / "I want to cancel".
            return FastIntentRouter._build(
                INTENT_CANCELLATION_REQUEST,
                confidence=0.92,
                reason="cancel verb present",
            )

        if _PAYMENT_RE.search(normalized) and _PAYMENT_ISSUE_HINT_RE.search(normalized):
            return FastIntentRouter._build(INTENT_PAYMENT_ISSUE, confidence=0.88, reason="payment + issue hint")

        if _MISSING_ITEM_RE.search(normalized):
            return FastIntentRouter._build(INTENT_MISSING_ITEM, confidence=0.85, reason="missing item phrase")
        if _FOOD_QUALITY_RE.search(normalized):
            return FastIntentRouter._build(INTENT_FOOD_QUALITY, confidence=0.85, reason="food quality phrase")

        if _ORDER_STATUS_RE.search(normalized):
            return FastIntentRouter._build(INTENT_ORDER_STATUS, confidence=0.88, reason="order-status phrase")
        if _DELIVERY_DELAY_RE.search(normalized):
            return FastIntentRouter._build(INTENT_DELIVERY_DELAY, confidence=0.82, reason="delivery delay phrase")

        if _ADDRESS_CHANGE_RE.search(normalized):
            return FastIntentRouter._build(INTENT_ADDRESS_CHANGE, confidence=0.85, reason="address change phrase")
        if _ACCOUNT_RE.search(normalized):
            return FastIntentRouter._build(INTENT_ACCOUNT_ISSUE, confidence=0.78, reason="account/login phrase")

        return IntentResult(
            intent=INTENT_UNKNOWN_GENERAL,
            topic=TOPIC_GENERAL,
            confidence=0.4,
            sensitive=False,
            requires_live_status=False,
            reason="no sensitive intent matched",
        )

    @staticmethod
    def _build(intent: str, *, confidence: float, reason: str) -> IntentResult:
        return IntentResult(
            intent=intent,
            topic=_topic_for_intent(intent),
            confidence=confidence,
            sensitive=intent in _SENSITIVE_INTENTS,
            requires_live_status=intent in _LIVE_STATUS_INTENTS,
            reason=reason,
        )

    @staticmethod
    def is_sensitive(intent: str) -> bool:
        return intent in _SENSITIVE_INTENTS

    @staticmethod
    def requires_live_status(intent: str) -> bool:
        return intent in _LIVE_STATUS_INTENTS
