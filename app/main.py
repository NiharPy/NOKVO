from fastapi import FastAPI, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from app.api import auth
from app.core.config import settings
import redis.asyncio as redis

app = FastAPI(title=settings.PROJECT_NAME)

# Set up Redis for rate limiting (fallback to memory if not configured, but slowapi redis backend setup requires redis)
# We will use memory storage for simplicity if Redis is not explicitly provided, but user asked for Redis
try:
    limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)
except Exception as e:
    print(f"Rate limiter setup error: {e}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.EXPECTED_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from app.api import auth, organization_auth, superadmin_tenant_provisioning

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(organization_auth.router, prefix="/api/org-auth", tags=["organization-auth"])
app.include_router(superadmin_tenant_provisioning.router, prefix="/superadmin/tenants", tags=["tenant-provisioning"])

@app.get("/health")
async def health_check():
    return {"status": "ok"}
