"""Deterministic spoken-line tables and pickers: quick acks, latency guards,
outros/busy closes, voicemail and goodbye scripts. Strings are part of the
TTS byte-cache key surface - byte-verbatim, never reformat.

Extracted from nokvo_one_voice_stream_service.py (which re-exports every
name here, so existing imports keep working). Byte-verbatim move — no
behavior change.
"""
from __future__ import annotations

import re


def _quick_ack_text(language: str | None) -> str:
    return {
        "hi": "जी हाँ, सुन रहा हूँ।",
        "ta": "ஆமா, கேக்குறேன்.",
        "te": "అవును, వింటున్నాను.",
        "bn": "হ্যাঁ, শুনছি।",
        "kn": "ಹೌದು, ಕೇಳ್ತಾ ಇದೀನಿ.",
        "ml": "അതെ, കേൾക്കുന്നു.",
        "mr": "हो, ऐकतोय.",
        "gu": "હા, સાંભળું છું.",
        "pa": "ਹਾਂ, ਸੁਣ ਰਿਹਾ ਹਾਂ।",
    }.get(language or "en", "Yes, I'm here.")


# Inbound latency filler: a reassuring "one moment, checking" hold. On a support
# call this reads as the agent looking something up — natural and expected.
# Native script per language (Sarvam TTS mispronounces romanised Indic text).
_LATENCY_GUARD_INBOUND = {
    "hi": "एक पल, मैं देख रहा हूँ।",
    "ta": "ஒரு நிமிடம், பார்த்துக்கிறேன்.",
    "te": "ఒక్క క్షణం, చూస్తున్నాను.",
    "bn": "একটু সময় দিন, আমি দেখছি।",
    "kn": "ಒಂದು ಕ್ಷಣ, ನೋಡುತ್ತಿದ್ದೇನೆ.",
    "ml": "ഒരു നിമിഷം, ഞാൻ നോക്കുകയാണ്.",
    "mr": "एक क्षण, मी पाहतोय.",
    "gu": "એક ક્ષણ, હું જોઈ રહ્યો છું.",
    "pa": "ਇੱਕ ਪਲ, ਮੈਂ ਵੇਖ ਰਿਹਾ ਹਾਂ।",
    "ur": "ایک لمحہ، میں دیکھ رہا ہوں۔",
    "od": "ଟିକେ ଅପେକ୍ଷା କରନ୍ତୁ, ମୁଁ ଦେଖୁଛି।",
}
# Beat to wait before speaking the OUTBOUND opener. When we dial out, the
# callee's audio path isn't up the instant our media WS opens — they're still
# raising the handset and saying "hello?". Speaking immediately gets the intro
# ("Riya here from <company>…") clipped or talked over, so the prospect never
# catches who's calling and asks "what is this?". A short pause lets their
# "hello" land and the path settle, so the full intro is heard. Inbound is
# unaffected (it has its own 0.35s opener delay). Tune against a live call.
_OUTBOUND_OPENER_DELAY_SECONDS = 0.7

# Outbound latency bridge: NOT a hold. On an outbound sales call "please hold /
# one moment" reads as a stalled call-center queue and gets the prospect to hang
# up. Instead a short, natural thinking-aloud token ("Mhm…", "I see,") so it
# sounds like a human gathering a thought, not a system stalling.
_LATENCY_GUARD_OUTBOUND = {
    "en": "Okay, just a sec…",
    "hi": "जी, बस एक सेकंड…",
    "ta": "ம்ம், சரி…",
    "te": "ఊఁ, అలాగే…",
    "bn": "হুম, আচ্ছা…",
    "kn": "ಹ್ಮ್, ಸರಿ…",
    "ml": "ഹ്മ്, ശരി…",
    "mr": "हम्म, बरं…",
    "gu": "હમ્મ, સારું…",
    "pa": "ਹਾਂ ਜੀ, ਬੱਸ…",
    "ur": "جی، بس ایک سیکنڈ…",
    "od": "ହଁ, ଠିକ୍ ଅଛି…",
}


