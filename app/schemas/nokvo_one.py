from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.email_policy import validate_work_email
from app.schemas.token import Token
from app.services.nokvo_one_business_templates import validate_business_type


NOKVO_ONE_ORG_STATUSES = {
    "pending_email_verification",
    "pending_totp",
    "active",
    "suspended",
}


def _validate_password(value: str) -> str:
    if value is None or len(value) < 10:
        raise ValueError("Password must be at least 10 characters")
    if value.strip() != value:
        raise ValueError("Password must not start or end with whitespace")
    has_letter = any(c.isalpha() for c in value)
    has_digit = any(c.isdigit() for c in value)
    if not (has_letter and has_digit):
        raise ValueError("Password must include at least one letter and one digit")
    return value


# ─────────── Signup / verification ───────────


class NokvoOneSignupRequest(BaseModel):
    org_name: str = Field(min_length=1, max_length=200)
    admin_name: Optional[str] = Field(default=None, max_length=200)
    admin_email: EmailStr
    password: str
    region: str = "southindia"
    industry: Optional[str] = None
    country_code: Optional[str] = "IN"
    language: Optional[str] = "en-IN"

    @field_validator("org_name")
    @classmethod
    def _validate_org_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Organization name is required")
        return value

    @field_validator("admin_email")
    @classmethod
    def _validate_admin_email(cls, value: EmailStr) -> str:
        return validate_work_email(value)

    @field_validator("password")
    @classmethod
    def _validate_password_field(cls, value: str) -> str:
        return _validate_password(value)

    @field_validator("industry")
    @classmethod
    def _validate_industry(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return validate_business_type(value)


class NokvoOneProvisioningStepResponse(BaseModel):
    name: str
    status: str
    message: Optional[str] = None


class NokvoOneProvisioningSummary(BaseModel):
    tenant_id: str
    azure_resource_group_name: Optional[str]
    azure_region: Optional[str]
    blob_prefix: Optional[str]
    storage_account_name: Optional[str]
    qdrant_collection_name: Optional[str]
    qdrant_url_ref: Optional[str]
    redis_namespace: Optional[str]
    llm_provider: Optional[str]
    llm_model: Optional[str]
    llm_deployment: Optional[str]
    llm_endpoint: Optional[str]
    llm_region: Optional[str]
    llm_status: Optional[str]
    llm_api_key_present: bool = False
    key_vault_name: Optional[str] = None
    key_vault_status: Optional[str] = None
    llm_api_key_secret_ref: Optional[str] = None
    exotel_status: Optional[str]
    steps: list[NokvoOneProvisioningStepResponse]
    provisioning_status: str


class NokvoOneSignupResponse(BaseModel):
    organization_id: UUID
    admin_user_id: UUID
    email: EmailStr
    org_status: str
    message: str = "Choose a plan and pay to continue."
    # Short-lived token (stage="payment") that authorizes the payment endpoints
    # before any session exists. Provisioning happens after payment.
    payment_token: Optional[str] = None
    provisioning: Optional[NokvoOneProvisioningSummary] = None


class NokvoOneEmailVerifiedResponse(BaseModel):
    organization_id: UUID
    email: EmailStr
    org_status: str
    setup_token: str
    message: str = "Email verified. Complete TOTP setup to continue."


class NokvoOneTOTPSetupRequest(BaseModel):
    setup_token: str


class NokvoOneTOTPSetupResponse(BaseModel):
    email: EmailStr
    secret: str
    uri: str
    setup_token: str


class NokvoOneTOTPVerifyRequest(BaseModel):
    setup_token: str
    code: str


class NokvoOnePostTotpResponse(BaseModel):
    organization_id: UUID
    org_status: str
    message: str = (
        "TOTP enrolled. Your organization is now pending Nokvo One activation by a Nokvo administrator."
    )


# ─────────── Login ───────────


class NokvoOneLoginRequest(BaseModel):
    email: EmailStr
    password: str

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: EmailStr) -> str:
        return validate_work_email(value)


