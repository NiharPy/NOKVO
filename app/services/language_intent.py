"""Detect explicit "switch to language X" intent in a transcript.

When the caller says "please speak in Telugu" in English, the streaming STT
reports ``en-IN`` and the agent obeys, replies in English, and sounds like
it's refusing the request. This detector spots the case by looking for a
known language NAME together with a switching verb ("speak", "talk",
"reply", "bolo", "pesu", …) — or a short bare-name utterance like "Telugu
please". When triggered, the caller's request overrides the STT language
for the current turn.

Conservative by design: false positives are costly (caller mentioned "a
Telugu movie" and the agent switches), false negatives are recoverable
(caller can just speak in the target language and STT will route it).

Ported from agent_lab/voice-rag-agent/orchestrator/app/language_intent.py.
Adapted to NOKVO's two-letter language codes (``hi``, ``ta``, …) instead of
BCP-47 (``hi-IN``, ``ta-IN``).
"""

from __future__ import annotations

import re


# Map any common spelling of a language name → NOKVO's two-letter code.
_LANGUAGE_NAMES: dict[str, str] = {
    # English names
    "telugu": "te",
    "telegu": "te",
    "hindi": "hi",
    "tamil": "ta",
    "kannada": "kn",
    "malayalam": "ml",
    "bengali": "bn",
    "bangla": "bn",
    "marathi": "mr",
    "gujarati": "gu",
    "punjabi": "pa",
    "odia": "or",
    "oriya": "or",
    "urdu": "ur",
    "english": "en",
    # Native-script names
    "తెలుగు": "te",
    "हिन्दी": "hi",
    "हिंदी": "hi",
    "தமிழ்": "ta",
    "ಕನ್ನಡ": "kn",
    "മലയാളം": "ml",
    "বাংলা": "bn",
    "मराठी": "mr",
    "ગુજરાતી": "gu",
    "ਪੰਜਾਬੀ": "pa",
    "ଓଡ଼ିଆ": "or",
    "اردو": "ur",
    "ఇంగ్లీష్": "en",
    "अंग्रेज़ी": "en",
    "अंग्रेजी": "en",
}

# Verbs / particles that signal a "switch" intent. Excludes "in"/"to" (too
# common — would false-positive on "Telugu biryani"). Native verbs included
# so transliterated commands also work.
_SWITCH_TOKENS = {
    "speak", "speaking", "talk", "talking", "reply", "replying",
    "continue", "switch", "use", "using", "language",
    "respond", "responding", "answer", "answering",
    # Native verbs / particles
    "matlad", "matladu", "matladandi", "matladandee",  # Telugu "speak"
    "cheppu", "cheppandi",                              # Telugu "say"
    "bolo", "bolega", "bolna", "boliye",                # Hindi "speak"
    "kaho",                                             # Hindi "say"
    "pesu", "pesuna", "pesa",                           # Tamil "speak"
    "mein", "me", "lo", "loo", "il", "vil",             # "in" particles
}

_NATIVE_SWITCH_RE = re.compile(
    "|".join([
        # Telugu: మాట్లాడదాం, మాట్లాడాలి, మాట్లాడండి, చెప్పండి, etc.
        r"మాట్లాడ[ఀ-౿]*",
        r"మాట్లాడు[ఀ-౿]*",
        r"చెప్ప[ఀ-౿]*",
        # Hindi / Marathi
        r"बोल[ऀ-ॿ]*",
        r"कह[ऀ-ॿ]*",
        # Tamil
        r"பேச[஀-௿]*",
        r"சொல்ல[஀-௿]*",
        # Kannada
        r"ಮಾತನಾಡ[ಀ-೿]*",
        r"ಹೇಳ[ಀ-೿]*",
        # Malayalam
        r"സംസാര[ഀ-ൿ]*",
        r"പറയ[ഀ-ൿ]*",
        # Bengali
        r"বল[ঀ-৿]*",
        # Gujarati
        r"બોલ[઀-૿]*",
        # Punjabi
        r"ਬੋਲ[਀-੿]*",
    ])
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def detect_language_switch(transcript: str) -> str | None:
    """Return a two-letter language code if the transcript clearly asks for
    a language switch, otherwise None.

    Logic:
      1. Find any language-name token in the transcript.
      2. Accept the switch when:
         - a switch verb/particle appears within ~30 chars, OR
         - the entire utterance is ≤4 words ("Telugu please" by itself).
      3. Otherwise return None.
    """
    if not transcript:
        return None
    normalized = _normalize(transcript)
    if not normalized:
        return None

    # Match longest names first so multi-word entries beat single-word substrings.
    found_code: str | None = None
    found_at: int | None = None
    for name, code in sorted(_LANGUAGE_NAMES.items(), key=lambda kv: -len(kv[0])):
        idx = normalized.find(name)
        if idx == -1:
            continue
        if name.isascii():
            before = normalized[idx - 1] if idx > 0 else " "
            after_idx = idx + len(name)
            after = normalized[after_idx] if after_idx < len(normalized) else " "
            if before.isalnum() or after.isalnum():
                continue  # substring inside another word — skip
        found_code = code
        found_at = idx
        break

    if not found_code:
        return None

    word_count = len(normalized.split())
    if word_count <= 4:
        return found_code

    window_start = max(0, (found_at or 0) - 30)
    window_end = min(len(normalized), (found_at or 0) + 30)
    window = normalized[window_start:window_end]
    if _NATIVE_SWITCH_RE.search(window):
        return found_code
    window_tokens = re.findall(r"[a-zA-Z]+", window)
    if any(tok in _SWITCH_TOKENS for tok in window_tokens):
        return found_code
    return None
