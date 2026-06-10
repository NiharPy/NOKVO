"""Shared LLM pool — the "cupcake-box" budgeter.

Replaces per-tenant Azure OpenAI provisioning with a small pool of global
GPT-5-mini Azure OpenAI deployments shared by ALL tenants. A *centralized*
per-key token budget lives in Redis so every worker/tenant draws from the same
counters:

    Think of each key as a box of cupcakes (its tokens-per-minute budget). Taking
    cupcakes (tokens) decrements the box's counter. When a box hits 0 everyone
    skips it and moves to the next box until it's restacked — the 60s window
    expires and the budget refills.

Selection + decrement is a single atomic Lua script so concurrent workers can't
oversubscribe a box. A reservation uses an *estimate*; after the call we
reconcile against the real `usage.total_tokens`. A 429 from a key zeroes its box
for the rest of the window (cooldown).
"""
from __future__ import annotations

import json
import logging
import random
import hashlib
import random
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Optional
from urllib import parse as urllib_parse

import httpx
import redis.asyncio as redis

from app.core.config import settings

logger = logging.getLogger("nokvo_one.llm_pool")

_BUDGET_PREFIX = "llm_pool:budget"

# Sticky routing: the active call's id, set once per call at the turn boundary.
# reserve() hashes it to a stable "home" box so every turn of a call hits the
# same Azure deployment → the provider's per-deployment prompt cache keeps
# hitting. asyncio tasks copy the context, so descendant LLM calls inherit it.
_call_id_var: ContextVar[Optional[str]] = ContextVar("llm_pool_call_id", default=None)


def set_call_id(call_id: str | None) -> None:
    """Set the sticky-routing key for the current call (and its child tasks)."""
    _call_id_var.set(call_id or None)


def _sticky_start(n: int, sticky_key: str | None) -> int:
    """Deterministic home-box index for ``sticky_key`` (stable across processes —
    blake2b, NOT the per-process-salted builtin hash). Random when no key."""
    if n <= 1:
        return 0
    if sticky_key:
        digest = hashlib.blake2b(sticky_key.encode("utf-8"), digest_size=8).hexdigest()
        return int(digest, 16) % n
    return random.randrange(n)

# Atomic: walk the boxes from a rotating start, return the first KEY_ID whose
# remaining budget >= estimate (seeding a fresh window to its TPM on first
# touch), decrement it, and set the window TTL. Returns "" when all boxes are
# empty. KEYS = budget keys (one per box, in order); ARGV = [est, window_s,
# start_idx, tpm0, tpm1, ...].
_RESERVE_LUA = """
local est = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local start = tonumber(ARGV[3])
local n = #KEYS
for off = 0, n - 1 do
  local i = ((start + off) % n) + 1
  local cap = tonumber(ARGV[3 + i])
  local cur = redis.call('GET', KEYS[i])
  local remaining
  if cur == false then remaining = cap else remaining = tonumber(cur) end
  if remaining >= est then
    redis.call('SET', KEYS[i], remaining - est)
    redis.call('EXPIRE', KEYS[i], window)
    return i - 1
  end
end
return -1
"""


@dataclass(frozen=True)
class PoolMember:
    key_id: str
    endpoint: str
    api_key: str
    deployment: str
    tpm: int


def _parse_pool_json(raw: str, default_model: str) -> list[PoolMember]:
    members: list[PoolMember] = []
    raw = (raw or "").strip()
    if not raw:
        return members
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            for i, m in enumerate(parsed):
                if not isinstance(m, dict):
                    continue
                endpoint = str(m.get("endpoint") or "").rstrip("/")
                api_key = str(m.get("api_key") or "")
                if not endpoint or not api_key:
                    continue
                members.append(
                    PoolMember(
                        key_id=str(m.get("key_id") or f"pool-{i}"),
                        endpoint=endpoint,
                        api_key=api_key,
                        deployment=str(m.get("deployment") or default_model),
                        tpm=int(m.get("tpm") or settings.LLM_POOL_DEFAULT_TPM),
                    )
                )
    except Exception:
        logger.exception("Failed to parse pool JSON; ignoring")
    return members


