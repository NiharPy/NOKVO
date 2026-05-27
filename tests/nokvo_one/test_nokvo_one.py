"""Unit tests for Nokvo One: TOTP crypto, predefined tools, schema validators, tier guard."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, time, timezone, timedelta

import pytest
from pydantic import ValidationError

from app.api.nokvo_one_auth import (
    nokvo_one_me,
    nokvo_one_save_business_template,
    nokvo_one_update_business_template_schema,
)
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser
from app.models.member_assignment import (
    ClinicMemberScheduleSettings,
    MemberBlockedSlot,
    NokvoOneAssignmentAuditLog,
    OrganizationMemberAssignmentSettings,
)
from app.models.nokvo_one_tool_record import NokvoOneToolRecord
from app.models.tenant_resources import TenantResources
from app.core.totp_crypto import (
    TOTPDecryptionError,
    decrypt_totp_secret,
    encrypt_totp_secret,
)
from app.schemas.nokvo_one import (
    NokvoOneAssignmentSettingsUpdateRequest,
    NokvoOneBusinessFieldDefinition,
    NokvoOneBusinessSchemaUpdateRequest,
    NokvoOneBusinessTemplateRequest,
    NokvoOneInvitationAcceptRequest,
    NokvoOneSignupRequest,
)
from app.services.nokvo_one_agent_runtime import NokvoOneAgentRuntime
from app.services.nokvo_one_assignment_service import NokvoOneAssignmentService
from app.services.nokvo_one_business_templates import member_label_for_business_type
from app.services.predefined_tools_service import (
    CROSS_CUTTING_CATALOG as CATALOG,
    PredefinedToolsService,
    get_tool,
    list_tools,
    validate_cross_cutting_keys as validate_tool_keys,
)


# ─────────── TOTP crypto ───────────


def test_totp_encryption_roundtrip():
    plaintext = "JBSWY3DPEHPK3PXP"
    ciphertext = encrypt_totp_secret(plaintext)
    assert ciphertext != plaintext
    assert decrypt_totp_secret(ciphertext) == plaintext


def test_totp_encryption_rejects_empty():
    with pytest.raises(ValueError):
        encrypt_totp_secret("")


def test_totp_decryption_rejects_garbage():
    with pytest.raises(TOTPDecryptionError):
        decrypt_totp_secret("not-a-valid-fernet-ciphertext")


def test_totp_ciphertexts_are_unique_per_invocation():
    """Fernet adds a nonce, so the same plaintext encrypts to different ciphertexts."""
    plaintext = "JBSWY3DPEHPK3PXP"
    a = encrypt_totp_secret(plaintext)
    b = encrypt_totp_secret(plaintext)
    assert a != b
    assert decrypt_totp_secret(a) == plaintext
    assert decrypt_totp_secret(b) == plaintext


# ─────────── Predefined tools catalog ───────────


def test_cross_cutting_catalog_has_expected_tools():
    expected = {
        "call_log_create",
        "call_log_search",
        "schedule_callback",
        "send_email_draft",
        "escalate_to_human",
        "find_contact",
    }
    actual = {tool.key for tool in CATALOG}
    assert actual == expected, f"Cross-cutting catalog drift: missing={expected - actual} extra={actual - expected}"


def test_catalog_excludes_dangerous_tools():
    """V1 must not ship web_search, direct DB writes, or payment/refund tools."""
    forbidden = {"web_search", "execute_sql", "refund_payment", "modify_order", "create_payment"}
    actual = {tool.key for tool in CATALOG}
    assert not (forbidden & actual), f"Dangerous tools must not be present: {forbidden & actual}"


def test_send_email_draft_requires_confirmation():
    tool = get_tool("send_email_draft")
    assert tool is not None
    assert tool.requires_confirmation is True, "send_email_draft must require human confirmation"


def test_send_email_draft_description_explains_no_direct_send():
    tool = get_tool("send_email_draft")
    assert tool is not None
    desc = tool.description.lower()
    assert "draft" in desc
    assert "human" in desc or "confirmation" in desc


def test_list_tools_returns_serialisable_dicts():
    items = list_tools()
    # Cross-cutting catalog: call_log_create/search, schedule_callback,
    # send_email_draft, escalate_to_human, find_contact = 6.
    assert len(items) == 6
    for item in items:
        assert {"key", "display_name", "description", "input_schema", "requires_confirmation"} <= set(item.keys())


def test_validate_tool_keys_rejects_unknown():
    with pytest.raises(ValueError):
        validate_tool_keys(["send_email_draft", "web_search"])


def test_validate_tool_keys_resolves_legacy_keys():
    # legacy keys are now aliased to the new names; validator returns the resolved set.
    out = validate_tool_keys(["call_logger_get_history", "schedule_callback"])
    assert "call_log_search" in out
    assert "schedule_callback" in out


# ─────────── Schema validators ───────────


def test_signup_password_validator_rejects_short():
    with pytest.raises(ValidationError):
        NokvoOneSignupRequest(
            org_name="Acme",
            admin_name="A",
            admin_email="a@acmecorp.com",
            password="short1",
        )


def test_signup_password_validator_requires_letter_and_digit():
    with pytest.raises(ValidationError):
        NokvoOneSignupRequest(
            org_name="Acme",
            admin_name="A",
            admin_email="a@acmecorp.com",
            password="onlyletters",
        )
    with pytest.raises(ValidationError):
        NokvoOneSignupRequest(
            org_name="Acme",
            admin_name="A",
            admin_email="a@acmecorp.com",
            password="1234567890",
        )


def test_signup_rejects_personal_email():
    with pytest.raises(ValidationError):
        NokvoOneSignupRequest(
            org_name="Acme",
            admin_name="A",
            admin_email="a@gmail.com",
            password="ValidPass123",
        )


def test_signup_accepts_valid_payload():
    req = NokvoOneSignupRequest(
        org_name="Acme Inc",
        admin_name="Alice",
        admin_email="alice@acmecorp.com",
        password="ValidPass123",
    )
    assert req.admin_email == "alice@acmecorp.com"
    assert req.org_name == "Acme Inc"
    assert req.industry is None


def test_business_template_request_accepts_allowed_values():
    for value in ["real_estate", "clinics", "ecommerce", "hospitality", "other"]:
        assert NokvoOneBusinessTemplateRequest(business_type=value).business_type == value


def test_business_template_request_rejects_invalid_value():
    with pytest.raises(ValidationError):
        NokvoOneBusinessTemplateRequest(business_type="manufacturing")


def test_invite_accept_password_validator():
    with pytest.raises(ValidationError):
        NokvoOneInvitationAcceptRequest(token="abc", password="weak")
    ok = NokvoOneInvitationAcceptRequest(token="abc", password="StrongPass1")
    assert ok.password == "StrongPass1"


# ─────────── Predefined tools dispatcher (DB-mocked) ───────────


class _FakeDB:
    def __init__(self):
        self.added = []
        self.flushed = False
        self.rolled_back = False

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        pass

    async def rollback(self):
        self.rolled_back = True

    async def execute(self, _stmt):
        class _Result:
            def scalars(self):
                return self

            def first(self):
                return None

            def all(self):
                return []

        return _Result()


class _FakeOrgDB:
    def __init__(self, organization, tenant_resources=None):
        self.organization = organization
        self.tenant_resources = tenant_resources or TenantResources(
            id=uuid.uuid4(),
            organization_id=organization.id,
            tenant_id=f"tenant-{organization.id}",
            provider_status={},
            provisioning_status="success",
        )
        self.added = []
        self.committed = False
        self.refreshed = []

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        self.refreshed.append(obj)

    async def execute(self, stmt):
        item = self.tenant_resources if "tenant_resources" in str(stmt) else self.organization

        class _Result:
            def scalars(self):
                return self

            def first(self):
                return item

        return _Result()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_send_email_draft_dispatch_creates_pending_confirmation():
    db = _FakeDB()
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    result = _run(
        PredefinedToolsService.execute(
            db,
            org_id,
            user_id,
            "send_email_draft",
            {
                "to_email": "customer@example.com",
                "subject": "Follow-up",
                "body": "Hi there, thanks for reaching out.",
            },
        )
    )
    assert result["ok"] is True
    assert result["status"] == "pending_confirmation"
    assert "human" in result["message"].lower() or "confirm" in result["message"].lower()
    # One side-effect row (the draft) plus one audit row (AgentToolInvocation).
    drafts = [o for o in db.added if getattr(o, "record_type", None) == "email_draft"]
    assert len(drafts) == 1
    record = drafts[0]
    assert record.status == "pending_confirmation"


def test_unknown_tool_rejected_by_dispatcher():
    db = _FakeDB()
    with pytest.raises(ValueError):
        _run(
            PredefinedToolsService.execute(
                db, uuid.uuid4(), uuid.uuid4(), "web_search", {"query": "x"}
            )
        )


def test_tool_dispatcher_rejects_missing_required_fields_before_side_effects():
    db = _FakeDB()
    with pytest.raises(ValueError) as exc:
        _run(
            PredefinedToolsService.execute(
                db,
                uuid.uuid4(),
                uuid.uuid4(),
                "schedule_callback",
                {"contact_phone": "7569672503"},
            )
        )
    assert "callback_at" in str(exc.value)
    assert db.added == []


def _org_and_user(industry=None):
    org_id = uuid.uuid4()
    organization = Organization(
        id=org_id,
        name="Acme Clinic",
        admin_email="admin@acmeclinic.com",
        email_domain="acmeclinic.com",
        region="southindia",
        environment="staging",
        product_tier="nokvo_one",
        status="active",
        calling_enabled=False,
        industry=industry,
    )
    user = OrganizationUser(
        id=uuid.uuid4(),
        organization_id=org_id,
        email="admin@acmeclinic.com",
        full_name="Admin",
        role="admin",
        status="active",
        auth_provider="password",
        mfa_required=True,
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    return organization, user


class _FakeAssignmentDB:
    def __init__(self, organization, members, settings, records=None, clinic_settings=None, blocked_slots=None):
        self.organization = organization
        self.members = members
        self.settings = settings
        self.records = records or []
        self.clinic_settings = clinic_settings or []
        self.blocked_slots = blocked_slots or []
        self.audits = []
        self.flushed = False
        self.committed = False

    def add(self, obj):
        if isinstance(obj, NokvoOneToolRecord) and obj not in self.records:
            obj.created_at = obj.created_at or datetime.now(timezone.utc)
            self.records.append(obj)
        elif isinstance(obj, NokvoOneAssignmentAuditLog):
            self.audits.append(obj)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass

    async def execute(self, stmt):
        text = str(stmt).lower()
        if "organization_member_assignment_settings" in text:
            rows = self.settings
        elif "clinic_member_schedule_settings" in text:
            rows = self.clinic_settings
        elif "member_blocked_slots" in text:
            rows = self.blocked_slots
        elif "nokvo_one_tool_records" in text:
            rows = self.records
            params = getattr(stmt.compile(), "params", {})
            requested_id = params.get("id_1")
            if requested_id is not None:
                rows = [row for row in rows if row.id == requested_id]
        elif "organization_users" in text:
            rows = self.members
        elif "organizations" in text:
            rows = [self.organization]
        else:
            rows = []

        class _Result:
            def __init__(self, items):
                self.items = items

            def scalars(self):
                return self

            def first(self):
                return self.items[0] if self.items else None

            def all(self):
                return self.items

        return _Result(rows)


def _member(org_id, name, created_offset=0):
    return OrganizationUser(
        id=uuid.uuid4(),
        organization_id=org_id,
        email=f"{name.lower()}@acme.com",
        full_name=name,
        role="member",
        status="active",
        auth_provider="password",
        mfa_required=True,
        email_verified=True,
        created_at=datetime(2026, 5, 16, 8, created_offset, tzinfo=timezone.utc),
    )


def _settings(
    org_id,
    member_id,
    request_types=None,
    start=time(9, 0),
    end=time(18, 0),
    max_active=3,
    max_day=None,
    max_hour=6,
):
    return OrganizationMemberAssignmentSettings(
        id=uuid.uuid4(),
        organization_id=org_id,
        member_id=member_id,
        is_assignable=True,
        working_days=["sat"],
        start_time=start,
        end_time=end,
        timezone="UTC",
        request_types=request_types or ["property_inquiry"],
        max_active_requests=max_active,
        max_requests_per_day=max_day,
        max_requests_per_hour=max_hour,
    )


def _assigned_record(org_id, member_id, status="assigned", created_at=None, request_type="property_inquiry"):
    return NokvoOneToolRecord(
        id=uuid.uuid4(),
        organization_id=org_id,
        record_type="request",
        status=status,
        data={
            "assigned_member_id": str(member_id),
            "request_type": request_type,
            "requested_time": (created_at or datetime(2026, 5, 16, 10, 0, tzinfo=timezone.utc)).isoformat(),
        },
        created_at=created_at or datetime(2026, 5, 16, 10, 0, tzinfo=timezone.utc),
    )


def test_save_business_template_updates_organization_industry():
    organization, user = _org_and_user(industry=None)
    db = _FakeOrgDB(organization)
    response = _run(
        nokvo_one_save_business_template(
            NokvoOneBusinessTemplateRequest(business_type="clinics"),
            user=user,
            db=db,
        )
    )
    assert organization.industry == "clinics"
    assert db.committed is True
    assert response.organization.industry == "clinics"
    assert response.business_template.value == "clinics"
    assert "appointments" in response.business_template.tabs


def test_me_response_includes_business_type():
    organization, user = _org_and_user(industry="ecommerce")
    response = _run(nokvo_one_me(user=user, db=_FakeOrgDB(organization), token="access-token"))
    assert response.organization.industry == "ecommerce"
    assert response.access_token == "access-token"


def test_business_member_labels_are_template_specific():
    assert member_label_for_business_type("clinics") == "Doctors / Staff"
    assert member_label_for_business_type("real_estate") == "Agents"
    assert member_label_for_business_type("ecommerce") == "Team Members"
    assert member_label_for_business_type("hospitality") == "Staff"
    assert member_label_for_business_type("other") == "Members"


def test_assignment_settings_accept_only_ist_timezone():
    payload = NokvoOneAssignmentSettingsUpdateRequest(timezone="IST", max_requests_per_hour=8)
    assert payload.timezone == "Asia/Kolkata"
    assert payload.max_requests_per_hour == 8
    with pytest.raises(ValidationError):
        NokvoOneAssignmentSettingsUpdateRequest(timezone="UTC")


def test_assignment_selects_lowest_load_member_and_audits_decision():
    organization, _ = _org_and_user(industry="real_estate")
    a = _member(organization.id, "AgentA")
    b = _member(organization.id, "AgentB", created_offset=1)
    db = _FakeAssignmentDB(
        organization,
        [a, b],
        [_settings(organization.id, a.id), _settings(organization.id, b.id)],
        records=[_assigned_record(organization.id, a.id)],
    )
    result = _run(
        NokvoOneAssignmentService.assign_request(
            db,
            organization,
            request_type="property_inquiry",
            requested_time=datetime(2026, 5, 16, 11, 0, tzinfo=timezone.utc),
        )
    )
    assert result["assignment_status"] == "assigned"
    assert result["selected_member_id"] == b.id
    assert result["active_load_count"] == 0
    assert db.audits[-1].selected_member_id == b.id


def test_assignment_prefers_other_member_same_time_over_first_member_next_time():
    """If Member A's 10am is booked and Member B's 10am is free, the
    agent must propose Member B at 10am — never Member A at 11am.

    Regression for the operator complaint that the agent was shifting
    the FIRST member to the next slot rather than handing the same
    time to another available member. The sort in ``assign_request``
    orders by ``shift_minutes`` first, so a zero-shift candidate
    always beats a shifted one regardless of which member came first
    in the roster.
    """
    organization, _ = _org_and_user(industry="real_estate")
    a = _member(organization.id, "AgentA")
    b = _member(organization.id, "AgentB", created_offset=1)
    settings_list = [
        _settings(organization.id, a.id, request_types=["property_inquiry"]),
        _settings(organization.id, b.id, request_types=["property_inquiry"]),
    ]
    # Booking the 10am slot for Member A only. The standard slot
    # duration is 30 minutes; Member A's next free slot is therefore
    # 10:30 (shift=30). Member B has nothing on the calendar so
    # 10am itself (shift=0) is available.
    existing_for_a = _assigned_record(
        organization.id,
        a.id,
        created_at=datetime(2026, 5, 16, 10, 0, tzinfo=timezone.utc),
    )
    db = _FakeAssignmentDB(
        organization,
        [a, b],
        settings_list,
        records=[existing_for_a],
    )
    result = _run(
        NokvoOneAssignmentService.assign_request(
            db,
            organization,
            request_type="property_inquiry",
            requested_time=datetime(2026, 5, 16, 10, 0, tzinfo=timezone.utc),
        )
    )
    assert result["assignment_status"] == "assigned"
    # Member B's 10am wins over Member A's 10:30.
    assert result["selected_member_id"] == b.id
    assert result["scheduled_time"] == datetime(2026, 5, 16, 10, 0, tzinfo=timezone.utc)
    assert result["time_adjusted"] is False


def test_assignment_returns_no_available_when_all_members_skipped():
    organization, _ = _org_and_user(industry="real_estate")
    member = _member(organization.id, "AgentA")
    settings = _settings(organization.id, member.id, request_types=["site_visit"])
    db = _FakeAssignmentDB(organization, [member], [settings])
    result = _run(
        NokvoOneAssignmentService.assign_request(
            db,
            organization,
            request_type="property_inquiry",
            requested_time=datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc),
        )
    )
    assert result["assignment_status"] == "no_available_member"
    assert result["reason"] == "no_members_matched_assignment_rules"
    assert "request_type_not_supported" in result["skipped_member_reasons"][str(member.id)]
    assert db.audits[-1].assignment_status == "no_available_member"


def test_assignment_rejects_invalid_request_type():
    organization, _ = _org_and_user(industry="ecommerce")
    db = _FakeAssignmentDB(organization, [], [])
    with pytest.raises(ValueError):
        _run(
            NokvoOneAssignmentService.assign_request(
                db,
                organization,
                request_type="site_visit",
                requested_time=datetime(2026, 5, 16, 12, 0, tzinfo=timezone.utc),
            )
        )


def test_assignment_shifts_to_next_slot_when_hourly_capacity_reached():
    organization, _ = _org_and_user(industry="real_estate")
    member = _member(organization.id, "AgentA")
    settings = _settings(organization.id, member.id, max_hour=1)
    db = _FakeAssignmentDB(
        organization,
        [member],
        [settings],
        records=[
            _assigned_record(
                organization.id,
                member.id,
                created_at=datetime(2026, 5, 16, 11, 10, tzinfo=timezone.utc),
            )
        ],
    )
    result = _run(
        NokvoOneAssignmentService.assign_request(
            db,
            organization,
            request_type="property_inquiry",
            requested_time=datetime(2026, 5, 16, 11, 0, tzinfo=timezone.utc),
        )
    )
    assert result["assignment_status"] == "assigned"
    assert result["time_adjusted"] is True
    requested = datetime(2026, 5, 16, 11, 0, tzinfo=timezone.utc)
    assert result["scheduled_time"] > requested
    assert result["scheduled_time"].hour >= 11
    assert result["scheduled_time"] >= datetime(2026, 5, 16, 11, 40, tzinfo=timezone.utc)


def test_assignment_hourly_capacity_still_counts_completed_requests():
    organization, _ = _org_and_user(industry="real_estate")
    member = _member(organization.id, "AgentA")
    settings = _settings(organization.id, member.id, max_hour=1)
    db = _FakeAssignmentDB(
        organization,
        [member],
        [settings],
        records=[
            _assigned_record(
                organization.id,
                member.id,
                status="closed",
                created_at=datetime(2026, 5, 16, 11, 5, tzinfo=timezone.utc),
            )
        ],
    )
    result = _run(
        NokvoOneAssignmentService.assign_request(
            db,
            organization,
            request_type="property_inquiry",
            requested_time=datetime(2026, 5, 16, 11, 0, tzinfo=timezone.utc),
        )
    )
    assert result["assignment_status"] == "assigned"
    assert result["time_adjusted"] is True
    assert result["scheduled_time"] >= datetime(2026, 5, 16, 11, 35, tzinfo=timezone.utc)


def test_assignment_shifts_past_blocked_slots_for_non_clinic_members():
    organization, _ = _org_and_user(industry="real_estate")
    member = _member(organization.id, "AgentA")
    settings = _settings(organization.id, member.id)
    slot = MemberBlockedSlot(
        id=uuid.uuid4(),
        organization_id=organization.id,
        member_id=member.id,
        start_time=datetime(2026, 5, 16, 10, 30, tzinfo=timezone.utc),
        end_time=datetime(2026, 5, 16, 11, 30, tzinfo=timezone.utc),
    )
    db = _FakeAssignmentDB(organization, [member], [settings], blocked_slots=[slot])
    result = _run(
        NokvoOneAssignmentService.assign_request(
            db,
            organization,
            request_type="property_inquiry",
            requested_time=datetime(2026, 5, 16, 11, 0, tzinfo=timezone.utc),
        )
    )
    assert result["assignment_status"] == "assigned"
    assert result["time_adjusted"] is True
    assert result["scheduled_time"] >= datetime(2026, 5, 16, 11, 30, tzinfo=timezone.utc)


def test_clinic_assignment_shifts_past_blocked_slot():
    organization, _ = _org_and_user(industry="clinics")
    doctor = _member(organization.id, "Dr Rao")
    settings = _settings(organization.id, doctor.id, request_types=["appointment"])
    clinic_settings = ClinicMemberScheduleSettings(
        id=uuid.uuid4(),
        organization_id=organization.id,
        member_id=doctor.id,
        appointment_duration_minutes=30,
        buffer_minutes=0,
        max_patients_per_hour=4,
        max_patients_per_day=20,
        consultation_types=["appointment"],
    )
    slot = MemberBlockedSlot(
        id=uuid.uuid4(),
        organization_id=organization.id,
        member_id=doctor.id,
        start_time=datetime(2026, 5, 16, 10, 30, tzinfo=timezone.utc),
        end_time=datetime(2026, 5, 16, 11, 30, tzinfo=timezone.utc),
    )
    db = _FakeAssignmentDB(organization, [doctor], [settings], clinic_settings=[clinic_settings], blocked_slots=[slot])
    result = _run(
        NokvoOneAssignmentService.assign_request(
            db,
            organization,
            request_type="appointment",
            requested_time=datetime(2026, 5, 16, 11, 0, tzinfo=timezone.utc),
        )
    )
    assert result["assignment_status"] == "assigned"
    assert result["time_adjusted"] is True
    assert result["scheduled_time"] >= datetime(2026, 5, 16, 11, 30, tzinfo=timezone.utc)


def test_clinic_assignment_uses_base_schedule_when_clinic_schedule_missing():
    organization, _ = _org_and_user(industry="clinics")
    doctor = _member(organization.id, "Dr Rao")
    settings = _settings(organization.id, doctor.id, request_types=["appointment"])
    db = _FakeAssignmentDB(organization, [doctor], [settings])
    result = _run(
        NokvoOneAssignmentService.assign_request(
            db,
            organization,
            request_type="appointment",
            requested_time=datetime(2026, 5, 16, 11, 0, tzinfo=timezone.utc),
        )
    )
    assert result["assignment_status"] == "assigned"
    assert result["selected_member_name"] == "Dr Rao"


def test_clinic_assignment_shifts_past_hourly_patient_capacity():
    organization, _ = _org_and_user(industry="clinics")
    doctor = _member(organization.id, "Dr Rao")
    settings = _settings(organization.id, doctor.id, request_types=["appointment"])
    clinic_settings = ClinicMemberScheduleSettings(
        id=uuid.uuid4(),
        organization_id=organization.id,
        member_id=doctor.id,
        appointment_duration_minutes=30,
        buffer_minutes=0,
        max_patients_per_hour=1,
        max_patients_per_day=20,
        consultation_types=["appointment"],
    )
    db = _FakeAssignmentDB(
        organization,
        [doctor],
        [settings],
        records=[
            _assigned_record(
                organization.id,
                doctor.id,
                request_type="appointment",
                created_at=datetime(2026, 5, 16, 10, 45, tzinfo=timezone.utc),
            )
        ],
        clinic_settings=[clinic_settings],
    )
    result = _run(
        NokvoOneAssignmentService.assign_request(
            db,
            organization,
            request_type="appointment",
            requested_time=datetime(2026, 5, 16, 10, 30, tzinfo=timezone.utc),
        )
    )
    assert result["assignment_status"] == "assigned"
    assert result["time_adjusted"] is True
    assert result["scheduled_time"] >= datetime(2026, 5, 16, 11, 15, tzinfo=timezone.utc)


def test_appointments_create_assigns_available_clinic_doctor():
    from app.services.dynamic_tool_resolver import resolve_index

    organization, _ = _org_and_user(industry="clinics")
    doctor = _member(organization.id, "Dr Rao")
    settings = _settings(organization.id, doctor.id, request_types=["appointment"])
    clinic_settings = ClinicMemberScheduleSettings(
        id=uuid.uuid4(),
        organization_id=organization.id,
        member_id=doctor.id,
        appointment_duration_minutes=30,
        buffer_minutes=0,
        max_patients_per_hour=4,
        max_patients_per_day=20,
        consultation_types=["appointment"],
    )
    db = _FakeAssignmentDB(organization, [doctor], [settings], clinic_settings=[clinic_settings])
    tool = resolve_index("clinics")["appointments_create"]
    result = _run(
        PredefinedToolsService.execute(
            db,
            organization.id,
            uuid.uuid4(),
            tool,
            {
                "patient_name": "Ravi Kumar",
                "phone": "7569672503",
                "appointment_time": "2026-05-16T11:00:00+00:00",
                "reason": "Red eye consultation",
            },
        )
    )
    assert result["ok"] is True
    assert result["record_type"] == "appointment"
    assert result["assignment_status"] == "assigned"
    assert result["assigned_member_name"] == "Dr Rao"
    appointment = next(r for r in db.records if r.record_type == "appointment")
    assert appointment.status == "assigned"
    assert appointment.data["doctor"] == "Dr Rao"
    assert appointment.data["assigned_doctor_id"] == str(doctor.id)
    assert db.audits[-1].selected_member_id == doctor.id


def test_appointments_back_to_back_get_next_slot_per_duration():
    """Screenshot scenario: agent tries to double-book 11:00 — second booking should
    move to the next free slot based on appointment_duration_minutes."""
    from app.services.dynamic_tool_resolver import resolve_index

    organization, _ = _org_and_user(industry="clinics")
    doctor = _member(organization.id, "Dr Rao")
    settings = _settings(organization.id, doctor.id, request_types=["appointment"])
    clinic_settings = ClinicMemberScheduleSettings(
        id=uuid.uuid4(),
        organization_id=organization.id,
        member_id=doctor.id,
        appointment_duration_minutes=30,
        buffer_minutes=0,
        max_patients_per_hour=4,
        max_patients_per_day=20,
        consultation_types=["appointment"],
    )
    db = _FakeAssignmentDB(organization, [doctor], [settings], clinic_settings=[clinic_settings])
    tool = resolve_index("clinics")["appointments_create"]

    first = _run(
        PredefinedToolsService.execute(
            db,
            organization.id,
            uuid.uuid4(),
            tool,
            {
                "patient_name": "Patient A",
                "phone": "9999999991",
                "appointment_time": "2026-05-23T11:00:00+00:00",
                "reason": "Checkup",
            },
        )
    )
    second = _run(
        PredefinedToolsService.execute(
            db,
            organization.id,
            uuid.uuid4(),
            tool,
            {
                "patient_name": "Patient B",
                "phone": "9999999992",
                "appointment_time": "2026-05-23T11:00:00+00:00",
                "reason": "Checkup",
            },
        )
    )
    third = _run(
        PredefinedToolsService.execute(
            db,
            organization.id,
            uuid.uuid4(),
            tool,
            {
                "patient_name": "Patient C",
                "phone": "9999999993",
                "appointment_time": "2026-05-23T11:00:00+00:00",
                "reason": "Checkup",
            },
        )
    )
    assert first["assignment_status"] == "assigned"
    assert first["time_adjusted"] is False
    assert second["assignment_status"] == "assigned"
    assert second["time_adjusted"] is True
    assert str(second["scheduled_time"]).startswith("2026-05-23T11:30")
    assert third["assignment_status"] == "assigned"
    assert third["time_adjusted"] is True
    assert str(third["scheduled_time"]).startswith("2026-05-23T12:00")


def test_appointments_create_shifts_to_next_working_window():
    from app.services.dynamic_tool_resolver import resolve_index

    organization, _ = _org_and_user(industry="clinics")
    doctor = _member(organization.id, "Dr Rao")
    settings = _settings(organization.id, doctor.id, request_types=["appointment"], start=time(9, 0), end=time(10, 0))
    clinic_settings = ClinicMemberScheduleSettings(
        id=uuid.uuid4(),
        organization_id=organization.id,
        member_id=doctor.id,
        appointment_duration_minutes=30,
        buffer_minutes=0,
        max_patients_per_hour=4,
        max_patients_per_day=20,
        consultation_types=["appointment"],
    )
    db = _FakeAssignmentDB(organization, [doctor], [settings], clinic_settings=[clinic_settings])
    tool = resolve_index("clinics")["appointments_create"]
    result = _run(
        PredefinedToolsService.execute(
            db,
            organization.id,
            uuid.uuid4(),
            tool,
            {
                "patient_name": "Ravi Kumar",
                "phone": "7569672503",
                "appointment_time": "2026-05-16T12:00:00+00:00",
                "reason": "Red eye consultation",
            },
        )
    )
    assert result["assignment_status"] == "assigned"
    assert result["time_adjusted"] is True
    appointment = next(r for r in db.records if r.record_type == "appointment")
    assert appointment.status == "assigned"
    assert appointment.data["time_adjusted"] is True
    assert appointment.data["original_requested_time"] == "2026-05-16T12:00:00+00:00"


def test_real_estate_site_visit_macro_assigns_available_agent():
    from app.services.dynamic_tool_resolver import resolve_index

    organization, _ = _org_and_user(industry="real_estate")
    agent = _member(organization.id, "Agent Priya")
    settings = _settings(organization.id, agent.id, request_types=["site_visit"])
    db = _FakeAssignmentDB(organization, [agent], [settings])
    tool = resolve_index("real_estate")["qualify_lead_and_schedule_visit"]
    result = _run(
        PredefinedToolsService.execute(
            db,
            organization.id,
            uuid.uuid4(),
            tool,
            {
                "name": "Buyer One",
                "phone": "7569672503",
                "property_type": "Apartment",
                "location": "KPHB",
                "visit_at": "2026-05-16T11:00:00+00:00",
            },
        )
    )
    assert result["assignment_status"] == "assigned"
    assert result["assigned_member_name"] == "Agent Priya"
    callback = next(r for r in db.records if r.record_type == "callback")
    assert callback.status == "assigned"
    assert callback.data["agent"] == "Agent Priya"
    assert callback.data["assigned_agent_id"] == str(agent.id)


def test_clinic_emergency_creates_escalation_without_normal_assignment():
    organization, _ = _org_and_user(industry="clinics")
    doctor = _member(organization.id, "Dr Rao")
    settings = _settings(organization.id, doctor.id, request_types=["emergency_escalation"])
    db = _FakeAssignmentDB(organization, [doctor], [settings])
    result = _run(
        NokvoOneAssignmentService.assign_request(
            db,
            organization,
            request_type="emergency_escalation",
            requested_time=datetime(2026, 5, 16, 11, 0, tzinfo=timezone.utc),
            summary="Patient has chest pain and breathing trouble",
        )
    )
    assert result["assignment_status"] == "no_available_member"
    assert result["reason"] == "urgent_escalation_requires_staff_review"
    assert db.records[-1].data["urgent_escalation"] is True


def test_update_business_template_schema_saves_org_override():
    organization, user = _org_and_user(industry="real_estate")
    tenant_res = TenantResources(
        id=uuid.uuid4(),
        organization_id=organization.id,
        tenant_id=f"tenant-{organization.id}",
        provider_status={},
        provisioning_status="success",
    )
    db = _FakeOrgDB(organization, tenant_resources=tenant_res)
    response = _run(
        nokvo_one_update_business_template_schema(
            "leads",
            NokvoOneBusinessSchemaUpdateRequest(
                fields=[
                    NokvoOneBusinessFieldDefinition(
                        key="buyer_name",
                        label="Buyer Name",
                        type="text",
                        required=True,
                    ),
                    NokvoOneBusinessFieldDefinition(
                        key="budget",
                        label="Budget",
                        type="currency",
                        required=False,
                    ),
                ]
            ),
            user=user,
            db=db,
        )
    )

    overrides = tenant_res.provider_status["business_template_schema_overrides"]
    assert overrides["leads"][0]["label"] == "Buyer Name"
    assert response.schemas["leads"][0]["label"] == "Buyer Name"
    assert db.committed is True


def test_runtime_injects_business_template_prompt(monkeypatch):
    captured = {}

    async def fake_chat(messages):
        captured["messages"] = messages
        return "ok"

    monkeypatch.setattr("app.services.nokvo_one_agent_runtime._AzureOpenAIClient.chat", fake_chat)
    result = _run(
        NokvoOneAgentRuntime.chat_turn(
            _FakeDB(),
            organization_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            agent_system_prompt=None,
            business_type="clinics",
            tool_keys=[],
            user_message="Hello",
        )
    )

    system_prompt = captured["messages"][0]["content"]
    assert result["reply"] == "ok"
    assert "Global Nokvo rules:" in system_prompt
    assert "Business template rules:" in system_prompt
    assert "Agent custom prompt:" in system_prompt
    assert "Business Type: Clinics" in system_prompt
    assert "appointments" in system_prompt.lower()
    assert system_prompt.index("Global Nokvo rules:") < system_prompt.index("Business template rules:")
    assert system_prompt.index("Business template rules:") < system_prompt.index("Agent custom prompt:")
    assert system_prompt.index("Agent custom prompt:") < system_prompt.index("RAG rules:")
    assert system_prompt.index("RAG rules:") < system_prompt.index("Member and availability rules:")
    assert system_prompt.index("Member and availability rules:") < system_prompt.index("Tool rules:")
    assert system_prompt.index("Tool rules:") < system_prompt.index("Escalation rules:")


def test_runtime_turns_missing_tool_fields_into_clarifying_reply(monkeypatch):
    responses = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "schedule_callback",
                        "arguments": '{"contact_phone": "7569672503"}',
                    },
                }
            ],
        },
        {"role": "assistant", "content": "I need one more detail before I can do that: callback at."},
    ]

    async def fake_chat_with_tools(messages, tools):
        return responses.pop(0)

    monkeypatch.setattr(
        "app.services.nokvo_one_agent_runtime._AzureOpenAIClient.chat_with_tools",
        fake_chat_with_tools,
    )
    db = _FakeDB()
    result = _run(
        NokvoOneAgentRuntime.chat_turn(
            db,
            organization_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            agent_system_prompt=None,
            business_type="clinics",
            tool_keys=["schedule_callback"],
            user_message="Please call me back on 7569672503",
        )
    )
    assert "callback at" in result["reply"].lower()
    assert result["tool_calls"][0]["ok"] is False
    assert db.rolled_back is True


def test_tickets_create_dispatch():
    from app.services.dynamic_tool_resolver import resolve_index

    db = _FakeDB()
    tool = resolve_index("ecommerce")["tickets_create"]
    result = _run(
        PredefinedToolsService.execute(
            db,
            uuid.uuid4(),
            uuid.uuid4(),
            tool,
            {
                "subject": "Login broken",
                "customer_name": "Sample User",
                "issue_type": "complaint",
                "priority": "high",
            },
        )
    )
    assert result["ok"] is True
    assert result["record_type"] == "ticket"
    # ecommerce tickets vocab initial = "open"
    assert result["status"] == "open"
    record = next(o for o in db.added if hasattr(o, "record_type") and o.record_type == "ticket")
    assert record.status == "open"


# ─────────── Voice stream: are-you-there ack during composing latency ───────────


def test_check_in_utterance_matches_common_phrases():
    from app.services.nokvo_one_voice_stream_service import _is_check_in_utterance

    assert _is_check_in_utterance("hello?") is True
    assert _is_check_in_utterance("Hello are you there?") is True
    assert _is_check_in_utterance("Are you there?") is True
    assert _is_check_in_utterance("can you hear me") is True
    assert _is_check_in_utterance("still there?") is True
    assert _is_check_in_utterance("हलो") is True


def test_check_in_utterance_rejects_real_questions():
    from app.services.nokvo_one_voice_stream_service import _is_check_in_utterance

    assert _is_check_in_utterance("what are your business hours") is False
    assert _is_check_in_utterance("can you book me an appointment for tomorrow") is False
    assert _is_check_in_utterance("") is False
    # Long utterances are never check-ins even if they contain "hello"
    assert _is_check_in_utterance("hello I would like to ask about my last appointment please") is False


def test_quick_ack_text_localised_for_indian_languages():
    from app.services.nokvo_one_voice_stream_service import _quick_ack_text

    assert "Yes" in _quick_ack_text("en")
    assert _quick_ack_text("hi").startswith("जी")
    assert _quick_ack_text("te").startswith("అవును")
    assert _quick_ack_text(None).startswith("Yes")


# ─────────── Slot fill: name extraction + past-time guard ───────────


def test_patient_name_extracted_from_verbose_phrasing():
    from app.services.voice_turn_policy import _extract_patient_name

    assert _extract_patient_name("Nivas Kondapalli") == "Nivas Kondapalli"
    assert _extract_patient_name("The full name of the patient is Nivas Kondapalli") == "Nivas Kondapalli"
    assert _extract_patient_name("The patient's name is Nivas Kondapalli") == "Nivas Kondapalli"
    assert _extract_patient_name("Patient name is Nivas Kondapalli") == "Nivas Kondapalli"
    assert _extract_patient_name("Her name is Anita Rao") == "Anita Rao"
    assert _extract_patient_name("My name is Ravi") == "Ravi"
    assert _extract_patient_name("I am Said Nihar Rao") == "Said Nihar Rao"


def test_patient_name_rejects_non_names():
    from app.services.voice_turn_policy import _extract_patient_name

    assert _extract_patient_name("It's actually about eye pain") is None
    assert _extract_patient_name("What time can I come in") is None
    # Trailing digits would be a phone, not a name
    assert _extract_patient_name("Patient name is 1234") is None


def test_appointment_past_time_guard_clears_date_and_time(monkeypatch):
    """Same flow as the transcript: caller fills date+time that have already
    passed today; the policy should immediately re-prompt instead of marching
    through the remaining slots."""
    from datetime import datetime as _dt, timezone as _tz
    from app.services import voice_turn_policy

    # Freeze "now" to 2026-05-20 12:00 IST (so 11 AM today is in the past).
    fake_now = _dt(2026, 5, 20, 12, 0, tzinfo=voice_turn_policy._APPOINTMENT_LOCAL_TZ)

    class _FrozenDatetime(_dt):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fake_now.replace(tzinfo=None)
            return fake_now.astimezone(tz)

    monkeypatch.setattr(voice_turn_policy, "datetime", _FrozenDatetime)

    assert voice_turn_policy._appointment_is_past("20 May", "11 AM") is True
    assert voice_turn_policy._appointment_is_past("20 May", "11:30 AM") is True
    assert voice_turn_policy._appointment_is_past("20 May", "4 PM") is False
    assert voice_turn_policy._appointment_is_past("21 May", "9 AM") is False
    # Missing pieces should not trip the guard
    assert voice_turn_policy._appointment_is_past("20 May", None) is False
    assert voice_turn_policy._appointment_is_past(None, "4 PM") is False


# ─────────── Autonomy: inference, name confirm, smart past-time, availability ───────────


def _patch_now(monkeypatch, fake_now):
    from datetime import datetime as _dt
    from app.services import voice_turn_policy

    class _FrozenDatetime(_dt):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return fake_now.replace(tzinfo=None)
            return fake_now.astimezone(tz)

    monkeypatch.setattr(voice_turn_policy, "datetime", _FrozenDatetime)


def test_infer_first_visit_and_no_urgent_from_reason():
    from app.services.voice_turn_policy import _infer_implicit_slots

    appt = {}
    filled = _infer_implicit_slots("routine checkup, first visit, nothing urgent", appt)
    assert "visit_type" in filled
    assert "urgent_symptoms" in filled
    assert appt["visit_type"] == "first_visit"
    assert appt["urgent_symptoms"] == "none_reported"


def test_infer_does_not_overwrite_existing_slots():
    from app.services.voice_turn_policy import _infer_implicit_slots

    appt = {"visit_type": "follow_up", "urgent_symptoms": "none_reported"}
    filled = _infer_implicit_slots("routine checkup first visit", appt)
    assert filled == []
    assert appt["visit_type"] == "follow_up"


def test_name_confirmation_handshake_accepts_yes(monkeypatch):
    """After capturing patient_name the policy should ask to confirm; a
    follow-up "yes" continues to the next slot."""
    from app.services.voice_turn_policy import evaluate_voice_turn_policy
    from datetime import datetime as _dt
    from app.services import voice_turn_policy as vtp

    _patch_now(monkeypatch, _dt(2026, 5, 20, 9, 0, tzinfo=vtp._APPOINTMENT_LOCAL_TZ))

    state = {
        "appointment": {
            "active": True,
            "reason": "eye blurriness",
            "pending_slot": "patient_name",
        }
    }
    history = [{"role": "assistant", "content": "Could I have the patient's full name?"}]
    first = evaluate_voice_turn_policy(
        "The full name of the patient is Nivas Kondapalli",
        history=history, state=state, language="en",
    )
    assert first is not None
    assert first["state_slot"] == "patient_name_confirm"
    assert "Nivas Kondapalli" in first["answer"]
    appt = first["state_patch"]["appointment"]
    assert appt["patient_name"] == "Nivas Kondapalli"
    assert appt["awaiting_name_confirmation"] is True

    # Second turn: caller says "yes" → policy moves on without re-asking.
    history2 = history + [
        {"role": "user", "content": "The full name of the patient is Nivas Kondapalli"},
        {"role": "assistant", "content": first["answer"]},
    ]
    second = evaluate_voice_turn_policy(
        "yes that's right",
        history=history2,
        state={"appointment": appt},
        language="en",
    )
    assert second is not None
    next_appt = second["state_patch"]["appointment"]
    assert next_appt["awaiting_name_confirmation"] is False
    assert next_appt["patient_name"] == "Nivas Kondapalli"
    assert second["state_slot"] != "patient_name_confirm"


def test_name_confirmation_treats_no_as_request_to_recapture(monkeypatch):
    from app.services.voice_turn_policy import evaluate_voice_turn_policy
    from datetime import datetime as _dt
    from app.services import voice_turn_policy as vtp

    _patch_now(monkeypatch, _dt(2026, 5, 20, 9, 0, tzinfo=vtp._APPOINTMENT_LOCAL_TZ))

    state = {
        "appointment": {
            "active": True,
            "patient_name": "Nives Konda Padli",
            "awaiting_name_confirmation": True,
            "pending_slot": "patient_name_confirm",
        }
    }
    history = [{"role": "assistant", "content": "Just to confirm — the patient's name is Nives Konda Padli. Is that right?"}]
    result = evaluate_voice_turn_policy("no that's wrong", history=history, state=state, language="en")
    assert result is not None
    assert result["state_slot"] == "patient_name"
    assert result["state_patch"]["appointment"]["patient_name"] is None


def test_past_time_offers_next_day_instead_of_full_reset(monkeypatch):
    from app.services.voice_turn_policy import evaluate_voice_turn_policy
    from datetime import datetime as _dt
    from app.services import voice_turn_policy as vtp

    # 2026-05-20 12:00 IST: "20 May 11 AM" is past, "21 May 11 AM" is future
    _patch_now(monkeypatch, _dt(2026, 5, 20, 12, 0, tzinfo=vtp._APPOINTMENT_LOCAL_TZ))

    state = {
        "appointment": {
            "active": True,
            "reason": "checkup",
            "patient_name": "Ravi Kumar",
            "phone": "9177627064",
            "preferred_date": "20 May",
            "pending_slot": "preferred_time",
        }
    }
    history = [{"role": "assistant", "content": "What time would you prefer?"}]
    result = evaluate_voice_turn_policy("11 AM", history=history, state=state, language="en")
    assert result is not None
    assert result["state_slot"] == "preferred_date_shift_confirm"
    assert "11 AM" in result["answer"]
    assert "21 May" in result["answer"] or "tomorrow" in result["answer"].lower() or "21" in result["answer"]
    appt = result["state_patch"]["appointment"]
    assert appt["awaiting_past_time_shift"] is True
    assert appt["preferred_time"] == "11 AM"


def test_availability_query_returns_check_intent(monkeypatch):
    from app.services.voice_turn_policy import evaluate_voice_turn_policy
    from datetime import datetime as _dt
    from app.services import voice_turn_policy as vtp

    _patch_now(monkeypatch, _dt(2026, 5, 20, 12, 0, tzinfo=vtp._APPOINTMENT_LOCAL_TZ))

    state = {"appointment": {"active": True, "pending_slot": "preferred_time"}}
    history = [{"role": "assistant", "content": "What time would you prefer?"}]
    result = evaluate_voice_turn_policy(
        "Is 20 May 11:30 AM available?",
        history=history, state=state, language="en",
    )
    assert result is not None
    assert result["intent"] == "availability_check"
    assert result["entities"].get("time_text", "").lower().startswith("11")


# ─────────── Cross-business autonomy: tool_flow (real_estate / hospitality) ───────────


def test_tool_flow_detects_availability_query(monkeypatch):
    """The same "is X available?" detector that works for clinic appointments
    should also fire mid-flow for real-estate site visits."""
    from app.services.tool_flow_policy import evaluate_tool_flow_policy
    from datetime import datetime as _dt
    from app.services import voice_turn_policy as vtp

    _patch_now(monkeypatch, _dt(2026, 5, 20, 12, 0, tzinfo=vtp._APPOINTMENT_LOCAL_TZ))

    state = {
        "tool_flow": {
            "active": True,
            "flow_key": "real_estate_site_visit",
            "tool_key": "qualify_lead_and_schedule_visit",
            "collected": {"name": "Ravi", "phone": "9177627064"},
            "pending_slot": "visit_date",
        }
    }
    history = [{"role": "assistant", "content": "What date would you prefer?"}]
    result = evaluate_tool_flow_policy(
        "Is 25 May 11 AM available?",
        business_type="real_estate",
        history=history,
        state=state,
        language="en",
    )
    assert result is not None
    assert result["intent"] == "availability_check"
    assert result["flow_key"] == "real_estate_site_visit"
    assert result["entities"].get("time_text", "").lower().startswith("11")


def test_tool_flow_name_confirmation_handshake(monkeypatch):
    """Name-kind slot capture in real-estate flow asks the caller to confirm
    before moving on, mirroring the clinic patient-name behaviour."""
    from app.services.tool_flow_policy import evaluate_tool_flow_policy
    from datetime import datetime as _dt
    from app.services import voice_turn_policy as vtp

    _patch_now(monkeypatch, _dt(2026, 5, 20, 12, 0, tzinfo=vtp._APPOINTMENT_LOCAL_TZ))

    state = {
        "tool_flow": {
            "active": True,
            "flow_key": "real_estate_site_visit",
            "tool_key": "qualify_lead_and_schedule_visit",
            "collected": {},
            "pending_slot": "name",
        }
    }
    history = [{"role": "assistant", "content": "May I have your name?"}]
    first = evaluate_tool_flow_policy(
        "My name is Anita Rao",
        business_type="real_estate",
        history=history,
        state=state,
        language="en",
    )
    assert first is not None
    assert first["state_slot"] == "name_confirm"
    assert "Anita Rao" in first["answer"]
    flow_after = first["state_patch"]["tool_flow"]
    assert flow_after["awaiting_name_confirmation"] is True
    assert flow_after["collected"]["name"] == "Anita Rao"

    second = evaluate_tool_flow_policy(
        "yes correct",
        business_type="real_estate",
        history=history + [
            {"role": "user", "content": "My name is Anita Rao"},
            {"role": "assistant", "content": first["answer"]},
        ],
        state={"tool_flow": flow_after},
        language="en",
    )
    assert second is not None
    flow_done = second["state_patch"]["tool_flow"]
    assert flow_done["awaiting_name_confirmation"] is False
    assert flow_done["collected"]["name"] == "Anita Rao"
    assert second["state_slot"] != "name_confirm"


def test_tool_flow_past_time_offers_next_day(monkeypatch):
    """When the caller fills a past date+time for a site visit, offer same
    time on the next day instead of clearing both."""
    from app.services.tool_flow_policy import evaluate_tool_flow_policy
    from datetime import datetime as _dt
    from app.services import voice_turn_policy as vtp

    _patch_now(monkeypatch, _dt(2026, 5, 20, 12, 0, tzinfo=vtp._APPOINTMENT_LOCAL_TZ))

    state = {
        "tool_flow": {
            "active": True,
            "flow_key": "real_estate_site_visit",
            "tool_key": "qualify_lead_and_schedule_visit",
            "collected": {
                "name": "Ravi",
                "phone": "9177627064",
                "property_type": "Apartment",
                "location": "KPHB",
                "budget": 5000000.0,
                "visit_date": "20 May",
                # No visit_time yet — will be supplied this turn.
            },
            "pending_slot": "visit_time",
        }
    }
    history = [{"role": "assistant", "content": "What time would you prefer?"}]
    result = evaluate_tool_flow_policy(
        "11 AM",
        business_type="real_estate",
        history=history,
        state=state,
        language="en",
    )
    assert result is not None
    assert result["state_slot"] == "date_shift_confirm"
    assert "21 May" in result["answer"]
    flow_after = result["state_patch"]["tool_flow"]
    assert flow_after["awaiting_past_time_shift"] is True
    assert flow_after.get("original_time") == "11 AM"


# ─────────── Autonomy #1: phone & ID confirmation handshake ───────────


def test_phone_confirmation_skip_for_clean_indian_number():
    from app.services.voice_turn_policy import _is_high_confidence_phone

    assert _is_high_confidence_phone("9177627064") is True
    assert _is_high_confidence_phone("+919177627064") is True
    # Repeats like 9999999999 are clearly STT garbage
    assert _is_high_confidence_phone("9999999999") is False
    # Wrong leading digit
    assert _is_high_confidence_phone("4177627064") is False
    # Too short
    assert _is_high_confidence_phone("91776") is False


def test_phone_format_for_speech_groups_digits():
    from app.services.voice_turn_policy import _format_phone_for_speech

    assert _format_phone_for_speech("9177627064") == "9 1 7 7 6 2 7 0 6 4"
    # 12-digit (country code + 10) keeps the prefix separate
    spoken = _format_phone_for_speech("919177627064")
    assert spoken.startswith("9 1 ")
    assert "7 0 6 4" in spoken


# ─────────── Autonomy #2: adaptive disambiguation on date-only ───────────


def test_clinic_flow_triggers_availability_when_date_only(monkeypatch):
    from app.services.voice_turn_policy import evaluate_voice_turn_policy
    from datetime import datetime as _dt
    from app.services import voice_turn_policy as vtp

    _patch_now(monkeypatch, _dt(2026, 5, 20, 9, 0, tzinfo=vtp._APPOINTMENT_LOCAL_TZ))

    state = {
        "appointment": {
            "active": True,
            "reason": "checkup",
            "patient_name": "Ravi Kumar",
            "phone": "9177627064",
            "preferred_date": "23 May",
            "pending_slot": "preferred_time",
        }
    }
    history = [{"role": "assistant", "content": "Which date would you prefer?"}]
    result = evaluate_voice_turn_policy(
        "23 May", history=history, state=state, language="en",
    )
    assert result is not None
    assert result["intent"] == "availability_check"
    assert result["state_slot"] == "availability_disambiguation"


# ─────────── Autonomy #8: domain inference parity ───────────


def test_real_estate_inference_pulls_budget_and_property_type(monkeypatch):
    """Caller volunteers details in the opener; the agent shouldn't re-ask them."""
    from app.services.tool_flow_policy import evaluate_tool_flow_policy
    from datetime import datetime as _dt
    from app.services import voice_turn_policy as vtp

    _patch_now(monkeypatch, _dt(2026, 5, 20, 12, 0, tzinfo=vtp._APPOINTMENT_LOCAL_TZ))

    state = {
        "tool_flow": {
            "active": True,
            "flow_key": "real_estate_site_visit",
            "tool_key": "qualify_lead_and_schedule_visit",
            "collected": {"name": "Ravi", "phone": "9177627064"},
            "pending_slot": "property_type",
        }
    }
    history = [{"role": "assistant", "content": "What type of property are you looking for?"}]
    result = evaluate_tool_flow_policy(
        "Looking for a 2 BHK in KPHB, budget around 60 lakhs",
        business_type="real_estate",
        history=history,
        state=state,
        language="en",
    )
    assert result is not None
    flow = result["state_patch"]["tool_flow"]
    collected = flow.get("collected") or {}
    # property_type was the pending slot — captured directly
    assert "2 bhk" in str(collected.get("property_type", "")).lower()
    # location + budget came in the same sentence and should have been inferred
    assert collected.get("location") == "KPHB"
    assert float(collected.get("budget")) == 60.0


