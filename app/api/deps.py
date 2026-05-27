from datetime import datetime, timezone
from typing import AsyncGenerator, Optional
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.core import api_keys as api_key_utils
from app.models.connect_api_key import OrganizationApiKey
from app.models.organization import Organization
from app.models.organization_session import OrganizationSession
from app.models.organization_user import OrganizationUser
from app.models.user import SuperAdminUser
from app.models.session import SuperAdminSession
from app.models.tenant_resources import TenantResources
from app.core import security
import jwt

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session

async def get_current_session_id(token: str = Depends(oauth2_scheme)) -> Optional[str]:
    try:
        payload = security.decode_access_token(token, expected_tiers=[security.JWT_TIER_SUPERADMIN])
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload.get("sid")

async def _decode_token(token: str, *, expected_tiers: list[str] | None = None) -> dict:
    try:
        return security.decode_access_token(token, expected_tiers=expected_tiers)
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def get_current_user(
    db: AsyncSession = Depends(get_db),
    token: str = Depends(oauth2_scheme)
) -> SuperAdminUser:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    payload = await _decode_token(token, expected_tiers=[security.JWT_TIER_SUPERADMIN])
    user_id: str = payload.get("sub")
    session_id: str = payload.get("sid")
    principal_type: str | None = payload.get("principal_type")
    if user_id is None:
        raise credentials_exception
    if principal_type not in (None, "superadmin"):
        raise credentials_exception

    if session_id:
        # Check if session is revoked
        res = await db.execute(select(SuperAdminSession).where(SuperAdminSession.id == session_id))
        session = res.scalars().first()
        if not session or session.revoked_at is not None:
            raise HTTPException(status_code=401, detail="Session has been revoked or logged out")

    result = await db.execute(select(SuperAdminUser).where(SuperAdminUser.id == user_id))
    user = result.scalars().first()

    if user is None:
        raise credentials_exception
    if user.status not in ["active", "pending"]:
        raise HTTPException(status_code=403, detail="User account is locked or disabled")

    # Stash the decoded payload on the user so downstream deps (e.g.,
    # get_current_active_user) avoid a second decode.
    setattr(user, "_jwt_payload", payload)
    return user

async def get_current_active_user(
    current_user: SuperAdminUser = Depends(get_current_user),
) -> SuperAdminUser:
    payload = getattr(current_user, "_jwt_payload", None) or {}
    mfa_completed: bool = bool(payload.get("mfa_completed", False))

    if current_user.mfa_required and not mfa_completed:
        raise HTTPException(status_code=403, detail="MFA required")

    return current_user

async def get_current_user_require_mfa_setup(
    db: AsyncSession = Depends(get_db),
    token: str = Depends(oauth2_scheme)
) -> SuperAdminUser:
    # Used only for MFA setup flow where MFA is not yet complete
    return await get_current_user(db, token)


async def get_current_org_session_id(token: str = Depends(oauth2_scheme)) -> Optional[str]:
    try:
        payload = security.decode_access_token(token, expected_tiers=[security.JWT_TIER_ORGANIZATION])
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate organization credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if payload.get("principal_type") != "organization_user":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization credentials required",
        )
    return payload.get("sid")


async def get_current_organization_user(
    db: AsyncSession = Depends(get_db),
    token: str = Depends(oauth2_scheme)
) -> OrganizationUser:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate organization credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    payload = await _decode_token(token, expected_tiers=[security.JWT_TIER_ORGANIZATION])
    user_id: str = payload.get("sub")
    organization_id: str = payload.get("organization_id")
    session_id: str = payload.get("sid")
    principal_type: str | None = payload.get("principal_type")
    if user_id is None or organization_id is None or principal_type != "organization_user":
        raise credentials_exception

    if session_id:
        session_res = await db.execute(select(OrganizationSession).where(OrganizationSession.id == session_id))
        session = session_res.scalars().first()
        if not session or session.revoked_at is not None:
            raise HTTPException(status_code=401, detail="Session has been revoked or logged out")

    result = await db.execute(
        select(OrganizationUser).where(
            OrganizationUser.id == user_id,
            OrganizationUser.organization_id == organization_id,
        )
    )
    user = result.scalars().first()
    if user is None:
        raise credentials_exception
    if user.status == "disabled":
        raise HTTPException(status_code=403, detail="Organization user account is disabled")
    setattr(user, "_jwt_payload", payload)
    return user


