"""End-of-utterance completeness classification and the humanized pre-speech
delay for verbatim questionnaire delivery.

Extracted from nokvo_one_voice_stream_service.py (which re-exports every
name here, so existing imports keep working). Byte-verbatim move — no
behavior change.
"""
from __future__ import annotations

import random
from time import perf_counter
from typing import Any

from app.core.config import settings


# ── End-of-utterance completeness tiering ────────────────────────────────────
# Adaptive endpointing: vary the silence debounce by how COMPLETE the caller's
# utterance looks, so the common "answering the agent" turn fires fast (~450ms)
# while trailing-off speech still waits long enough not to get cut off mid-
# thought. Pure + side-effect-free so it is unit-testable in isolation. A false
# "fast" is the quality risk, so the logic biases toward the slower tiers.

# STRONG continuation cues: function words / fillers that almost never end a
# complete utterance — they REQUIRE a following word, so always wait the longest.
# Sarvam STT returns NATIVE SCRIPT for hi/te, so each set carries Devanagari +
# Telugu-script members alongside the English/romanised ones — otherwise hi/te
# trailing speech ("मेरा budget मतलब…") reads as neutral and gets cut off at
# 650ms while the caller is mid-thought.
_EOU_STRONG_CONTINUATION = {
    "a", "an", "the", "and", "but", "or", "because", "than", "as", "if", "while",
    "when", "to", "of", "for", "with", "from", "at", "by", "in", "on",
    "uh", "um", "uhm",
    # Hindi (Devanagari) + Telugu: CONJUNCTIONS and "I mean…" lead-ins only.
    # Postpositions/case markers ("में", "को", "के", "లో", "గురించి") are
    # deliberately NOT here even though English prepositions are: hi/te are
    # postpositional, so elliptical COMPLETE answers routinely end in them
    # ("शाम को", "3 BHK के लिए") — treating them as continuation would 2300ms-
    # stall the most common complete replies. They stay neutral. NOTE "मतलब"
    # mirrors trailing English "because" here, NOT romanised "matlab" in the
    # particle set — Devanagari register is real Hindi where trailing "मतलब…"
    # is an unfinished explanation, while the Hinglish tic ("do BHK matlab")
    # surfaces romanised.
    "और", "या", "लेकिन", "क्योंकि", "मतलब", "अगर", "यानी", "जैसे",
    "మరియు", "లేదా", "కానీ", "అంటే", "ఇంకా",
    # Native-script hesitations (STT writes them out in-script). "ఊఁ" is NOT
    # here — in final position it's overwhelmingly the Telugu affirmation
    # ("hmm"=yes, see _EOU_YESNO_WORDS), and a mid-thought "ఊఁ…" that continues
    # simply restarts the timer when the next segment lands.
    "अं", "आं", "అం",
}
# WEAK continuation cues: auxiliaries / pronouns / determiners that OFTEN end a
# COMPLETE utterance after real content ("…you guys have", "I'd like that") but
# also trail off in a short fragment ("I have", "do you"). They force the long
# wait only when the utterance is a SHORT fragment; after substantial content
# they're treated as complete (neutral). This is the fix for the scan finding
# where "…projects you guys have" / "thank you" over-waited at 2300ms.
_EOU_WEAK_CONTINUATION = {
    "this", "these", "those", "that", "my", "your", "his", "her", "our", "their",
    "i", "he", "she", "we", "they", "it", "you",
    "is", "are", "was", "were", "be", "been", "being", "have", "has", "had",
    "do", "does", "did", "will", "would", "shall", "should", "can", "could",
    "may", "might", "so", "then", "where", "why", "how", "like", "hmm",
    # Hindi auxiliaries / pronouns / connectives — complete after real content
    # ("ठीक है"), trailing in a short fragment ("मैं तो…"). "तो" lives here (not
    # STRONG): sentence-final after content it's an emphatic particle, but a
    # short fragment ending "तो" is an unfinished conditional.
    "है", "हैं", "हूँ", "हूं", "था", "थी", "थे", "मैं", "हम", "वो", "वह", "ये",
    "यह", "आप", "मेरा", "मेरी", "मेरे", "तो",
    # Telugu equivalents. ("अभी"/"फिर"/"అయితే"/"మరి" deliberately absent: as
    # tails they end complete "okay then"-type replies as often as they trail,
    # and the short-fragment gate would over-wait 2300ms on those — neutral is
    # the honest tier for them.)
    "ఉంది", "ఉన్నాయి", "ఉన్నాను", "నేను", "మేము", "మనం", "అది", "ఇది", "నా",
    "మా", "మీ", "హ్మ్",
}
_EOU_WEAK_FRAGMENT_MAXWORDS = 4
# Closing phrases — complete by definition; never make the caller wait on these.
_EOU_CLOSER_TAILS = (
    "thank you so much", "thank you", "no thank you", "no thanks", "thanks",
    "bye", "goodbye", "good bye", "that's it", "thats it", "that's all",
    "thats all", "nothing else", "that'll be all", "thatll be all",
    "धन्यवाद", "शुक्रिया", "बस इतना ही", "और कुछ नहीं", "ठीक है बाय",
    "ధన్యవాదాలు", "థాంక్స్", "అంతే చాలు", "ఇంకేమీ లేదు", "సరే బై",
)
# Sentence-FINAL discourse particles (Indian English / Hinglish). These mark
# completion, NOT continuation ("two BHK na" is a finished answer), so strip
# them before judging and never treat them as continuation cues. "to"/"toh" is
# deliberately EXCLUDED — English infinitive "I want to" must stay continuation.
_EOU_DISCOURSE_PARTICLES = {
    "na", "naa", "haan", "han", "haa", "matlab", "bas", "only", "ya", "yaa",
    "re", "da", "ra",
    # Native-script sentence-final particles that mark completion ("दो BHK ना",
    # "రెండు BHK అంతే"). Devanagari "या" is NOT here — it's the conjunction
    # "or" (strong continuation); romanised "ya"/"yaa" stays because Hinglish
    # uses it as the tag-question tic.
    "ना", "बस", "ही", "మాత్రమే", "అంతే", "కదా",
}
# Short yes/no/acknowledgement answers (incl. a few Hinglish) → complete.
# Native-script members mirror the romanised ones — Sarvam STT emits Devanagari/
# Telugu script for hi/te, so without these a crisp "हाँ"/"అవును" never reaches
# the fast tier and waits the neutral 650ms instead of 400ms.
_EOU_YESNO_WORDS = {
    "yes", "yeah", "yep", "yup", "ya", "no", "nope", "nah", "sure", "ok", "okay",
    "correct", "right", "exactly", "fine", "done", "perfect", "alright", "haan",
    "han", "theek", "sari", "sare", "avunu", "thanks", "thank",
    # Hindi (Devanagari) affirmations / negations.
    "हाँ", "हां", "जी", "नहीं", "नही", "ठीक", "बिलकुल", "बिल्कुल", "सही", "अच्छा",
    "ओके", "हो", "चलेगा",
    # Telugu affirmations / negations.
    "అవును", "సరే", "లేదు", "కాదు", "ఓకే", "మంచిది", "కరెక్ట్", "వద్దు", "ఊఁ",
}
# Time/day tokens + the small connectors allowed inside a pure time answer.
_EOU_TIME_WORDS = {
    "morning", "afternoon", "evening", "noon", "midnight", "tonight", "tomorrow",
    "today", "yesterday", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "weekend", "weekday", "oclock", "o'clock", "am", "pm",
    "sharp", "anytime",
    # Hindi (Devanagari) day/time answers ("कल सुबह", "आज शाम को").
    "कल", "आज", "परसों", "सुबह", "शाम", "दोपहर", "रात", "अभी", "सोमवार",
    "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार", "बजे",
    # Telugu day/time answers ("రేపు ఉదయం", "ఈరోజు సాయంత్రం").
    "రేపు", "ఈరోజు", "ఇవాళ", "ఎల్లుండి", "ఉదయం", "మధ్యాహ్నం", "సాయంత్రం",
    "రాత్రి", "ఇప్పుడు", "సోమవారం", "మంగళవారం", "బుధవారం", "గురువారం",
    "శుక్రవారం", "శనివారం", "ఆదివారం", "గంటలకు",
}
_EOU_TIME_CONNECTORS = {
    "at", "on", "by", "around", "this", "next", "the", "in", "a", "of", "and",
    "to", "after", "before",
    "को", "के", "में", "లో", "కి", "కు",
}
_EOU_NUMBER_CONTEXT_WORDS = {"number", "phone", "mobile", "contact", "whatsapp"}
# Pronoun contractions ("it's", "I'm", "that's", "you're") trail off like their
# ROOT pronoun, but STT keeps the contraction so the continuation sets miss them
# and the caller gets cut off mid-thought ("uh, it's…" → fragment). Judge
# continuation on the root too. Restricted to PRONOUN roots so NEGATIVE
# contractions ("don't", "can't") are NOT dragged into the long wait (they end a
# complete reply far more often than they trail off).
_EOU_PRONOUN_CONTRACTION_ROOTS = {"it", "i", "that", "this", "you", "he", "she", "we", "they"}