# ─────────── Autonomy #5: cross-call memory by phone ───────────


def test_returning_caller_opener_uses_last_record_summary():
    from app.services.nokvo_one_voice_stream_service import NokvoOneVoiceStreamService

    record = {
        "name": "Nivas Kondapalli",
        "summary": "eye blurriness checkup",
        "record_type": "appointment",
    }
    line = NokvoOneVoiceStreamService._returning_caller_opener(record, "en")
    assert "Nivas Kondapalli" in line
    assert "eye blurriness" in line.lower()


# ─────────── Unified flow_session bookkeeping ───────────


def test_flow_session_records_confirmation_and_audit():
    from app.services import flow_session

    data: dict = {}
    flow_session.record_confirmation(data, "patient_name", "Nivas Kondapalli")
    flow_session.append_audit_trail(data, "name_captured", detail="Nivas Kondapalli")
    assert flow_session.is_confirmed(data, "patient_name") is True
    assert data["confirmations"]["patient_name"]["value"] == "Nivas Kondapalli"
    assert data["audit_trail"][-1]["event"] == "name_captured"


def test_flow_session_mark_outcome_writes_and_reads_back():
    from app.services import flow_session

    data: dict = {}
    flow_session.mark_outcome(data, "no_show", source="ops_dashboard", notes="patient never arrived")
    outcome = flow_session.outcome_summary(data)
    assert outcome is not None
    assert outcome["status"] == "no_show"
    assert outcome["notes"] == "patient never arrived"


