"""LangSmith prompt-observability seam.

Why this module exists
----------------------
Debugging "the agent quoted the wrong price on call X turn 7" requires the
exact system prompt + history + response for that turn. Python logs don't
capture the dynamically-composed system prompt (campaign brief + memory
facts + FSM mode + retrieved chunks). LangSmith does.

Design constraints
------------------
1. **No-op when disabled.** ``LANGSMITH_API_KEY=""`` (the default) means
   every public helper is a cheap async-context-manager that yields and
   exits. Zero SDK imports on the hot path. Zero latency added.

2. **Async-first.** All context managers are :func:`asynccontextmanager` so
   they cooperate with the WS-bound voice pipeline and survive
   ``asyncio.CancelledError`` (barge-in). A sync ``contextmanager`` works
   inside an ``async def`` but can't ``await`` cleanly in ``__exit__``,
   which is the failure mode that silently drops the trace.

3. **One shared project, tenant_id as metadata.** Multi-tenant filtering is
   one search facet in the LangSmith dashboard.

4. **Auto-attach via contextvar.** LangSmith's ``tracing_context`` puts the
   active RunTree in a context variable that asyncio tasks created from
   the same frame inherit automatically. Child runs (per-turn, per-LLM)
   don't need to know about the parent — they just open and the SDK wires
   them up.

Public API
----------
- :func:`init_tracer` — call once at FastAPI startup.
- :func:`is_enabled` — cheap check the hot path can short-circuit on.
- :func:`trace_call` — root span around the entire voice call.
- :func:`trace_turn` — child span around one user turn.
- :func:`trace_llm` — leaf span around a single Azure OpenAI call. Returns
  the RunTree (or None when disabled) so the caller can attach outputs.
- :func:`traceable_chain` — decorator wrapper for higher-level callers
  (intent classifier, outcome classifier, condenser). Renders the chain
  in the trace tree without per-call boilerplate.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)


_initialized: bool = False
_enabled: bool = False
_client: Any = None  # langsmith.Client, but kept Any so a disabled instance never imports the SDK


def init_tracer() -> None:
    """Initialise the LangSmith SDK exactly once.

    Idempotent: subsequent calls are no-ops. Silent no-op when
    ``LANGSMITH_API_KEY`` is unset — leaves :func:`is_enabled` False so
    every helper takes its cheap path.

    Sets the env vars the LangSmith SDK reads (`LANGSMITH_API_KEY`,
    `LANGSMITH_PROJECT`, `LANGSMITH_TRACING_V2`, `LANGSMITH_ENDPOINT`) so
    any future ``@traceable`` decorator anywhere in the codebase wires up
    without additional config.
    """
    global _initialized, _enabled, _client
    if _initialized:
        return
    _initialized = True

    api_key = (settings.LANGSMITH_API_KEY or "").strip()
    if not api_key:
        logger.info("NOKVO-LANGSMITH: tracing disabled (no LANGSMITH_API_KEY)")
        return

    os.environ.setdefault("LANGSMITH_API_KEY", api_key)
    os.environ.setdefault("LANGSMITH_PROJECT", settings.LANGSMITH_PROJECT or "nokvo-one")
    # The SDK reads either LANGSMITH_TRACING_V2 or LANGCHAIN_TRACING_V2.
    # Set both so the implicit @traceable hooks (used in langchain-* deps
    # we have transitively) also fire under the same toggle.
    flag = "true" if settings.LANGSMITH_TRACING_V2 else "false"
    os.environ.setdefault("LANGSMITH_TRACING_V2", flag)
    os.environ.setdefault("LANGCHAIN_TRACING_V2", flag)
    if (settings.LANGSMITH_ENDPOINT or "").strip():
        os.environ.setdefault("LANGSMITH_ENDPOINT", settings.LANGSMITH_ENDPOINT.strip())
    # Org-scoped keys must declare the target workspace; the SDK turns this
    # into the X-Tenant-Id header on every ingest. Without it /runs/* 403s
    # while reads still succeed.
    if (settings.LANGSMITH_WORKSPACE_ID or "").strip():
        os.environ.setdefault("LANGSMITH_WORKSPACE_ID", settings.LANGSMITH_WORKSPACE_ID.strip())

    try:
        from langsmith import Client

        _client = Client(api_key=api_key)
        _enabled = True
        logger.info(
            "NOKVO-LANGSMITH: tracing enabled (project=%s)",
            os.environ.get("LANGSMITH_PROJECT"),
        )
    except Exception:
        logger.exception(
            "NOKVO-LANGSMITH: tracing init failed — falling back to disabled"
        )
        _enabled = False
        _client = None


def is_enabled() -> bool:
    """Hot-path check. ``False`` means every helper is a cheap no-op."""
    return _enabled


def _project_name() -> str:
    return os.environ.get("LANGSMITH_PROJECT") or settings.LANGSMITH_PROJECT or "nokvo-one"


# ──────────────────────────────────────────────────────────────────────────
# Context-manager helpers
# ──────────────────────────────────────────────────────────────────────────
#
# All three are ``asynccontextmanager`` so callers ``async with`` them.
# They yield either a ``RunTree`` (when enabled) or ``None`` (when
# disabled). Caller code that wants to attach outputs / metadata does
# ``if span is not None: span.add_outputs(...)``.
#
# The SDK's ``RunTree`` end/post calls are synchronous in v0.8 (the SDK
# batches submissions to a background thread). The asynccontextmanager
# shape doesn't depend on that — it cooperates correctly with cancellation
# and with the surrounding ``async with`` blocks regardless of whether
# the SDK happens to be sync underneath.


@asynccontextmanager
async def trace_call(
    *,
    call_id: str,
    tenant_id: str | None = None,
    organization_id: Any = None,
    **meta: Any,
) -> AsyncIterator[Any]:
    """Root span for a voice call. Wraps ``run_session``.

    Sets the RunTree as the current ``tracing_context`` so child runs
    auto-attach (intent classifier, retrieval, LLM, etc.) without
    threading the parent through the call chain.

    On exit (including via exception), the RunTree is ended and queued
    for submission to LangSmith. Submission is best-effort: a failed
    POST never bubbles out of the contextmanager.
    """
    if not _enabled:
        yield None
        return

    try:
        from langsmith.run_trees import RunTree
        from langsmith.run_helpers import tracing_context
    except Exception:
        logger.debug("NOKVO-LANGSMITH: SDK import failed at trace_call")
        yield None
        return

    meta = {
        "call_id": call_id,
        "tenant_id": str(tenant_id) if tenant_id else None,
        "organization_id": str(organization_id) if organization_id else None,
        **{k: v for k, v in meta.items() if v is not None},
    }
    run = RunTree(
        name="voice_call",
        run_type="chain",
        inputs={"call_id": call_id, **{k: v for k, v in meta.items() if k != "call_id"}},
        extra={"metadata": meta},
        project_name=_project_name(),
    )
    try:
        run.post()
    except Exception:
        logger.debug("NOKVO-LANGSMITH: root post failed", exc_info=True)

    error: BaseException | None = None
    try:
        with tracing_context(parent=run):
            yield run
    except BaseException as exc:  # noqa: BLE001 — re-raised below
        error = exc
        raise
    finally:
        try:
            if error is not None:
                run.end(error=repr(error)[:300])
            else:
                run.end()
            run.patch()
        except Exception:
            logger.debug("NOKVO-LANGSMITH: root end/patch failed", exc_info=True)


@asynccontextmanager
async def trace_turn(
    *,
    turn_index: int | None = None,
    user_text: str | None = None,
    mode: str | None = None,
    **meta: Any,
) -> AsyncIterator[Any]:
    """Child span for one user turn inside a voice call.

    Reads the current parent from ``tracing_context`` (set by
    :func:`trace_call`). When no parent is active, the turn becomes its
    own root run — still useful, just disconnected from the call tree.
    """
    if not _enabled:
        yield None
        return

    try:
        from langsmith.run_helpers import get_current_run_tree, tracing_context
    except Exception:
        yield None
        return

    parent = get_current_run_tree()
    name = f"turn:{turn_index}" if turn_index is not None else "turn"
    inputs = {
        "turn_index": turn_index,
        "user_text": user_text,
        "mode": mode,
        **{k: v for k, v in meta.items() if v is not None},
    }

    if parent is not None:
        run = parent.create_child(
            name=name,
            run_type="chain",
            inputs=inputs,
        )
    else:
        from langsmith.run_trees import RunTree

        run = RunTree(
            name=name,
            run_type="chain",
            inputs=inputs,
            extra={"metadata": meta},
            project_name=_project_name(),
        )
    try:
        run.post()
    except Exception:
        logger.debug("NOKVO-LANGSMITH: turn post failed", exc_info=True)

    error: BaseException | None = None
    try:
        with tracing_context(parent=run):
            yield run
    except BaseException as exc:  # noqa: BLE001 — re-raised
        error = exc
        raise
    finally:
        try:
            if error is not None:
                run.end(error=repr(error)[:300])
            else:
                run.end()
            run.patch()
        except Exception:
            logger.debug("NOKVO-LANGSMITH: turn end/patch failed", exc_info=True)


@asynccontextmanager
async def trace_llm(
    *,
    name: str,
    model: str | None = None,
    messages: list | None = None,
    max_tokens: int | None = None,
    parent_run_id: uuid.UUID | str | None = None,
    **meta: Any,
) -> AsyncIterator[Any]:
    """Leaf span for one LLM call. The caller attaches outputs via
    ``span.add_outputs({...})`` before exiting.

    ``parent_run_id`` is an explicit override used by the post-call
    condenser background task — it runs after the call's WS frame is
    gone, so the contextvar parent is no longer set. Passing the call's
    root run id keeps the condenser visible under the call's tree.
    """
    if not _enabled:
        yield None
        return

    try:
        from langsmith.run_helpers import get_current_run_tree
        from langsmith.run_trees import RunTree
    except Exception:
        yield None
        return

    inputs: dict[str, Any] = {}
    if messages is not None:
        inputs["messages"] = messages
    if max_tokens is not None:
        inputs["max_tokens"] = max_tokens

    metadata = {
        "model": model,
        **{k: v for k, v in meta.items() if v is not None},
    }

    parent = get_current_run_tree() if parent_run_id is None else None
    if parent is not None:
        run = parent.create_child(
            name=name,
            run_type="llm",
            inputs=inputs,
            extra={"metadata": metadata},
        )
    else:
        # Explicit parent_run_id or no parent at all → free-floating run.
        # parent_run_id is best-effort; LangSmith stitches it server-side
        # if the parent exists.
        kwargs: dict[str, Any] = dict(
            name=name,
            run_type="llm",
            inputs=inputs,
            extra={"metadata": metadata},
            project_name=_project_name(),
        )
        if parent_run_id is not None:
            kwargs["parent_run_id"] = str(parent_run_id)
        run = RunTree(**kwargs)
    try:
        run.post()
    except Exception:
        logger.debug("NOKVO-LANGSMITH: llm post failed", exc_info=True)

    error: BaseException | None = None
    try:
        yield run
    except BaseException as exc:  # noqa: BLE001 — re-raised
        error = exc
        raise
    finally:
        try:
            if error is not None:
                run.end(error=repr(error)[:300])
            else:
                run.end()
            run.patch()
        except Exception:
            logger.debug("NOKVO-LANGSMITH: llm end/patch failed", exc_info=True)


# ──────────────────────────────────────────────────────────────────────────
# Decorator wrapper for higher-level chain callers
# ──────────────────────────────────────────────────────────────────────────


def traceable_chain(name: str, **decorator_meta: Any) -> Callable:
    """Decorator wrapping ``langsmith.traceable`` with run_type=chain.

    Cleanly no-ops when tracing is disabled — the wrapped function is
    returned untouched, no SDK import is attempted. Used on the three
    higher-level chain callers (intent classifier, outcome classifier,
    condenser). Inner LLM calls auto-attach as children.
    """
    def _wrap(fn: Callable) -> Callable:
        if not _enabled:
            return fn
        try:
            from langsmith.run_helpers import traceable

            return traceable(name=name, run_type="chain", metadata=decorator_meta)(fn)
        except Exception:
            logger.debug(
                "NOKVO-LANGSMITH: traceable_chain wrap failed for %s", name, exc_info=True,
            )
            return fn

    return _wrap


# ──────────────────────────────────────────────────────────────────────────
# Helper for the barge-in streaming pattern
# ──────────────────────────────────────────────────────────────────────────


def end_llm_span(span: Any, outputs: dict[str, Any]) -> None:
    """Best-effort outputs attach. Helper so call sites don't sprinkle
    None-checks everywhere.

    Use case: streaming wrappers buffer tokens then call this on either
    successful completion or barge-in cancellation. The barge-in path
    sets ``status="cancelled_barge_in"`` so debug searches can find
    "agent stopped mid-sentence" turns easily.
    """
    if span is None:
        return
    try:
        span.add_outputs(outputs)
    except Exception:
        logger.debug("NOKVO-LANGSMITH: add_outputs failed", exc_info=True)