class NokvoOneLoginTOTPRequest(BaseModel):
    code: str


class NokvoOneGoogleLoginRequest(BaseModel):
    id_token: str


# ─────────── Member invitations ───────────


class NokvoOneMemberInviteRequest(BaseModel):
    email: EmailStr
    full_name: Optional[str] = Field(default=None, max_length=200)
    role: str = "member"

    @field_validator("email")
    @classmethod
    def _validate_email(cls, value: EmailStr) -> str:
        return validate_work_email(value)

    @field_validator("role")
    @classmethod
    def _validate_role(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"admin", "manager", "member", "viewer"}:
            raise ValueError("Invalid role")
        return value


class NokvoOneOrganizationUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Organization name cannot be empty")
        return value


class NokvoOneInvitationResponse(BaseModel):
    id: UUID
    organization_id: UUID
    email: EmailStr
    role: str
    expires_at: datetime
    accepted_at: Optional[datetime]

    model_config = ConfigDict(from_attributes=True)


class NokvoOneInvitationContextResponse(BaseModel):
    organization_id: UUID
    organization_name: str
    email: EmailStr
    role: str
    expires_at: datetime


class NokvoOneInvitationAcceptRequest(BaseModel):
    token: str
    password: str

    @field_validator("password")
    @classmethod
    def _validate_password_field(cls, value: str) -> str:
        return _validate_password(value)


# ─────────── Org/user views ───────────


class NokvoOneOrganizationResponse(BaseModel):
    id: UUID
    name: str
    product_tier: str
    status: str
    calling_enabled: bool
    admin_email: Optional[EmailStr]
    email_domain: Optional[str]
    environment: str
    region: str
    industry: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class NokvoOneUserResponse(BaseModel):
    id: UUID
    organization_id: UUID
    email: EmailStr
    full_name: Optional[str]
    role: str
    status: str
    auth_provider: str
    email_verified: bool
    mfa_pending: bool = False
    last_login_at: Optional[datetime]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class NokvoOneSessionResponse(Token):
    user: NokvoOneUserResponse
    organization: NokvoOneOrganizationResponse


class NokvoOneBusinessTemplateRequest(BaseModel):
    business_type: str = Field(min_length=1, max_length=40)

    @field_validator("business_type")
    @classmethod
    def _validate_business_type(cls, value: str) -> str:
        return validate_business_type(value)


class NokvoOneBusinessTemplateOptionResponse(BaseModel):
    value: str
    label: str
    member_label: str = "Members"
    tabs: list[str]
    request_types: list[dict[str, str]] = Field(default_factory=list)
    consultation_types: list[dict[str, str]] = Field(default_factory=list)
    schemas: dict[str, list[dict[str, Any]]]


class NokvoOneBusinessTemplateSaveResponse(BaseModel):
    organization: NokvoOneOrganizationResponse
    business_template: NokvoOneBusinessTemplateOptionResponse


class NokvoOneBusinessFieldDefinition(BaseModel):
    key: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=120)
    type: str = Field(default="text", max_length=40)
    required: bool = False

    @field_validator("key")
    @classmethod
    def _validate_key(cls, value: str) -> str:
        cleaned = value.strip().lower().replace(" ", "_").replace("-", "_")
        cleaned = "".join(ch for ch in cleaned if ch.isalnum() or ch == "_")
        if not cleaned:
            raise ValueError("Field key is required")
        return cleaned

    @field_validator("label", "type")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Field value is required")
        return value


class NokvoOneBusinessSchemaUpdateRequest(BaseModel):
    fields: list[NokvoOneBusinessFieldDefinition] = Field(min_length=1, max_length=25)


class NokvoOneCustomTabStatusVocabulary(BaseModel):
    initial: str = Field(min_length=1, max_length=40)
    all: list[str] = Field(min_length=1, max_length=20)
    forward: list[str] = Field(default_factory=list, max_length=20)