# ─────────── Confirmation persisted via clinic FSM ───────────


def test_clinic_name_confirmation_writes_to_appointment_confirmations(monkeypatch):
    from app.services.voice_turn_policy import evaluate_voice_turn_policy
    from datetime import datetime as _dt
    from app.services import voice_turn_policy as vtp

    _patch_now(monkeypatch, _dt(2026, 5, 21, 9, 0, tzinfo=vtp._APPOINTMENT_LOCAL_TZ))

    # State: name was captured + we're awaiting confirmation.
    state = {
        "appointment": {
            "active": True,
            "reason": "eye blurriness",
            "patient_name": "Nivas Kondapalli",
            "awaiting_name_confirmation": True,
            "pending_slot": "patient_name_confirm",
        }
    }
    history = [{"role": "assistant", "content": "Just to confirm — the patient's name is Nivas Kondapalli. Is that right?"}]
    result = evaluate_voice_turn_policy("yes that's right", history=history, state=state, language="en")
    assert result is not None
    appt = result["state_patch"]["appointment"]
    assert appt.get("confirmations", {}).get("patient_name", {}).get("value") == "Nivas Kondapalli"
    assert any(e["event"] == "name_confirmed" for e in appt.get("audit_trail", []))


# ─────────── Tool retry queue ───────────