def _eou_token_is_timeish(tok: str) -> bool:
    if tok in _EOU_TIME_WORDS or tok in _EOU_TIME_CONNECTORS:
        return True
    if tok.isdigit():
        return True
    if ":" in tok and tok.replace(":", "").isdigit():
        return True
    return False


def _eou_completeness_tier(text: str, answer_kind: str | None = None) -> str:
    """Return ``"fast"`` | ``"neutral"`` | ``"continuation"`` for the EOU wait.

    Only HIGH-confidence-complete utterances get ``"fast"``; anything ambiguous
    falls to ``"neutral"``; trailing-off speech stays ``"continuation"``.

    ``answer_kind`` is the optional expected-answer hint from the deterministic
    questionnaire (the verbatim path KNOWS what shape of reply the question it
    just asked invites): ``"yesno"`` promotes a short affirmation/negation to
    fast, ``"number"`` keeps a digit-trailing reply in continuation (they're
    mid-figure). Hints only ever PROMOTE toward the safer read for that shape —
    they never demote a continuation classification.
    """
    low = (text or "").strip().lower()
    if not low:
        return "neutral"
    # Normalize STT artifacts before tokenizing: zero-width (non-)joiners ride
    # INSIDE Indic tokens ("హైదరాబాద్‌లో"), NBSP splits unlike a plain space,
    # and trailing ellipses/danda stick to words ("हाँ…", "అవును।"). Every set
    # membership below assumes clean tokens.
    low = low.replace("\u200c", "").replace("\u200d", "").replace("\u00a0", " ")
    # A real '?' is a reliable completion signal (STT adds it for question
    # intonation). A trailing '.' is NOT (STT inserts it on any pause), so we
    # never fast-fire on '.' alone.
    ended_question = low.rstrip(" .,!;:'\"`…").endswith("?") or "؟" in low
    words = [w.strip(".,!?;:'\"`।॥…") for w in low.split()]
    words = [w for w in words if w]
    if not words:
        return "neutral"
    # Strip trailing Hinglish discourse particles ("two BHK na" → "two BHK").
    while len(words) > 1 and words[-1] in _EOU_DISCOURSE_PARTICLES:
        words.pop()
    last = words[-1]
    # A trailing pronoun contraction ("it's", "I'm", "that's") trails off like its
    # root pronoun — judge continuation on the root so it isn't cut off.
    last_base = last
    if "'" in last:
        _root = last.split("'", 1)[0]
        if _root in _EOU_PRONOUN_CONTRACTION_ROOTS:
            last_base = _root
    # 1) Clear question → complete regardless of the trailing word.
    if ended_question:
        return "fast"
    # 2) A closing phrase ("thank you", "that's all") → complete; never wait.
    norm = " ".join(words)
    if any(norm == c or norm.endswith(" " + c) for c in _EOU_CLOSER_TAILS):
        return "fast"
    # 3) Mid-number dictation ("my number is 98…") → the rest is coming.
    if last.isdigit() and len(last) < 10 and any(w in _EOU_NUMBER_CONTEXT_WORDS for w in words):
        return "continuation"
    # 3b) Numeric-answer hint: the questionnaire just asked for a figure
    #     (budget/quantity) and the reply ENDS on a bare digit run — they're
    #     mid-figure ("50" … "lakhs" still coming). Must sit BEFORE the pure-
    #     time rule, which would otherwise fast-fire on a lone "50".
    if answer_kind == "number" and last.isdigit():
        return "continuation"
    # 4) Trails off on a STRONG continuation word / filler → wait the longest.
    if last in _EOU_STRONG_CONTINUATION or last_base in _EOU_STRONG_CONTINUATION:
        return "continuation"
    # 5) WEAK tail (auxiliary / pronoun / determiner): after substantial content
    #    it's usually COMPLETE ("…you guys have") → neutral; only a SHORT fragment
    #    ("I have", "do you", "uh, it's") is genuinely trailing off → continuation.
    #    (Fix for the scan finding: complete utterances over-waiting at 2300ms.)
    if last in _EOU_WEAK_CONTINUATION or last_base in _EOU_WEAK_CONTINUATION:
        return "continuation" if len(words) <= _EOU_WEAK_FRAGMENT_MAXWORDS else "neutral"
    # 6) Short yes/no/acknowledgement answer.
    if len(words) <= 3 and (words[0] in _EOU_YESNO_WORDS or last in _EOU_YESNO_WORDS):
        return "fast"
    # 7) A PURE time/day answer ("10 AM", "tomorrow at 4", "Saturday morning") —
    #    but NOT a declarative that merely mentions a day ("Saturday works",
    #    where "works" is not time-ish → falls through to neutral).
    if (
        len(words) <= 6
        and all(_eou_token_is_timeish(w) for w in words)
        and any(w not in _EOU_TIME_CONNECTORS for w in words)  # ≥1 real time token, not just connectors
    ):
        return "fast"
    # 8) Yes/no-answer hint: the questionnaire just asked an intent (yes/no)
    #    question and the short reply CONTAINS an affirmation/negation anywhere
    #    ("हाँ चाहिए", "yes definitely") — complete even though the tail word
    #    isn't itself a yes/no token. Slightly wider than rule 6 (any position,
    #    ≤4 words) because here we KNOW the question invited exactly this shape.
    #    Continuation classifications never reach this point, so the hint can
    #    only promote neutral → fast.
    if answer_kind == "yesno" and len(words) <= 4 and any(w in _EOU_YESNO_WORDS for w in words):
        return "fast"
    # 9) Everything else (declaratives, noun phrases) → neutral: leaves room for
    #    a self-correction ("…actually Sunday") to land and restart the timer.
    return "neutral"


