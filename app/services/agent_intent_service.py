"""Agent Intent Classification Service — the 'gate' between Conversation Brain and Knowledge Brain.

Production rule:
    1. Conversation Brain — handles greetings, small talk, empathy, clarification,
       identity, and normal human flow WITHOUT RAG. Must not invent ZapEats policy facts.
    2. Knowledge Brain — uses uploaded documents through RAG ONLY for ZapEats-specific
       factual/policy/process answers. Strictly grounded to retrieved documents.

Never retrieve just because the user sent a message.
Retrieve only when intent gating says the user needs approved knowledge.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib import parse as urllib_parse

import httpx

from app.core.config import settings


# ---------------------------------------------------------------------------
# Language detection — zero-cost, script-based
# ---------------------------------------------------------------------------

_SCRIPT_RANGES: list[tuple[int, int, str]] = [
    (0x0900, 0x097F, "hi"),   # Devanagari → Hindi/Marathi
    (0x0980, 0x09FF, "bn"),   # Bengali
    (0x0A00, 0x0A7F, "pa"),   # Gurmukhi → Punjabi
    (0x0A80, 0x0AFF, "gu"),   # Gujarati
    (0x0B80, 0x0BFF, "ta"),   # Tamil
    (0x0C00, 0x0C7F, "te"),   # Telugu
    (0x0C80, 0x0CFF, "kn"),   # Kannada
    (0x0D00, 0x0D7F, "ml"),   # Malayalam
    (0x0600, 0x06FF, "ur"),   # Arabic script → Urdu
]

_HINDI_MARKERS = re.compile(
    r"\b(kya|kaise|kahan|kyun|mera|meri|mere|hai|hain|nahi|haan|ji|"
    r"aapka|aapki|madad|chahiye|batao|bolo|karo|kijiye|dedo|"
    r"swagat|dhanyavad|shukriya|namaste|namaskar)\b",
    re.IGNORECASE,
)

_TRANSCRIPT_CONTROL_TOKENS = re.compile(
    r"(?i)(<\s*/?\s*(end|eos|silence|speech_final|unk|unknown)\s*>|\[\s*(end|eos|silence|speech_final)\s*\])"
)
_TRAILING_TRANSCRIPT_CONTROL_WORDS = re.compile(
    r"(?i)(?:[\s,.;:!?]+(?:end|eos|silence|speech_final|speech final))+$"
)


def detect_language(text: str) -> str:
    """Detect language from text using Unicode script analysis.

    Returns ISO 639-1 code: en, hi, bn, gu, kn, ml, mr, pa, ta, te, ur.
    Falls back to 'en' if no non-Latin script is detected.
    Zero cost — no LLM call, pure character inspection.
    """
    if not text or not text.strip():
        return "en"

    script_counts: dict[str, int] = {}
    latin_count = 0
    for char in text:
        code_point = ord(char)
        if 0x0041 <= code_point <= 0x007A:
            latin_count += 1
            continue
        for start, end, lang in _SCRIPT_RANGES:
            if start <= code_point <= end:
                script_counts[lang] = script_counts.get(lang, 0) + 1
                break

    # If we found non-Latin script characters, use the dominant one
    if script_counts:
        detected = max(script_counts, key=script_counts.get)
        # Devanagari is shared by Hindi and Marathi — default to Hindi
        return detected

    # All Latin — check for romanized Hindi/Hinglish
    if _HINDI_MARKERS.search(text):
        return "hi"

    return "en"


# ---------------------------------------------------------------------------
# Intent types
# ---------------------------------------------------------------------------

INTENT_GREETING = "GREETING_OR_SMALLTALK"
INTENT_SUPPORT = "SUPPORT_POLICY_QUESTION"
INTENT_ORDER_ACTION = "ORDER_SPECIFIC_ACTION"
INTENT_AMBIGUOUS = "AMBIGUOUS"

VALID_INTENTS = {INTENT_GREETING, INTENT_SUPPORT, INTENT_ORDER_ACTION, INTENT_AMBIGUOUS}
_INTENT_HTTP_CLIENT: httpx.AsyncClient | None = None


def _intent_http_client() -> httpx.AsyncClient:
    global _INTENT_HTTP_CLIENT
    if _INTENT_HTTP_CLIENT is None or _INTENT_HTTP_CLIENT.is_closed:
        timeout = max(0.5, settings.AGENT_INTENT_CLASSIFIER_TIMEOUT_MS / 1000)
        _INTENT_HTTP_CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
    return _INTENT_HTTP_CLIENT


# ---------------------------------------------------------------------------
# Fast regex classifier — catches obvious cases without any LLM call
# ---------------------------------------------------------------------------

_GREETING_PATTERNS = re.compile(
    r"^("
    r"h(ello|i|ey(\s+there)?|ola|owdy)|"
    r"good\s*(morning|afternoon|evening|night|day)|"
    r"namaste|namaskar|sat sri akal|"
    r"thanks?(\s+you)?|thank\s*u|ty|thx|"
    r"okay|ok|o\.?k\.?|k|kk|"
    r"yes|yeah|yep|yup|ya|haan|ji|"
    r"no|nope|nah|nahi|"
    r"bye|goodbye|good\s*bye|see\s*ya|later|take\s*care|"
    r"hmm+|huh|what\??|cool|nice|great|awesome|sure|"
    r"who\s*are\s*you|what\s*are\s*you|your\s*name|"
    r"how\s*are\s*you|what('s|\s*is)\s*up|sup|"
    r"welcome|no\s*problem|no\s*worries|"
    r"got\s*it|understood|alright|all\s*right|fine|right"
    r")[\s!?.,:;]*$",
    re.IGNORECASE,
)

_SMALLTALK_SEQUENCE_PATTERN = re.compile(
    r"^("
    r"(all\s*right|alright|ok(?:ay)?|got\s*it|understood|sure|yes|yeah|that\s*works|perfect|great|nice)"
    r"([\s,.;:!-]+(thanks?|thank\s*you)(\s+(so\s+much|very\s+much|a\s+lot))?)?|"
    r"(thanks?|thank\s*you)(\s+(so\s+much|very\s+much|a\s+lot))?"
    r")[\s!?.,:;]*$",
    re.IGNORECASE,
)

_FILLER_MAX_WORDS = 3  # Messages with ≤3 words and no policy keywords → likely filler

_POLICY_KEYWORDS = re.compile(
    r"("
    # English
    r"cancel|refund|deliver|order|payment|pay|charged|billing|"
    r"missing|cold|wrong|late|delay|stuck|track|address|change|"
    r"coupon|discount|coins?|wallet|subscription|plan|upgrade|"
    r"compens|escalat|complain|poison|allerg|hygien|"
    r"policy|process|procedure|guideline|rule|sla|sop|"
    r"agent|partner|rider|restaurant|vendor|merchant|"
    r"account|login|password|otp|verify|"
    r"share|data|privacy|gdpr|"
    r"phone\s*number|contact|email|"
    # Hindi / Devanagari
    r"रिफंड|कैंसल|ऑर्डर|डिलीवरी|पेमेंट|भुगतान|बिलिंग|"
    r"शिकायत|देरी|लेट|गलत|ठंडा|पता|बदलो|कूपन|छूट|"
    r"वॉलेट|सब्सक्रिप्शन|अकाउंट|पासवर्ड|नंबर|"
    # Telugu
    r"రీఫండ్|క్యాన్సల్|ఆర్డర్|డెలివరీ|పేమెంట్|బిల్లింగ్|"
    r"ఫిర్యాదు|ఆలస్యం|తప్పు|చల్లగా|చిరునామా|కూపన్|"
    r"వాలెట్|ఖాతా|పాస్‌వర్డ్|నంబర్|"
    # Tamil
    r"ரீஃபண்ட்|கேன்சல்|ஆர்டர்|டெலிவரி|பேமெண்ட்|பில்லிங்|"
    r"புகார்|தாமதம்|தவறு|குளிர்|முகவரி|கூப்பன்|"
    r"வாலட்|கணக்கு|"
    # Bengali
    r"রিফান্ড|ক্যান্সেল|অর্ডার|ডেলিভারি|পেমেন্ট|বিলিং|"
    r"অভিযোগ|দেরি|ভুল|ঠান্ডা|ঠিকানা|কুপন|"
    r"ওয়ালেট|অ্যাকাউন্ট|"
    # Kannada
    r"ರೀಫಂಡ್|ಕ್ಯಾನ್ಸಲ್|ಆರ್ಡರ್|ಡೆಲಿವರಿ|ಪೇಮೆಂಟ್|"
    r"ದೂರು|ವಿಳಂಬ|ತಪ್ಪು|ವಿಳಾಸ|ಖಾತೆ|"
    # Malayalam
    r"റീഫണ്ട്|ക്യാൻസൽ|ഓർഡർ|ഡെലിവറി|പേമെന്റ്|"
    r"പരാതി|കാലതാമസം|തെറ്റ്|വിലാസം|അക്കൗണ്ട്|"
    # Gujarati
    r"રીફંડ|કેન્સલ|ઓર્ડર|ડિલિવરી|પેમેન્ટ|"
    r"ફરિયાદ|મોડું|ખોટું|સરનામું|એકાઉન્ટ|"
    # Punjabi
    r"ਰਿਫੰਡ|ਕੈਂਸਲ|ਆਰਡਰ|ਡਿਲਿਵਰੀ|ਪੇਮੈਂਟ|"
    r"ਸ਼ਿਕਾਇਤ|ਦੇਰੀ|ਗਲਤ|ਪਤਾ|ਅਕਾਊਂਟ|"
    # Urdu
    r"ریفنڈ|کینسل|آرڈر|ڈیلیوری|پیمنٹ|"
    r"شکایت|تاخیر|غلط|پتا|اکاؤنٹ|"
    # Romanized Hindi / Hinglish
    r"refund\s*(chahiye|do|karo|milega|kab)|"
    r"cancel\s*(karo|kijiye|kar\s*do)|"
    r"order\s*(kahan|nahi\s*aaya|late|wrong|galat)|"
    r"paisa\s*(wapas|return)|paise|rupay"
    r")",
    re.IGNORECASE,
)

_CONCRETE_SUPPORT_PHRASES = re.compile(
    r"("
    r"\b(food|item|items|meal|dish|package)\b.*\b("
    r"stale|spoiled|rotten|bad|unsafe|uncooked|raw|burnt|cold|hair|smell|smelled|leak|leaked|spill|spilled|missing|wrong"
    r")\b|"
    r"\b(stale|spoiled|rotten|bad|unsafe|uncooked|raw|burnt|cold|hair|smell|smelled|leak|leaked|spill|spilled|missing|wrong)\b.*\b("
    r"food|item|items|meal|dish|package"
    r")\b|"
    r"\b(i|we)\s+(did\s+not|didn't|dont|don't|never)\s+(receive|get|got)\b|"
    r"\b(not\s+received|didn'?t\s+receive|never\s+arrived|not\s+delivered|wasn'?t\s+delivered)\b|"
    r"\b(money\s+back|paisa\s+wapas|paise\s+wapas|charged\s+twice|double\s+charged|payment\s+failed)\b|"
    r"\b(speak|talk)\s+to\s+(a\s+)?(supervisor|manager|human|agent)\b|"
    r"\b(rider|delivery\s+partner|driver|agent|support)\b.*\b(rude|abusive|misbehav|threat|harass|asked\s+extra|extra\s+(cash|money))\b|"
    r"\b(complaint|complain|escalate|escalation)\b"
    r")",
    re.IGNORECASE,
)

_VAGUE_SINGLE_TERMS = {
    "refund", "late", "cancel", "delivery", "payment", "problem", "issue",
    "help", "order", "app", "account", "wallet", "coins",
}

_ORDER_ACTION_PATTERN = re.compile(
    r"\b("
    r"cancel|refund|track|change|update|check|escalate|raise|modify|"
    r"कैंसल|रिफंड|ट्रैक|बदलो|देखो|"
    r"క్యాన్సల్|రీఫండ్|ట్రాక్|మార్చు|"
    r"கேன்சல்|ரீஃபண்ட்|டிராக்|மாற்று"
    r")\b.*\b(my|mera|meri|मेरे|मेरा|నా|என்|order|payment|address|wallet|account|coins?)\b",
    re.IGNORECASE,
)


def normalize_message(text: str) -> str:
    """Strip, lowercase, collapse whitespace."""
    cleaned = _TRANSCRIPT_CONTROL_TOKENS.sub(" ", text or "")
    cleaned = _TRAILING_TRANSCRIPT_CONTROL_WORDS.sub("", cleaned)
    return re.sub(r"\s+", " ", cleaned.strip())


def classify_intent_fast(message: str) -> dict[str, Any] | None:
    """Tier-1: Regex-based classification. Returns result or None if uncertain.

    This avoids any LLM call for obvious greetings, acknowledgements, and fillers.
    """
    normalized = normalize_message(message)
    if not normalized:
        return {
            "type": INTENT_AMBIGUOUS,
            "confidence": 1.0,
            "reason": "empty message",
            "shouldRetrieve": False,
        }

    # Check obvious greeting/smalltalk patterns
    if _GREETING_PATTERNS.match(normalized) or _SMALLTALK_SEQUENCE_PATTERN.match(normalized):
        return {
            "type": INTENT_GREETING,
            "confidence": 0.95,
            "reason": "matched greeting/smalltalk pattern",
            "shouldRetrieve": False,
        }

    word_count = len(normalized.split())
    if word_count <= 2 and normalized.lower() in _VAGUE_SINGLE_TERMS:
        return {
            "type": INTENT_AMBIGUOUS,
            "confidence": 0.86,
            "reason": "short unclear support phrase",
            "shouldRetrieve": False,
        }

    if _ORDER_ACTION_PATTERN.search(normalized):
        return {
            "type": INTENT_ORDER_ACTION,
            "confidence": 0.88,
            "reason": "personal order/account action request",
            "shouldRetrieve": False,
        }

    if _CONCRETE_SUPPORT_PHRASES.search(normalized):
        return {
            "type": INTENT_SUPPORT,
            "confidence": 0.90,
            "reason": "matched concrete support complaint pattern",
            "shouldRetrieve": True,
        }

    # If message has concrete policy/support language, use Knowledge Brain.
    # Vague one-word support phrases are handled above as clarification.
    if _POLICY_KEYWORDS.search(normalized):
        return {
            "type": INTENT_SUPPORT,
            "confidence": 0.85,
            "reason": "contains policy/support keywords",
            "shouldRetrieve": True,
        }

    # Very short messages without policy keywords → ambiguous filler
    if word_count <= _FILLER_MAX_WORDS:
        return {
            "type": INTENT_AMBIGUOUS,
            "confidence": 0.80,
            "reason": "short message without policy keywords",
            "shouldRetrieve": False,
        }

    # Uncertain — fall through to LLM classifier
    return None


# ---------------------------------------------------------------------------
# LLM-based intent classifier — Tier 2
# ---------------------------------------------------------------------------

_CLASSIFIER_PROMPT = """You are an intent classifier for a customer support voice/chat agent.