def test_tool_retry_service_backoff_increases_with_attempts():
    from datetime import datetime as _dt, timezone as _tz
    from app.services.tool_retry_service import _next_attempt_after

    one = _next_attempt_after(1)
    five = _next_attempt_after(5)  # capped to last backoff
    assert (five - _dt.now(_tz.utc)).total_seconds() >= (one - _dt.now(_tz.utc)).total_seconds()


# ─────────── Outcome tracker ───────────


def test_outcome_tracker_rejects_unknown_status():
    import asyncio
    from app.services.outcome_tracker import OutcomeTracker

    async def _go():
        await OutcomeTracker.record_outcome(
            db=None,  # type: ignore[arg-type]  — never reached, validation throws first
            organization_id=uuid.uuid4(),
            record_id=uuid.uuid4(),
            status="something_invented",
        )

    with pytest.raises(ValueError):
        asyncio.get_event_loop().run_until_complete(_go())


# ─────────── Outcome-aware returning-caller opener ───────────


def test_returning_caller_opener_adapts_to_no_show_history():
    from app.services.nokvo_one_voice_stream_service import NokvoOneVoiceStreamService

    record = {
        "name": "Nivas Kondapalli",
        "summary": "eye blurriness checkup",
        "record_type": "appointment",
    }
    line = NokvoOneVoiceStreamService._returning_caller_opener(
        record,
        "en",
        outcome_history=[{"outcome": {"status": "no_show"}}],
    )
    assert "miss" in line.lower() or "remind" in line.lower()


