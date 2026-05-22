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

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(
        {
            settings.EXPECTED_ORIGIN.rstrip("/"),
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        }
    ),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["authorization", "content-type", "x-requested-with"],
)

from app.api import (
    auth,
    organization_auth,
    superadmin_tenant_provisioning,
    nokvo_one_auth,
    nokvo_one_members,
    nokvo_one_agents,
    nokvo_one_knowledge_base,
    nokvo_one_outcomes,
    nokvo_one_requests,
    nokvo_one_voice,
    connect_admin,
    connect_public,
)

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(organization_auth.router, prefix="/api/org-auth", tags=["organization-auth"])
app.include_router(superadmin_tenant_provisioning.router, prefix="/superadmin/tenants", tags=["tenant-provisioning"])
app.include_router(nokvo_one_auth.router, prefix="/api/nokvo-one", tags=["nokvo-one"])
app.include_router(nokvo_one_requests.router, prefix="/api/nokvo-one", tags=["nokvo-one-requests"])
app.include_router(nokvo_one_outcomes.router, prefix="/api/nokvo-one", tags=["nokvo-one-outcomes"])
app.include_router(nokvo_one_members.router, prefix="/api/nokvo-one/members", tags=["nokvo-one-members"])
app.include_router(nokvo_one_agents.router, prefix="/api/nokvo-one/agents", tags=["nokvo-one-agents"])
app.include_router(
    nokvo_one_voice.router,
    prefix="/api/nokvo-one/agents",
    tags=["nokvo-one-voice"],
)
app.include_router(
    nokvo_one_knowledge_base.router,
    prefix="/api/nokvo-one/knowledge-base",
    tags=["nokvo-one-knowledge-base"],
)

# Nokvo Connect — Nokvo One organizations only. Admin key management lives
# under the Nokvo One prefix so it shares JWT semantics with the rest of the
# Nokvo One portal; the public voice/text API is namespace-neutral because
# customer apps will hit it directly with an API key.
app.include_router(connect_admin.router, prefix="/api/nokvo-one/connect", tags=["connect-admin"])
app.include_router(connect_public.router, prefix="/api/voice", tags=["connect-public"])

# Serve CC0 call-center ambience clips bundled in app/assets/audio/ so the
# frontend can mix them under the live agent voice. Path is public — the
# files are public-domain assets, not tenant data.
_AMBIENCE_DIR = Path(__file__).resolve().parent / "assets" / "audio"
if _AMBIENCE_DIR.exists():
    app.mount("/assets/audio", StaticFiles(directory=str(_AMBIENCE_DIR)), name="audio_assets")


@app.get("/health")
async def health_check():
    return {"status": "ok"}


# In-process retry-queue scheduler: drains pending_tool_retries on a 2-minute
# cadence so the operator doesn't have to wire a separate worker process.
from app.services.retry_scheduler import start_scheduler, stop_scheduler  # noqa: E402


@app.on_event("startup")
async def _start_retry_scheduler() -> None:
    start_scheduler()


@app.on_event("shutdown")
async def _stop_retry_scheduler() -> None:
    await stop_scheduler()