def _append_box(members: list[PoolMember], *, key_id, endpoint, api_key, deployment) -> None:
    """Append a single configured account as a pool box (de-duped by endpoint),
    so pool JSON *augments* the standalone account rather than replacing it."""
    if not (endpoint and api_key):
        return
    ep = endpoint.rstrip("/")
    if not any(m.endpoint == ep for m in members):
        members.append(PoolMember(key_id=key_id, endpoint=ep, api_key=api_key,
                                  deployment=deployment, tpm=settings.LLM_POOL_DEFAULT_TPM))


def _members(pool: str = "mini") -> list[PoolMember]:
    """Build the member list for a named pool.

    mini → AZURE_OPENAI_POOL_JSON + the global account as a box.
    nano → AZURE_OPENAI_NANO_POOL_JSON + the single AZURE_OPENAI_NANO_* as a box.
    """
    if pool == "nano":
        members = _parse_pool_json(settings.AZURE_OPENAI_NANO_POOL_JSON, settings.AZURE_OPENAI_NANO_DEPLOYMENT)
        _append_box(members, key_id="nano", endpoint=settings.AZURE_OPENAI_NANO_ENDPOINT,
                    api_key=settings.AZURE_OPENAI_NANO_API_KEY, deployment=settings.AZURE_OPENAI_NANO_DEPLOYMENT)
        return members
    members = _parse_pool_json(settings.AZURE_OPENAI_POOL_JSON, settings.AZURE_OPENAI_POOL_MODEL)
    _append_box(members, key_id="global", endpoint=settings.AZURE_OPENAI_GLOBAL_ENDPOINT,
                api_key=settings.AZURE_OPENAI_GLOBAL_API_KEY, deployment=settings.AZURE_OPENAI_GLOBAL_DEPLOYMENT)
    return members


class LLMPoolError(RuntimeError):
    """Raised when no pool member has capacity / is configured."""