# ─────────── Canonical agent spec ───────────


def test_agent_spec_snapshot_has_all_required_sections():
    from app.services.agent_spec import spec_snapshot

    snap = spec_snapshot()
    for key in ("agent_version", "confirmation", "retry", "identity", "outcomes", "capabilities"):
        assert key in snap, f"spec snapshot missing {key}"


def test_agent_spec_is_single_source_of_truth_for_outcomes():
    """outcome_tracker.VALID_OUTCOMES must come from agent_spec.OUTCOME_STATES."""
    from app.services.agent_spec import OUTCOME_STATES
    from app.services.outcome_tracker import VALID_OUTCOMES

    assert VALID_OUTCOMES == OUTCOME_STATES.all_values()


def test_agent_spec_is_single_source_of_truth_for_retry():
    """tool_retry_service constants must mirror agent_spec.RETRY_POLICY."""
    from app.services.agent_spec import RETRY_POLICY
    from app.services.tool_retry_service import MAX_ATTEMPTS, _BACKOFF_MINUTES

    assert MAX_ATTEMPTS == RETRY_POLICY.max_attempts
    assert _BACKOFF_MINUTES == RETRY_POLICY.backoff_minutes


def test_agent_spec_capabilities_lists_known_intents():
    from app.services.agent_spec import CAPABILITIES

    expected_subset = {
        "appointment_flow",
        "tool_flow",
        "availability_check",
        "cancellation_request",
        "refund_eligibility",
    }
    assert expected_subset.issubset(CAPABILITIES.handled_intents)