class NokvoOneCustomTabCreateRequest(BaseModel):
    slug: str = Field(min_length=1, max_length=32)
    label: str = Field(min_length=1, max_length=80)
    fields: list[NokvoOneBusinessFieldDefinition] = Field(default_factory=list, max_length=25)
    status_vocabulary: Optional[NokvoOneCustomTabStatusVocabulary] = None
    search_keys: list[str] = Field(default_factory=list, max_length=12)


class NokvoOneCustomTabResponse(BaseModel):
    slug: str
    label: str
    fields: list[dict[str, Any]] = Field(default_factory=list)
    status_vocabulary: dict[str, Any] = Field(default_factory=dict)
    search_keys: list[str] = Field(default_factory=list)


class NokvoOneAssignmentSettingsResponse(BaseModel):
    id: Optional[UUID] = None
    organization_id: UUID
    member_id: UUID
    is_assignable: bool = False
    working_days: list[str] = Field(default_factory=list)
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    timezone: str = "Asia/Kolkata"
    request_types: list[str] = Field(default_factory=list)
    max_active_requests: int = 100
    max_requests_per_day: Optional[int] = None
    max_requests_per_hour: int = 6
    appointment_duration_minutes: int = 30
    active_request_count: int = 0
    availability_summary: str = "Not assignable"


class NokvoOneAssignmentSettingsUpdateRequest(BaseModel):
    is_assignable: bool = False
    working_days: list[str] = Field(default_factory=list, max_length=7)
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    timezone: str = Field(default="Asia/Kolkata", min_length=1, max_length=80)
    request_types: list[str] = Field(default_factory=list)
    max_active_requests: int = Field(default=100, ge=1, le=100)
    max_requests_per_day: Optional[int] = Field(default=None, ge=1, le=1000)
    max_requests_per_hour: int = Field(default=6, ge=1, le=100)
    appointment_duration_minutes: int = Field(default=30, ge=5, le=480)

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        value = (value or "Asia/Kolkata").strip()
        if value in {"IST", "Asia/Kolkata"}:
            return "Asia/Kolkata"
        raise ValueError("Only IST is supported")

    @field_validator("working_days")
    @classmethod
    def _validate_working_days(cls, value: list[str]) -> list[str]:
        allowed = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
        cleaned = []
        for item in value:
            day = item.strip().lower()[:3]
            if day not in allowed:
                raise ValueError("working_days must contain mon, tue, wed, thu, fri, sat, or sun")
            if day not in cleaned:
                cleaned.append(day)
        return cleaned


class NokvoOneAssignmentDefaultsResponse(BaseModel):
    organization_id: UUID
    working_days: list[str] = Field(default_factory=list)
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    timezone: str = "Asia/Kolkata"
    working_hours_summary: str = "Not set"


class NokvoOneAssignmentDefaultsUpdateRequest(BaseModel):
    """Org-wide default working hours members inherit when their own are unset."""

    working_days: list[str] = Field(default_factory=list, max_length=7)
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    timezone: str = Field(default="Asia/Kolkata", min_length=1, max_length=80)

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        value = (value or "Asia/Kolkata").strip()
        if value in {"IST", "Asia/Kolkata"}:
            return "Asia/Kolkata"
        raise ValueError("Only IST is supported")

    @field_validator("working_days")
    @classmethod
    def _validate_working_days(cls, value: list[str]) -> list[str]:
        allowed = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
        cleaned: list[str] = []
        for item in value:
            day = item.strip().lower()[:3]
            if day not in allowed:
                raise ValueError("working_days must contain mon, tue, wed, thu, fri, sat, or sun")
            if day not in cleaned:
                cleaned.append(day)
        return cleaned


class NokvoOneClinicScheduleSettingsResponse(BaseModel):
    id: Optional[UUID] = None
    organization_id: UUID
    member_id: UUID
    appointment_duration_minutes: int = 30
    buffer_minutes: int = 0
    max_patients_per_hour: Optional[int] = None
    max_patients_per_day: Optional[int] = None
    consultation_types: list[str] = Field(default_factory=list)


