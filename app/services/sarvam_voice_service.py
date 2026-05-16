from __future__ import annotations

import base64
import json
from time import perf_counter
from typing import Any
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
        if settings.SARVAM_API_KEY:
            return settings.SARVAM_API_KEY
        raise RuntimeError("Sarvam API key is not configured.")

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
        async with httpx.AsyncClient(timeout=httpx.Timeout(35.0)) as client:
            response = await client.post(
                endpoint,
                headers={"api-subscription-key": api_key},
                data=data,
                files=files,
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
        is_final = event_type in {"data", "transcript"} or bool(
            data.get("is_final")
            or data.get("final")
            or data.get("finished")
            or payload.get("is_final")
            or payload.get("final")
        )
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
            "enable_cached_responses": bool(settings.SARVAM_TTS_ENABLE_CACHED_RESPONSES),
        }
        if model == "bulbul:v3":
            body["temperature"] = float(provider_status.get("sarvam_tts_temperature") or 0.6)
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            response = await client.post(
                endpoint,
                headers={"api-subscription-key": api_key, "Content-Type": "application/json"},
                json=body,
            )
            if response.status_code >= 400:
                raise RuntimeError(f"Sarvam TTS failed ({response.status_code}): {response.text[:300]}")
            payload = response.json()
        return {
            "request_id": payload.get("request_id"),
            "audios": list(payload.get("audios") or []),
            "audio_format": settings.SARVAM_TTS_AUDIO_CODEC,
            "sample_rate": int(provider_status.get("tts_sample_rate") or settings.SARVAM_TTS_SAMPLE_RATE),
            "raw": payload,
        }

    @staticmethod
    async def stream_sentence_tts(
        websocket: WebSocket,
        tenant_res: TenantResources,
        text: str,
        *,
        language: str | None = None,
        purpose: str = "answer",
    ) -> dict[str, Any]:
        stream_id = f"sarvam-tts-{int(perf_counter() * 1000)}"
        started = perf_counter()
        try:
            await websocket.send_json({"type": "tts_started", "stream_id": stream_id, "purpose": purpose, "provider": "sarvam"})
        except Exception:
            pass
        result = await SarvamVoiceService.synthesize(tenant_res, text, language=language)
        first_audio_ms: int | None = None
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
