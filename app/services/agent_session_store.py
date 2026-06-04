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
        call_context: str | None = None,
    ) -> str:
        normalized = _clean(query)
        words = sorted({word for word in normalized.split() if len(word) > 2})
        signature = " ".join(words) if words else normalized
        scope_parts = [f"campaign:{campaign_id}" if campaign_id else "tenant"]
        if call_context:
            scope_parts.append(f"call:{cls._hash(str(call_context))}")
        scope = ":".join(scope_parts)
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
        call_context: str | None = None,
    ) -> dict[str, Any] | None:
        if not settings.AGENT_ANSWER_CACHE_ENABLED:
            return None
        try:
            raw = await cls.client().get(
                cls.semantic_cache_key(
                    tenant_res,
                    query,
                    language,
                    campaign_id=campaign_id,
                    call_context=call_context,
                )
            )
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
        call_context: str | None = None,
    ) -> None:
        if not settings.AGENT_ANSWER_CACHE_ENABLED:
            return
        try:
            await cls.client().setex(
                cls.semantic_cache_key(
                    tenant_res,
                    query,
                    language,
                    campaign_id=campaign_id,
                    call_context=call_context,
                ),
                int(settings.AGENT_ANSWER_CACHE_TTL_SECONDS),
                json.dumps(payload, ensure_ascii=False),
            )
        except Exception:
            return

    @classmethod
    def session_key(cls, tenant_res: TenantResources, call_id: str) -> str:
        """Legacy ``:history`` key path. The unified store writes it via
        :func:`session_state_v2.save_state` for dual-write rollback safety,
        so the path stays valid even though ``get_history`` now reads from
        the unified blob."""
        return f"{cls.namespace(tenant_res)}:agent:call:{call_id}:history"

    # ─── Unified-store facade ─────────────────────────────────────────────
    #
    # Every classmethod below now routes through ``session_state_v2``. The
    # public method shapes are unchanged so the 23+ call sites in
    # ``nokvo_one_voice_pipeline.py`` + ``nokvo_one_voice_stream_service.py``
    # compile without edits. ``mutate_state`` provides the per-call lock
    # that the old read-modify-write pattern was missing — fixing the
    # lost-update race between concurrent ``merge_state`` and
    # ``save_memory`` writers in the same turn.

    @classmethod
    async def get_history(cls, tenant_res: TenantResources, call_id: str | None) -> list[dict[str, str]]:
        if not call_id:
            return []
        try:
            from app.services.session_state_v2 import load_state as _load_state

            state = await _load_state(cls.client(), cls.namespace(tenant_res), call_id)
        except Exception:
            return []
        max_turns = int(settings.AGENT_SESSION_HISTORY_MAX_TURNS)
        return [
            {"role": turn.role, "content": turn.content}
            for turn in state.history[-max_turns:]
        ]

    @classmethod
    async def append_turn(
        cls,
        tenant_res: TenantResources,
        call_id: str | None,
        user_text: str,
        answer: str,
    ) -> None:
        if not call_id:
            return
        max_turns = int(settings.AGENT_SESSION_HISTORY_MAX_TURNS)
        try:
            from app.services.session_state_v2 import (
                HistoryTurn,
                SessionState,
                mutate_state,
            )
        except Exception:
            return

        def _append(state: SessionState) -> None:
            turn_idx = state.next_turn_index
            state.history.append(
                HistoryTurn(role="user", content=user_text[:2000], turn_index=turn_idx)
            )
            state.history.append(
                HistoryTurn(role="assistant", content=answer[:2000], turn_index=turn_idx)
            )
            state.next_turn_index = turn_idx + 1
            # Bound the history exactly like the legacy code did.
            if len(state.history) > max_turns:
                state.history = state.history[-max_turns:]

        try:
            await mutate_state(
                cls.client(),
                cls.namespace(tenant_res),
                call_id,
                _append,
                dual_write=settings.SESSION_STATE_V2_DUAL_WRITE,
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
        """Replace the state blob.

        Historic semantics: this was a full overwrite of the Redis ``:state``
        key. The unified facade preserves that — but instead of clobbering
        ``next_turn_index`` / ``history`` / ``markers`` along with the
        targeted fields, ``set_state`` now performs a merge-style replace:
        the keys present in ``data`` are written, every OTHER field of
        :class:`SessionState` (history, next_turn_index, etc.) is preserved.
        The three legitimate session-start callers (status / language /
        campaign_id init in stream_service line 2056, identity_verified flag
        in pipeline) all pass tiny partial dicts and depend on this
        preservation today; without it, ``set_state`` at turn 1 would wipe
        the bootstrapped caller-memory facts.
        """
        if not call_id:
            return
        try:
            from app.services.session_state_v2 import (
                SessionState,
                apply_legacy_patch,
                mutate_state,
            )
        except Exception:
            return

        def _apply(state: SessionState) -> None:
            apply_legacy_patch(state, data)

        try:
            await mutate_state(
                cls.client(),
                cls.namespace(tenant_res),
                call_id,
                _apply,
                ttl_seconds=ttl_seconds,
                dual_write=settings.SESSION_STATE_V2_DUAL_WRITE,
            )
        except Exception:
            return

    @classmethod
    async def get_state(cls, tenant_res: TenantResources, call_id: str | None) -> dict[str, Any]:
        if not call_id:
            return {}
        try:
            from app.services.session_state_v2 import load_state as _load_state

            state = await _load_state(cls.client(), cls.namespace(tenant_res), call_id)
        except Exception:
            return {}
        return state.to_legacy_state_blob()

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
        try:
            from app.services.session_state_v2 import (
                SessionState,
                apply_legacy_patch,
                mutate_state,
            )
        except Exception:
            return {}

        def _apply(state: SessionState) -> None:
            apply_legacy_patch(state, patch)

        try:
            state = await mutate_state(
                cls.client(),
                cls.namespace(tenant_res),
                call_id,
                _apply,
                ttl_seconds=ttl_seconds,
                dual_write=settings.SESSION_STATE_V2_DUAL_WRITE,
            )
        except Exception:
            return {}
        return state.to_legacy_state_blob()