class NokvoOneClinicScheduleSettingsUpdateRequest(BaseModel):
    appointment_duration_minutes: int = Field(default=30, ge=5, le=480)
    buffer_minutes: int = Field(default=0, ge=0, le=180)
    max_patients_per_hour: Optional[int] = Field(default=None, ge=1, le=100)
    max_patients_per_day: Optional[int] = Field(default=None, ge=1, le=500)
    consultation_types: list[str] = Field(default_factory=list)


class NokvoOneBlockedSlotCreateRequest(BaseModel):
    start_time: datetime
    end_time: datetime
    reason: Optional[str] = Field(default=None, max_length=240)
    repeat_rule: Optional[str] = Field(default=None, max_length=240)


class NokvoOneBlockedSlotUpdateRequest(BaseModel):
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    reason: Optional[str] = Field(default=None, max_length=240)
    repeat_rule: Optional[str] = Field(default=None, max_length=240)


class NokvoOneBlockedSlotResponse(BaseModel):
    id: UUID
    organization_id: UUID
    member_id: UUID
    start_time: datetime
    end_time: datetime
    reason: Optional[str]
    repeat_rule: Optional[str]
    created_at: datetime
    updated_at: Optional[datetime]

    model_config = ConfigDict(from_attributes=True)


class NokvoOneMemberTimetableResponse(BaseModel):
    """Caller's own timetable: working-hours assignment + blocked slots.

    Returned by ``GET /members/me/timetable``. The member dashboard
    renders this directly without needing admin-scoped endpoints.
    """
    assignment: NokvoOneAssignmentSettingsResponse
    blocked_slots: list[NokvoOneBlockedSlotResponse] = Field(default_factory=list)


class NokvoOneRequestAssignRequest(BaseModel):
    request_type: str = Field(min_length=1, max_length=80)
    record_type: str = Field(default="request", max_length=40)
    requested_time: Optional[datetime] = None
    summary: Optional[str] = Field(default=None, max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)


class NokvoOneRequestAssignResponse(BaseModel):
    request_id: Optional[UUID]
    organization_id: UUID
    selected_member_id: Optional[UUID] = None
    request_type: str
    assignment_status: str
    reason: Optional[str] = None
    active_load_count: Optional[int] = None
    skipped_member_reasons: dict[str, list[str]] = Field(default_factory=dict)
    requested_time: Optional[datetime] = None
    scheduled_time: Optional[datetime] = None
    time_adjusted: bool = False
    timestamp: datetime


# ─────────── Agents / tools ───────────


class NokvoOneAgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=1000)
    system_prompt: Optional[str] = Field(default=None, max_length=8000)
    tool_keys: list[str] = Field(default_factory=list)


class NokvoOneAgentUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=1000)
    system_prompt: Optional[str] = Field(default=None, max_length=8000)
    tool_keys: Optional[list[str]] = None


class NokvoOneAgentResponse(BaseModel):
    id: UUID
    organization_id: UUID
    name: str
    description: Optional[str]
    system_prompt: Optional[str]
    tool_keys: list[str]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class NokvoOnePredefinedToolResponse(BaseModel):
    key: str
    display_name: str
    description: str
    input_schema: dict[str, Any]
    requires_confirmation: bool


class NokvoOneToolGroupResponse(BaseModel):
    label: str
    tools: list[NokvoOnePredefinedToolResponse]


class NokvoOneToolCatalogResponse(BaseModel):
    business_type: Optional[str] = None
    groups: list[NokvoOneToolGroupResponse] = Field(default_factory=list)
    default_tool_keys: list[str] = Field(default_factory=list)


class NokvoOneOutcomeOption(BaseModel):
    slug: str
    label: str
    description: str
    default_on: bool = False


class NokvoOneOutcomesResponse(BaseModel):
    business_type: Optional[str] = None
    outcomes: list[NokvoOneOutcomeOption] = Field(default_factory=list)
    default_agent_name: str


class NokvoOneAgentFromOutcomesRequest(BaseModel):
    outcomes: list[str] = Field(default_factory=list, max_length=20)
    agent_name: Optional[str] = Field(default=None, max_length=120)


