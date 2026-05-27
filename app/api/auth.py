from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timezone, timedelta
from app.api import deps
from app.models.user import SuperAdminUser
from app.models.session import SuperAdminSession
from app.models.audit import SuperAdminAuditLog
from app.schemas.user import LoginRequest, TOTPVerifyRequest
from app.schemas.token import Token, RefreshRequest
from app.core import security
from app.core.rate_limit import limiter
from app.core.totp_crypto import (
    TOTPDecryptionError,
    decrypt_totp_secret,
    encrypt_totp_secret,
)
from typing import Any
import uuid
import pyotp

router = APIRouter()


def _client_ip(req: Request) -> str | None:
    return req.client.host if req.client else None


async def log_audit(db: AsyncSession, user_id: uuid.UUID, action: str, risk_level: str, req: Request, meta: dict = None):
    log = SuperAdminAuditLog(
        superadmin_id=user_id,
        action=action,
        risk_level=risk_level,
        ip_address=_client_ip(req),
        user_agent=req.headers.get("user-agent"),
        metadata_=meta or {}
    )
    db.add(log)
    await db.commit()

@router.post("/login", response_model=Token)
@limiter.limit("10/minute")
async def login(request: Request, login_data: LoginRequest, db: AsyncSession = Depends(deps.get_db)):
    result = await db.execute(select(SuperAdminUser).where(SuperAdminUser.email == login_data.email))
    user = result.scalars().first()
    
    if not user:
        raise HTTPException(status_code=401, detail="Incorrect email or password")
        
    if user.status not in ["active", "pending"]:
        raise HTTPException(status_code=403, detail="Account locked or disabled")
        
    if not security.verify_password(login_data.password, user.password_hash):
        user.failed_login_count += 1
        
        if user.failed_login_count >= 5:
            user.status = "locked"
            user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=30)
            await log_audit(db, user.id, "account_locked", "high", request, {"reason": "Exceeded 5 failed login attempts"})
            db.add(user)
            await db.commit()
            raise HTTPException(status_code=403, detail="Account locked due to too many failed attempts")
            
        db.add(user)
        await db.commit()
        raise HTTPException(status_code=401, detail="Incorrect email or password")
        
    # Reset failed login count
    user.failed_login_count = 0
    db.add(user)
    await db.commit()
    
    if user.mfa_required:
        # Issue a temporary token that requires MFA step
        access_token = security.create_access_token(
            subject=user.id,
            mfa_completed=False,
            expires_delta=timedelta(minutes=5),
            extra_claims={"principal_type": "superadmin", "role": user.role},
            token_tier=security.JWT_TIER_SUPERADMIN,
        )
        return {"access_token": access_token, "refresh_token": "pending_mfa", "token_type": "bearer"}
    else:
        # If no MFA required (should be rare for superadmin), issue full tokens
        raw_refresh, token_hash = security.create_refresh_token()
        session = SuperAdminSession(
            id=uuid.uuid4(),
            superadmin_id=user.id,
            refresh_token_hash=token_hash,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=4)
        )
        access_token = security.create_access_token(
            subject=user.id,
            mfa_completed=True,
            session_id=str(session.id),
            extra_claims={"principal_type": "superadmin", "role": user.role},
            token_tier=security.JWT_TIER_SUPERADMIN,
        )

        db.add(session)
        user.last_login_at = datetime.now(timezone.utc)
        user.last_login_ip = _client_ip(request)
        db.add(user)
        await log_audit(db, user.id, "login_success", "low", request)
        await db.commit()

        return {"access_token": access_token, "refresh_token": raw_refresh, "token_type": "bearer"}

@router.post("/mfa/totp/setup")
@limiter.limit("10/hour")
async def setup_totp(request: Request, current_user: SuperAdminUser = Depends(deps.get_current_user_require_mfa_setup), db: AsyncSession = Depends(deps.get_db)):
    if current_user.totp_secret_encrypted:
        raise HTTPException(status_code=400, detail="TOTP already set up")

    secret = security.generate_totp_secret()
    current_user.totp_secret_encrypted = encrypt_totp_secret(secret)
    db.add(current_user)
    await log_audit(db, current_user.id, "totp_setup_initiated", "medium", request)
    await db.commit()

    # Return the secret so the user can add it to Google Authenticator. This is
    # the only point at which the plaintext is exposed; subsequent verify calls
    # decrypt the column on read.
    return {"secret": secret, "uri": pyotp.totp.TOTP(secret).provisioning_uri(name=current_user.email, issuer_name="NOKVO SuperAdmin")}