def _question_answer_kind(q: dict[str, Any] | None) -> str | None:
    """Expected reply shape for a just-asked questionnaire question — the
    ``answer_kind`` hint for :func:`_eou_completeness_tier`. ``intent``
    questions invite yes/no; ``answer`` questions whose desired answer or
    graded band labels carry digits invite a figure; anything else yields no
    hint (generic classification)."""
    if not isinstance(q, dict):
        return None
    qtype = str(q.get("type") or "").strip().lower()
    if qtype == "intent":
        return "yesno"
    if qtype == "answer":
        blobs = [str(q.get("desired_answer") or "")]
        for t in q.get("tiers") or []:
            if isinstance(t, dict):
                blobs.append(str(t.get("label") or ""))
        if any(ch.isdigit() for blob in blobs for ch in blob):
            return "number"
    return None


def _verbatim_prespeech_delay_s(
    *,
    eou_fired_at: float | None,
    ack_will_fire: bool = False,
    now: float | None = None,
    _rng: random.Random | None = None,
) -> float:
    """Humanized pre-speech pause (seconds) for the DETERMINISTIC reply path.

    Cached verbatim replies otherwise land with near-constant, unnaturally fast
    latency every turn — machine-gun cadence. Rather than a blind delay (which
    would stack on the EOU silence wait and feel slow), top up the TOTAL
    perceived gap — end-of-speech → first audio, which already includes the EOU
    tier wait via ``eou_fired_at`` — toward ``APEX_TURN_GAP_TARGET_MS`` ±
    jitter. After a fast-tier EOU (400ms) that adds a couple hundred ms; after
    the long continuation wait (2300ms) it adds ZERO — the caller has already
    experienced a long pause. Clamped to the sub-1s latency budget (less a
    ~150ms cached-fetch estimate) and to ``APEX_VERBATIM_DELAY_MAX_MS``. When a
    micro-ack will be spoken the ACK is the gap-filler, so the sleep clamps
    hard to ``APEX_VERBATIM_DELAY_ACK_MAX_MS`` instead. 0.0 when the feature is
    off (``APEX_TURN_GAP_TARGET_MS=0``) or there is no EOS anchor (manual /
    proactive turns)."""
    target = int(settings.APEX_TURN_GAP_TARGET_MS or 0)
    if target <= 0 or eou_fired_at is None:
        return 0.0
    _now = perf_counter() if now is None else now
    elapsed_ms = max(0.0, (_now - eou_fired_at) * 1000.0)
    fetch_estimate_ms = 150.0
    rng = _rng or random
    jitter = int(settings.APEX_TURN_GAP_JITTER_MS or 0)
    goal_ms = target + (rng.uniform(-jitter, jitter) if jitter > 0 else 0.0)
    delay_ms = goal_ms - elapsed_ms - fetch_estimate_ms
    budget_headroom_ms = (
        float(settings.VOICE_LATENCY_BUDGET_MS) - elapsed_ms - fetch_estimate_ms
    )
    cap_ms = float(
        settings.APEX_VERBATIM_DELAY_ACK_MAX_MS
        if ack_will_fire
        else settings.APEX_VERBATIM_DELAY_MAX_MS
    )
    delay_ms = min(delay_ms, budget_headroom_ms, cap_ms)
    return max(0.0, delay_ms) / 1000.0