async def get_current_active_organization_user(
    current_user: OrganizationUser = Depends(get_current_organization_user),
) -> OrganizationUser:
    payload = getattr(current_user, "_jwt_payload", None) or {}
    mfa_completed: bool = bool(payload.get("mfa_completed", False))
    has_totp = bool(
        getattr(current_user, "totp_secret_encrypted", None)
        or getattr(current_user, "totp_secret_encrypted_v2", None)
    )
    if current_user.mfa_required and has_totp and not mfa_completed:
        raise HTTPException(status_code=403, detail="Organization MFA required")
    if current_user.status not in {"active", "invited"}:
        raise HTTPException(status_code=403, detail="Organization user account is not active")
    return current_user


async def get_current_organization(
    db: AsyncSession = Depends(get_db),
    current_user: OrganizationUser = Depends(get_current_active_organization_user),
) -> Organization:
    result = await db.execute(select(Organization).where(Organization.id == current_user.organization_id))
    organization = result.scalars().first()
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    if (organization.product_tier or "nokvo_prime") != "nokvo_prime":
        raise HTTPException(
            status_code=403,
            detail="This endpoint is only available for Nokvo Prime organizations",
        )
    return organization

class RequireRole:
    def __init__(self, allowed_roles: list[str]):
        self.allowed_roles = allowed_roles

    async def __call__(self, user: SuperAdminUser = Depends(get_current_active_user)) -> SuperAdminUser:
        if user.role not in self.allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"Operation not permitted. Required role: {self.allowed_roles}"
            )
        return user


class RequireOrganizationRole:
    """Prime-only organization role guard. Use RequireNokvoOneOrganization for One routes."""

    def __init__(self, allowed_roles: list[str]):
        self.allowed_roles = allowed_roles

    async def __call__(
        self,
        db: AsyncSession = Depends(get_db),
        user: OrganizationUser = Depends(get_current_active_organization_user),
    ) -> OrganizationUser:
        if user.role not in self.allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"Operation not permitted. Required organization role: {self.allowed_roles}"
            )
        org_result = await db.execute(select(Organization).where(Organization.id == user.organization_id))
        org = org_result.scalars().first()
        if org is None:
            raise HTTPException(status_code=404, detail="Organization not found")
        if (org.product_tier or "nokvo_prime") != "nokvo_prime":
            raise HTTPException(
                status_code=403,
                detail="This endpoint is only available for Nokvo Prime organizations",
            )
        return user


class RequireProductTier:
    """Enforce that the calling org user's organization matches one of the allowed product tiers."""

    def __init__(self, allowed_tiers: list[str]):
        self.allowed_tiers = allowed_tiers

    async def __call__(
        self,
        db: AsyncSession = Depends(get_db),
        user: OrganizationUser = Depends(get_current_active_organization_user),
    ) -> OrganizationUser:
        result = await db.execute(select(Organization).where(Organization.id == user.organization_id))
        org = result.scalars().first()
        if org is None:
            raise HTTPException(status_code=404, detail="Organization not found")
        if (org.product_tier or "nokvo_prime") not in self.allowed_tiers:
            raise HTTPException(
                status_code=403,
                detail=f"Organization product tier '{org.product_tier}' is not permitted on this endpoint",
            )
        return user


class RequireMFACompleted:
    """Step-up gate for sensitive actions.

    When the user has no TOTP secret, MFA is treated as not enrolled and this
    dependency lets the action pass. Once TOTP exists, the current session must
    be MFA-elevated.
    """

    async def __call__(
        self,
        user: OrganizationUser = Depends(get_current_active_organization_user),
    ) -> OrganizationUser:
        has_totp = bool(
            getattr(user, "totp_secret_encrypted", None)
            or getattr(user, "totp_secret_encrypted_v2", None)
        )
        if not has_totp:
            return user
        payload = getattr(user, "_jwt_payload", None) or {}
        if not bool(payload.get("mfa_completed", False)):
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "mfa_step_up_required",
                    "message": (
                        "This action requires you to log in again with your MFA code."
                    ),
                },
            )
        return user