@router.post("/mfa/totp/verify", response_model=Token)
@limiter.limit("10/minute")
async def verify_totp(request: Request, data: TOTPVerifyRequest, current_user: SuperAdminUser = Depends(deps.get_current_user_require_mfa_setup), db: AsyncSession = Depends(deps.get_db)):
    if not current_user.totp_secret_encrypted:
        raise HTTPException(status_code=400, detail="TOTP not set up")

    try:
        secret = decrypt_totp_secret(current_user.totp_secret_encrypted)
    except TOTPDecryptionError as exc:
        raise HTTPException(status_code=500, detail="TOTP secret could not be decrypted") from exc

    if not security.verify_totp(secret, data.token):
        raise HTTPException(status_code=401, detail="Invalid TOTP token")

    # If this is their first login (pending), mark active
    if current_user.status == "pending":
        current_user.status = "active"

    raw_refresh, token_hash = security.create_refresh_token()

    session = SuperAdminSession(
        id=uuid.uuid4(),
        superadmin_id=current_user.id,
        refresh_token_hash=token_hash,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=4)
    )
    access_token = security.create_access_token(
        subject=current_user.id,
        mfa_completed=True,
        session_id=str(session.id),
        extra_claims={"principal_type": "superadmin", "role": current_user.role},
        token_tier=security.JWT_TIER_SUPERADMIN,
    )

    db.add(session)
    current_user.last_login_at = datetime.now(timezone.utc)
    current_user.last_login_ip = _client_ip(request)
    db.add(current_user)

    await log_audit(db, current_user.id, "login_mfa_success", "low", request)
    await db.commit()

    return {"access_token": access_token, "refresh_token": raw_refresh, "token_type": "bearer"}

@router.post("/refresh", response_model=Token)
@limiter.limit("30/minute")
async def refresh_token(request: Request, data: RefreshRequest, db: AsyncSession = Depends(deps.get_db)):
    # Hash the provided token and find it in DB
    provided_hash = security.hash_refresh_token(data.refresh_token)
    
    result = await db.execute(select(SuperAdminSession).where(
        SuperAdminSession.refresh_token_hash == provided_hash,
        SuperAdminSession.revoked_at == None,
        SuperAdminSession.expires_at > datetime.now(timezone.utc)
    ))
    session = result.scalars().first()
    
    if not session:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
        
    # Revoke old session (Rotation)
    session.revoked_at = datetime.now(timezone.utc)
    session.revoke_reason = "rotated"
    db.add(session)
    
    # Create new tokens
    # Create new tokens
    raw_refresh, token_hash = security.create_refresh_token()
    
    new_session = SuperAdminSession(
        id=uuid.uuid4(),
        superadmin_id=session.superadmin_id,
        refresh_token_hash=token_hash,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=4)
    )
    user_result = await db.execute(select(SuperAdminUser).where(SuperAdminUser.id == session.superadmin_id))
    user = user_result.scalars().first()
    access_token = security.create_access_token(
        subject=session.superadmin_id,
        mfa_completed=True,
        session_id=str(new_session.id),
        extra_claims={"principal_type": "superadmin", "role": user.role if user else None},
        token_tier=security.JWT_TIER_SUPERADMIN,
    )
    
    db.add(new_session)
    await log_audit(db, session.superadmin_id, "token_refreshed", "low", request)
    await db.commit()
    
    return {"access_token": access_token, "refresh_token": raw_refresh, "token_type": "bearer"}

@router.post("/logout")
async def logout(request: Request, current_user: SuperAdminUser = Depends(deps.get_current_active_user), session_id: str = Depends(deps.get_current_session_id), db: AsyncSession = Depends(deps.get_db)):
    if session_id:
        result = await db.execute(select(SuperAdminSession).where(SuperAdminSession.id == session_id))
        session = result.scalars().first()
        if session:
            session.revoked_at = datetime.now(timezone.utc)
            session.revoke_reason = "user_logout"
            db.add(session)
            await log_audit(db, current_user.id, "logout", "low", request)
            await db.commit()
    return {"status": "success"}
@router.get("/superadmin/test")
async def test_superadmin_access(current_user: SuperAdminUser = Depends(deps.RequireRole(["founder", "engineering"]))):
    return {"message": "Welcome to the highly classified superadmin dashboard!", "role": current_user.role}
@router.get("/me")
async def get_me(current_user: SuperAdminUser = Depends(deps.get_current_active_user)):
    return {
        "id": str(current_user.id),
        "email": current_user.email,
        "full_name": current_user.full_name,
        "role": current_user.role,
        "mfa_required": current_user.mfa_required,
        "webauthn_enabled": current_user.webauthn_enabled
    }