# ─────────── Session module boundaries ───────────


def test_session_modules_own_disjoint_keys():
    """Each session sub-module writes only its own keys on the record data
    dict. A confirmation write must not touch outcome / audit / retry keys
    and vice versa — that's what the split is for."""
    from app.services.session import audit, confirmation, outcome

    data: dict = {}
    confirmation.record_confirmation(data, "patient_name", "Nivas")
    confirmation.mark_identity_verified(data)
    confirmation.stamp_proposed_slot_accepted(data, slot_utc_iso="2026-05-21T11:00:00+00:00", member_name="Dr Rao")
    assert set(data.keys()) <= {"confirmations", "identity_verified", "identity_verified_at", "proposed_slot_accepted"}

    audit_data: dict = {}
    audit.append(audit_data, "name_confirmed", detail="Nivas")
    assert set(audit_data.keys()) == {"audit_trail"}

    outcome_data: dict = {}
    outcome.mark(outcome_data, "confirmed", source="ops_dashboard")
    assert set(outcome_data.keys()) == {"outcome"}


def test_outcome_module_rejects_status_not_in_spec():
    from app.services.session import outcome

    with pytest.raises(ValueError):
        outcome.mark({}, "totally_made_up_status")


def test_flow_session_backcompat_shim_still_exports_legacy_names():
    """Existing call sites import from app.services.flow_session — that
    surface must keep working after the split."""
    from app.services import flow_session

    for name in (
        "append_audit_trail",
        "record_confirmation",
        "mark_outcome",
        "outcome_summary",
        "mark_identity_verified",
        "stamp_proposed_slot_accepted",
        "is_confirmed",
        "is_identity_verified",
    ):
        assert hasattr(flow_session, name), f"flow_session.{name} missing after split"


