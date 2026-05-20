from typing import Generator, Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.session import AsyncSessionLocal
from app.models.organization import Organization
from app.models.organization_session import OrganizationSession
from app.models.organization_user import OrganizationUser
from app.models.user import SuperAdminUser
from app.models.session import SuperAdminSession
from app.core.config import settings
import jwt

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

async def get_db() -> Generator:
    async with AsyncSessionLocal() as session:
        yield session

async def get_current_session_id(token: str = Depends(oauth2_scheme)) -> Optional[str]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload.get("sid")
    except jwt.PyJWTError:
        return None

async def get_current_user(
    db: AsyncSession = Depends(get_db),
    token: str = Depends(oauth2_scheme)
) -> SuperAdminUser:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id: str = payload.get("sub")
        session_id: str = payload.get("sid")
        principal_type: str | None = payload.get("principal_type")
        if user_id is None:
            raise credentials_exception
        if principal_type not in (None, "superadmin"):
            raise credentials_exception
    except jwt.PyJWTError:
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
        
    return user

async def get_current_active_user(
    current_user: SuperAdminUser = Depends(get_current_user),
    token: str = Depends(oauth2_scheme)
) -> SuperAdminUser:
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    mfa_completed: bool = payload.get("mfa_completed", False)
    
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
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("principal_type") != "organization_user":
            return None
        return payload.get("sid")
    except jwt.PyJWTError:
        return None


async def get_current_organization_user(
    db: AsyncSession = Depends(get_db),
    token: str = Depends(oauth2_scheme)
) -> OrganizationUser:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate organization credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id: str = payload.get("sub")
        organization_id: str = payload.get("organization_id")
        session_id: str = payload.get("sid")
        principal_type: str | None = payload.get("principal_type")
        if user_id is None or organization_id is None or principal_type != "organization_user":
            raise credentials_exception
    except jwt.PyJWTError:
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
    return user


async def get_current_active_organization_user(
    current_user: OrganizationUser = Depends(get_current_organization_user),
    token: str = Depends(oauth2_scheme),
) -> OrganizationUser:
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    mfa_completed: bool = payload.get("mfa_completed", False)
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
        token: str = Depends(oauth2_scheme),
    ) -> OrganizationUser:
        has_totp = bool(
            getattr(user, "totp_secret_encrypted", None)
            or getattr(user, "totp_secret_encrypted_v2", None)
        )
        if not has_totp:
            return user
        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        except jwt.PyJWTError:
            raise HTTPException(status_code=401, detail="Could not validate credentials")
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