class NokvoOneSignupSkipTOTPRequest(BaseModel):
    setup_token: str


class NokvoOneAgentChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class NokvoOneAgentChatResponse(BaseModel):
    reply: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)


# ─────────── Real-estate projects ───────────


class RealEstateProjectBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    location: Optional[str] = Field(default=None, max_length=500)
    rera_number: Optional[str] = Field(default=None, max_length=120)
    property_type: Optional[str] = Field(default=None, max_length=80)
    price_min: Optional[float] = Field(default=None, ge=0)
    price_max: Optional[float] = Field(default=None, ge=0)
    price_display: Optional[str] = Field(default=None, max_length=200)
    configurations: list[str] = Field(default_factory=list, max_length=40)
    amenities: list[str] = Field(default_factory=list, max_length=80)
    description: Optional[str] = Field(default=None, max_length=8000)
    possession_date: Optional[str] = Field(default=None, max_length=80)
    builder_name: Optional[str] = Field(default=None, max_length=200)
    brochure_url: Optional[str] = Field(default=None, max_length=1000)
    contact_phone: Optional[str] = Field(default=None, max_length=40)
    # Per-project WhatsApp pre-set message config (Meta-approved templates):
    #   {"location": {"template","language","maps_url"}, "brochure": {"template","language"}}
    whatsapp: dict[str, Any] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)
    status: Optional[str] = Field(default="active", max_length=40)

    @field_validator("name", "location", "rera_number", "property_type", "price_display", "description", "possession_date", "builder_name", "brochure_url", "contact_phone")
    @classmethod
    def _trim_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("configurations", "amenities")
    @classmethod
    def _trim_list(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in values or []:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                cleaned.append(text[:120])
        return cleaned


class RealEstateProjectCreateRequest(RealEstateProjectBase):
    pass


class RealEstateProjectUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    location: Optional[str] = Field(default=None, max_length=500)
    rera_number: Optional[str] = Field(default=None, max_length=120)
    property_type: Optional[str] = Field(default=None, max_length=80)
    price_min: Optional[float] = Field(default=None, ge=0)
    price_max: Optional[float] = Field(default=None, ge=0)
    price_display: Optional[str] = Field(default=None, max_length=200)
    configurations: Optional[list[str]] = Field(default=None, max_length=40)
    amenities: Optional[list[str]] = Field(default=None, max_length=80)
    description: Optional[str] = Field(default=None, max_length=8000)
    possession_date: Optional[str] = Field(default=None, max_length=80)
    builder_name: Optional[str] = Field(default=None, max_length=200)
    brochure_url: Optional[str] = Field(default=None, max_length=1000)
    contact_phone: Optional[str] = Field(default=None, max_length=40)
    whatsapp: Optional[dict[str, Any]] = None
    extra: Optional[dict[str, Any]] = None
    status: Optional[str] = Field(default=None, max_length=40)


class RealEstateProjectResponse(BaseModel):
    id: UUID
    organization_id: UUID
    name: str
    location: Optional[str]
    rera_number: Optional[str]
    property_type: Optional[str]
    price_min: Optional[float]
    price_max: Optional[float]
    price_display: Optional[str]
    configurations: list[str] = Field(default_factory=list)
    amenities: list[str] = Field(default_factory=list)
    description: Optional[str]
    possession_date: Optional[str]
    builder_name: Optional[str]
    brochure_url: Optional[str]
    contact_phone: Optional[str]
    whatsapp: dict[str, Any] = Field(default_factory=dict)
    extra: dict[str, Any] = Field(default_factory=dict)
    status: str
    created_at: datetime
    updated_at: Optional[datetime]

    model_config = ConfigDict(from_attributes=True)


# ─────────── Superadmin approval ───────────


class NokvoOneApprovalRequest(BaseModel):
    enable_calling: bool = False
    plan_type: Optional[str] = None


class NokvoOnePendingOrgResponse(BaseModel):
    id: UUID
    name: str
    admin_email: Optional[EmailStr]
    admin_name: Optional[str]
    email_domain: Optional[str]
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
