"""NOKVO APEX members + qualified-lead claim pool.

APEX is otherwise a single-admin product. This adds team members:

  * an APEX **admin** invites a member by email (own account, scoped to the apex
    org) — mirrors Nokvo One's member invite, but PASSWORD-ONLY (no TOTP) and the
    accept issues a session so the member lands straight in;
  * a **member** sees the org's **Qualified Leads** (the deterministic campaigns'
    contacts that crossed the lead score) as a pool and **claims** them (exclusive,
    first-come), then moves each through a status (claimed → contacted → won/lost).

Qualified leads live INSIDE ``OutboundCampaign.contacts`` (JSONB), so claiming is a
read-modify-write of that blob and MUST go through
``OutboundCampaignService._lock_campaign`` (SELECT…FOR UPDATE) + ``flag_modified`` —
the same race-/persistence-safe pattern the bulk dialer uses. Claim state lives on
the contact (``claimed_by``/``claimed_by_name``/``claimed_at``/``claim_status``) —
no new table. Mounted at ``/api/nokvo-one/apex``.
"""
from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy import text as _members_sql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.api import deps
from app.api.nokvo_one_auth import _issue_full_session
from app.api.nokvo_one_members import _hash_token
from app.core import security
from app.core.config import settings
from app.core.email_policy import normalize_email
from app.models.member_invitation import MemberInvitation
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser
from app.models.tenant_resources import TenantResources
from app.services.email_service import EmailService
from app.services.outbound_campaign_service import OutboundCampaignService

router = APIRouter()

APEX_TIER = "nokvo_apex"
_CLAIM_STATUSES = {"claimed", "contacted", "won", "lost"}


def _apex_admin_dep():
    return deps.RequireApexOrganization(allowed_statuses=["active"], allowed_roles=["admin"])


def _apex_member_dep():
    # Any active apex user (admin OR member) — the lead pool + claims.
    return deps.RequireApexOrganization(allowed_statuses=["active"])


async def _org(db: AsyncSession, org_id: uuid.UUID) -> Organization:
    org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalars().first()
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org


async def _tenant(db: AsyncSession, org_id: uuid.UUID) -> TenantResources:
    tr = (
        await db.execute(select(TenantResources).where(TenantResources.organization_id == org_id))
    ).scalars().first()
    if tr is None:
        raise HTTPException(status_code=404, detail="Tenant resources not found for organization")
    return tr


# ─────────────────────────── qualified-lead helpers ───────────────────────────


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_qualified(contact: dict, cfg: dict) -> bool:
    """Mirror the frontend ``leadCategory`` *successful* branch: a contact qualifies
    when the questionnaire scored it at/above threshold (or the agent flagged it
    ``qualified``); for a questionnaire-less campaign, when interest == interested."""
    q = (cfg or {}).get("questionnaire") or {}
    if q.get("questions"):
        score = contact.get("lead_score")
        if not (_is_number(score) or isinstance(contact.get("qualified"), bool)):
            return False
        if contact.get("qualified") is True:
            return True
        try:
            threshold = float(q.get("threshold") or 0)
        except (TypeError, ValueError):
            threshold = 0.0
        return _is_number(score) and float(score) >= threshold
    io = contact.get("interest_outcome")
    if io not in (None, ""):
        return io == "interested"
    return False


def _lead_row(contact: dict, campaign) -> dict:
    from app.services.agent_outbound_context import questionnaire_max_points

    cfg = campaign.agent_config or {}
    q = cfg.get("questionnaire") or {}
    has_q = bool(q.get("questions"))
    # Weighted/graded points via the ONE shared helper — len(questions) showed
    # members impossible "7/4" scores on any weighted questionnaire (the campaign
    # endpoints already use this denominator).
    max_score = cfg.get("max_score") or (
        questionnaire_max_points(q.get("questions")) if has_q else None
    )
    score = contact.get("lead_score")
    return {
        "campaign_id": str(campaign.id),
        "campaign_name": campaign.name,
        "call_link_id": contact.get("call_link_id"),
        "name": contact.get("name") or contact.get("phone"),
        "phone": contact.get("phone"),
        "lead_score": score if _is_number(score) else None,
        "max_score": max_score,
        "score_breakdown": contact.get("score_breakdown") or [],
        "lead_score_reason": contact.get("lead_score_reason") or contact.get("interest_reason"),
        "call_note": contact.get("call_note"),
        "claim_status": contact.get("claim_status"),
        "claimed_by_name": contact.get("claimed_by_name"),
        "claimed_at": contact.get("claimed_at"),
    }


