from typing import Generator, Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.db.session import AsyncSessionLocal
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
        mfa_completed: bool = payload.get("mfa_completed", False)
        if user_id is None:
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
