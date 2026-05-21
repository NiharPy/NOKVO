from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, time, timedelta, timezone

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
from app.models.member_assignment import (
    ClinicMemberScheduleSettings,
    MemberBlockedSlot,
    OrganizationMemberAssignmentSettings,
)
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser
from app.schemas.nokvo_one import (
    NokvoOneAssignmentSettingsResponse,
    NokvoOneAssignmentSettingsUpdateRequest,
    NokvoOneBlockedSlotCreateRequest,
    NokvoOneBlockedSlotResponse,
    NokvoOneBlockedSlotUpdateRequest,
    NokvoOneClinicScheduleSettingsResponse,
    NokvoOneClinicScheduleSettingsUpdateRequest,
    NokvoOneInvitationAcceptRequest,
    NokvoOneInvitationContextResponse,
    NokvoOneInvitationResponse,
    NokvoOneMemberInviteRequest,
    NokvoOneMemberTimetableResponse,
    NokvoOneTOTPSetupResponse,
    NokvoOneUserResponse,
)
from app.services.email_service import EmailService
from app.services.nokvo_one_assignment_service import NokvoOneAssignmentService
from app.services.nokvo_one_business_templates import allowed_consultation_types, allowed_request_types

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


async def _get_organization(db: AsyncSession, organization_id: uuid.UUID) -> Organization:
    org_res = await db.execute(select(Organization).where(Organization.id == organization_id))
    organization = org_res.scalars().first()
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return organization


async def _get_member(db: AsyncSession, organization_id: uuid.UUID, member_id: uuid.UUID) -> OrganizationUser:
    res = await db.execute(
        select(OrganizationUser).where(
            OrganizationUser.id == member_id,
            OrganizationUser.organization_id == organization_id,
        )
    )
    member = res.scalars().first()
    if member is None:
        raise HTTPException(status_code=404, detail="Member not found")
    return member


def _parse_time(value: str | None) -> time | None:
    if value is None:
        return None
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid time: {value}") from exc


def _assignment_timezone() -> str:
    return "Asia/Kolkata"


def _default_working_days_for_business(industry: str | None) -> list[str]:
    if industry in {"real_estate", "hospitality"}:
        return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    if industry == "clinics":
        return ["mon", "tue", "wed", "thu", "fri", "sat"]
    return ["mon", "tue", "wed", "thu", "fri"]


def _default_working_window_for_business(industry: str | None) -> tuple[str, str]:
    if industry == "clinics":
        return "10:00", "21:00"
    if industry == "real_estate":
        return "09:00", "20:00"
    if industry == "hospitality":
        return "08:00", "22:00"
    return "09:00", "18:00"


def _validate_request_types_for_org(organization: Organization, request_types: list[str]) -> list[str]:
    allowed = allowed_request_types(organization.industry)
    invalid = [item for item in request_types if item not in allowed]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid request types for Business Type: {invalid}")
    return list(dict.fromkeys(request_types))


def _validate_consultation_types_for_org(organization: Organization, consultation_types: list[str]) -> list[str]:
    allowed = allowed_consultation_types(organization.industry)
    invalid = [item for item in consultation_types if item not in allowed]
    if invalid:
        raise HTTPException(status_code=400, detail=f"Invalid consultation types for Clinics: {invalid}")
    return list(dict.fromkeys(consultation_types))


def _assignment_response(
    settings: OrganizationMemberAssignmentSettings,
    active_count: int = 0,
) -> NokvoOneAssignmentSettingsResponse:
    days = list(settings.working_days or [])
    hours = (
        f"{settings.start_time.strftime('%H:%M')} - {settings.end_time.strftime('%H:%M')}"
        if settings.start_time and settings.end_time
        else "No hours set"
    )
    summary = "Not assignable"
    if settings.is_assignable:
        summary = f"Available {', '.join(days) or 'no days set'}, {hours}. Handles {len(settings.request_types or [])} type(s)."
    return NokvoOneAssignmentSettingsResponse(
        id=settings.id,
        organization_id=settings.organization_id,
        member_id=settings.member_id,
        is_assignable=bool(settings.is_assignable),
        working_days=days,
        start_time=settings.start_time.strftime("%H:%M") if settings.start_time else None,
        end_time=settings.end_time.strftime("%H:%M") if settings.end_time else None,
        timezone=_assignment_timezone(),
        request_types=list(settings.request_types or []),
        max_active_requests=int(settings.max_active_requests or 100),
        max_requests_per_day=settings.max_requests_per_day,
        max_requests_per_hour=int(getattr(settings, "max_requests_per_hour", None) or 6),
        appointment_duration_minutes=int(getattr(settings, "appointment_duration_minutes", None) or 30),
        active_request_count=active_count,
        availability_summary=summary,
    )