Classify the user message into exactly one category:

1. GREETING_OR_SMALLTALK
Greeting, thanks, goodbye, acknowledgement, casual phrase, or non-support small talk.

2. SUPPORT_POLICY_QUESTION
A question about company policy, refund, cancellation, delivery, payments, food quality, account, app, compliance, subscription, agent process, or product behavior.

3. ORDER_SPECIFIC_ACTION
The user wants an action on their personal order/account: cancel, refund, track, change address, check wallet, check status, escalate, update account.

4. AMBIGUOUS
The message is too short or unclear to know what the user needs.

Return strict JSON:
{
  "type": "...",
  "confidence": 0.0-1.0,
  "reason": "short reason",
  "shouldRetrieve": true/false
}

Rules:
- Greetings and small talk shouldRetrieve=false.
- Ambiguous messages shouldRetrieve=false.
- Support policy questions shouldRetrieve=true.
- Order-specific actions shouldRetrieve=true only if policy or process knowledge is needed; otherwise ask for order/customer details first.
- Never classify "hello", "hi", "thanks", "okay", "bye" as retrieval-needed.
- Return ONLY the JSON object, no other text."""


async def classify_intent_llm(
    message: str,
    *,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Tier-2: LLM-based intent classification for non-obvious messages."""
    resolved_key = api_key or settings.AZURE_OPENAI_GLOBAL_API_KEY
    if not resolved_key:
        # No LLM available — default to AMBIGUOUS (safe, will ask clarification)
        return {
            "type": INTENT_AMBIGUOUS,
            "confidence": 0.5,
            "reason": "no LLM configured for intent classification",
            "shouldRetrieve": False,
        }

    endpoint = (settings.AZURE_OPENAI_GLOBAL_ENDPOINT or "").rstrip("/")
    if not endpoint:
        return {
            "type": INTENT_AMBIGUOUS,
            "confidence": 0.5,
            "reason": "no LLM endpoint configured",
            "shouldRetrieve": False,
        }

    deployment = settings.AZURE_OPENAI_GLOBAL_DEPLOYMENT or "gpt-4-1-mini"
    messages = [
        {"role": "system", "content": _CLASSIFIER_PROMPT},
        {"role": "user", "content": message},
    ]

    if endpoint.endswith("/responses"):
        url = endpoint
        body = {
            "model": deployment,
            "input": messages,
            "temperature": 0.0,
            "max_output_tokens": 120,
        }
    else:
        api_version = urllib_parse.quote(
            (settings.AZURE_OPENAI_GLOBAL_API_VERSION or "2024-10-21").strip()
        )
        deployment_path = urllib_parse.quote(deployment.strip())
        if "/openai/deployments/" in endpoint:
            url = f"{endpoint}?api-version={api_version}" if "api-version=" not in endpoint else endpoint
        else:
            url = f"{endpoint}/openai/deployments/{deployment_path}/chat/completions?api-version={api_version}"
        body = {
            "messages": messages,
            "temperature": 0.0,
            "max_tokens": 120,
        }

    try:
        response = await _intent_http_client().post(
            url,
            json=body,
            headers={"Content-Type": "application/json", "api-key": resolved_key},
        )
        response.raise_for_status()
        payload = response.json()

        # Extract text from Azure OpenAI response.
        text = ""
        if isinstance(payload.get("output_text"), str):
            text = payload["output_text"].strip()
        else:
            choices = payload.get("choices") or []
            if choices:
                content = ((choices[0] or {}).get("message") or {}).get("content")
                if isinstance(content, str):
                    text = content.strip()
            if not text:
                for item in payload.get("output") or []:
                    for content_item in item.get("content") or []:
                        t = content_item.get("text")
                        if isinstance(t, str):
                            text = t.strip()
                            break
                    if text:
                        break

        cleaned = re.sub(r"```json\s*", "", text)
        cleaned = re.sub(r"```\s*$", "", cleaned).strip()
        result = json.loads(cleaned)

        intent_type = result.get("type", INTENT_AMBIGUOUS)
        if intent_type not in VALID_INTENTS:
            intent_type = INTENT_AMBIGUOUS

        return {
            "type": intent_type,
            "confidence": float(result.get("confidence", 0.7)),
            "reason": str(result.get("reason", "LLM classification")),
            "shouldRetrieve": bool(result.get("shouldRetrieve", False)),
        }
    except (httpx.HTTPError, json.JSONDecodeError, KeyError, ValueError):
        return {
            "type": INTENT_AMBIGUOUS,
            "confidence": 0.4,
            "reason": "intent classifier LLM call failed",
            "shouldRetrieve": False,
        }


