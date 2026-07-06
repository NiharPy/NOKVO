"""Redis session state for Nova (the APEX in-product assistant).

One JSON blob per chat session, keyed ``nova:{tenant_namespace}:{session_id}``,
TTL-refreshed on every write (30 min of inactivity ends the session). The blob
holds the chat history, the in-progress campaign draft, and at most one
``pending_action`` (a side effect awaiting the user's explicit Confirm click).

Mutations run under a short Redis lock (SET NX, same discipline as
``session_state_v2.mutate_state``) so a double-clicked Confirm and a concurrent
chat turn can't interleave a read-modify-write — consuming an ``action_id`` is
therefore atomic: the first confirm wins, the second finds it gone.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Callable

from app.core.config import settings
from app.services.agent_session_store import AgentSessionStore

logger = logging.getLogger(__name__)

SESSION_TTL_S = 1800          # 30 min inactivity
HISTORY_MAX_TURNS = 30        # user+assistant pairs kept
CONTENT_CAP = 4000            # per-message char cap stored in history

_LOCK_TTL_S = 5
_LOCK_RETRIES = 20
_LOCK_RETRY_DELAY_S = 0.1


def new_session_id() -> str:
    return uuid.uuid4().hex


def _key(tenant_namespace: str, session_id: str) -> str:
    return f"nova:{tenant_namespace}:{session_id}"


def empty_state() -> dict[str, Any]:
    return {"history": [], "draft": {}, "pending_action": None}


async def load(tenant_namespace: str, session_id: str) -> dict[str, Any]:
    """Best-effort read; an unknown/expired session is just a fresh one."""
    try:
        raw = await AgentSessionStore.client().get(_key(tenant_namespace, session_id))
        if raw:
            state = json.loads(raw)
            if isinstance(state, dict):
                return {**empty_state(), **state}
    except Exception:
        logger.exception("NOVA: session load failed (starting fresh)")
    return empty_state()


async def mutate(
    tenant_namespace: str,
    session_id: str,
    fn: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """Lock → load → apply ``fn`` in place → save (TTL refreshed) → unlock.
    Returns the post-mutation state. Raises only on a lock that never frees
    (surface as a 503 at the API — the panel retries)."""
    client = AgentSessionStore.client()
    key = _key(tenant_namespace, session_id)
    lock_key = f"{key}:lock"
    for _ in range(_LOCK_RETRIES):
        if await client.set(lock_key, "1", nx=True, ex=_LOCK_TTL_S):
            break
        await asyncio.sleep(_LOCK_RETRY_DELAY_S)
    else:
        raise RuntimeError("nova session busy")
    try:
        state = await load(tenant_namespace, session_id)
        fn(state)
        # Bound the history AFTER the mutation so fn can append freely.
        history = state.get("history") or []
        if len(history) > HISTORY_MAX_TURNS * 2:
            state["history"] = history[-HISTORY_MAX_TURNS * 2:]
        await client.setex(key, SESSION_TTL_S, json.dumps(state))
        return state
    finally:
        try:
            await client.delete(lock_key)
        except Exception:
            pass


def append_history(state: dict[str, Any], role: str, content: str) -> None:
    (state.setdefault("history", [])).append(
        {"role": role, "content": (content or "")[:CONTENT_CAP]}
    )
