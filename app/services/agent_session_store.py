from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import redis.asyncio as redis

from app.core.config import settings
from app.models.tenant_resources import TenantResources


def _clean(value: str) -> str:
    value = re.sub(r"[^\w\s]", " ", (value or "").lower())
    return re.sub(r"\s+", " ", value).strip()


class AgentSessionStore:
    _client: redis.Redis | None = None

    @classmethod
    def client(cls) -> redis.Redis:
        if cls._client is None:
            cls._client = redis.from_url(settings.REDIS_URL, decode_responses=True)
        return cls._client

    @staticmethod
    def namespace(tenant_res: TenantResources) -> str:
        return tenant_res.redis_namespace or f"tenant:{tenant_res.tenant_id}"

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _policy_version(tenant_res: TenantResources) -> str:
        provider_status = dict(tenant_res.provider_status or {})
        base_version = str(provider_status.get("agent_policy_version") or "pv_default")
        prompt_config = provider_status.get("single_prompt_voice_agent") or {}
        if not isinstance(prompt_config, dict):
            return base_version
        prompt_enabled = bool(prompt_config.get("enabled") and prompt_config.get("prompt"))
        if not prompt_enabled:
            return base_version
        marker = str(prompt_config.get("updated_at") or "")
        if not marker:
            marker = hashlib.sha256(str(prompt_config.get("prompt") or "").encode("utf-8")).hexdigest()[:12]
        if not marker:
            return base_version
        return f"{base_version}:single_prompt:1:{marker}"

    @classmethod
    def semantic_cache_key(
        cls,
        tenant_res: TenantResources,
        query: str,
        language: str,
        *,
        campaign_id: str | None = None,
    ) -> str:
        normalized = _clean(query)
        words = sorted({word for word in normalized.split() if len(word) > 2})
        signature = " ".join(words) if words else normalized
        scope = f"campaign:{campaign_id}" if campaign_id else "tenant"
        return (
            f"{cls.namespace(tenant_res)}:agent:semantic_cache:v1:"
            f"{cls._policy_version(tenant_res)}:{scope}:{language}:{cls._hash(signature)}"
        )

    @classmethod
    async def get_cached_answer(
        cls,
        tenant_res: TenantResources,
        query: str,
        language: str,
        *,
        campaign_id: str | None = None,
    ) -> dict[str, Any] | None:
        if not settings.AGENT_ANSWER_CACHE_ENABLED:
            return None
        try:
            raw = await cls.client().get(cls.semantic_cache_key(tenant_res, query, language, campaign_id=campaign_id))
            return json.loads(raw) if raw else None
        except Exception:
            return None

    @classmethod
    async def set_cached_answer(
        cls,
        tenant_res: TenantResources,
        query: str,
        language: str,
        payload: dict[str, Any],
        *,
        campaign_id: str | None = None,
    ) -> None:
        if not settings.AGENT_ANSWER_CACHE_ENABLED:
            return
        try:
            await cls.client().setex(
                cls.semantic_cache_key(tenant_res, query, language, campaign_id=campaign_id),
                int(settings.AGENT_ANSWER_CACHE_TTL_SECONDS),
                json.dumps(payload, ensure_ascii=False),
            )
        except Exception:
            return

    @classmethod
    def session_key(cls, tenant_res: TenantResources, call_id: str) -> str:
        return f"{cls.namespace(tenant_res)}:agent:call:{call_id}:history"

    @classmethod
    async def get_history(cls, tenant_res: TenantResources, call_id: str | None) -> list[dict[str, str]]:
        if not call_id:
            return []
        try:
            raw = await cls.client().get(cls.session_key(tenant_res, call_id))
            value = json.loads(raw) if raw else []
            if isinstance(value, list):
                max_turns = int(settings.AGENT_SESSION_HISTORY_MAX_TURNS)
                return [
                    {"role": str(item.get("role") or ""), "content": str(item.get("content") or "")}
                    for item in value
                    if isinstance(item, dict)
                ][-max_turns:]
        except Exception:
            return []
        return []

    @classmethod
    async def append_turn(cls, tenant_res: TenantResources, call_id: str | None, user_text: str, answer: str) -> None:
        if not call_id:
            return
        history = await cls.get_history(tenant_res, call_id)
        history.extend(
            [
                {"role": "user", "content": user_text[:2000]},
                {"role": "assistant", "content": answer[:2000]},
            ]
        )
        max_turns = int(settings.AGENT_SESSION_HISTORY_MAX_TURNS)
        history = history[-max_turns:]
        try:
            await cls.client().setex(
                cls.session_key(tenant_res, call_id),
                int(settings.AGENT_SESSION_HISTORY_TTL_SECONDS),
                json.dumps(history, ensure_ascii=False),
            )
        except Exception:
            return

    @classmethod
    async def set_state(
        cls,
        tenant_res: TenantResources,
        call_id: str,
        data: dict[str, Any],
        *,
        ttl_seconds: int = 900,
    ) -> None:
        try:
            await cls.client().setex(
                f"{cls.namespace(tenant_res)}:agent:call:{call_id}:state",
                ttl_seconds,
                json.dumps(data, ensure_ascii=False),
            )
        except Exception:
            return

    @classmethod
    async def get_state(cls, tenant_res: TenantResources, call_id: str | None) -> dict[str, Any]:
        if not call_id:
            return {}
        try:
            raw = await cls.client().get(f"{cls.namespace(tenant_res)}:agent:call:{call_id}:state")
            value = json.loads(raw) if raw else {}
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    @classmethod
    async def merge_state(
        cls,
        tenant_res: TenantResources,
        call_id: str | None,
        patch: dict[str, Any],
        *,
        ttl_seconds: int = 900,
    ) -> dict[str, Any]:
        if not call_id or not isinstance(patch, dict) or not patch:
            return {}
        state = await cls.get_state(tenant_res, call_id)
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(state.get(key), dict):
                merged = dict(state[key])
                merged.update(value)
                state[key] = merged
            else:
                state[key] = value
        await cls.set_state(tenant_res, call_id, state, ttl_seconds=ttl_seconds)
        return state
