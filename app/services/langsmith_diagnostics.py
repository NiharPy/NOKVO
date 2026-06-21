"""LangSmith diagnostics for the SuperAdmin console.

Thin read-only wrapper over the LangSmith SDK that powers the in-console
"LangSmith" tab: list recent voice calls (root ``voice_call`` runs) and, for a
chosen call, stitch its full conversation back together — system prompt, each
turn's user text + agent reply, latency, and any errors — so an engineer can
diagnose a bad call without leaving the console or opening a terminal.

The stitching mirrors ``app/scripts/pull_outbound_convo.py``: we gather every
LLM/turn run inside the call's wall-clock window (NOT by trace_id, which splits
on barge-in / cancelled tasks) and order by start time.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from functools import lru_cache

from app.core.config import settings

logger = logging.getLogger(__name__)


def is_configured() -> bool:
    return bool(settings.LANGSMITH_API_KEY)


@lru_cache(maxsize=1)
def _client():
    from langsmith import Client

    # workspace_id is required when the API key spans multiple workspaces —
    # without it LangSmith returns 403 on /sessions for a workspace-scoped
    # project (e.g. our EU "nokvo-one").
    return Client(
        api_key=settings.LANGSMITH_API_KEY,
        api_url=settings.LANGSMITH_ENDPOINT or None,
        workspace_id=settings.LANGSMITH_WORKSPACE_ID or None,
    )


def _project() -> str:
    return settings.LANGSMITH_PROJECT or "nokvo-one"


def _iso(dt):
    return dt.isoformat() if dt else None


def _duration(start, end) -> float | None:
    if not start or not end:
        return None
    return round((end - start).total_seconds(), 2)


def _first(d, *keys):
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, ""):
            return d.get(k)
    return None


def _meta(run) -> dict:
    extra = run.extra or {}
    return extra.get("metadata", {}) if isinstance(extra, dict) else {}


def _call_summary(run) -> dict:
    meta = _meta(run)
    return {
        "id": str(run.id),
        "name": run.name,
        "start_time": _iso(run.start_time),
        "end_time": _iso(run.end_time),
        "duration_sec": _duration(run.start_time, run.end_time),
        "error": run.error or None,
        "has_error": bool(run.error),
        # Surface the fields an operator searches by.
        "call_id": _first(meta, "call_id", "callId", "session_id"),
        "kind": _first(meta, "kind", "direction"),
        "tenant_id": _first(meta, "tenant_id", "tenantId", "organization_id"),
        "phone": _first(meta, "phone", "to", "caller", "from"),
        "metadata": meta,
    }


def _list_recent_calls_sync(limit: int, q: str | None, errors_only: bool) -> list[dict]:
    client = _client()
    roots = list(
        client.list_runs(
            project_name=_project(),
            is_root=True,
            limit=max(limit * 3, limit + 10),
            order="desc",
        )
    )
    calls = [r for r in roots if (r.name or "") == "voice_call"]
    if not calls:
        calls = roots  # fall back so the tab is never mysteriously empty
    items = [_call_summary(r) for r in calls]
    if errors_only:
        items = [it for it in items if it["has_error"]]
    if q:
        needle = q.strip().lower()
        items = [
            it
            for it in items
            if needle in str(it.get("call_id") or "").lower()
            or needle in str(it.get("phone") or "").lower()
            or needle in str(it.get("tenant_id") or "").lower()
            or needle in str(it.get("metadata") or "").lower()
        ]
    return items[:limit]


def _call_detail_sync(run_id: str) -> dict:
    client = _client()
    call = client.read_run(run_id)
    win_start = call.start_time
    win_end = (call.end_time or call.start_time) + timedelta(seconds=5)
    window = list(
        client.list_runs(
            project_name=_project(),
            start_time=win_start,
            limit=100,  # LangSmith caps page size at 100
            order="desc",
        )
    )
    trace_runs = [
        r
        for r in window
        if (r.start_time and win_start <= r.start_time <= win_end)
        and ((r.run_type == "llm") or (r.name or "").startswith("turn") or str(r.id) == str(call.id))
    ]
    trace_runs.sort(key=lambda r: (r.start_time or call.start_time))

    # System prompt (first llm system message) — the brief/persona.
    system_prompt = None
    for r in trace_runs:
        if (r.run_type or "") != "llm":
            continue
        msgs = (r.inputs or {}).get("messages") or []
        sys_msg = next(
            (m for m in msgs if isinstance(m, dict) and m.get("role") == "system"),
            None,
        )
        if sys_msg:
            system_prompt = str(sys_msg.get("content"))
            break

    # Stitch the conversation: a turn span opens a turn (user text); the llm
    # leaf inside it is the agent reply.
    turns: list[dict] = []
    current: dict | None = None
    for r in trace_runs:
        name = r.name or ""
        rtype = r.run_type or ""
        if rtype == "chain" and name.startswith("turn"):
            current = {
                "turn": len(turns) + 1,
                "mode": _first(r.inputs or {}, "mode"),
                "user_text": _first(r.inputs or {}, "user_text"),
                "agent_reply": None,
                "barge_in": False,
                "error": r.error or None,
                "start_time": _iso(r.start_time),
                "latency_sec": _duration(r.start_time, r.end_time),
            }
            turns.append(current)
        if rtype == "llm":
            out = r.outputs or {}
            resp = _first(out, "response", "output", "content")
            barge = out.get("status") == "cancelled_barge_in"
            target = current
            if target is None:
                # An orphan llm run with no enclosing turn — surface it anyway.
                target = {
                    "turn": len(turns) + 1,
                    "mode": None,
                    "user_text": None,
                    "agent_reply": None,
                    "barge_in": False,
                    "error": None,
                    "start_time": _iso(r.start_time),
                    "latency_sec": _duration(r.start_time, r.end_time),
                }
                turns.append(target)
            if target.get("agent_reply") is None:
                target["agent_reply"] = resp
            target["barge_in"] = target["barge_in"] or barge
            if r.error:
                target["error"] = r.error

    return {
        "id": str(call.id),
        "name": call.name,
        "start_time": _iso(call.start_time),
        "end_time": _iso(call.end_time),
        "duration_sec": _duration(call.start_time, call.end_time),
        "error": call.error or None,
        "metadata": _meta(call),
        "system_prompt": system_prompt,
        "turns": turns,
        "outputs": {k: str(v)[:2000] for k, v in (call.outputs or {}).items()},
    }


class LangSmithDiagnostics:
    @staticmethod
    async def list_recent_calls(limit: int = 25, q: str | None = None, errors_only: bool = False) -> dict:
        if not is_configured():
            return {"configured": False, "project": _project(), "items": []}
        try:
            items = await asyncio.to_thread(_list_recent_calls_sync, limit, q, errors_only)
            return {"configured": True, "project": _project(), "items": items}
        except Exception as exc:  # noqa: BLE001
            logger.exception("LangSmith list failed")
            return {"configured": True, "project": _project(), "items": [], "error": str(exc)}

    @staticmethod
    async def get_call_detail(run_id: str) -> dict:
        if not is_configured():
            return {"configured": False}
        return await asyncio.to_thread(_call_detail_sync, run_id)