def _latency_guard_text(language: str | None, direction: str = "inbound") -> str:
    """Localized sub-1s filler. ``direction`` selects the phrase register:
    inbound uses the reassuring "one moment, checking" hold; outbound uses a
    short conversational bridge so it sounds like a person thinking, not a queue.
    All 12 supported languages are covered; unknown → English."""
    table = _LATENCY_GUARD_OUTBOUND if direction == "outbound" else _LATENCY_GUARD_INBOUND
    default = "Okay, just a sec…" if direction == "outbound" else "One moment, I'm checking that."
    return table.get(language or "en", default)


# Grace period (seconds) to let the outro audio drain to the caller before we
# drop the media WS — closing immediately can clip the last words.
_OUTRO_DRAIN_SECONDS = 2.5

# Close line for questionnaire campaigns with NO authored outro. Without this a
# completed questionnaire either hung up with dead air or — before the close
# stopped requiring an outro at all — never closed and looped its questions.
# Native script for hi/te (romanised text makes Sarvam TTS mispronounce).
_DEFAULT_QUESTIONNAIRE_OUTROS = {
    "en": "Thank you for your time. Have a great day!",
    "hi": "आपके समय के लिए धन्यवाद। आपका दिन शुभ हो!",
    "te": "మీ సమయానికి ధన్యవాదాలు. మీకు శుభదినం!",
}

# The BUSY dealbreaker close: the caller said "I'm busy / call me later", so
# the agent acknowledges, promises the call-back, and hangs up — pushing the
# next question at someone who asked to go is how campaigns get numbers
# blocked. Native hi/te (romanised Indic mispronounces on Sarvam). Static +
# shared across campaigns → TTS-prewarmed alongside the outros.
_BUSY_OUTROS = {
    "en": "No problem — we'll call you back at a better time. Have a great day!",
    "hi": "कोई बात नहीं — हम आपको बाद में call करेंगे। आपका दिन शुभ हो!",
    "te": "పర్వాలేదు — మేము మీకు తర్వాత call చేస్తాము. మీకు శుభదినం!",
}


def _default_questionnaire_outro(language: str | None) -> str:
    lang = str(language or "en").split("-")[0].lower()
    return _DEFAULT_QUESTIONNAIRE_OUTROS.get(lang, _DEFAULT_QUESTIONNAIRE_OUTROS["en"])


def _busy_outro(language: str | None) -> str:
    lang = str(language or "en").split("-")[0].lower()
    return _BUSY_OUTROS.get(lang, _BUSY_OUTROS["en"])


def _answer_is_outro(answer: str | None, outro: str | None) -> bool:
    """True when the agent's spoken reply IS (essentially) the campaign's outro.

    Token-overlap match: ``True`` when ≥70% of the outro's words appear in the
    reply. Used to detect that the agent delivered its closing line (a failed
    intent gate, a normal wrap, or a disinterest close) so the system can play it
    out and hang up. Pure + unit-testable. Conservative on purpose — a miss just
    means the call isn't auto-cut (the agent still wrapped), never a wrong hangup
    on a live prospect.

    GUARD — a turn that STILL ASKS A QUESTION is never a closing turn. The model
    sometimes bundles the closing line onto the SAME turn as the last
    questionnaire question (e.g. "Would you like our team to reach out…? Thanks
    for your time, our team will reach out shortly."), and that last question
    often shares most of its words with the outro, so the overlap alone
    false-fires. If the reply carries MORE question marks than the outro itself,
    there is a pending question — the call must continue, so it is NOT a close."""
    a_raw = answer or ""
    o_raw = outro or ""
    if a_raw.count("?") > o_raw.count("?"):
        return False
    a = re.sub(r"[^\w\s]", " ", a_raw.lower()).split()
    o = re.sub(r"[^\w\s]", " ", o_raw.lower()).split()
    if not o or not a:
        return False
    aset = set(a)
    hits = sum(1 for w in o if w in aset)
    return hits / len(o) >= 0.7