class RequireNokvoOneOrganization:
    """Composite dep: active Nokvo One org user, optionally restricted to a set of org statuses."""

    def __init__(self, allowed_statuses: list[str] | None = None, allowed_roles: list[str] | None = None):
        self.allowed_statuses = allowed_statuses or ["active"]
        self.allowed_roles = allowed_roles

    async def __call__(
        self,
        db: AsyncSession = Depends(get_db),
        user: OrganizationUser = Depends(get_current_active_organization_user),
    ) -> OrganizationUser:
        result = await db.execute(select(Organization).where(Organization.id == user.organization_id))
        org = result.scalars().first()
        if org is None:
            raise HTTPException(status_code=404, detail="Organization not found")
        if (org.product_tier or "nokvo_prime") != "nokvo_one":
            raise HTTPException(status_code=403, detail="This endpoint is only available for Nokvo One organizations")
        if org.status not in self.allowed_statuses:
            raise HTTPException(
                status_code=403,
                detail=f"Organization status '{org.status}' is not permitted on this endpoint",
            )
        if self.allowed_roles and user.role not in self.allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"Operation not permitted. Required organization role: {self.allowed_roles}",
            )
        return user


# ─────────────────────────────────────────────────────────────────────────────
# Nokvo Connect — public API-key auth
# ─────────────────────────────────────────────────────────────────────────────


async def _resolve_api_key_from_request(request: Request) -> str | None:
    """Pull the raw API key off the request. Accepts either header form so the
    same key works from a server (Authorization: Bearer) and from a browser
    SDK (X-Nokvo-API-Key)."""
    api_key = request.headers.get("x-nokvo-api-key")
    if api_key:
        return api_key.strip()
    authorization = request.headers.get("authorization") or ""
    if authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    return None


async def get_api_key_record(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> tuple[OrganizationApiKey, Organization, TenantResources]:
    """Authenticate an inbound public-API request and return the (api_key,
    organization, tenant_resources) triple every downstream call needs."""
    raw = await _resolve_api_key_from_request(request)
    parsed = api_key_utils.parse(raw)
    if parsed is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed API key",
            headers={"WWW-Authenticate": 'ApiKey realm="nokvo-connect"'},
        )
    result = await db.execute(
        select(OrganizationApiKey).where(OrganizationApiKey.key_prefix == parsed.key_prefix)
    )
    record = result.scalars().first()
    if record is None or record.status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    if record.expires_at is not None and record.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="API key has expired")
    if not api_key_utils.verify(parsed, record.secret_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    # Origin allowlist — applies only when the request actually carried an
    # Origin (i.e., from a browser). Server-to-server calls without Origin pass
    # through; servers are expected to keep their key secret.
    origin = request.headers.get("origin")
    allowed = list(record.allowed_origins or [])
    if origin and allowed and origin not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Origin not permitted for this API key")

    org_res = await db.execute(select(Organization).where(Organization.id == record.organization_id))
    organization = org_res.scalars().first()
    if organization is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Organization not available")
    if (organization.product_tier or "nokvo_prime") != "nokvo_one":
        # Nokvo Connect is a Nokvo One product. Keys can't be minted on
        # Prime orgs (the admin route guards that), but refuse defensively
        # at the public surface too in case a key outlives a tier downgrade.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Nokvo Connect is available only on Nokvo One organizations",
        )
    if organization.status not in {"active", "pending_approval"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Organization is not active")

    tr_res = await db.execute(
        select(TenantResources).where(TenantResources.organization_id == organization.id)
    )
    tenant_res = tr_res.scalars().first()
    if tenant_res is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tenant resources not provisioned")

    # Bookkeeping — not in a transaction so a stale write here cannot break
    # the request. Best-effort.
    try:
        record.last_used_at = datetime.now(timezone.utc)
        db.add(record)
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass

    return record, organization, tenant_res
