from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import asyncio
import base64
import json
import re
from time import perf_counter
from typing import Any, AsyncIterator
from urllib import parse as urllib_parse

import httpx
from fastapi import WebSocket
from websockets.asyncio.client import connect

from app.core.config import settings
from app.models.tenant_resources import TenantResources
from app.services.azure_keyvault_service import AzureKeyVaultService


# ── TTS text normalization (numbers + stray transliteration) ─────────────────
# Two jobs, applied ONLY to the synthesized audio text (the on-screen transcript
# keeps "₹2.45Cr"/digits for the UI):
#   1. Speak EVERY number in English. bulbul:v3 preprocessing otherwise reads
#      digits in the voice's own language (Telugu "మూడు" for 3), and reads "₹"
#      as "rupees" split across the decimal ("2 rupees 45 rupees"). We convert
#      money, times, phones, decimals and plain integers to English words.
#   2. Repair recurring transliterated / wrong-script loanwords the model emits
#      ("వాట్సాప్" → WhatsApp) which the native TTS otherwise mispronounces.
# Words "point"/"rupees"/"crore"/"lakh" + the English number words are natural
# code-switches in te/hi/en.

_ONES = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _int_to_words(n: int) -> str:
    """0–9999 → English words ("five hundred", "twenty six"). Larger falls back
    to digit-by-digit so we never silently drop magnitude."""
    if n < 0:
        return "minus " + _int_to_words(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        t, o = divmod(n, 10)
        return _TENS[t] + ("" if o == 0 else " " + _ONES[o])
    if n < 1000:
        h, r = divmod(n, 100)
        return _ONES[h] + " hundred" + ("" if r == 0 else " " + _int_to_words(r))
    if n < 10000:
        th, r = divmod(n, 1000)
        return _ONES[th] + " thousand" + ("" if r == 0 else " " + _int_to_words(r))
    return _digits_to_words(str(n))


def _digits_to_words(s: str) -> str:
    """Spell each digit ("7503" → "seven five zero three"). For phone numbers."""
    return " ".join(_ONES[int(c)] for c in s if c.isdigit())


def _year_to_words(y: int) -> str:
    """1900–2099 → spoken year ("twenty twenty six", "two thousand five")."""
    if 2000 <= y <= 2009:
        return "two thousand" + ("" if y == 2000 else " " + _ONES[y - 2000])
    hi, lo = divmod(y, 100)
    if lo == 0:
        return _int_to_words(hi) + " hundred"
    if lo < 10:
        return _int_to_words(hi) + " oh " + _ONES[lo]
    return _int_to_words(hi) + " " + _int_to_words(lo)


def _spoken_number(num: str) -> str:
    """"2.45" → "two point four five"; "500" → "five hundred". Commas stripped."""
    num = num.replace(",", "")
    if "." in num:
        whole, frac = num.split(".", 1)
        whole_w = _int_to_words(int(whole)) if whole.isdigit() else whole
        return whole_w + " point " + " ".join(_ONES[int(c)] for c in frac if c.isdigit())
    return _int_to_words(int(num)) if num.isdigit() else num


_AMOUNT_UNIT_MAP = {
    "cr": "crore", "crore": "crore", "crores": "crore",
    "l": "lakh", "lakh": "lakh", "lakhs": "lakh", "lac": "lakh", "lacs": "lakh",
    "k": "thousand", "thousand": "thousand",
}
# ₹ / Rs / INR amount, with an optional crore/lakh/k suffix.
_TTS_CURRENCY_RE = re.compile(
    r"(?:₹|\bRs\.?|\bINR)\s*([\d,]+(?:\.\d+)?)(?:\s*(crores?|cr|lakhs?|lacs?|l|k|thousand)\b)?",
    re.IGNORECASE,
)
# A decimal number directly followed by crore/lakh but WITHOUT a currency mark.
_TTS_NUM_UNIT_RE = re.compile(
    r"\b(\d[\d,]*\.\d+)\s*(crores?|cr|lakhs?|lacs?)\b",
    re.IGNORECASE,
)
# Clock time: "11:00 AM", "9 AM", "12 PM", "11:30 pm".
_TTS_TIME_RE = re.compile(r"(?<![\d:])(\d{1,2})(?::([0-5]\d))?\s*([AaPp]\.?[Mm]\.?)")
# Phone / long digit run (≥6 digits, allowing +91, spaces, dashes).
_TTS_PHONE_RE = re.compile(r"(?<!\d)(\+?\d[\d\s-]{5,}\d)(?!\d)")
# Bare decimal ("2.45"). Dotted, so it never matches colon times.
_TTS_BARE_DECIMAL_RE = re.compile(r"(?<![\d.])(\d+)\.(\d+)(?![\d.])")
# Standalone integer — LAST pass. Not flanked by a letter/digit, so "B2B",
# "Web3" and joined ids like "3BHK" are left alone; "3 BHK" / "500" convert.
_TTS_INT_RE = re.compile(r"(?<![A-Za-z0-9])(\d{1,4})(?![A-Za-z0-9])")
_HAS_DIGIT_RE = re.compile(r"\d")

# Recurring transliterated / wrong-script loanwords → Latin. DISTINCT,
# multi-syllable words only (short ambiguous ones like "నోట్" are left to the
# prompt rule to avoid corrupting a longer genuine word). Devanagari + Telugu.
_TRANSLIT_FIX = {
    "వాట్సాప్": "WhatsApp", "వాట్సప్": "WhatsApp",
    "హ్యాంగ్ అప్": "hang up", "హ్యాంగప్": "hang up",
    "బ్రోచర్": "brochure",
    "బెటర్": "better",
    "व्हाट्सएप": "WhatsApp", "टीम": "team",
}
# Indic letter blocks (Telugu + Devanagari) — used as boundaries so a mapped
# word isn't replaced when it's a substring inside a longer native word.
_INDIC = "ఀ-౿ऀ-ॿ"
_TRANSLIT_RES = [
    (re.compile(rf"(?<![{_INDIC}]){re.escape(k)}(?![{_INDIC}])"), v)
    for k, v in _TRANSLIT_FIX.items()
]


def normalize_text_for_tts(text: str) -> str:
    """Rewrite numbers to English words + repair transliterated loanwords, for
    the synthesized audio only (never the displayed transcript)."""
    if not text:
        return text

    # Transliteration repair runs regardless of digits (operates on words).
    for pat, repl in _TRANSLIT_RES:
        text = pat.sub(repl, text)

    if not _HAS_DIGIT_RE.search(text):
        return text

    # Collapse a decimal the model spaced out ("₹2. 45Cr" → "₹2.45Cr").
    text = re.sub(r"(\d)\s*\.\s*(\d)", r"\1.\2", text)

    def _currency(m: "re.Match[str]") -> str:
        spoken = _spoken_number(m.group(1))
        unit = _AMOUNT_UNIT_MAP.get((m.group(2) or "").lower(), "")
        return " ".join(p for p in (spoken, unit, "rupees") if p)

    def _num_unit(m: "re.Match[str]") -> str:
        unit = _AMOUNT_UNIT_MAP.get(m.group(2).lower(), "")
        return f"{_spoken_number(m.group(1))} {unit}".strip()

    def _time(m: "re.Match[str]") -> str:
        h = int(m.group(1))
        if not (1 <= h <= 12):
            return m.group(0)  # not a clock hour — leave for the integer pass
        ap = m.group(3).upper().replace(".", "")
        out = _int_to_words(h)
        if m.group(2) and int(m.group(2)) != 0:
            out += " " + _int_to_words(int(m.group(2)))
        return f"{out} {ap}"

    def _phone(m: "re.Match[str]") -> str:
        digits = re.sub(r"\D", "", m.group(1))
        return _digits_to_words(digits) if len(digits) >= 6 else m.group(0)

    def _bare_decimal(m: "re.Match[str]") -> str:
        return f"{_int_to_words(int(m.group(1)))} point " + " ".join(
            _ONES[int(c)] for c in m.group(2)
        )

    def _standalone_int(m: "re.Match[str]") -> str:
        tok = m.group(1)
        n = int(tok)
        if len(tok) == 4 and 1900 <= n <= 2099:
            return _year_to_words(n)
        return _int_to_words(n)

    text = _TTS_CURRENCY_RE.sub(_currency, text)
    text = _TTS_NUM_UNIT_RE.sub(_num_unit, text)
    text = _TTS_TIME_RE.sub(_time, text)
    text = _TTS_PHONE_RE.sub(_phone, text)
    text = _TTS_BARE_DECIMAL_RE.sub(_bare_decimal, text)
    text = _TTS_INT_RE.sub(_standalone_int, text)
    return text


SARVAM_LANGUAGE_OPTIONS = [
    {"code": "en", "bcp47": "en-IN", "label": "English", "native_label": "English"},
    {"code": "hi", "bcp47": "hi-IN", "label": "Hindi", "native_label": "हिन्दी"},
    {"code": "bn", "bcp47": "bn-IN", "label": "Bengali", "native_label": "বাংলা"},
    {"code": "gu", "bcp47": "gu-IN", "label": "Gujarati", "native_label": "ગુજરાતી"},
    {"code": "kn", "bcp47": "kn-IN", "label": "Kannada", "native_label": "ಕನ್ನಡ"},
    {"code": "ml", "bcp47": "ml-IN", "label": "Malayalam", "native_label": "മലയാളം"},
    {"code": "mr", "bcp47": "mr-IN", "label": "Marathi", "native_label": "मराठी"},
    {"code": "pa", "bcp47": "pa-IN", "label": "Punjabi", "native_label": "ਪੰਜਾਬੀ"},
    {"code": "ta", "bcp47": "ta-IN", "label": "Tamil", "native_label": "தமிழ்"},
    {"code": "te", "bcp47": "te-IN", "label": "Telugu", "native_label": "తెలుగు"},
    {"code": "ur", "bcp47": "ur-IN", "label": "Urdu", "native_label": "اُردُو"},
    {"code": "od", "bcp47": "od-IN", "label": "Odia", "native_label": "ଓଡ଼ିଆ"},
]

_SHORT_TO_BCP47 = {item["code"]: item["bcp47"] for item in SARVAM_LANGUAGE_OPTIONS}
_BCP47_TO_SHORT = {item["bcp47"].lower(): item["code"] for item in SARVAM_LANGUAGE_OPTIONS}


class SarvamVoiceService:
    # Shared httpx client for Sarvam REST endpoints (STT + TTS). The previous
    # implementation opened a fresh AsyncClient per call, which paid a TLS
    # handshake every TTS sentence and every STT fragment. A long-lived
    # client keeps the connection pool warm so subsequent calls re-use the
    # established TLS session — typically saves 80-200 ms per request.
    _http: httpx.AsyncClient | None = None

    @classmethod
    def http_client(cls) -> httpx.AsyncClient:
        if cls._http is None or cls._http.is_closed:
            cls._http = httpx.AsyncClient(
                timeout=httpx.Timeout(35.0),
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            )
        return cls._http

    @staticmethod
    def normalize_language(language: str | None) -> str:
        raw = (language or "en").strip().lower()
        if not raw or raw == "unknown":
            return "en"
        if raw in _SHORT_TO_BCP47:
            return raw
        if raw in _BCP47_TO_SHORT:
            return _BCP47_TO_SHORT[raw]
        short = raw.split("-", 1)[0]
        return short if short in _SHORT_TO_BCP47 else "en"

    @staticmethod
    def to_bcp47(language: str | None, *, allow_unknown: bool = False) -> str:
        raw = (language or "").strip().lower()
        if allow_unknown and (not raw or raw == "unknown"):
            return "unknown"
        short = SarvamVoiceService.normalize_language(language)
        return _SHORT_TO_BCP47.get(short, "en-IN")

    @staticmethod
    def language_label(language: str | None) -> str:
        code = SarvamVoiceService.normalize_language(language)
        for item in SARVAM_LANGUAGE_OPTIONS:
            if item["code"] == code:
                return f"{item['label']} ({item['native_label']})"
        return "English"

    @staticmethod
    def tts_speaker_for(
        language: str | None, provider_status: dict[str, Any] | None = None
    ) -> str:
        """Pick the TTS speaker for a language.

        Precedence: tenant override (``provider_status``) → per-language native
        default (``settings.SARVAM_TTS_SPEAKER_<LANG>``) → global
        ``SARVAM_TTS_SPEAKER``.

        A single global speaker makes Telugu speak in the Hindi-leaning default
        voice; the per-language defaults let each language use a native-sounding
        speaker. Returns the global default when nothing per-language is set, so
        behaviour is unchanged until the overrides are populated (and a bad id
        degrades to the default at synthesis time — see :meth:`synthesize`).
        """
        ps = provider_status or {}
        override = ps.get("sarvam_tts_speaker") or ps.get("tts_voice")
        if override:
            return str(override)
        code = SarvamVoiceService.normalize_language(language)
        per_lang_tenant = ps.get(f"sarvam_tts_speaker_{code}")
        if per_lang_tenant:
            return str(per_lang_tenant)
        env_default = getattr(settings, f"SARVAM_TTS_SPEAKER_{code.upper()}", "") or ""
        if env_default:
            return str(env_default)
        return settings.SARVAM_TTS_SPEAKER

    @staticmethod
    def pace_for(language: str | None, pace: float | None) -> float | None:
        """Apply the per-language pace multiplier (``SARVAM_TTS_PACE_<LANG>``).

        The bulbul:v3 Telugu voice is slow at pace 1.0, so Telugu is sped up.
        The factor applies even when the turn carries no explicit ``pace`` —
        we set the factor as the baseline so EVERY Telugu chunk speeds up, not
        just the tone-tagged first one. Returns the (possibly clamped) pace, or
        ``None`` when there's nothing to change (factor 1.0 and no input pace)."""
        code = SarvamVoiceService.normalize_language(language)
        factor = float(getattr(settings, f"SARVAM_TTS_PACE_{code.upper()}", 1.0) or 1.0)
        if factor == 1.0:
            return pace
        base = 1.0 if pace is None else float(pace)
        return max(0.3, min(3.0, base * factor))

    @staticmethod
    async def api_key(tenant_res: TenantResources | None = None, role: str | None = None) -> str:
        # Local/operator override takes precedence. Tenant Key Vault refs are
        # provisioned snapshots and can lag behind a rotated platform key.
        if settings.SARVAM_API_KEY:
            return settings.SARVAM_API_KEY
        provider_status = dict(getattr(tenant_res, "provider_status", None) or {})
        secret_keys = [
            "sarvam_api_key_ref",
            "sarvam_api_key_secret_ref",
        ]
        if role:
            secret_keys = [f"{role}_api_key_ref", f"{role}_api_key_secret_ref"] + secret_keys
        for key in secret_keys:
            ref = provider_status.get(key)
            if not ref:
                continue
            try:
                secret = await AzureKeyVaultService.get_secret_value(ref)
            except Exception:
                secret = None
            if secret:
                return secret
        raise RuntimeError("Sarvam API key is not configured.")

    @staticmethod
    async def _stt_post_with_retry(
        client: "httpx.AsyncClient",
        url: str,
        api_key: str,
        data: dict[str, Any],
        files: dict[str, Any],
        max_attempts: int = 6,
    ) -> "httpx.Response":
        """POST to a Sarvam STT endpoint, retrying on 429 with respect for
        the Retry-After header. Returns the final response (success or last failure).

        Sarvam's quota is the actual constraint here; this just makes the client
        wait it out instead of bubbling the 429 to the user. Per-wait cap is 4s
        so voice latency degrades but doesn't go infinite when Sarvam throttles.
        """
        import asyncio

        attempt = 0
        last_response: "httpx.Response" | None = None
        while attempt < max_attempts:
            attempt += 1
            response = await client.post(
                url,
                headers={"api-subscription-key": api_key},
                data=data,
                files=files,
            )
            last_response = response
            if response.status_code != 429:
                return response
            # 429 — figure out how long to wait
            retry_after_hdr = response.headers.get("retry-after", "")
            try:
                retry_after = float(retry_after_hdr) if retry_after_hdr else 0.0
            except ValueError:
                retry_after = 0.0
            # Default backoff if Retry-After missing: 0.8s, 1.6s, 3.2s, ...
            backoff = retry_after if retry_after > 0 else (0.8 * (2 ** (attempt - 1)))
            # Per-wait cap (longer than before — the user opted to absorb 429s
            # at the cost of latency rather than surface them).
            wait = min(backoff, 4.0)
            if attempt >= max_attempts:
                print(
                    f"[NOKVO-SARVAM] STT 429 after {attempt} attempts — giving up "
                    f"(retry_after={retry_after_hdr!r}, body={response.text[:120]!r})"
                )
                return response
            print(
                f"[NOKVO-SARVAM] STT 429 attempt {attempt}/{max_attempts}; "
                f"sleeping {wait:.2f}s before retry"
            )
            await asyncio.sleep(wait)
        return last_response  # unreachable in practice

    @staticmethod
    async def transcribe_rest(
        tenant_res: TenantResources,
        audio_bytes: bytes,
        *,
        filename: str = "audio.webm",
        content_type: str = "audio/webm",
        language: str | None = None,
        mode: str | None = None,
    ) -> dict[str, Any]:
        if not audio_bytes:
            return {"transcript": "", "language_code": None, "language": "en"}

        provider_status = dict(tenant_res.provider_status or {})
        api_key = await SarvamVoiceService.api_key(tenant_res, "stt")
        stt_model = provider_status.get("sarvam_stt_model") or provider_status.get("stt_model") or settings.SARVAM_STT_MODEL
        stt_mode = mode or provider_status.get("sarvam_stt_mode") or settings.SARVAM_STT_MODE
        language_code = SarvamVoiceService.to_bcp47(language, allow_unknown=True)
        data: dict[str, str] = {"model": stt_model, "language_code": language_code}
        if stt_model == "saaras:v3":
            data["mode"] = stt_mode
        files = {"file": (filename, audio_bytes, content_type or "application/octet-stream")}
        endpoint = provider_status.get("sarvam_stt_rest_url") or settings.SARVAM_STT_REST_URL
        client = SarvamVoiceService.http_client()
        response = await SarvamVoiceService._stt_post_with_retry(
            client, endpoint, api_key, data, files
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Sarvam STT failed ({response.status_code}): {response.text[:300]}")
        payload = response.json()
        language_code = payload.get("language_code")
        return {
            "request_id": payload.get("request_id"),
            "transcript": str(payload.get("transcript") or "").strip(),
            "language_code": language_code,
            "language": SarvamVoiceService.normalize_language(language_code),
            "language_probability": payload.get("language_probability"),
            "raw": payload,
        }

    @staticmethod
    async def transcribe_translate(
        tenant_res: TenantResources,
        audio_bytes: bytes,
        *,
        filename: str = "audio.wav",
        content_type: str = "audio/wav",
    ) -> dict[str, Any]:
        """Call Sarvam's speech-to-text-translate endpoint.

        Returns an English-translated transcript regardless of the source
        language. Used by the cross-lingual retrieval path: the LLM gets the
        native transcript (preserving the caller's exact words) while the
        embedding query uses this English version so it matches an English
        document corpus.
        """
        if not audio_bytes:
            return {"transcript": "", "language_code": None}
        provider_status = dict(tenant_res.provider_status or {})
        api_key = await SarvamVoiceService.api_key(tenant_res, "stt")
        # The translate endpoint requires a saaras:* model.
        translate_model = provider_status.get("sarvam_stt_translate_model") or "saaras:v3"
        base = provider_status.get("sarvam_stt_rest_url") or settings.SARVAM_STT_REST_URL
        translate_endpoint = base.rstrip("/").replace("/speech-to-text", "/speech-to-text-translate")
        if "/speech-to-text-translate" not in translate_endpoint:
            translate_endpoint = "https://api.sarvam.ai/speech-to-text-translate"
        files = {"file": (filename, audio_bytes, content_type or "application/octet-stream")}
        client = SarvamVoiceService.http_client()
        response = await SarvamVoiceService._stt_post_with_retry(
            client, translate_endpoint, api_key, {"model": translate_model}, files,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Sarvam translate STT failed ({response.status_code}): {response.text[:300]}"
            )
        payload = response.json()
        return {
            "request_id": payload.get("request_id"),
            "transcript": str(payload.get("transcript") or "").strip(),
            "language_code": payload.get("language_code"),
            "raw": payload,
        }

    @staticmethod
    def stt_websocket_url(
        tenant_res: TenantResources,
        *,
        language: str | None = None,
        sample_rate: int | None = None,
        mode: str | None = None,
    ) -> str:
        provider_status = dict(tenant_res.provider_status or {})
        base = provider_status.get("sarvam_stt_ws_url") or provider_status.get("stt_endpoint") or settings.SARVAM_STT_WEBSOCKET_URL
        query = {
            "language-code": SarvamVoiceService.to_bcp47(language, allow_unknown=True),
            "model": provider_status.get("sarvam_stt_model") or provider_status.get("stt_model") or settings.SARVAM_STT_MODEL,
            "mode": mode or provider_status.get("sarvam_stt_mode") or settings.SARVAM_STT_MODE,
            "sample_rate": str(sample_rate or provider_status.get("stt_sample_rate") or settings.SARVAM_STT_SAMPLE_RATE),
            "input_audio_codec": provider_status.get("stt_audio_encoding") or settings.SARVAM_STT_AUDIO_ENCODING,
            "high_vad_sensitivity": "true",
            "vad_signals": "true",
            "flush_signal": "true",
        }
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}{urllib_parse.urlencode(query)}"

    @staticmethod
    async def connect_stt(tenant_res: TenantResources, *, language: str | None = None, sample_rate: int | None = None):
        api_key = await SarvamVoiceService.api_key(tenant_res, "stt")
        return await connect(
            SarvamVoiceService.stt_websocket_url(tenant_res, language=language, sample_rate=sample_rate),
            additional_headers={"Api-Subscription-Key": api_key},
            max_size=8 * 1024 * 1024,
        )

    @staticmethod
    async def send_stt_audio(stt_ws: Any, audio_bytes: bytes, *, sample_rate: int | None = None) -> None:
        if not audio_bytes:
            return
        await stt_ws.send(
            json.dumps(
                {
                    "audio": {
                        "data": base64.b64encode(audio_bytes).decode("ascii"),
                        "sample_rate": sample_rate or settings.SARVAM_STT_SAMPLE_RATE,
                        "encoding": settings.SARVAM_STT_AUDIO_ENCODING,
                    }
                }
            )
        )

    @staticmethod
    async def flush_stt(stt_ws: Any) -> None:
        for payload in ({"type": "flush"}, {"flush": True}):
            try:
                await stt_ws.send(json.dumps(payload))
                return
            except Exception:
                continue

    @staticmethod
    def parse_stt_message(raw: str | bytes) -> dict[str, Any] | None:
        try:
            payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except Exception:
            return None
        event_type = str(payload.get("type") or "")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        transcript = (
            data.get("transcript")
            or data.get("text")
            or payload.get("transcript")
            or payload.get("text")
            or ""
        )
        if not transcript and event_type not in {"speech_start", "speech_end", "data", "transcript"}:
            return None
        explicit_final = bool(
            data.get("is_final")
            or data.get("final")
            or data.get("finished")
            or payload.get("is_final")
            or payload.get("final")
        )
        explicit_partial = bool(
            data.get("is_partial")
            or data.get("partial")
            or payload.get("is_partial")
            or payload.get("partial")
        )
        # Sarvam emits incremental "data" frames during a single utterance. Treat
        # those as final ONLY when the payload doesn't explicitly flag them as
        # partial — otherwise we accept partials as finals and the dedup logic
        # downstream drops words mid-thought.
        if event_type == "transcript":
            is_final = True
        elif event_type == "data":
            is_final = (explicit_final or not explicit_partial) and bool(transcript)
        else:
            is_final = explicit_final
        language_code = data.get("language_code") or payload.get("language_code")
        return {
            "type": event_type or ("transcript" if transcript else "event"),
            "text": str(transcript).strip(),
            "is_final": bool(is_final and transcript),
            "language": SarvamVoiceService.normalize_language(language_code),
            # Per-segment language confidence — lets detect_spoken_language_switch
            # reject low-confidence flips. Carried through from the raw payload.
            "language_probability": data.get("language_probability")
            or payload.get("language_probability"),
            "raw": payload,
        }

    @staticmethod
    async def synthesize(
        tenant_res: TenantResources,
        text: str,
        *,
        language: str | None = None,
        pace: float | None = None,
        pitch: float | None = None,
        loudness: float | None = None,
        enable_cached_responses: bool | None = None,
    ) -> dict[str, Any]:
        if not text.strip():
            return {"audios": [], "audio_format": settings.SARVAM_TTS_AUDIO_CODEC}
        text = normalize_text_for_tts(text)
        provider_status = dict(tenant_res.provider_status or {})
        api_key = await SarvamVoiceService.api_key(tenant_res, "tts")
        endpoint = provider_status.get("sarvam_tts_rest_url") or provider_status.get("tts_rest_endpoint") or settings.SARVAM_TTS_REST_URL
        model = provider_status.get("sarvam_tts_model") or provider_status.get("tts_model") or settings.SARVAM_TTS_MODEL
        body: dict[str, Any] = {
            "text": text[:3500],
            "target_language_code": SarvamVoiceService.to_bcp47(language),
            "speaker": SarvamVoiceService.tts_speaker_for(language, provider_status),
            "model": model,
            "speech_sample_rate": int(provider_status.get("tts_sample_rate") or settings.SARVAM_TTS_SAMPLE_RATE),
            "enable_cached_responses": (
                bool(settings.SARVAM_TTS_ENABLE_CACHED_RESPONSES)
                if enable_cached_responses is None
                else bool(enable_cached_responses)
            ),
        }
        if model == "bulbul:v3":
            body["temperature"] = float(provider_status.get("sarvam_tts_temperature") or 0.6)
        # Per-tone prosody modulation. Sarvam Bulbul accepts pace (0.3-3.0),
        # pitch (-0.75-0.75), loudness (0.1-3.0); we clamp defensively. Older
        # bulbul versions / non-bulbul models may not accept these params,
        # so we add them and gracefully retry without them on 400.
        prosody_body: dict[str, Any] = {}
        pace = SarvamVoiceService.pace_for(language, pace)
        if pace is not None:
            prosody_body["pace"] = max(0.3, min(3.0, float(pace)))
        # Bulbul V3 supports ONLY pace — it rejects pitch/loudness with a 400.
        if model != "bulbul:v3":
            if pitch is not None:
                prosody_body["pitch"] = max(-0.75, min(0.75, float(pitch)))
            if loudness is not None:
                prosody_body["loudness"] = max(0.1, min(3.0, float(loudness)))
        body.update(prosody_body)
        client = SarvamVoiceService.http_client()
        response = await client.post(
            endpoint,
            headers={"api-subscription-key": api_key, "Content-Type": "application/json"},
            json=body,
            timeout=httpx.Timeout(30.0),
        )
        # Retry once on a 4xx: strip prosody params (older models reject
        # pace/pitch/loudness) AND reset to the global default speaker. A bad
        # per-language speaker id (a mis-configured SARVAM_TTS_SPEAKER_<LANG>)
        # must degrade to the default voice, never to silence — so the
        # per-language map is safe to A/B by ear. One retry covers both modes.
        default_speaker = settings.SARVAM_TTS_SPEAKER
        if response.status_code >= 400 and (prosody_body or body.get("speaker") != default_speaker):
            first_err = response.text[:300]
            logger.warning(f"NOKVO-TTS: Sarvam rejected request ({response.status_code}): {first_err!r}; retrying with default speaker / no prosody")
            retry_body = {k: v for k, v in body.items() if k not in prosody_body}
            retry_body["speaker"] = default_speaker
            response = await client.post(
                endpoint,
                headers={"api-subscription-key": api_key, "Content-Type": "application/json"},
                json=retry_body,
                timeout=httpx.Timeout(30.0),
            )
        if response.status_code >= 400:
            error_body = response.text[:300]
            logger.warning(f"NOKVO-TTS: Sarvam TTS failed ({response.status_code}): {error_body!r}")
            raise RuntimeError(f"Sarvam TTS failed ({response.status_code}): {error_body}")
        payload = response.json()
        return {
            "request_id": payload.get("request_id"),
            "audios": list(payload.get("audios") or []),
            "audio_format": settings.SARVAM_TTS_AUDIO_CODEC,
            "sample_rate": int(provider_status.get("tts_sample_rate") or settings.SARVAM_TTS_SAMPLE_RATE),
            "raw": payload,
        }

    @staticmethod
    async def synthesize_streaming(
        tenant_res: TenantResources,
        text: str,
        *,
        language: str | None = None,
        pace: float | None = None,
        pitch: float | None = None,
        loudness: float | None = None,
        enable_cached_responses: bool | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Streaming variant of :meth:`synthesize`.

        Hits Sarvam's ``/text-to-speech/stream`` endpoint and yields audio
        chunks as they arrive instead of waiting for the full payload. Each
        yielded dict has shape ``{"audio_base64": str, "sample_rate": int,
        "audio_format": str}``. First-chunk latency on Sarvam streaming is
        ~80-180ms vs ~250-500ms for the REST path — meaningful on the hot
        first-sentence path where every 100ms shows up at the caller's ear.

        Falls back to the REST path on any streaming-side error so a Sarvam
        streaming hiccup never silences the agent.
        """
        if not text.strip():
            return
        text = normalize_text_for_tts(text)
        provider_status = dict(tenant_res.provider_status or {})
        api_key = await SarvamVoiceService.api_key(tenant_res, "tts")
        stream_endpoint = (
            provider_status.get("sarvam_tts_stream_url")
            or settings.SARVAM_TTS_STREAM_URL
        )
        model = provider_status.get("sarvam_tts_model") or provider_status.get("tts_model") or settings.SARVAM_TTS_MODEL
        body: dict[str, Any] = {
            "text": text[:3500],
            "target_language_code": SarvamVoiceService.to_bcp47(language),
            "speaker": SarvamVoiceService.tts_speaker_for(language, provider_status),
            "model": model,
            "speech_sample_rate": int(provider_status.get("tts_sample_rate") or settings.SARVAM_TTS_SAMPLE_RATE),
            "enable_cached_responses": (
                bool(settings.SARVAM_TTS_ENABLE_CACHED_RESPONSES)
                if enable_cached_responses is None
                else bool(enable_cached_responses)
            ),
        }
        if model == "bulbul:v3":
            body["temperature"] = float(provider_status.get("sarvam_tts_temperature") or 0.6)
        prosody_body: dict[str, Any] = {}
        pace = SarvamVoiceService.pace_for(language, pace)
        if pace is not None:
            prosody_body["pace"] = max(0.3, min(3.0, float(pace)))
        # Bulbul V3 supports ONLY pace — it rejects pitch/loudness with a 400.
        if model != "bulbul:v3":
            if pitch is not None:
                prosody_body["pitch"] = max(-0.75, min(0.75, float(pitch)))
            if loudness is not None:
                prosody_body["loudness"] = max(0.1, min(3.0, float(loudness)))
        body.update(prosody_body)

        sample_rate = int(provider_status.get("tts_sample_rate") or settings.SARVAM_TTS_SAMPLE_RATE)
        audio_format = settings.SARVAM_TTS_AUDIO_CODEC

        client = SarvamVoiceService.http_client()
        async with client.stream(
            "POST",
            stream_endpoint,
            headers={"api-subscription-key": api_key, "Content-Type": "application/json"},
            json=body,
            timeout=httpx.Timeout(30.0, connect=4.0),
        ) as response:
            if response.status_code >= 400:
                # Pull the error body so the caller can fall back cleanly.
                err_body = (await response.aread()).decode("utf-8", errors="replace")[:300]
                raise RuntimeError(
                    f"Sarvam TTS streaming failed ({response.status_code}): {err_body}"
                )
            # Sarvam streams NDJSON (one JSON object per line). Each object
            # contains either an ``audios`` array (one or more base64 frames)
            # or a terminal ``status``/``error`` marker. Tolerant parsing so
            # an occasional malformed line doesn't kill the turn.
            buffer = ""
            async for chunk in response.aiter_text():
                buffer += chunk
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    # Tolerate SSE-style ``data: …`` prefixes too in case
                    # Sarvam returns them on this endpoint.
                    if line.startswith("data:"):
                        line = line[5:].strip()
                    if line == "[DONE]":
                        return
                    try:
                        payload = json.loads(line)
                    except (ValueError, TypeError):
                        continue
                    # A streamed line can be a bare JSON scalar (e.g. a number
                    # or "[DONE]"-like token) — guard so ``.get`` never hits an
                    # int/str ('int' object has no attribute 'get').
                    if not isinstance(payload, dict):
                        continue
                    audios = payload.get("audios") or []
                    for audio_b64 in audios:
                        if not audio_b64:
                            continue
                        yield {
                            "audio_base64": audio_b64,
                            "sample_rate": sample_rate,
                            "audio_format": audio_format,
                        }
            # Drain any trailing partial JSON line.
            tail = buffer.strip()
            if tail:
                if tail.startswith("data:"):
                    tail = tail[5:].strip()
                if tail and tail != "[DONE]":
                    try:
                        payload = json.loads(tail)
                        for audio_b64 in (payload.get("audios") or []) if isinstance(payload, dict) else []:
                            if audio_b64:
                                yield {
                                    "audio_base64": audio_b64,
                                    "sample_rate": sample_rate,
                                    "audio_format": audio_format,
                                }
                    except (ValueError, TypeError):
                        pass

    @staticmethod
    async def stream_sentence_tts(
        websocket: WebSocket,
        tenant_res: TenantResources,
        text: str,
        *,
        language: str | None = None,
        purpose: str = "answer",
        pace: float | None = None,
        pitch: float | None = None,
        loudness: float | None = None,
        enable_cached_responses: bool | None = None,
    ) -> dict[str, Any]:
        stream_id = f"sarvam-tts-{int(perf_counter() * 1000)}"
        started = perf_counter()
        try:
            await websocket.send_json({"type": "tts_started", "stream_id": stream_id, "purpose": purpose, "provider": "sarvam"})
        except Exception:
            pass

        first_audio_ms: int | None = None
        chunks_sent = 0
        sample_rate = int(
            (tenant_res.provider_status or {}).get("tts_sample_rate") or settings.SARVAM_TTS_SAMPLE_RATE
        )
        audio_format = settings.SARVAM_TTS_AUDIO_CODEC
        used_streaming = False
        fell_back = False
        # ``result`` is only populated on the REST path; default it so a clean
        # streaming success doesn't NameError on the merged return below.
        result: dict[str, Any] = {}
        # Per-sentence FIRST-audio deadline. The serial TTS pump means one slow
        # sentence stalls every sentence after it, and the streaming endpoint
        # otherwise relies only on httpx's 30s timeout — so a degraded stream can
        # hang the whole turn for seconds. Bound the wait for FIRST audio; once
        # audio is flowing we don't interrupt the sentence.
        _first_audio_deadline_s = max(
            0.1, settings.VOICE_TTS_STREAM_FIRST_AUDIO_DEADLINE_MS / 1000
        )

        # Try the streaming endpoint first — push each chunk to the WS as it
        # arrives so the caller hears the start of the sentence ~150-300ms
        # earlier than the REST path. Fall back to REST on any streaming error,
        # OR if first audio doesn't arrive within the deadline (degraded stream).
        _stream_gen = SarvamVoiceService.synthesize_streaming(
            tenant_res,
            text,
            language=language,
            pace=pace,
            pitch=pitch,
            loudness=loudness,
            enable_cached_responses=enable_cached_responses,
        )
        try:
            while True:
                if first_audio_ms is None:
                    chunk = await asyncio.wait_for(
                        _stream_gen.__anext__(), timeout=_first_audio_deadline_s
                    )
                else:
                    chunk = await _stream_gen.__anext__()
                used_streaming = True
                audio = chunk.get("audio_base64")
                if not audio:
                    continue
                if first_audio_ms is None:
                    first_audio_ms = int((perf_counter() - started) * 1000)
                    try:
                        await websocket.send_json(
                            {
                                "type": "tts_first_audio",
                                "stream_id": stream_id,
                                "purpose": purpose,
                                "first_audio_latency_ms": first_audio_ms,
                                "provider": "sarvam",
                                "audio_format": chunk.get("audio_format", audio_format),
                                "sample_rate": chunk.get("sample_rate", sample_rate),
                                "streaming": True,
                            }
                        )
                    except Exception:
                        pass
                try:
                    await websocket.send_json(
                        {
                            "type": "tts_audio",
                            "stream_id": stream_id,
                            "purpose": purpose,
                            "audio_base64": audio,
                            "audio_format": chunk.get("audio_format", audio_format),
                            "sample_rate": chunk.get("sample_rate", sample_rate),
                            "audio_end": False,
                        }
                    )
                    chunks_sent += 1
                except Exception:
                    pass
        except StopAsyncIteration:
            pass
        except BaseException as exc:
            # Deadline exceeded (degraded stream) OR a streaming-side error —
            # in either case abandon streaming and let the REST fallback try.
            if isinstance(exc, asyncio.TimeoutError):
                logger.warning(
                    "NOKVO-TTS: Sarvam streaming first-audio exceeded %dms deadline; REST fallback",
                    settings.VOICE_TTS_STREAM_FIRST_AUDIO_DEADLINE_MS,
                )
            elif isinstance(exc, asyncio.CancelledError):
                # Real cancellation (barge-in) — close the stream and re-raise.
                try:
                    await _stream_gen.aclose()
                except Exception:
                    pass
                raise
            else:
                logger.warning(
                    "NOKVO-TTS: Sarvam streaming TTS failed (%s); falling back to REST", exc
                )
            try:
                await _stream_gen.aclose()
            except Exception:
                pass
            fell_back = True
            used_streaming = False
            chunks_sent = 0
            first_audio_ms = None

        # REST fallback — same shape as the previous implementation. Fires
        # when streaming raised OR returned no chunks (some Sarvam models
        # ignore the /stream endpoint for short utterances).
        if not used_streaming or chunks_sent == 0:
            result = await SarvamVoiceService.synthesize(
                tenant_res,
                text,
                language=language,
                pace=pace,
                pitch=pitch,
                loudness=loudness,
                enable_cached_responses=enable_cached_responses,
            )
            for audio in result.get("audios") or []:
                if not audio:
                    continue
                if first_audio_ms is None:
                    first_audio_ms = int((perf_counter() - started) * 1000)
                    try:
                        await websocket.send_json(
                            {
                                "type": "tts_first_audio",
                                "stream_id": stream_id,
                                "purpose": purpose,
                                "first_audio_latency_ms": first_audio_ms,
                                "provider": "sarvam",
                                "audio_format": result.get("audio_format"),
                                "sample_rate": result.get("sample_rate"),
                                "streaming": False,
                            }
                        )
                    except Exception:
                        pass
                try:
                    await websocket.send_json(
                        {
                            "type": "tts_audio",
                            "stream_id": stream_id,
                            "purpose": purpose,
                            "audio_base64": audio,
                            "audio_format": result.get("audio_format"),
                            "sample_rate": result.get("sample_rate"),
                            "audio_end": True,
                        }
                    )
                    chunks_sent += 1
                except Exception:
                    pass

        # Send a terminal marker so the frontend playback scheduler knows
        # this sentence is complete (streaming mode emits multiple chunks
        # with audio_end=False then one tts_end frame here).
        if used_streaming and chunks_sent:
            try:
                await websocket.send_json(
                    {
                        "type": "tts_audio",
                        "stream_id": stream_id,
                        "purpose": purpose,
                        "audio_base64": "",
                        "audio_format": audio_format,
                        "sample_rate": sample_rate,
                        "audio_end": True,
                    }
                )
            except Exception:
                pass
        try:
            await websocket.send_json(
                {
                    "type": "tts_done",
                    "stream_id": stream_id,
                    "purpose": purpose,
                    "provider": "sarvam",
                    "first_audio_latency_ms": first_audio_ms,
                }
            )
        except Exception:
            pass
        # Per-sentence TTS latency — previously a blind spot (TTS isn't traced to
        # LangSmith), which hid multi-second stalls behind a healthy LLM. mode =
        # which path actually produced audio; fell_back flags a streaming→REST
        # switch (deadline or error). This is what makes a TTS stall attributable.
        total_ms = int((perf_counter() - started) * 1000)
        logger.info(
            "NOKVO-TTS-LATENCY: purpose=%s lang=%s mode=%s fell_back=%s "
            "first_audio_ms=%s total_ms=%d chars=%d chunks=%d",
            purpose, language or "-", "stream" if used_streaming else "rest",
            fell_back, first_audio_ms, total_ms, len(text or ""), chunks_sent,
        )
        return {"stream_id": stream_id, "first_audio_ms": first_audio_ms, **result}
