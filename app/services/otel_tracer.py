"""OpenTelemetry correlation seam for the voice hot path.

Why this module exists
----------------------
When a call goes wrong, support needs ONE id that ties together: the Python
logs, the STT/LLM/TTS stages, and the LangSmith run. LangSmith already captures
the per-turn prompt/response tree (see :mod:`langsmith_tracer`); this module
adds the missing piece — a W3C **trace id** that is

  1. generated once per call (a real OTel span, so it's standards-compliant),
  2. stamped on *every* log line emitted during the call (via a logging filter,
     no per-call-site edits), and
  3. persisted on the ``call_costs`` row + cross-linked into the LangSmith run,

so "the call from org X at 3pm that quoted the wrong price" can be traced from
the dashboard → logs → LangSmith in seconds.

Design constraints (same bar as ``langsmith_tracer``)
-----------------------------------------------------
1. **No-op when disabled.** ``OTEL_ENABLED=false`` (the default) means
   :func:`init_otel` is a cheap return, the span context-managers yield ``None``,
   and the hot path pays zero latency. No SDK provider is installed.
2. **Exporter optional.** With ``OTEL_EXPORTER=none`` (default-when-enabled) a
   real ``TracerProvider`` is installed so trace ids ARE generated in-process,
   but no span processor ships them anywhere — no collector needed at launch.
   ``console`` prints spans; ``otlp`` ships to a collector (exporter imported
   lazily, so a missing endpoint/package never breaks startup).
3. **Cross-task safe.** :func:`trace_call_span` sets a ``ContextVar`` that the
   ``asyncio.create_task`` snapshot carries into child turn tasks, so the trace
   id is available even where the OTel context doesn't auto-propagate.

Public API
----------
- :func:`init_otel` — call once at FastAPI startup.
- :func:`install_log_correlation` — add the ``trace_id`` field to app logs.
- :func:`is_enabled` — cheap hot-path check.
- :func:`trace_call_span` — root span around the whole call.
- :func:`trace_stage` — child span around a hot-path stage (stt/llm/tts/response).
- :func:`current_trace_id` — the active trace id (32-hex) or ``""``.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any, AsyncIterator

from app.core.config import settings

logger = logging.getLogger(__name__)

_initialized: bool = False
_enabled: bool = False
_tracer: Any = None  # opentelemetry.trace.Tracer — kept Any so disabled never imports the SDK

# Correlation id for the active call. ``asyncio.create_task`` copies the current
# context, so a turn task spawned inside the call inherits this without wiring.
_trace_id_var: ContextVar[str] = ContextVar("nokvo_otel_trace_id", default="")


def init_otel() -> None:
    """Initialise the OTel SDK exactly once. Idempotent; silent no-op when
    ``OTEL_ENABLED`` is false. Any failure leaves tracing disabled."""
    global _initialized, _enabled, _tracer
    if _initialized:
        return
    _initialized = True

    if not settings.OTEL_ENABLED:
        logger.info("NOKVO-OTEL: tracing disabled (OTEL_ENABLED=false)")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            ConsoleSpanExporter,
        )

        resource = Resource.create({"service.name": settings.OTEL_SERVICE_NAME or "nokvo-one"})
        provider = TracerProvider(resource=resource)

        exporter_kind = (settings.OTEL_EXPORTER or "none").strip().lower()
        if exporter_kind == "console":
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        elif exporter_kind == "otlp":
            # Lazy import: a missing exporter package / endpoint must not break
            # startup — spans are still generated, just not shipped.
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )

                endpoint = (settings.OTEL_EXPORTER_OTLP_ENDPOINT or "").strip() or None
                provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
                )
            except Exception:
                logger.exception(
                    "NOKVO-OTEL: OTLP exporter init failed — spans generated but not exported"
                )
        # 'none' → no span processor; trace ids still generated in-process.

        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("nokvo_one.voice")
        _enabled = True
        logger.info(
            "NOKVO-OTEL: tracing enabled (exporter=%s, service=%s)",
            exporter_kind,
            settings.OTEL_SERVICE_NAME,
        )
    except Exception:
        logger.exception("NOKVO-OTEL: init failed — falling back to disabled")
        _enabled = False
        _tracer = None


def is_enabled() -> bool:
    """Hot-path check. ``False`` means every helper is a cheap no-op."""
    return _enabled


def current_trace_id() -> str:
    """32-char hex of the active span's trace id, falling back to the contextvar
    (set at call start, inherited by child tasks), or ``""`` when no trace."""
    if _enabled:
        try:
            from opentelemetry import trace

            ctx = trace.get_current_span().get_span_context()
            if ctx is not None and ctx.trace_id:
                return format(ctx.trace_id, "032x")
        except Exception:
            pass
    return _trace_id_var.get()


# ──────────────────────────────────────────────────────────────────────────
# Span context managers
# ──────────────────────────────────────────────────────────────────────────


def _clean_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    return {f"nokvo.{k}": v for k, v in attrs.items() if v is not None}


@asynccontextmanager
async def trace_call_span(
    *,
    call_id: str,
    tenant_id: str | None = None,
    caller_phone: str | None = None,
    **attrs: Any,
) -> AsyncIterator[Any]:
    """Root span for a voice call. Sets the ``_trace_id_var`` contextvar so child
    turn tasks (spawned via ``create_task``) carry the trace id for logging."""
    if not _enabled or _tracer is None:
        yield None
        return

    span_attrs = _clean_attrs(
        {"call_id": call_id, "tenant_id": tenant_id, "caller_phone": caller_phone, **attrs}
    )
    with _tracer.start_as_current_span("voice_call", attributes=span_attrs) as span:
        token = _trace_id_var.set(current_trace_id())
        try:
            yield span
        finally:
            _trace_id_var.reset(token)


@asynccontextmanager
async def trace_stage(name: str, **attrs: Any) -> AsyncIterator[Any]:
    """Child span for one hot-path stage: ``stt`` | ``llm`` | ``tts`` |
    ``response``. Auto-attaches to the active call span via the OTel context."""
    if not _enabled or _tracer is None:
        yield None
        return
    with _tracer.start_as_current_span(f"stage:{name}", attributes=_clean_attrs(attrs)) as span:
        yield span


# ──────────────────────────────────────────────────────────────────────────
# Log correlation
# ──────────────────────────────────────────────────────────────────────────


class TraceIdFilter(logging.Filter):
    """Injects ``record.trace_id`` from the contextvar so every formatter can
    reference ``%(trace_id)s`` without the call site knowing about it."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        record.trace_id = current_trace_id() or "-"
        return True


def install_log_correlation() -> None:
    """Stamp the active call's trace id onto every application log line.

    Adds a :class:`TraceIdFilter` to the root logger's handlers and rewrites
    each handler's format to inject ``[trace=<id>]`` just before the message —
    preserving uvicorn's existing format. The ``uvicorn.access`` logger has its
    own handler (not on root) and is intentionally left untouched.

    Idempotent: re-running won't double-stamp.
    """
    root = logging.getLogger()
    handlers = list(root.handlers)
    if not handlers:
        handlers = [logging.StreamHandler()]
        root.addHandler(handlers[0])

    for handler in handlers:
        if not any(isinstance(f, TraceIdFilter) for f in handler.filters):
            handler.addFilter(TraceIdFilter())
        existing = handler.formatter
        base_fmt = (
            existing._fmt
            if existing is not None and existing._fmt
            else "%(levelname)s:%(name)s:%(message)s"
        )
        if "%(trace_id)s" not in base_fmt:
            new_fmt = base_fmt.replace("%(message)s", "[trace=%(trace_id)s] %(message)s")
            handler.setFormatter(logging.Formatter(new_fmt, datefmt=getattr(existing, "datefmt", None)))