def _voicemail_message(
    language: str | None, *, caller_name: str = "", company_name: str = ""
) -> str:
    """One short, on-brand line to leave on a prospect's voicemail before we hang
    up. Personalised with the campaign's agent + company name; native script for
    hi/te (so Sarvam TTS pronounces it right), English fallback otherwise."""
    caller = (caller_name or "").strip()
    company = (company_name or "").strip()
    lang = (language or "en")[:2]
    if lang == "hi":
        who = (f"{company} से {caller}".strip() if company else caller) or "हमारी टीम"
        return (
            f"नमस्ते, मैं {who} बोल रही हूँ — आपसे बात नहीं हो पाई। "
            "हम दोबारा कोशिश करेंगे, या आप हमें कॉल बैक कर सकते हैं। धन्यवाद!"
        )
    if lang == "te":
        who = (f"{company} నుండి {caller}".strip() if company else caller) or "మా టీమ్"
        return (
            f"నమస్తే, నేను {who} మాట్లాడుతున్నాను — మిమ్మల్ని కలవలేకపోయాం. "
            "మేము మళ్ళీ ప్రయత్నిస్తాం, లేదా మీరు మాకు తిరిగి కాల్ చేయవచ్చు. ధన్యవాదాలు!"
        )
    if caller and company:
        who = f"this is {caller} from {company}"
    elif caller:
        who = f"this is {caller}"
    elif company:
        who = f"this is {company}"
    else:
        who = "this is your callback team"
    return (
        f"Hi, {who} — sorry we missed you. "
        "We'll try you again, or feel free to call us back. Thank you!"
    )


def _no_response_goodbye_text(language: str | None) -> str:
    """One short line spoken when a picked-up caller has gone silent through a
    nudge and we're about to hang up. Native script for hi/te (Sarvam TTS
    pronunciation), English fallback. Frames it as "not a good time" + a callback,
    NOT disinterest."""
    lang = (language or "en")[:2]
    if lang == "hi":
        return (
            "लगता है अभी सही समय नहीं है — हम आपको बाद में दोबारा कॉल करेंगे। "
            "धन्यवाद!"
        )
    if lang == "te":
        return (
            "ఇప్పుడు సరైన సమయం కాదు అనిపిస్తోంది — మేము మిమ్మల్ని తర్వాత మళ్ళీ కాల్ చేస్తాం. "
            "ధన్యవాదాలు!"
        )
    return "Seems like now isn't a good time — I'll try you again later. Goodbye!"


def _site_visit_confirm_text(language: str | None, when: str = "") -> str:
    """Deterministic, per-language confirmation for the moment a caller agrees
    to a site visit and gives a date/time. Replaces free-generated Telugu/Hindi
    (which the LLM corrupts) with a clean templated line. ``when`` (e.g.
    ``"tomorrow 10 AM"``) stays in English/digits per the native-script rule;
    Telugu/Hindi are hand-tuned, other languages fall back to English."""
    lang = (language or "en")[:2]
    if when:
        return {
            "te": f"సరే అండి, {when} కి note చేసుకున్నాను. మా team confirm చేసి SMS పంపిస్తారు.",
            "hi": f"ठीक है जी, {when} के लिए note कर लिया. हमारी team confirm करके SMS भेज देगी.",
        }.get(lang, f"Perfect, noted for {when}. Our team will confirm and send you an SMS shortly.")
    return {
        "te": "సరే అండి, note చేసుకున్నాను. మా team confirm చేసి SMS పంపిస్తారు.",
        "hi": "ठीक है जी, note कर लिया. हमारी team confirm करके SMS भेज देगी.",
    }.get(lang, "Perfect, noted. Our team will confirm and send you an SMS shortly.")
