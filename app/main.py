from fastapi import FastAPI, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from app.api import auth
from app.core.config import settings
from app.core.rate_limit import limiter
import redis.asyncio as redis

app = FastAPI(title=settings.PROJECT_NAME)

try:
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)
except Exception as e:
    print(f"Rate limiter setup error: {e}")

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
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.api import (
    auth,
    organization_auth,
    superadmin_tenant_provisioning,
    nokvo_one_auth,
    nokvo_one_members,
    nokvo_one_agents,
)

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(organization_auth.router, prefix="/api/org-auth", tags=["organization-auth"])
app.include_router(superadmin_tenant_provisioning.router, prefix="/superadmin/tenants", tags=["tenant-provisioning"])
app.include_router(nokvo_one_auth.router, prefix="/api/nokvo-one", tags=["nokvo-one"])
app.include_router(nokvo_one_members.router, prefix="/api/nokvo-one/members", tags=["nokvo-one-members"])
app.include_router(nokvo_one_agents.router, prefix="/api/nokvo-one/agents", tags=["nokvo-one-agents"])

@app.get("/health")
async def health_check():
    return {"status": "ok"}