# ─────────── Pipeline consumes spec, not hardcoded constants ───────────


def test_pipeline_retry_loop_reads_max_attempts_from_spec():
    """If anyone replaces RETRY_POLICY.inline_retries the pipeline must
    follow — this test fails if the pipeline reverts to range(2)."""
    import re
    pipeline_source = open("app/services/nokvo_one_voice_pipeline.py").read()
    # Two failure paths (clinic + tool_flow) should both compute inline
    # attempts from the spec, not hardcode `range(2)`.
    assert "RETRY_POLICY.inline_retries" in pipeline_source
    assert "RETRY_POLICY.inline_delay_seconds" in pipeline_source
    assert re.search(r"for attempt in range\(2\)", pipeline_source) is None, (
        "Pipeline still hardcodes the retry attempt count"
    )


def test_pipeline_phone_confirm_gate_reads_policy():
    voice_source = open("app/services/voice_turn_policy.py").read()
    flow_source = open("app/services/tool_flow_policy.py").read()
    assert "CONFIRMATION_POLICY.confirm_phone_unless_high_confidence" in voice_source
    assert "CONFIRMATION_POLICY.confirm_phone_unless_high_confidence" in flow_source


# ─────────── Outcome closed loop ───────────


def test_outcome_disposition_mapping_covers_known_call_states():
    from app.services.outcome_tracker import DISPOSITION_TO_OUTCOME
    from app.services.agent_spec import OUTCOME_STATES

    assert DISPOSITION_TO_OUTCOME["answered"] == OUTCOME_STATES.completed
    assert DISPOSITION_TO_OUTCOME["no_answer"] == OUTCOME_STATES.failed_followup
    assert DISPOSITION_TO_OUTCOME["failed"] == OUTCOME_STATES.failed_followup
    assert DISPOSITION_TO_OUTCOME["no_show"] == OUTCOME_STATES.no_show


# ─────────── Unified runtime context ───────────


def test_runtime_context_dataclass_round_trips_legacy_tuple():
    from app.services.agent_runtime_context import AgentRuntimeContext

    class _StubOrg:
        id = uuid.uuid4()
        industry = "clinics"

    ctx = AgentRuntimeContext(
        organization=_StubOrg(),  # type: ignore[arg-type]
        tenant_res=None,
        surface="chat",
        overrides={"foo": "bar"},
        custom_tabs=[{"slug": "x"}],
        campaign_id="camp-123",
        campaign_goal="qualify",
        caller_phone="9177627064",
        caller_name="Nivas",
    )
    org, overrides, custom_tabs = ctx.as_legacy_business_context_tuple()
    assert overrides == {"foo": "bar"}
    assert custom_tabs == [{"slug": "x"}]
    legacy_campaign = ctx.as_legacy_campaign_context()
    assert legacy_campaign["campaign_id"] == "camp-123"
    assert legacy_campaign["contact"]["phone"] == "9177627064"
    assert legacy_campaign["contact"]["name"] == "Nivas"


def test_runtime_context_rejects_unknown_surface():
    import asyncio
    from app.services.agent_runtime_context import build

    with pytest.raises(ValueError):
        asyncio.get_event_loop().run_until_complete(
            build(db=None, tenant_res=None, surface="carrier_pigeon")  # type: ignore[arg-type]
        )


# ─────────── Regression: "Yes" alone confirms during pending handshake ───────────


def test_pipeline_suppresses_smalltalk_when_awaiting_confirmation():
    """Reproduces the live bug: 'Yes' alone got 'Sure, go ahead.' because the
    SMALLTALK template fired before the FSM saw the confirmation flag.
    Guards: every awaiting_* flag is referenced AND the template return
    path is gated by ``not suppress_template``."""
    pipeline_source = open("app/services/nokvo_one_voice_pipeline.py").read()
    assert "suppress_template" in pipeline_source
    for flag in (
        "awaiting_slot_confirm",
        "awaiting_name_confirmation",
        "awaiting_phone_confirmation",
        "awaiting_id_confirmation",
        "awaiting_past_time_shift",
    ):
        assert flag in pipeline_source, f"smalltalk gate missing {flag}"
    assert "if templated and not suppress_template" in pipeline_source


def test_slot_acceptance_stores_parser_friendly_date_and_canonical_iso(monkeypatch):
    """After the caller accepts an offered slot, the appointment must carry
    BOTH a date string the appointment-date parser accepts AND a canonical
    UTC ISO the tool path can short-circuit on."""
    from app.services.voice_turn_policy import evaluate_voice_turn_policy
    from datetime import datetime as _dt
    from app.services import voice_turn_policy as vtp

    _patch_now(monkeypatch, _dt(2026, 5, 21, 9, 0, tzinfo=vtp._APPOINTMENT_LOCAL_TZ))

    state = {
        "appointment": {
            "active": True,
            "reason": "dry eye",
            "patient_name": "Mamata Neelala",
            "phone": "9177627064",
            "pending_slot": "preferred_date_shift_confirm",
            "awaiting_slot_confirm": True,
            "proposed_slot_utc": "2026-05-22T04:40:00+00:00",  # 10:10 AM IST
            "proposed_slot_label": "Nihar Neelala",
        }
    }
    history = [{"role": "assistant", "content": "Want me to lock that in?"}]
    result = evaluate_voice_turn_policy("Yes", history=history, state=state, language="en")
    assert result is not None
    appt = result["state_patch"]["appointment"]
    # Date stored in a format the parser handles (not bare ISO).
    assert "May" in str(appt.get("preferred_date") or "")
    # Canonical UTC ISO stamped for the tool short-circuit.
    assert appt.get("appointment_time") == "2026-05-22T04:40:00+00:00"
    # And the slot confirmation was actually consumed.
    assert appt.get("awaiting_slot_confirm") is False