class LLMPool:
    """Centralized, Redis-backed token-budget over the pool members."""

    _redis: redis.Redis | None = None
    _members_cache: dict[str, list[PoolMember]] = {}
    _MAX_RETRY_WAIT = 1.5
    _RETRY_BASE = 0.25

    # ── infra ────────────────────────────────────────────────────────────────
    @classmethod
    def client(cls) -> redis.Redis:
        if cls._redis is None:
            cls._redis = redis.from_url(settings.REDIS_URL, decode_responses=True)
        return cls._redis

    @classmethod
    def members(cls, pool: str = "mini") -> list[PoolMember]:
        if pool not in cls._members_cache:
            cls._members_cache[pool] = _members(pool)
        return cls._members_cache[pool]

    @classmethod
    def reset_cache(cls) -> None:
        """Test hook — re-read members for all pools."""
        cls._members_cache = {}

    @staticmethod
    def _window_key(key_id: str, *, pool: str = "mini", now: float | None = None) -> str:
        minute = int((now if now is not None else time.time()) // settings.LLM_POOL_WINDOW_SECONDS)
        return f"{_BUDGET_PREFIX}:{pool}:{key_id}:{minute}"

    # ── budget ops ─────────────────────────────────────────────────────────────
    @classmethod
    async def reserve(cls, est_tokens: int, *, pool: str = "mini", sticky: bool = True) -> Optional[PoolMember]:
        """Atomically reserve ``est_tokens`` from a box with capacity.

        With ``sticky`` (default), the search STARTS at the call's hashed home box
        (so every turn of a call hits the same deployment → prompt cache hits) and
        only walks to other boxes if home is at its TPM cap. Returns the chosen
        member, or None when every box is empty.
        """
        members = cls.members(pool)
        if not members:
            raise LLMPoolError(f"No LLM pool members configured for pool '{pool}'")
        est = max(1, int(est_tokens))
        keys = [cls._window_key(m.key_id, pool=pool) for m in members]
        start = _sticky_start(len(members), _call_id_var.get() if sticky else None)
        argv = [est, settings.LLM_POOL_WINDOW_SECONDS, start] + [m.tpm for m in members]
        try:
            idx = await cls.client().eval(_RESERVE_LUA, len(keys), *keys, *argv)
        except Exception:
            logger.exception("LLM pool reserve failed")
            return None
        idx = int(idx)
        if idx < 0:
            return None
        return members[idx]

    @classmethod
    async def reconcile(cls, member: PoolMember, est_tokens: int, actual_tokens: int, *, pool: str = "mini") -> None:
        """Adjust the reservation to the real usage: give budget back when the
        call used fewer tokens than estimated, take more when it used more."""
        delta = int(est_tokens) - int(actual_tokens)  # >0 → refund, <0 → take more
        if delta == 0:
            return
        key = cls._window_key(member.key_id, pool=pool)
        try:
            await cls.client().incrby(key, delta)
        except Exception:
            logger.debug("LLM pool reconcile failed for %s", member.key_id)

    @classmethod
    async def cooldown(cls, member: PoolMember, *, pool: str = "mini") -> None:
        """Zero a box for the rest of the window (e.g. after a 429)."""
        key = cls._window_key(member.key_id, pool=pool)
        try:
            await cls.client().set(key, 0, ex=settings.LLM_POOL_WINDOW_SECONDS)
        except Exception:
            logger.debug("LLM pool cooldown failed for %s", member.key_id)


class LLMPoolClient:
    """Thin chat client over the pool — what every call site uses."""

    _http: httpx.AsyncClient | None = None

    @classmethod
    def http(cls) -> httpx.AsyncClient:
        if cls._http is None or cls._http.is_closed:
            cls._http = httpx.AsyncClient(
                timeout=httpx.Timeout(8.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return cls._http

    @staticmethod
    def _estimate_tokens(messages: list[dict[str, str]], max_tokens: int) -> int:
        # ~4 chars/token for the prompt + the requested completion budget.
        chars = sum(len(str(m.get("content") or "")) for m in messages)
        return max(1, chars // 4) + int(max_tokens)

    @staticmethod
    def _url(member: PoolMember) -> str:
        api_version = urllib_parse.quote(settings.AZURE_OPENAI_POOL_API_VERSION.strip())
        deployment = urllib_parse.quote(member.deployment)
        if "/openai/deployments/" in member.endpoint:
            sep = "" if "api-version=" in member.endpoint else f"?api-version={api_version}"
            return f"{member.endpoint}{sep}"
        return f"{member.endpoint}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"

    @classmethod
    async def chat(
        cls,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 120,
        temperature: float = 0.3,
        est_tokens: int | None = None,
    ) -> str:
        """Reserve a box → call its deployment → on 429 cooldown + try the next →
        reconcile usage. Returns the assistant text. Raises ``LLMPoolError`` only
        when the whole pool is saturated/unconfigured."""
        members = LLMPool.members()
        if not members:
            raise LLMPoolError("No LLM pool members configured")
        estimate = est_tokens if est_tokens is not None else cls._estimate_tokens(messages, max_tokens)
        body = {"messages": messages, "temperature": temperature, "max_tokens": max_tokens}

        tried: set[str] = set()
        deadline = time.time() + LLMPool._MAX_RETRY_WAIT
        attempt = 0
        while True:
            member = await LLMPool.reserve(estimate)
            if member is None:
                # All boxes empty — brief wait for the window to refill, then retry.
                if time.time() >= deadline:
                    raise LLMPoolError("LLM pool saturated — all keys at TPM cap")
                await _sleep(min(0.2, deadline - time.time()))
                continue
            if member.key_id in tried and len(tried) >= len(members):
                raise LLMPoolError("LLM pool saturated — all keys failing")
            tried.add(member.key_id)
            try:
                resp = await cls.http().post(
                    cls._url(member),
                    headers={"api-key": member.api_key, "Content-Type": "application/json"},
                    json=body,
                )
                if resp.status_code == 429:
                    await LLMPool.cooldown(member)
                    attempt += 1
                    continue
                resp.raise_for_status()
                payload = resp.json()
                actual = int(((payload.get("usage") or {}).get("total_tokens")) or estimate)
                await LLMPool.reconcile(member, estimate, actual)
                choices = payload.get("choices") or []
                content = ((choices[0] or {}).get("message") or {}).get("content") if choices else None
                return (content or "").strip()
            except httpx.HTTPStatusError as exc:
                await LLMPool.reconcile(member, estimate, 0)  # refund the reservation
                if exc.response is not None and exc.response.status_code == 429:
                    await LLMPool.cooldown(member)
                    attempt += 1
                    continue
                raise
            except Exception:
                await LLMPool.reconcile(member, estimate, 0)
                raise


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(max(0.0, seconds))
