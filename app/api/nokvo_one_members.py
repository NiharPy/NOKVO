from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core import security
from app.core.config import settings
from app.core.email_policy import extract_email_domain, normalize_email
from app.core.rate_limit import limiter
from app.core.totp_crypto import encrypt_totp_secret
from app.models.member_invitation import MemberInvitation
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser
from app.schemas.nokvo_one import (
    NokvoOneInvitationAcceptRequest,
    NokvoOneInvitationContextResponse,
    NokvoOneInvitationResponse,
    NokvoOneMemberInviteRequest,
    NokvoOneTOTPSetupResponse,
    NokvoOneUserResponse,
)
from app.services.email_service import EmailService

import jwt
import pyotp


router = APIRouter()


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _issue_invite_setup_token(user_id: uuid.UUID, organization_id: uuid.UUID) -> str:
    payload = {
        "sub": str(user_id),
        "organization_id": str(organization_id),
        "stage": "totp_setup",
        "principal_type": "nokvo_one_signup",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


@router.get("/", response_model=list[NokvoOneUserResponse])
async def list_members(
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(allowed_statuses=["pending_approval", "active"])
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    res = await db.execute(
        select(OrganizationUser)
        .where(OrganizationUser.organization_id == user.organization_id)
        .order_by(OrganizationUser.created_at.asc())
    )
    return [NokvoOneUserResponse.model_validate(m) for m in res.scalars().all()]


@router.post(
    "/invite",
    response_model=NokvoOneInvitationResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit("30/hour")
async def invite_member(
    request: Request,
    payload: NokvoOneMemberInviteRequest,
    background: BackgroundTasks,
    inviter: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(
            allowed_statuses=["pending_approval", "active"],
            allowed_roles=["admin"],
        )
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    org_res = await db.execute(select(Organization).where(Organization.id == inviter.organization_id))
    organization = org_res.scalars().first()
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    email = normalize_email(payload.email)
    invited_domain = extract_email_domain(email)
    if (organization.email_domain or "").lower() != invited_domain.lower():
        raise HTTPException(
            status_code=400,
            detail="Invitees must share the organization's work-email domain",
        )

    existing_res = await db.execute(
        select(OrganizationUser).where(
            OrganizationUser.organization_id == organization.id,
            OrganizationUser.email == email,
        )
    )
    if existing_res.scalars().first() is not None:
        raise HTTPException(status_code=409, detail="A member with this email already exists")

    invitee = OrganizationUser(
        id=uuid.uuid4(),
        organization_id=organization.id,
        invited_by=inviter.id,
        email=email,
        full_name=payload.full_name,
        role=payload.role,
        status="invited",
        auth_provider="password",
        mfa_required=True,
        email_verified=False,
    )
    db.add(invitee)
    await db.flush()

    raw = secrets.token_urlsafe(32)
    invitation = MemberInvitation(
        id=uuid.uuid4(),
        organization_id=organization.id,
        organization_user_id=invitee.id,
        invited_by=inviter.id,
        email=email,
        role=payload.role,
        token_hash=_hash_token(raw),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=settings.NOKVO_ONE_INVITE_TOKEN_TTL_HOURS),
    )
    db.add(invitation)
    await db.commit()
    await db.refresh(invitation)

    background.add_task(
        EmailService.send_member_invitation_email,
        email,
        organization.name,
        inviter.full_name,
        raw,
    )
    return NokvoOneInvitationResponse.model_validate(invitation)


@router.get("/invitations/{token}", response_model=NokvoOneInvitationContextResponse)
@limiter.limit("60/hour")
async def get_invitation_context(
    request: Request,
    token: str,
    db: AsyncSession = Depends(deps.get_db),
):
    token_hash = _hash_token(token)
    res = await db.execute(select(MemberInvitation).where(MemberInvitation.token_hash == token_hash))
    invitation = res.scalars().first()
    if invitation is None:
        raise HTTPException(status_code=404, detail="Invitation not found")
    if invitation.accepted_at is not None:
        raise HTTPException(status_code=410, detail="Invitation has already been accepted")
    if invitation.revoked_at is not None:
        raise HTTPException(status_code=410, detail="Invitation has been revoked")
    if invitation.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="Invitation has expired")

    org_res = await db.execute(select(Organization).where(Organization.id == invitation.organization_id))
    organization = org_res.scalars().first()
    if organization is None or organization.product_tier != "nokvo_one":
        raise HTTPException(status_code=404, detail="Organization not found")

    return NokvoOneInvitationContextResponse(
        organization_id=organization.id,
        organization_name=organization.name,
        email=invitation.email,
        role=invitation.role,
        expires_at=invitation.expires_at,
    )


@router.post("/invitations/{token}/accept", response_model=NokvoOneTOTPSetupResponse)
@limiter.limit("20/hour")
async def accept_invitation(
    request: Request,
    token: str,
    payload: NokvoOneInvitationAcceptRequest,
    db: AsyncSession = Depends(deps.get_db),
):
    if payload.token != token:
        raise HTTPException(status_code=400, detail="Token in body must match the URL token")

    token_hash = _hash_token(token)
    res = await db.execute(select(MemberInvitation).where(MemberInvitation.token_hash == token_hash))
    invitation = res.scalars().first()
    if invitation is None:
        raise HTTPException(status_code=404, detail="Invitation not found")
    if invitation.accepted_at is not None:
        raise HTTPException(status_code=410, detail="Invitation has already been accepted")
    if invitation.revoked_at is not None:
        raise HTTPException(status_code=410, detail="Invitation has been revoked")
    if invitation.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="Invitation has expired")

    user_res = await db.execute(select(OrganizationUser).where(OrganizationUser.id == invitation.organization_user_id))
    user = user_res.scalars().first()
    if user is None:
        raise HTTPException(status_code=404, detail="Invitee user not found")

    user.password_hash = security.get_password_hash(payload.password)
    user.email_verified = True
    user.auth_provider = "password"
    if user.status == "invited":
        user.status = "pending_totp"

    # Initialise TOTP secret (encrypted) so the next step can return the QR.
    secret = security.generate_totp_secret()
    user.totp_secret_encrypted_v2 = encrypt_totp_secret(secret)

    invitation.accepted_at = datetime.now(timezone.utc)
    db.add(user)
    db.add(invitation)
    await db.commit()
    await db.refresh(user)

    provisioning_uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="Nokvo One")
    setup_token = _issue_invite_setup_token(user.id, user.organization_id)
    return NokvoOneTOTPSetupResponse(
        email=user.email,
        secret=secret,
        uri=provisioning_uri,
        setup_token=setup_token,
    )