async def classify_intent(
    message: str,
    *,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Classify user message intent using tiered approach:
    1. Fast regex (zero latency, zero cost)
    2. LLM classifier (only if regex is uncertain)
    """
    # Tier 1: Fast regex
    fast_result = classify_intent_fast(message)
    if fast_result is not None:
        fast_result["classifier"] = "fast_regex"
        return fast_result

    # Tier 2: LLM
    try:
        llm_result = await asyncio.wait_for(
            classify_intent_llm(message, api_key=api_key),
            timeout=max(0.5, settings.AGENT_INTENT_CLASSIFIER_TIMEOUT_MS / 1000),
        )
        llm_result["classifier"] = "llm"
        return llm_result
    except asyncio.TimeoutError:
        heuristic = classify_intent_fast(message)
        if heuristic is not None and heuristic["type"] != INTENT_AMBIGUOUS:
            heuristic["classifier"] = "timeout_heuristic"
            return heuristic
        return {
            "type": INTENT_AMBIGUOUS,
            "confidence": 0.4,
            "reason": "intent classifier timed out",
            "shouldRetrieve": False,
            "classifier": "timeout_fallback",
        }


# ---------------------------------------------------------------------------
# Conversation Brain replies — multilingual, natural, human, no policy facts
# ---------------------------------------------------------------------------

_GREETING_REPLIES_EN = {
    "hello": "Hello! Thank you for contacting ZapEats support. How can I help you today?",
    "hi": "Hi there! Welcome to ZapEats support. What can I help you with?",
    "hey": "Hey! Thanks for reaching out to ZapEats. How can I assist you?",
    "good morning": "Good morning! Welcome to ZapEats support. How can I help you today?",
    "good afternoon": "Good afternoon! Thanks for calling ZapEats. How can I assist?",
    "good evening": "Good evening! Welcome to ZapEats support. What can I help you with?",
    "thanks": "You're welcome! Is there anything else I can help you with?",
    "thank you": "You're welcome! Happy to help. Anything else?",
    "okay": "Got it! Let me know if there's anything else you need.",
    "ok": "Got it! Let me know if there's anything else you need.",
    "all right": "Got it. Anything else I can help you with?",
    "alright": "Got it. Anything else I can help you with?",
    "got it": "Got it. Anything else I can help you with?",
    "that works": "Perfect. Anything else I can help you with?",
    "sounds good": "Sounds good. Anything else I can help you with?",
    "bye": "Thank you for contacting ZapEats support. Have a great day!",
    "goodbye": "Goodbye! Thank you for choosing ZapEats. Take care!",
    "who are you": "I'm your ZapEats support agent. I'm here to help with orders, deliveries, refunds, or any other questions you have.",
    "how are you": "I'm doing well, thank you! How can I help you with your ZapEats experience today?",
}

_GREETING_REPLIES_HI = {
    "default": "नमस्ते! ZapEats सपोर्ट में आपका स्वागत है। मैं आपकी कैसे मदद कर सकता हूँ?",
    "namaste": "नमस्ते! ZapEats सपोर्ट में आपका स्वागत है। मैं आपकी कैसे मदद कर सकता हूँ?",
    "namaskar": "नमस्कार! ZapEats सपोर्ट में आपका स्वागत है। कैसे मदद कर सकता हूँ?",
    "thanks": "आपका स्वागत है! क्या कोई और मदद चाहिए?",
    "okay": "ठीक है! और कुछ चाहिए तो बताइए।",
    "bye": "ZapEats सपोर्ट से संपर्क करने के लिए धन्यवाद। आपका दिन शुभ हो!",
}

_GREETING_REPLIES_BN = {
    "default": "নমস্কার! ZapEats সাপোর্টে আপনাকে স্বাগতম। আমি কীভাবে সাহায্য করতে পারি?",
    "thanks": "আপনাকে ধন্যবাদ! আর কোনো সাহায্য লাগবে?",
    "bye": "ZapEats সাপোর্টে যোগাযোগ করার জন্য ধন্যবাদ। শুভ দিন!",
}

_GREETING_REPLIES_TA = {
    "default": "வணக்கம்! ZapEats ஆதரவுக்கு வரவேற்கிறோம். நான் எப்படி உதவ முடியும்?",
    "thanks": "நன்றி! வேறு ஏதாவது உதவி வேண்டுமா?",
    "bye": "ZapEats ஐ தொடர்பு கொண்டதற்கு நன்றி. நல்ல நாள்!",
}

_GREETING_REPLIES_TE = {
    "default": "నమస్కారం! ZapEats సపోర్ట్‌కి స్వాగతం. నేను మీకు ఎలా సహాయం చేయగలను?",
    "thanks": "ధన్యవాదాలు! మరేదైనా సహాయం కావాలా?",
    "bye": "ZapEats ని సంప్రదించినందుకు ధన్యవాదాలు. శుభ దినం!",
}

_GREETING_REPLIES_KN = {
    "default": "ನಮಸ್ಕಾರ! ZapEats ಬೆಂಬಲಕ್ಕೆ ಸ್ವಾಗತ. ನಾನು ಹೇಗೆ ಸಹಾಯ ಮಾಡಬಹುದು?",
    "thanks": "ಧನ್ಯವಾದಗಳು! ಬೇರೆ ಏನಾದರೂ ಸಹಾಯ ಬೇಕೇ?",
}

_GREETING_REPLIES_ML = {
    "default": "നമസ്കാരം! ZapEats സപ്പോർട്ടിലേക്ക് സ്വാഗതം. ഞാൻ എങ്ങനെ സഹായിക്കാം?",
    "thanks": "നന്ദി! മറ്റെന്തെങ്കിലും സഹായം വേണോ?",
}

_GREETING_REPLIES_GU = {
    "default": "નમસ્તે! ZapEats સપોર્ટમાં આપનું સ્વાગત છે. હું કેવી રીતે મદદ કરી શકું?",
    "thanks": "આપનું સ્વાગત છે! બીજું કંઈ મદદ જોઈએ?",
}

_GREETING_REPLIES_PA = {
    "default": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ! ZapEats ਸਪੋਰਟ ਵਿੱਚ ਤੁਹਾਡਾ ਸੁਆਗਤ ਹੈ। ਮੈਂ ਤੁਹਾਡੀ ਕਿਵੇਂ ਮਦਦ ਕਰ ਸਕਦਾ ਹਾਂ?",
    "thanks": "ਤੁਹਾਡਾ ਧੰਨਵਾਦ! ਹੋਰ ਕੋਈ ਮਦਦ ਚਾਹੀਦੀ ਹੈ?",
}

_GREETING_REPLIES_UR = {
    "default": "السلام علیکم! ZapEats سپورٹ میں خوش آمدید۔ میں آپ کی کیسے مدد کر سکتا ہوں?",
    "thanks": "شکریہ! کوئی اور مدد چاہیے?",
}

_GREETING_REPLIES_MR = {
    "default": "नमस्कार! ZapEats सपोर्टमध्ये आपले स्वागत आहे. मी कशी मदत करू शकतो?",
    "thanks": "तुमचे स्वागत आहे! आणखी काही मदत हवी का?",
}

_LANG_GREETING_MAP: dict[str, dict[str, str]] = {
    "en": _GREETING_REPLIES_EN,
    "hi": _GREETING_REPLIES_HI,
    "bn": _GREETING_REPLIES_BN,
    "ta": _GREETING_REPLIES_TA,
    "te": _GREETING_REPLIES_TE,
    "kn": _GREETING_REPLIES_KN,
    "ml": _GREETING_REPLIES_ML,
    "gu": _GREETING_REPLIES_GU,
    "pa": _GREETING_REPLIES_PA,
    "ur": _GREETING_REPLIES_UR,
    "mr": _GREETING_REPLIES_MR,
}

_CLARIFICATION_REPLIES: dict[str, str] = {
    "en": "Sure, I can help with that. Could you tell me a bit more? Is this about a refund, delivery issue, payment problem, account question, or something else?",
    "hi": "ज़रूर, मैं मदद कर सकता हूँ। क्या आप थोड़ा और बता सकते हैं? क्या यह रिफंड, डिलीवरी, पेमेंट, या अकाउंट से जुड़ा है?",
    "bn": "অবশ্যই, আমি সাহায্য করতে পারি। একটু বিস্তারিত বলবেন? এটা কি রিফান্ড, ডেলিভারি, পেমেন্ট, না অ্যাকাউন্ট সম্পর্কিত?",
    "ta": "நிச்சயமாக, நான் உதவ முடியும். இது ரீஃபண்ட், டெலிவரி, பேமெண்ட் அல்லது அக்கவுண்ட் பற்றியதா?",
    "te": "తప్పకుండా, నేను సహాయం చేయగలను. ఇది రీఫండ్, డెలివరీ, పేమెంట్ లేదా ఖాతా గురించా?",
}

_ORDER_ACTION_REPLIES: dict[str, str] = {
    "en": "I'd be happy to help with that. Could you share your order ID or the phone number associated with your account?",
    "hi": "मैं इसमें मदद कर सकता हूँ। क्या आप अपना ऑर्डर ID या अकाउंट से जुड़ा फोन नंबर बता सकते हैं?",
    "bn": "আমি সাহায্য করতে পারি। আপনার অর্ডার ID বা অ্যাকাউন্টের ফোন নম্বর দেবেন?",
    "ta": "நான் உதவ முடியும். உங்கள் ஆர்டர் ID அல்லது கணக்குடன் தொடர்புடைய தொலைபேசி எண்ணை பகிர முடியுமா?",
    "te": "నేను సహాయం చేయగలను. మీ ఆర్డర్ ID లేదా ఖాతాకు సంబంధించిన ఫోన్ నంబర్ చెప్పగలరా?",
}


def generate_conversation_reply(message: str, intent_type: str, language: str = "en") -> str:
    """Generate a reply from the Conversation Brain (no RAG, no policy facts).

    Replies in the user's detected language.
    """
    lang = language if language in _LANG_GREETING_MAP else "en"
    normalized = normalize_message(message).lower().rstrip("!?.,")

    if intent_type == INTENT_GREETING:
        greetings = _LANG_GREETING_MAP.get(lang, _GREETING_REPLIES_EN)
        # For non-English: try key match, then fall back to "default"
        if lang != "en":
            for pattern in greetings:
                if pattern != "default" and normalized.startswith(pattern):
                    return greetings[pattern]
            return greetings.get("default", _GREETING_REPLIES_EN.get("hello", ""))

        # English: try exact then prefix match
        if re.search(r"\b(thanks?|thank\s+you|thank\s*u|ty|thx)\b", normalized):
            return greetings.get("thanks", "You're welcome! Is there anything else I can help you with?")
        if re.search(r"\b(all\s+right|alright|ok(?:ay)?|got\s+it|understood|that\s+works|sounds\s+good)\b", normalized):
            return greetings.get("all right", "Got it. Anything else I can help you with?")
        if normalized in greetings:
            return greetings[normalized]
        for pattern, reply in greetings.items():
            if normalized.startswith(pattern):
                return reply
        return greetings.get("hello", "Hello! Thank you for contacting ZapEats support. How can I help you today?")

    if intent_type == INTENT_AMBIGUOUS:
        return _CLARIFICATION_REPLIES.get(lang, _CLARIFICATION_REPLIES["en"])

    if intent_type == INTENT_ORDER_ACTION:
        return _ORDER_ACTION_REPLIES.get(lang, _ORDER_ACTION_REPLIES["en"])

    return _LANG_GREETING_MAP.get(lang, _GREETING_REPLIES_EN).get(
        "default",
        "Hello! Thank you for contacting ZapEats support. How can I help you today?",
    )


# ---------------------------------------------------------------------------
# Chunk relevance validation
# ---------------------------------------------------------------------------

_RELEVANCE_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for",
    "from", "get", "have", "how", "i", "if", "in", "is", "it", "me", "my", "of",
    "on", "or", "should", "the", "this", "to", "what", "when", "where", "with",
    "you", "your", "please", "zapeats",
}


def _semantic_terms(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-zA-Z0-9_]{3,}", (text or "").lower())
        if token not in _RELEVANCE_STOPWORDS
    }


def validate_chunk_relevance(
    chunks: list[dict[str, Any]],
    query: str | None = None,
    min_score: float | None = None,
) -> list[dict[str, Any]]:
    """Filter retrieved chunks by minimum relevance score.

    Chunks below the threshold are discarded to prevent the Knowledge Brain
    from using irrelevant context (e.g., career progression docs for a greeting).
    """
    threshold = min_score if min_score is not None else settings.AGENT_MIN_RELEVANCE_SCORE
    query_terms = _semantic_terms(query or "")
    relevant: list[dict[str, Any]] = []
    for chunk in chunks:
        score = float(chunk.get("score", 0.0) or 0.0)
        if score < threshold:
            continue
        if query_terms:
            chunk_terms = _semantic_terms(str(chunk.get("text") or ""))
            overlap = query_terms & chunk_terms
            # Very strong vector matches can pass, otherwise require a lexical anchor
            # so unrelated training chunks do not ground customer policy answers.
            if not overlap and score < max(0.72, threshold + 0.25):
                continue
            enriched = dict(chunk)
            enriched.setdefault("relevance", {})
            enriched["relevance"] = {
                **dict(enriched.get("relevance") or {}),
                "keyword_overlap": sorted(overlap)[:12],
                "score_threshold": threshold,
            }
            relevant.append(enriched)
            continue
        relevant.append(chunk)
    return relevant