def _default_assignment_settings(organization_id: uuid.UUID, member_id: uuid.UUID) -> OrganizationMemberAssignmentSettings:
    return OrganizationMemberAssignmentSettings(
        id=None,
        organization_id=organization_id,
        member_id=member_id,
        is_assignable=False,
        working_days=[],
        start_time=None,
        end_time=None,
        timezone=_assignment_timezone(),
        request_types=[],
        max_active_requests=100,
        max_requests_per_day=None,
        max_requests_per_hour=6,
        appointment_duration_minutes=30,
    )


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


@router.get("/assignment-settings", response_model=list[NokvoOneAssignmentSettingsResponse])
async def list_assignment_settings(
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(allowed_statuses=["pending_approval", "active"])
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    members_res = await db.execute(
        select(OrganizationUser)
        .where(OrganizationUser.organization_id == user.organization_id)
        .order_by(OrganizationUser.created_at.asc())
    )
    members = list(members_res.scalars().all())
    settings_map = await NokvoOneAssignmentService._load_assignment_settings(db, user.organization_id)
    records = await NokvoOneAssignmentService._load_request_records(db, user.organization_id)
    return [
        _assignment_response(
            settings_map.get(member.id) or _default_assignment_settings(user.organization_id, member.id),
            active_count=NokvoOneAssignmentService._active_load(records, member.id),
        )
        for member in members
    ]


# ─────────── Member self-service ───────────
#
# A member-role user can't touch other members' settings, but they need
# to see their own timetable and add/remove their own buffer or
# unavailability blocks. These ``/me/...`` routes are registered BEFORE
# the ``/{member_id}/...`` routes so FastAPI matches the literal ``me``
# before attempting UUID coercion.


@router.get("/me/timetable", response_model=NokvoOneMemberTimetableResponse)
async def get_my_timetable(
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(allowed_statuses=["pending_approval", "active"])
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    """Caller's own assignment settings + blocked slots.

    Members never get to see anyone else's schedule via this endpoint,
    so it's safe to expose without a role gate beyond the org-membership
    check.
    """
    organization = await _get_organization(db, user.organization_id)
    res = await db.execute(
        select(OrganizationMemberAssignmentSettings).where(
            OrganizationMemberAssignmentSettings.organization_id == organization.id,
            OrganizationMemberAssignmentSettings.member_id == user.id,
        )
    )
    settings = res.scalars().first()
    if settings is None:
        settings = _default_assignment_settings(organization.id, user.id)
    records = await NokvoOneAssignmentService._load_request_records(db, organization.id)
    active_count = NokvoOneAssignmentService._active_load(records, user.id)
    blocks_res = await db.execute(
        select(MemberBlockedSlot)
        .where(
            MemberBlockedSlot.organization_id == organization.id,
            MemberBlockedSlot.member_id == user.id,
        )
        .order_by(MemberBlockedSlot.start_time.asc())
    )
    blocked = [NokvoOneBlockedSlotResponse.model_validate(slot) for slot in blocks_res.scalars().all()]
    return NokvoOneMemberTimetableResponse(
        assignment=_assignment_response(settings, active_count),
        blocked_slots=blocked,
    )


@router.post(
    "/me/blocked-slots",
    response_model=NokvoOneBlockedSlotResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_my_blocked_slot(
    payload: NokvoOneBlockedSlotCreateRequest,
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(allowed_statuses=["pending_approval", "active"])
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    """Member adds a block on their own calendar.

    Reused for both **buffer** (short break between requests) and
    **unavailable** (longer time off). The semantic distinction lives in
    the ``reason`` field — the scheduler treats both as hard blocks.
    """
    if payload.end_time <= payload.start_time:
        raise HTTPException(status_code=400, detail="Blocked slot end_time must be after start_time")
    slot = MemberBlockedSlot(
        id=uuid.uuid4(),
        organization_id=user.organization_id,
        member_id=user.id,
        start_time=payload.start_time,
        end_time=payload.end_time,
        reason=payload.reason,
        repeat_rule=payload.repeat_rule,
    )
    db.add(slot)
    await db.commit()
    await db.refresh(slot)
    return NokvoOneBlockedSlotResponse.model_validate(slot)


@router.delete("/me/blocked-slots/{slot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_blocked_slot(
    slot_id: uuid.UUID,
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(allowed_statuses=["pending_approval", "active"])
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    """Member cancels one of their own blocks (mistaken buffer, came
    back early from leave, etc.). Other members' blocks are invisible
    here — the where-clause scopes by ``member_id == user.id``."""
    res = await db.execute(
        select(MemberBlockedSlot).where(
            MemberBlockedSlot.id == slot_id,
            MemberBlockedSlot.organization_id == user.organization_id,
            MemberBlockedSlot.member_id == user.id,
        )
    )
    slot = res.scalars().first()
    if slot is None:
        raise HTTPException(status_code=404, detail="Blocked slot not found")
    await db.delete(slot)
    await db.commit()
    return None


@router.put("/{member_id}/assignment-settings", response_model=NokvoOneAssignmentSettingsResponse)
async def update_assignment_settings(
    member_id: uuid.UUID,
    payload: NokvoOneAssignmentSettingsUpdateRequest,
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(
            allowed_statuses=["pending_approval", "active"],
            allowed_roles=["admin", "manager"],
        )
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    organization = await _get_organization(db, user.organization_id)
    await _get_member(db, organization.id, member_id)
    request_types = _validate_request_types_for_org(organization, payload.request_types)

    res = await db.execute(
        select(OrganizationMemberAssignmentSettings).where(
            OrganizationMemberAssignmentSettings.organization_id == organization.id,
            OrganizationMemberAssignmentSettings.member_id == member_id,
        )
    )
    settings = res.scalars().first()
    if settings is None:
        settings = OrganizationMemberAssignmentSettings(
            id=uuid.uuid4(),
            organization_id=organization.id,
            member_id=member_id,
        )
    start_default, end_default = _default_working_window_for_business(organization.industry)
    working_days = payload.working_days
    start_time = payload.start_time
    end_time = payload.end_time
    if payload.is_assignable:
        working_days = working_days or _default_working_days_for_business(organization.industry)
        start_time = start_time or start_default
        end_time = end_time or end_default
        if not request_types:
            request_types = sorted(allowed_request_types(organization.industry))
    settings.is_assignable = payload.is_assignable
    settings.working_days = working_days
    settings.start_time = _parse_time(start_time)
    settings.end_time = _parse_time(end_time)
    settings.timezone = _assignment_timezone()
    settings.request_types = request_types
    settings.max_active_requests = payload.max_active_requests
    settings.max_requests_per_day = payload.max_requests_per_day
    settings.max_requests_per_hour = payload.max_requests_per_hour
    settings.appointment_duration_minutes = payload.appointment_duration_minutes
    db.add(settings)
    if organization.industry == "clinics" and settings.is_assignable:
        clinic_res = await db.execute(
            select(ClinicMemberScheduleSettings).where(
                ClinicMemberScheduleSettings.organization_id == organization.id,
                ClinicMemberScheduleSettings.member_id == member_id,
            )
        )
        clinic_settings = clinic_res.scalars().first()
        if clinic_settings is None:
            clinic_settings = ClinicMemberScheduleSettings(
                id=uuid.uuid4(),
                organization_id=organization.id,
                member_id=member_id,
            )
        if not clinic_settings.consultation_types:
            clinic_settings.consultation_types = sorted(allowed_consultation_types(organization.industry))
        db.add(clinic_settings)
    await db.commit()
    await db.refresh(settings)
    records = await NokvoOneAssignmentService._load_request_records(db, organization.id)
    return _assignment_response(settings, NokvoOneAssignmentService._active_load(records, member_id))


@router.get("/{member_id}/clinic-schedule-settings", response_model=NokvoOneClinicScheduleSettingsResponse)
async def get_clinic_schedule_settings(
    member_id: uuid.UUID,
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(allowed_statuses=["pending_approval", "active"])
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    organization = await _get_organization(db, user.organization_id)
    if organization.industry != "clinics":
        raise HTTPException(status_code=404, detail="Clinic scheduling is only available for Clinics")
    await _get_member(db, organization.id, member_id)
    res = await db.execute(
        select(ClinicMemberScheduleSettings).where(
            ClinicMemberScheduleSettings.organization_id == organization.id,
            ClinicMemberScheduleSettings.member_id == member_id,
        )
    )
    settings = res.scalars().first()
    if settings is None:
        return NokvoOneClinicScheduleSettingsResponse(
            organization_id=organization.id,
            member_id=member_id,
            appointment_duration_minutes=30,
            buffer_minutes=0,
            consultation_types=[],
        )
    return NokvoOneClinicScheduleSettingsResponse(
        id=settings.id,
        organization_id=settings.organization_id,
        member_id=settings.member_id,
        appointment_duration_minutes=settings.appointment_duration_minutes,
        buffer_minutes=settings.buffer_minutes,
        max_patients_per_hour=settings.max_patients_per_hour,
        max_patients_per_day=settings.max_patients_per_day,
        consultation_types=list(settings.consultation_types or []),
    )


@router.put("/{member_id}/clinic-schedule-settings", response_model=NokvoOneClinicScheduleSettingsResponse)
async def update_clinic_schedule_settings(
    member_id: uuid.UUID,
    payload: NokvoOneClinicScheduleSettingsUpdateRequest,
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(
            allowed_statuses=["pending_approval", "active"],
            allowed_roles=["admin", "manager"],
        )
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    organization = await _get_organization(db, user.organization_id)
    if organization.industry != "clinics":
        raise HTTPException(status_code=404, detail="Clinic scheduling is only available for Clinics")
    await _get_member(db, organization.id, member_id)
    consultation_types = _validate_consultation_types_for_org(organization, payload.consultation_types)
    res = await db.execute(
        select(ClinicMemberScheduleSettings).where(
            ClinicMemberScheduleSettings.organization_id == organization.id,
            ClinicMemberScheduleSettings.member_id == member_id,
        )
    )
    settings = res.scalars().first()
    if settings is None:
        settings = ClinicMemberScheduleSettings(
            id=uuid.uuid4(),
            organization_id=organization.id,
            member_id=member_id,
        )
    settings.appointment_duration_minutes = payload.appointment_duration_minutes
    settings.buffer_minutes = payload.buffer_minutes
    settings.max_patients_per_hour = payload.max_patients_per_hour
    settings.max_patients_per_day = payload.max_patients_per_day
    settings.consultation_types = consultation_types
    db.add(settings)
    await db.commit()
    await db.refresh(settings)
    return NokvoOneClinicScheduleSettingsResponse(
        id=settings.id,
        organization_id=settings.organization_id,
        member_id=settings.member_id,
        appointment_duration_minutes=settings.appointment_duration_minutes,
        buffer_minutes=settings.buffer_minutes,
        max_patients_per_hour=settings.max_patients_per_hour,
        max_patients_per_day=settings.max_patients_per_day,
        consultation_types=list(settings.consultation_types or []),
    )


@router.get("/{member_id}/blocked-slots", response_model=list[NokvoOneBlockedSlotResponse])
async def list_blocked_slots(
    member_id: uuid.UUID,
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(allowed_statuses=["pending_approval", "active"])
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    organization = await _get_organization(db, user.organization_id)
    await _get_member(db, organization.id, member_id)
    res = await db.execute(
        select(MemberBlockedSlot)
        .where(MemberBlockedSlot.organization_id == organization.id, MemberBlockedSlot.member_id == member_id)
        .order_by(MemberBlockedSlot.start_time.asc())
    )
    return [NokvoOneBlockedSlotResponse.model_validate(slot) for slot in res.scalars().all()]


@router.post("/{member_id}/blocked-slots", response_model=NokvoOneBlockedSlotResponse, status_code=status.HTTP_201_CREATED)
async def create_blocked_slot(
    member_id: uuid.UUID,
    payload: NokvoOneBlockedSlotCreateRequest,
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(
            allowed_statuses=["pending_approval", "active"],
            allowed_roles=["admin", "manager"],
        )
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    organization = await _get_organization(db, user.organization_id)
    await _get_member(db, organization.id, member_id)
    if payload.end_time <= payload.start_time:
        raise HTTPException(status_code=400, detail="Blocked slot end_time must be after start_time")
    slot = MemberBlockedSlot(
        id=uuid.uuid4(),
        organization_id=organization.id,
        member_id=member_id,
        start_time=payload.start_time,
        end_time=payload.end_time,
        reason=payload.reason,
        repeat_rule=payload.repeat_rule,
    )
    db.add(slot)
    await db.commit()
    await db.refresh(slot)
    return NokvoOneBlockedSlotResponse.model_validate(slot)


@router.delete("/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    member_id: uuid.UUID,
    inviter: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(
            allowed_statuses=["pending_approval", "active"],
            allowed_roles=["admin"],
        )
    ),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Soft-remove a member.

    Login is disabled (``password_hash`` and ``totp_secret`` cleared,
    ``status="removed"``) and any open invitations for them are
    revoked. We don't hard-delete the row because several FKs
    (``nokvo_one_agents.created_by``, ``nokvo_one_tool_records.created_by``,
    ``outgoing_leads.created_by_user_id``, ``invited_by`` self-references)
    don't cascade — preserving the row keeps audit trails intact and
    lets the admin re-invite later by clearing ``status``.
    """
    if member_id == inviter.id:
        raise HTTPException(status_code=400, detail="You can't remove your own account")
    member = await _get_member(db, inviter.organization_id, member_id)
    if member.status == "removed":
        # Idempotent — already gone.
        return None

    # Block removing the last remaining admin so the org doesn't lock
    # itself out.
    if member.role == "admin":
        other_admins_res = await db.execute(
            select(OrganizationUser).where(
                OrganizationUser.organization_id == inviter.organization_id,
                OrganizationUser.role == "admin",
                OrganizationUser.status != "removed",
                OrganizationUser.id != member.id,
            )
        )
        if other_admins_res.scalars().first() is None:
            raise HTTPException(
                status_code=400,
                detail="Can't remove the last admin. Promote another member to admin first.",
            )

    # Revoke any in-flight invitations for this user so the email link
    # can't be used after removal.
    now = datetime.now(timezone.utc)
    invites_res = await db.execute(
        select(MemberInvitation).where(
            MemberInvitation.organization_user_id == member.id,
            MemberInvitation.accepted_at.is_(None),
            MemberInvitation.revoked_at.is_(None),
        )
    )
    for invitation in invites_res.scalars().all():
        invitation.revoked_at = now
        db.add(invitation)

    member.status = "removed"
    member.password_hash = None
    member.totp_secret_encrypted_v2 = None
    member.email_verified = False
    db.add(member)
    await db.commit()
    return None


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
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
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

    # Re-invite policy: if a user row already exists for this email we
    # only block when the seat is actively in use (``active`` /
    # ``pending_totp`` — they've started or finished onboarding). Rows
    # in ``invited`` (email never reached them) or ``removed`` (admin
    # took them out and now wants them back) are recycled so we don't
    # accumulate orphan rows + the admin can always resend.
    existing_res = await db.execute(
        select(OrganizationUser).where(
            OrganizationUser.organization_id == organization.id,
            OrganizationUser.email == email,
        )
    )
    existing = existing_res.scalars().first()
    now = datetime.now(timezone.utc)
    if existing is not None:
        if existing.status in {"active", "pending_totp"}:
            raise HTTPException(
                status_code=409,
                detail="A member with this email already exists",
            )
        # Recycle the row. Reset auth state so the new invite link is
        # the only path to access; revoke any prior open invitations so
        # an older email link can't be redeemed concurrently.
        existing.role = payload.role
        existing.status = "invited"
        existing.full_name = payload.full_name or existing.full_name
        existing.invited_by = inviter.id
        existing.auth_provider = "password"
        existing.mfa_required = True
        existing.email_verified = False
        existing.password_hash = None
        existing.totp_secret_encrypted_v2 = None
        old_invites_res = await db.execute(
            select(MemberInvitation).where(
                MemberInvitation.organization_user_id == existing.id,
                MemberInvitation.accepted_at.is_(None),
                MemberInvitation.revoked_at.is_(None),
            )
        )
        for invitation in old_invites_res.scalars().all():
            invitation.revoked_at = now
            db.add(invitation)
        db.add(existing)
        invitee = existing
    else:
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
        expires_at=now + timedelta(hours=settings.NOKVO_ONE_INVITE_TOKEN_TTL_HOURS),
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
