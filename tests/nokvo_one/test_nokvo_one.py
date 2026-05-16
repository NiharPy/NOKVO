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
    CATALOG,
    PredefinedToolsService,
    get_tool,
    list_tools,
    validate_tool_keys,
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


def test_catalog_has_exactly_v1_tools():
    expected = {
        "lead_tracker_create_lead",
        "lead_tracker_update_status",
        "lead_tracker_add_note",
        "call_logger_create_entry",
        "call_logger_get_history",
        "create_ticket",
        "schedule_callback",
        "send_email_draft",
    }
    actual = {tool.key for tool in CATALOG}
    assert actual == expected, f"V1 catalog drift: missing={expected - actual} extra={actual - expected}"


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
    assert len(items) == 8
    for item in items:
        assert {"key", "display_name", "description", "input_schema", "requires_confirmation"} <= set(item.keys())


def test_validate_tool_keys_rejects_unknown():
    with pytest.raises(ValueError):
        validate_tool_keys(["lead_tracker_create_lead", "web_search"])


def test_validate_tool_keys_accepts_known():
    keys = ["lead_tracker_create_lead", "create_ticket"]
    assert validate_tool_keys(keys) == keys


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
    assert len(db.added) == 1
    record = db.added[0]
    assert record.record_type == "email_draft"
    assert record.status == "pending_confirmation"


def test_unknown_tool_rejected_by_dispatcher():
    db = _FakeDB()
    with pytest.raises(ValueError):
        _run(
            PredefinedToolsService.execute(
                db, uuid.uuid4(), uuid.uuid4(), "web_search", {"query": "x"}
            )
        )


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


def test_assignment_returns_no_available_when_all_members_skipped():
    organization, _ = _org_and_user(industry="real_estate")
    member = _member(organization.id, "AgentA")
    settings = _settings(organization.id, member.id, start=time(9, 0), end=time(10, 0))
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
    assert "outside_working_hours" in result["skipped_member_reasons"][str(member.id)]
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


def test_assignment_skips_member_over_hourly_capacity():
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
    assert result["assignment_status"] == "no_available_member"
    assert "hourly_request_capacity_reached" in result["skipped_member_reasons"][str(member.id)]


def test_assignment_hourly_capacity_counts_completed_requests_in_same_hour():
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
    assert result["assignment_status"] == "no_available_member"
    assert "hourly_request_capacity_reached" in result["skipped_member_reasons"][str(member.id)]


def test_clinic_assignment_skips_blocked_slot():
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
    assert result["assignment_status"] == "no_available_member"
    assert "blocked_slot_conflict" in result["skipped_member_reasons"][str(doctor.id)]


def test_clinic_assignment_respects_hourly_patient_capacity():
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
    assert result["assignment_status"] == "no_available_member"
    assert "hourly_patient_capacity_reached" in result["skipped_member_reasons"][str(doctor.id)]


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
    assert system_prompt.index("RAG rules:") < system_prompt.index("Tool rules:")
    assert system_prompt.index("Tool rules:") < system_prompt.index("Escalation rules:")


def test_create_ticket_dispatch():
    db = _FakeDB()
    result = _run(
        PredefinedToolsService.execute(
            db,
            uuid.uuid4(),
            uuid.uuid4(),
            "create_ticket",
            {
                "subject": "Login broken",
                "description": "Cannot log in since this morning.",
                "priority": "high",
            },
        )
    )
    assert result["ok"] is True
    assert "ticket_id" in result
    assert db.added[0].record_type == "ticket"
    assert db.added[0].status == "open"