def test_appointment_datetime_iso_short_circuits_on_accepted_slot():
    """The tool path must trust the canonical ISO an accepted slot leaves
    behind, instead of re-parsing the human-friendly date string and risking
    a 'That date does not look valid' rejection."""
    from datetime import datetime as _dt, timezone as _tz
    from app.services.nokvo_one_voice_pipeline import (
        NokvoOneVoicePipeline,
        _APPOINTMENT_LOCAL_TZ,
    )

    future = (_dt.now(_tz.utc) + __import__("datetime").timedelta(days=2)).replace(microsecond=0)
    appt = {
        "appointment_time": future.isoformat(),
        # Intentionally bogus date string — short-circuit must ignore it.
        "preferred_date": "garbage that wont parse",
        "preferred_time": "garbage",
    }
    iso = NokvoOneVoicePipeline._appointment_datetime_iso(appt)
    assert iso.startswith(future.date().isoformat())


def test_appointment_date_parser_now_accepts_iso():
    from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline

    parsed = NokvoOneVoicePipeline._parse_appointment_date("2026-05-22")
    assert parsed.year == 2026 and parsed.month == 5 and parsed.day == 22


# ─────────── Regression: Telugu/Hindi affirmatives + Indic-aware clinic gate ───────────


def test_affirmative_regex_matches_telugu_and_hindi():
    """Live bug: 'అవును' (Telugu yes) and 'हाँ' (Hindi yes) never matched
    because Python's \\b only fires at ASCII boundaries — the entire Indic
    branch of the regex was dead. This test fails if the split is reverted."""
    from app.services.voice_turn_policy import _looks_affirmative, _looks_negative

    # Affirmatives in Telugu / Hindi
    for yes in ("అవును", "అవును.", "సరే", "हाँ", "हां", "जी", "ठीक है"):
        assert _looks_affirmative(yes), f"affirmative regex missed {yes!r}"
    # Negatives in Telugu / Hindi
    for no in ("లేదు", "కాదు.", "नहीं", "गलत"):
        assert _looks_negative(no), f"negative regex missed {no!r}"
    # ASCII still works
    assert _looks_affirmative("yes please")
    assert _looks_affirmative("yeah")
    assert _looks_negative("no thanks")
    # Spurious matches should not fire
    assert not _looks_affirmative("yesterday")
    assert not _looks_affirmative("nothing")


def test_pipeline_skips_clinic_fsm_for_non_clinic_orgs():
    """Real-estate orgs must not see clinic prompts ('patient', 'eye concern').
    The pipeline checks organization.industry before calling
    evaluate_voice_turn_policy and skips it for anything that isn't 'clinics'."""
    pipeline_source = open("app/services/nokvo_one_voice_pipeline.py").read()
    assert "is_clinic_org" in pipeline_source
    assert "organization_industry" in pipeline_source
    # The clinic FSM call must be conditional on is_clinic_org.
    assert "if is_clinic_org" in pipeline_source


# ─────────── Regression: tool_flow availability_check must dispatch + persist ───────────


def test_pipeline_dispatches_tool_flow_availability_check():
    """Reproduces the live bug: tool_flow returned `intent: availability_check`
    with `answer=None` for a real-estate site visit. The pipeline's old guard
    was `if tool_flow.get("answer")` which dropped the response on the floor,
    never called the scheduler, AND silently discarded the state_patch (so
    `offered_disambiguation` was lost). Result: the FSM looped forever
    re-asking "What date would you prefer?".

    Guard: the source must (a) have a `tool_flow_needs_lookup` branch and
    (b) call `_handle_availability_check` from that branch."""
    src = open("app/services/nokvo_one_voice_pipeline.py").read()
    # All four landmarks must be present in source — proves the dispatch
    # branch exists, calls the scheduler handler, and is gated on the
    # specific availability_check intent rather than any answerless return.
    assert "tool_flow_needs_lookup" in src
    assert "if tool_flow_needs_lookup:" in src, "needs_lookup branch missing"
    # Find the if-branch and check it dispatches to the scheduler.
    branch = src.split("if tool_flow_needs_lookup:", 1)[1].split("else:", 1)[0]
    assert "_handle_availability_check" in branch
    assert '"availability_check"' in src


def test_pipeline_falls_back_to_slot_question_when_no_scheduler_match():
    """When the scheduler can't satisfy the availability lookup (no
    assignable site_visit agents yet), the pipeline must fall back to
    asking the original missing slot directly — NOT swallow the turn and
    fall through to RAG."""
    src = open("app/services/nokvo_one_voice_pipeline.py").read()
    # The fallback branch is `elif tool_flow_needs_lookup and not tool_flow.get("answer"):`
    assert "elif tool_flow_needs_lookup and not tool_flow.get(\"answer\")" in src
    # And it must build_tool_flow_questions to look up the slot prompt.
    assert "build_tool_flow_questions" in src


# ─────────── Inbound → tickets, outbound → leads routing ───────────


def test_call_surface_set_inbound_when_no_campaign(monkeypatch):
    """Browser tester / inbound voice line has no campaign_id → surface=voice_inbound."""
    voice_source = open("app/services/nokvo_one_voice_stream_service.py").read()
    # The set_state at session start must derive call_surface from campaign_context.
    assert "call_surface" in voice_source
    assert 'voice_outbound" if (campaign_context or {}).get("campaign_id") else "voice_inbound"' in voice_source


def test_route_record_by_surface_rewrites_lead_to_ticket_for_inbound():
    """Inbound voice surface must rewrite a `lead` record to `ticket` so it
    lands in the tickets tab. Outbound leaves it as a lead."""
    pipeline_source = open("app/services/nokvo_one_voice_pipeline.py").read()
    assert "_route_record_by_surface" in pipeline_source
    # The helper must guard on call_surface == voice_inbound for the rewrite
    helper_block = pipeline_source.split("_route_record_by_surface(", 1)[1]
    # Definition of the method should clearly check the surface.
    assert 'call_surface != "voice_inbound"' in pipeline_source
    # Only lead-type records get rewritten (appointments/callbacks/tickets stay).
    assert 'rec.record_type != "lead"' in pipeline_source


def test_pipeline_calls_route_after_tool_flow_execution():
    pipeline_source = open("app/services/nokvo_one_voice_pipeline.py").read()
    # The success path of _maybe_execute_tool_flow_action must invoke the
    # routing helper with both result['lead_id'] and result['id'].
    success_region = pipeline_source.split("Surface-based routing:", 1)[1].split("return {", 1)[0]
    assert "_route_record_by_surface" in success_region
    assert "lead_id" in success_region
    assert "call_surface" in success_region


# ─────────── Lead → Ticket projection (so the Tickets tab renders) ───────────


def test_lead_data_projects_onto_real_estate_ticket_schema():
    """Real-estate ticket schema needs `customer`, `issue_type`, `priority`,
    plus `property_id` when the lead carried a `location`. Without this
    mapping the row shows blank cells and the operator thinks no ticket
    was created."""
    from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline

    lead_data = {
        "name": "Nihar",
        "phone": "7569672503",
        "location": "Kokapet",
        "budget": None,
        "visit_date": "2026-05-22T03:30:00+00:00",
    }
    projected = NokvoOneVoicePipeline._map_lead_data_to_ticket_shape(lead_data, "real_estate")
    assert projected["customer"] == "Nihar"
    assert projected["issue_type"] == "site_visit"
    assert projected["priority"] == "normal"
    assert projected["property_id"] == "Kokapet"
    # Original keys preserved
    assert projected["name"] == "Nihar"
    assert projected["phone"] == "7569672503"


def test_lead_data_projection_industry_aware():
    """Each industry's ticket schema has different required fields. The
    mapper picks defaults appropriate to the industry."""
    from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline

    # Clinics: subject/customer
    clinic = NokvoOneVoicePipeline._map_lead_data_to_ticket_shape(
        {"patient_name": "Mamata", "reason": "eye blurriness"}, "clinics"
    )
    assert clinic["customer"] == "Mamata"
    assert clinic["subject"] == "eye blurriness"

    # Ecommerce: issue_type/subject
    ecom = NokvoOneVoicePipeline._map_lead_data_to_ticket_shape(
        {"customer_name": "Ravi", "issue_summary": "Order delayed"}, "ecommerce"
    )
    assert ecom["customer"] == "Ravi"
    assert ecom["subject"] == "Order delayed"
    assert ecom["issue_type"] == "support_request"

    # Hospitality
    hosp = NokvoOneVoicePipeline._map_lead_data_to_ticket_shape(
        {"guest_name": "Anita"}, "hospitality"
    )
    assert hosp["customer"] == "Anita"
    assert hosp["subject"] == "Guest inquiry"


def test_lead_data_projection_does_not_overwrite_existing_fields():
    """If the macro happened to set customer/priority/issue_type explicitly,
    the mapper must NOT clobber those values."""
    from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline

    data = {
        "name": "Nihar",
        "customer": "Existing Customer",
        "priority": "urgent",
        "issue_type": "complaint",
    }
    projected = NokvoOneVoicePipeline._map_lead_data_to_ticket_shape(data, "real_estate")
    assert projected["customer"] == "Existing Customer"
    assert projected["priority"] == "urgent"
    assert projected["issue_type"] == "complaint"