def _find_contact(campaign, call_link_id: str):
    """Return (index, contact_dict) for the contact with this call_link_id, or
    (None, None). Match on call_link_id (the stable per-contact key)."""
    for i, c in enumerate(list(campaign.contacts or [])):
        if str(c.get("call_link_id") or "") == str(call_link_id):
            return i, c
    return None, None


# ──────────────────────────────── members ─────────────────────────────────────


class ApexMemberInviteRequest(BaseModel):
    email: EmailStr
    full_name: str | None = Field(default=None, max_length=200)


@router.post("/apex/members/invite", status_code=status.HTTP_201_CREATED)
async def apex_invite_member(
    payload: ApexMemberInviteRequest,
    background: BackgroundTasks,
    inviter: OrganizationUser = Depends(_apex_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Invite a member to this APEX org. Recycles a prior ``invited``/``removed``
    row + revokes its open invites so re-inviting is clean; rejects an active seat."""
    organization = await _org(db, inviter.organization_id)
    email = normalize_email(payload.email)
    now = datetime.now(timezone.utc)

    # One APEX account per email, PRODUCT-wide: login resolves by email across
    # all APEX orgs, so a second org's seat makes sign-in ambiguous — and the
    # invite-accept below overwrites password_hash/status on whichever row it
    # points at. Same-org re-invites are handled by the recycle logic below.
    from app.api.nokvo_one_apex_auth import _resolve_apex_user

    _other = await _resolve_apex_user(db, email)
    if _other is not None and _other[1].id != organization.id:
        raise HTTPException(
            status_code=409,
            detail="This email already has an APEX account with another organization.",
        )

    existing = (
        await db.execute(
            select(OrganizationUser).where(
                OrganizationUser.organization_id == organization.id,
                OrganizationUser.email == email,
            )
        )
    ).scalars().first()
    if existing is not None:
        if existing.status in {"active", "pending_totp"}:
            raise HTTPException(status_code=409, detail="A member with this email already exists")
        existing.role = "member"
        existing.status = "invited"
        existing.full_name = payload.full_name or existing.full_name
        existing.invited_by = inviter.id
        existing.auth_provider = "password"
        existing.mfa_required = False  # APEX is password-only
        existing.email_verified = False
        existing.password_hash = None
        existing.totp_secret_encrypted_v2 = None
        old = (
            await db.execute(
                select(MemberInvitation).where(
                    MemberInvitation.organization_user_id == existing.id,
                    MemberInvitation.accepted_at.is_(None),
                    MemberInvitation.revoked_at.is_(None),
                )
            )
        ).scalars().all()
        for inv in old:
            inv.revoked_at = now
            db.add(inv)
        db.add(existing)
        invitee = existing
    else:
        invitee = OrganizationUser(
            id=uuid.uuid4(),
            organization_id=organization.id,
            invited_by=inviter.id,
            email=email,
            full_name=payload.full_name,
            role="member",
            status="invited",
            auth_provider="password",
            mfa_required=False,  # APEX is password-only
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
        role="member",
        token_hash=_hash_token(raw),
        expires_at=now + timedelta(hours=settings.NOKVO_ONE_INVITE_TOKEN_TTL_HOURS),
    )
    db.add(invitation)
    await db.commit()

    background.add_task(
        EmailService.send_apex_member_invitation_email,
        email,
        organization.name,
        inviter.full_name,
        raw,
    )
    return {"id": str(invitee.id), "email": email, "status": "invited"}


@router.get("/apex/members")
async def apex_list_members(
    user: OrganizationUser = Depends(_apex_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    rows = (
        await db.execute(
            select(OrganizationUser)
            .where(
                OrganizationUser.organization_id == user.organization_id,
                OrganizationUser.role == "member",
            )
            .order_by(OrganizationUser.created_at.desc())
        )
    ).scalars().all()
    return [
        {"id": str(m.id), "email": m.email, "full_name": m.full_name, "status": m.status}
        for m in rows
    ]


# ─────────────────────────── invite accept (public) ───────────────────────────


class ApexInviteAcceptRequest(BaseModel):
    password: str = Field(min_length=8, max_length=200)


async def _open_invitation(db: AsyncSession, token: str) -> MemberInvitation:
    invitation = (
        await db.execute(select(MemberInvitation).where(MemberInvitation.token_hash == _hash_token(token)))
    ).scalars().first()
    if invitation is None:
        raise HTTPException(status_code=404, detail="Invitation not found")
    if invitation.accepted_at is not None:
        raise HTTPException(status_code=410, detail="Invitation has already been accepted")
    if invitation.revoked_at is not None:
        raise HTTPException(status_code=410, detail="Invitation has been revoked")
    if invitation.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="Invitation has expired")
    return invitation


@router.get("/apex/members/invitations/{token}")
async def apex_invitation_context(token: str, db: AsyncSession = Depends(deps.get_db)):
    invitation = await _open_invitation(db, token)
    org = await _org(db, invitation.organization_id)
    if (org.product_tier or "") != APEX_TIER:
        raise HTTPException(status_code=404, detail="Invitation not found")
    return {
        "organization_name": org.name,
        "email": invitation.email,
        "role": invitation.role,
        "expires_at": invitation.expires_at.isoformat(),
    }


@router.post("/apex/members/invitations/{token}/accept")
async def apex_accept_invitation(
    token: str,
    payload: ApexInviteAcceptRequest,
    request: Request,
    db: AsyncSession = Depends(deps.get_db),
):
    """Set the member's password and drop them straight into a session (APEX is
    password-only — no TOTP step)."""
    invitation = await _open_invitation(db, token)
    org = await _org(db, invitation.organization_id)
    if (org.product_tier or "") != APEX_TIER:
        raise HTTPException(status_code=404, detail="Invitation not found")
    user = (
        await db.execute(select(OrganizationUser).where(OrganizationUser.id == invitation.organization_user_id))
    ).scalars().first()
    if user is None:
        raise HTTPException(status_code=404, detail="Invitee user not found")

    user.password_hash = security.get_password_hash(payload.password)
    user.email_verified = True
    user.auth_provider = "password"
    user.mfa_required = False
    user.status = "active"
    invitation.accepted_at = datetime.now(timezone.utc)
    db.add(user)
    db.add(invitation)
    await db.commit()
    await db.refresh(user)

    session = await _issue_full_session(db, request, user, org, mfa_completed=True)
    data = session.model_dump(mode="json")
    data["code"] = "ready"
    return data


# ───────────────────────── qualified leads + claiming ─────────────────────────


@router.get("/apex/leads/qualified")
async def apex_qualified_leads(
    user: OrganizationUser = Depends(_apex_member_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """The claim pool: qualified, UNCLAIMED contacts across the org's campaigns.
    Unions the V2 per-row store (scalable) with legacy blob campaigns."""
    tr = await _tenant(db, user.organization_id)
    campaigns = await OutboundCampaignService.list_campaigns(tr, db)
    out: list[dict] = []
    if settings.CAMPAIGN_CONTACTS_V2:
        from app.services import campaign_contacts_v2 as _v2

        out.extend(await _v2.qualified_pool(db, tr.tenant_id, claimed_by=None))
    for campaign in campaigns:
        if not campaign.contacts:
            continue  # V2 campaign (blob empty) — already covered above
        cfg = campaign.agent_config or {}
        for contact in campaign.contacts:
            if str(contact.get("claimed_by") or "").strip():
                continue  # already claimed
            if _is_qualified(contact, cfg):
                out.append(_lead_row(contact, campaign))
    return out


@router.get("/apex/leads/mine")
async def apex_my_leads(
    user: OrganizationUser = Depends(_apex_member_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Leads the calling member has claimed, with their working status."""
    tr = await _tenant(db, user.organization_id)
    campaigns = await OutboundCampaignService.list_campaigns(tr, db)
    me = str(user.id)
    out: list[dict] = []
    if settings.CAMPAIGN_CONTACTS_V2:
        from app.services import campaign_contacts_v2 as _v2

        out.extend(await _v2.qualified_pool(db, tr.tenant_id, claimed_by=me))
    for campaign in campaigns:
        if not campaign.contacts:
            continue
        for contact in campaign.contacts:
            if str(contact.get("claimed_by") or "") == me:
                out.append(_lead_row(contact, campaign))
    return out


async def _locked_campaign_for_org(
    db: AsyncSession, campaign_id: uuid.UUID, tr: TenantResources
):
    """Row-lock the campaign and confirm it belongs to this org's tenant (so a
    member can't claim a contact in another org's campaign by guessing an id)."""
    campaign = await OutboundCampaignService._lock_campaign(db, campaign_id)
    if campaign is None or str(campaign.tenant_id) != str(tr.tenant_id):
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


@router.post("/apex/leads/{campaign_id}/{call_link_id}/claim")
async def apex_claim_lead(
    campaign_id: uuid.UUID,
    call_link_id: str,
    user: OrganizationUser = Depends(_apex_member_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Claim a qualified lead from the pool — exclusive, first-come. The FOR UPDATE
    lock serializes two members claiming the same contact: the first wins, the
    second gets 409."""
    tr = await _tenant(db, user.organization_id)
    # V2: atomic claim on the row (no whole-campaign lock). None → no V2 row (fall
    # back to the blob path below for a legacy campaign).
    if settings.CAMPAIGN_CONTACTS_V2:
        from app.services import campaign_contacts_v2 as _v2

        # Distinguish "already claimed" (409) from "no V2 row" (fall through): a
        # matching V2 row that's already claimed must 409, not silently fall back.
        exists = (await db.execute(
            _members_sql("SELECT 1 FROM outbound_campaign_contacts occ JOIN outbound_campaigns c "
                         "ON c.id=occ.campaign_id WHERE c.tenant_id=:t AND occ.campaign_id=:cid "
                         "AND occ.call_link_id=:clid"),
            {"t": tr.tenant_id, "cid": str(campaign_id), "clid": call_link_id},
        )).scalar_one_or_none()
        if exists is not None:
            lead = await _v2.claim_lead(db, tr.tenant_id, campaign_id, call_link_id,
                                        str(user.id), user.full_name)
            if lead is None:
                raise HTTPException(status_code=409, detail="already_claimed")
            return lead

    campaign = await _locked_campaign_for_org(db, campaign_id, tr)
    contacts = list(campaign.contacts or [])
    idx, contact = _find_contact(campaign, call_link_id)
    if idx is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    # Only QUALIFIED contacts are claimable — a guessed link id must not let a
    # member claim an unqualified contact (the V2 path enforces this in SQL).
    if not _is_qualified(contact, campaign.agent_config or {}):
        raise HTTPException(status_code=404, detail="Lead not found")
    if str(contact.get("claimed_by") or "").strip():
        raise HTTPException(status_code=409, detail="already_claimed")

    contact = dict(contact)
    contact["claimed_by"] = str(user.id)
    contact["claimed_by_name"] = user.full_name
    contact["claimed_at"] = datetime.now(timezone.utc).isoformat()
    contact["claim_status"] = "claimed"
    contacts[idx] = contact
    campaign.contacts = contacts
    flag_modified(campaign, "contacts")
    await db.commit()
    return _lead_row(contact, campaign)


class ApexLeadStatusRequest(BaseModel):
    status: str


@router.post("/apex/leads/{campaign_id}/{call_link_id}/status")
async def apex_set_lead_status(
    campaign_id: uuid.UUID,
    call_link_id: str,
    payload: ApexLeadStatusRequest,
    user: OrganizationUser = Depends(_apex_member_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Move a claimed lead through its working status (claimed → contacted →
    won/lost). Only the member who claimed it may update it."""
    new_status = (payload.status or "").strip().lower()
    if new_status not in _CLAIM_STATUSES:
        raise HTTPException(status_code=400, detail=f"Status must be one of {sorted(_CLAIM_STATUSES)}")
    tr = await _tenant(db, user.organization_id)
    if settings.CAMPAIGN_CONTACTS_V2:
        from app.services import campaign_contacts_v2 as _v2

        res = await _v2.set_lead_status(db, tr.tenant_id, campaign_id, call_link_id, str(user.id), new_status)
        if res == "forbidden":
            raise HTTPException(status_code=403, detail="You can only update leads you claimed.")
        if res is not None:  # None → no V2 row, fall through to blob
            return res

    campaign = await _locked_campaign_for_org(db, campaign_id, tr)
    contacts = list(campaign.contacts or [])
    idx, contact = _find_contact(campaign, call_link_id)
    if idx is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    if str(contact.get("claimed_by") or "") != str(user.id):
        raise HTTPException(status_code=403, detail="You can only update leads you claimed.")

    contact = dict(contact)
    contact["claim_status"] = new_status
    contacts[idx] = contact
    campaign.contacts = contacts
    flag_modified(campaign, "contacts")
    await db.commit()
    return _lead_row(contact, campaign)
