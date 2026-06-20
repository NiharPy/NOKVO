import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from app.api import auth
from app.core.config import settings
from app.core.rate_limit import limiter
import redis.asyncio as redis

logger = logging.getLogger(__name__)

app = FastAPI(title=settings.PROJECT_NAME)

# Rate limiter wiring is required — if it fails here we want to fail fast
# rather than start an unlimited service. The previous swallow-and-print
# masked misconfiguration in production.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# CORS: production allows only the configured portal origin. Localhost dev
# origins are added only outside production so a deployed API can't be driven
# from arbitrary local pages.
_cors_origins = {settings.EXPECTED_ORIGIN.rstrip("/")}
if not settings.is_production:
    _cors_origins |= {"http://localhost:5173", "http://127.0.0.1:5173"}

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_cors_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["authorization", "content-type", "x-requested-with"],
)

from app.api import (
    auth,
    organization_auth,
    superadmin_tenant_provisioning,
    nokvo_one_auth,
    nokvo_one_payments,
    nokvo_one_onboarding,
    nokvo_one_members,
    nokvo_one_agents,
    nokvo_one_billing,
    nokvo_one_customers,
    nokvo_one_outcomes,
    nokvo_one_projects,
    nokvo_one_requests,
    nokvo_one_services,
    nokvo_one_transcripts,
    nokvo_one_voice,
    connect_admin,
    connect_public,
)

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(organization_auth.router, prefix="/api/org-auth", tags=["organization-auth"])
app.include_router(superadmin_tenant_provisioning.router, prefix="/superadmin/tenants", tags=["tenant-provisioning"])
app.include_router(nokvo_one_auth.router, prefix="/api/nokvo-one", tags=["nokvo-one"])
app.include_router(nokvo_one_payments.router, prefix="/api/nokvo-one", tags=["nokvo-one-payments"])
app.include_router(nokvo_one_onboarding.router, prefix="/api/nokvo-one/onboarding", tags=["nokvo-one-onboarding"])
app.include_router(nokvo_one_requests.router, prefix="/api/nokvo-one", tags=["nokvo-one-requests"])
app.include_router(nokvo_one_outcomes.router, prefix="/api/nokvo-one", tags=["nokvo-one-outcomes"])
app.include_router(nokvo_one_transcripts.router, prefix="/api/nokvo-one", tags=["nokvo-one-transcripts"])
app.include_router(nokvo_one_members.router, prefix="/api/nokvo-one/members", tags=["nokvo-one-members"])
app.include_router(nokvo_one_agents.router, prefix="/api/nokvo-one/agents", tags=["nokvo-one-agents"])
app.include_router(nokvo_one_projects.router, prefix="/api/nokvo-one/projects", tags=["nokvo-one-projects"])
app.include_router(nokvo_one_services.router, prefix="/api/nokvo-one/services", tags=["nokvo-one-services"])
app.include_router(nokvo_one_customers.router, prefix="/api/nokvo-one/customers", tags=["nokvo-one-customers"])
app.include_router(
    nokvo_one_voice.router,
    prefix="/api/nokvo-one/agents",
    tags=["nokvo-one-voice"],
)
# Billing (per-call cost ledger summary) + notification inbox / live fanout
app.include_router(
    nokvo_one_billing.router,
    prefix="/api/nokvo-one",
    tags=["nokvo-one-billing"],
)

# Nokvo Connect — Nokvo One organizations only. Admin key management lives
# under the Nokvo One prefix so it shares JWT semantics with the rest of the
# Nokvo One portal; the public voice/text API is namespace-neutral because
# customer apps will hit it directly with an API key.
#
# Gated behind ``NOKVO_CONNECT_ENABLED``. When the flag is off (the default),
# we never register the routers — every Connect URL falls through to FastAPI's
# default 404 handler, so leftover API keys can't be used and the surface is
# completely dormant until an operator opts in via .env. The frontend reads
# the same flag off ``/api/nokvo-one/config`` and hides its nav button +
# landing page so users don't see a dead-end UI.
if settings.NOKVO_CONNECT_ENABLED:
    app.include_router(connect_admin.router, prefix="/api/nokvo-one/connect", tags=["connect-admin"])
    app.include_router(connect_public.router, prefix="/api/voice", tags=["connect-public"])

# Serve CC0 call-center ambience clips bundled in app/assets/audio/ so the
# frontend can mix them under the live agent voice. Path is public — the
# files are public-domain assets, not tenant data.
_AMBIENCE_DIR = Path(__file__).resolve().parent / "assets" / "audio"
if _AMBIENCE_DIR.exists():
    app.mount("/assets/audio", StaticFiles(directory=str(_AMBIENCE_DIR)), name="audio_assets")


@app.get("/health/live")
@limiter.exempt
async def health_live():
    """Pure liveness — the process is up and serving. No dependency I/O, so an
    LB probe never flaps on a Redis/Qdrant blip and cycles the pod."""
    return {"status": "ok"}


# Per-dependency timeout (seconds). Kept short so /health stays fast even when a
# backend is hung — a slow check is itself a failure signal.
_HEALTH_CHECK_TIMEOUT = 2.0


async def _check_redis() -> dict:
    import time as _time
    from app.services.llm_pool import LLMPool  # reuse the shared pool client
    t0 = _time.monotonic()
    client = LLMPool.client()
    await asyncio.wait_for(client.ping(), timeout=_HEALTH_CHECK_TIMEOUT)
    return {"ok": True, "latency_ms": round((_time.monotonic() - t0) * 1000, 1)}


