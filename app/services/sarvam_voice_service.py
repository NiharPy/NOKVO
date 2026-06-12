from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import base64
import json
from time import perf_counter
from typing import Any, AsyncIterator
from urllib import parse as urllib_parse

import httpx
from fastapi import WebSocket
from websockets.asyncio.client import connect

from app.core.config import settings
from app.models.tenant_resources import TenantResources
from app.services.azure_keyvault_service import AzureKeyVaultService


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
        provider_status = dict(tenant_res.provider_status or {})
        api_key = await SarvamVoiceService.api_key(tenant_res, "tts")
        endpoint = provider_status.get("sarvam_tts_rest_url") or provider_status.get("tts_rest_endpoint") or settings.SARVAM_TTS_REST_URL
        model = provider_status.get("sarvam_tts_model") or provider_status.get("tts_model") or settings.SARVAM_TTS_MODEL
        body: dict[str, Any] = {
            "text": text[:3500],
            "target_language_code": SarvamVoiceService.to_bcp47(language),
            "speaker": provider_status.get("sarvam_tts_speaker") or provider_status.get("tts_voice") or settings.SARVAM_TTS_SPEAKER,
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
        # Retry once without prosody params if the server rejects the
        # request — saves the entire turn from going silent when a
        # tenant is on a model that doesn't support pace/pitch/loudness.
        if response.status_code >= 400 and prosody_body:
            first_err = response.text[:300]
            logger.warning(f"NOKVO-TTS: Sarvam rejected prosody params ({response.status_code}): {first_err!r}; retrying without prosody")
            retry_body = {k: v for k, v in body.items() if k not in prosody_body}
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
            "speaker": provider_status.get("sarvam_tts_speaker") or provider_status.get("tts_voice") or settings.SARVAM_TTS_SPEAKER,
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

        # Try the streaming endpoint first — push each chunk to the WS as it
        # arrives so the caller hears the start of the sentence ~150-300ms
        # earlier than the REST path. Fall back to REST on any streaming
        # error so a Sarvam streaming blip never silences the turn.
        try:
            async for chunk in SarvamVoiceService.synthesize_streaming(
                tenant_res,
                text,
                language=language,
                pace=pace,
                pitch=pitch,
                loudness=loudness,
                enable_cached_responses=enable_cached_responses,
            ):
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
        except Exception as exc:
            logger.warning(
                "NOKVO-TTS: Sarvam streaming TTS failed (%s); falling back to REST",
                exc,
            )
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
        return {"stream_id": stream_id, "first_audio_ms": first_audio_ms, **result}