async def _check_postgres() -> dict:
    import time as _time
    from sqlalchemy import text
    from app.db.session import AsyncSessionLocal
    t0 = _time.monotonic()

    async def _ping():
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))

    await asyncio.wait_for(_ping(), timeout=_HEALTH_CHECK_TIMEOUT)
    return {"ok": True, "latency_ms": round((_time.monotonic() - t0) * 1000, 1)}


async def _check_qdrant() -> dict:
    import time as _time
    from app.services.qdrant_service import QdrantService
    t0 = _time.monotonic()
    # QdrantClient is synchronous → run in a thread so it can't block the loop.
    await asyncio.wait_for(
        asyncio.to_thread(lambda: QdrantService._client().get_collections()),
        timeout=_HEALTH_CHECK_TIMEOUT,
    )
    return {"ok": True, "latency_ms": round((_time.monotonic() - t0) * 1000, 1)}


@app.get("/health")
@limiter.exempt
async def health_check():
    """Deep readiness check. Pings every backing service in parallel so a silent
    dependency outage can't masquerade as a healthy service.

    Redis and Postgres are on the request path → if either is down we return
    **503** (not ready for traffic). Qdrant backs a retired RAG path, so its
    failure is reported as ``degraded`` but does NOT fail the whole service."""
    redis_res, pg_res, qdrant_res = await asyncio.gather(
        _check_redis(), _check_postgres(), _check_qdrant(), return_exceptions=True
    )

    def _normalize(result) -> dict:
        if isinstance(result, BaseException):
            return {"ok": False, "error": f"{type(result).__name__}: {result}"}
        return result

    checks = {
        "redis": _normalize(redis_res),
        "postgres": _normalize(pg_res),
        "qdrant": _normalize(qdrant_res),
    }

    critical_ok = checks["redis"]["ok"] and checks["postgres"]["ok"]
    if not critical_ok:
        status = "unhealthy"
    elif not checks["qdrant"]["ok"]:
        status = "degraded"
    else:
        status = "ok"

    body = {"status": status, "checks": checks}
    if not critical_ok:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content=body)
    return body


# In-process retry-queue scheduler: drains pending_tool_retries on a 2-minute
# cadence so the operator doesn't have to wire a separate worker process.
from app.services.retry_scheduler import start_scheduler, stop_scheduler  # noqa: E402
# Periodic lead-source sync: pulls fresh leads from connected providers every
# 30 minutes so the dashboard always reflects the latest Meta/Google forms
# without admins needing to click "Sync".
from app.services.lead_sync_scheduler import (  # noqa: E402
    start_scheduler as start_lead_sync_scheduler,
    stop_scheduler as stop_lead_sync_scheduler,
)
# Follow-up agent: drains lead_followup_schedules every 60s and places the
# scheduled outbound calls via Exotel. Honours promise-extracted callback
# times over admin disposition rules, clamps into the call window, and
# enforces the four kill switches (opt-out, conversion, max attempts, pause).
from app.services.followup_scheduler import (  # noqa: E402
    start_followup_scheduler,
    stop_followup_scheduler,
)
from app.services.plivo_number_poller import (  # noqa: E402
    start_plivo_number_poller,
    stop_plivo_number_poller,
)
# Prompt-observability seam. ``init_tracer`` reads the LangSmith config
# and sets up the SDK (or no-ops cleanly when LANGSMITH_API_KEY is unset).
from app.services.langsmith_tracer import init_tracer  # noqa: E402
# OpenTelemetry trace-id correlation. ``init_otel`` sets up the SDK (or no-ops
# when OTEL_ENABLED is false); ``install_log_correlation`` stamps the per-call
# trace id onto every application log line.
from app.services.otel_tracer import init_otel, install_log_correlation  # noqa: E402


@app.on_event("startup")
async def _start_retry_scheduler() -> None:
    # Tracing first so any startup-side LLM call (rare, but possible if a
    # scheduler kicks one off in its first tick) is observed too.
    init_tracer()
    init_otel()
    # Runs after uvicorn has installed its log handlers so the trace field is
    # injected into the real formatters (idempotent if called again).
    install_log_correlation()
    start_scheduler()
    start_lead_sync_scheduler()
    start_followup_scheduler()
    start_plivo_number_poller()
    # Public-URL sanity: a wrong/unset PLIVO_WEBHOOK_BASE_URL is the #1 cause
    # of "inbound calls don't connect" (Plivo can't reach the webhook or the
    # media WS). Loud ERROR logs, never fatal — provisioning degrades anyway.
    try:
        import logging as _logging

        from app.services.public_url import validate_public_base_url

        for warning in validate_public_base_url():
            _logging.getLogger("app.startup").error("PUBLIC-URL: %s", warning)
    except Exception:
        pass
    # Webhook re-sync check: count tenants whose Plivo Application answer_url
    # no longer matches the current base URL (stale after a URL change). Log
    # only by default; mutate Plivo only when PLIVO_WEBHOOK_AUTOSYNC is on.
    try:
        from app.services.plivo_service import PlivoService

        asyncio.get_event_loop().create_task(PlivoService.startup_webhook_sync_check())
    except Exception:
        pass
    # Transcript retention: a daily sweep deleting transcripts older than the
    # rolling 1-month window (lazy purge on the list endpoint is the backstop).
    try:
        from app.services.transcript_service import TranscriptService

        asyncio.create_task(TranscriptService.run_forever())
    except Exception:
        pass


@app.on_event("shutdown")
async def _stop_retry_scheduler() -> None:
    await stop_scheduler()
    await stop_lead_sync_scheduler()
    await stop_followup_scheduler()
    await stop_plivo_number_poller()
