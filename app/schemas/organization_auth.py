from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.email_policy import validate_work_email
from app.schemas.token import Token


ORGANIZATION_MEMBER_ROLES = {"admin", "manager", "member", "viewer"}
ORGANIZATION_MEMBER_STATUSES = {"invited", "active", "disabled"}


class GoogleOAuthLoginRequest(BaseModel):
    organization_id: Optional[UUID] = None
    id_token: str


class OrganizationMemberCreate(BaseModel):
    email: EmailStr
    full_name: Optional[str] = None
    role: str = "member"

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: EmailStr) -> str:
        return validate_work_email(value)

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in ORGANIZATION_MEMBER_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(sorted(ORGANIZATION_MEMBER_ROLES))}")
        return value


class OrganizationMemberUpdate(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    status: Optional[str] = None

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.strip().lower()
        if value not in ORGANIZATION_MEMBER_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(sorted(ORGANIZATION_MEMBER_ROLES))}")
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        value = value.strip().lower()
        if value not in ORGANIZATION_MEMBER_STATUSES:
            raise ValueError(f"Status must be one of: {', '.join(sorted(ORGANIZATION_MEMBER_STATUSES))}")
        return value


class OrganizationUserResponse(BaseModel):
    id: UUID
    organization_id: UUID
    email: EmailStr
    full_name: Optional[str]
    role: str
    status: str
    auth_provider: str
    mfa_required: bool
    email_verified: bool
    last_login_at: Optional[datetime]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class OrganizationSummaryResponse(BaseModel):
    id: UUID
    name: str
    admin_email: Optional[EmailStr]
    admin_name: Optional[str]
    email_domain: Optional[str]
    environment: str
    region: str

    model_config = ConfigDict(from_attributes=True)


class OrganizationLoginResponse(Token):
    user: OrganizationUserResponse
    organization: OrganizationSummaryResponse


class OrganizationTOTPSetupResponse(BaseModel):
    email: EmailStr
    secret: str
    uri: str


class OrganizationTOTPVerifyRequest(BaseModel):
    token: str


class OrganizationDatabaseProviderResponse(BaseModel):
    value: str
    label: str
    group: str
    supported: bool


class OrganizationDatabaseSchemaColumnResponse(BaseModel):
    name: str
    type: str
    nullable: bool


class OrganizationDatabaseSchemaTableResponse(BaseModel):
    schema_name: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("schema_name", "schema"),
        serialization_alias="schema",
    )
    name: str
    columns: list[OrganizationDatabaseSchemaColumnResponse]

    model_config = ConfigDict(populate_by_name=True)


class OrganizationDatabaseConnectRequest(BaseModel):
    provider: str
    connection_string: str

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        value = value.strip().lower()
        if not value:
            raise ValueError("Database provider is required")
        return value

    @field_validator("connection_string")
    @classmethod
    def validate_connection_string(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Connection string is required")
        return value


class OrganizationDatabaseConnectResponse(BaseModel):
    provider: str
    database_name: Optional[str] = None
    secret_ref: str
    status: str
    schema_snapshot: list[OrganizationDatabaseSchemaTableResponse] = Field(
        validation_alias=AliasChoices("schema_snapshot", "schema"),
        serialization_alias="schema",
    )

    model_config = ConfigDict(populate_by_name=True)


class OrganizationDatabaseColumnSelection(BaseModel):
    table: str
    schema_name: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("schema_name", "schema"),
        serialization_alias="schema",
    )
    columns: list[str]

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("table")
    @classmethod
    def validate_table(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Table is required")
        return value

    @field_validator("columns")
    @classmethod
    def validate_columns(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item and item.strip()]
        if not cleaned:
            raise ValueError("At least one column must be selected")
        return cleaned


class OrganizationDatabaseIndexRequest(BaseModel):
    provider: Optional[str] = None
    row_limit: int = 50
    selections: list[OrganizationDatabaseColumnSelection]

    @field_validator("row_limit")
    @classmethod
    def validate_row_limit(cls, value: int) -> int:
        return max(1, min(value, 200))


class OrganizationDatabaseIndexResponse(BaseModel):
    status: str
    provider: str
    indexed_points: int
    tables: list[str]
    column_value_count: int
    row_limit: int


class OrganizationDatabaseStatusResponse(BaseModel):
    provider: Optional[str] = None
    status: str
    secret_ref: Optional[str] = None
    database_name: Optional[str] = None
    selected_sources: list[dict] = Field(default_factory=list)
    indexed_points: int = 0


class OrganizationCRMProviderResponse(BaseModel):
    value: str
    label: str
    group: str
    supported: bool


class OrganizationCRMActionResponse(BaseModel):
    module: str
    name: str
    method: str
    endpoint: str
    description: str


class OrganizationCRMFieldResponse(BaseModel):
    name: str
    label: str
    type: str
    required: bool = False


class OrganizationCRMModuleResponse(BaseModel):
    api_name: str
    label: str
    fields: list[OrganizationCRMFieldResponse]


class OrganizationCRMConnectRequest(BaseModel):
    provider: str
    account_url: Optional[str] = None
    api_domain: Optional[str] = None
    access_token: str
    refresh_token: Optional[str] = None

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        value = value.strip().lower()
        if not value:
            raise ValueError("CRM provider is required")
        return value

    @field_validator("access_token")
    @classmethod
    def validate_access_token(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("CRM access token is required")
        return value


class OrganizationCRMConnectResponse(BaseModel):
    provider: str
    account_name: str
    status: str
    secret_ref: str
    folder_path: str
    indexed_points: int
    module_count: int
    action_count: int
    modules: list[OrganizationCRMModuleResponse]
    actions: list[OrganizationCRMActionResponse]


class OrganizationCRMStatusResponse(BaseModel):
    provider: Optional[str] = None
    status: str
    secret_ref: Optional[str] = None
    account_name: Optional[str] = None
    indexed_points: int = 0
    module_count: int = 0
    action_count: int = 0
    folder_path: Optional[str] = None


class OrganizationERPProviderResponse(BaseModel):
    value: str
    label: str
    group: str
    supported: bool


class OrganizationERPFieldResponse(BaseModel):
    name: str
    label: str
    type: str
    required: bool = False


class OrganizationERPModuleResponse(BaseModel):
    api_name: str
    label: str
    object_type: str
    fields: list[OrganizationERPFieldResponse]
    record_count: int = 0
    sample_records: list[dict[str, Any]] = Field(default_factory=list)
    status: str = "unknown"
    last_error: Optional[str] = None


class OrganizationERPActionResponse(BaseModel):
    module: str
    name: str
    method: str
    endpoint: str
    description: str


class OrganizationERPConnectRequest(BaseModel):
    provider: str
    base_url: str = "http://localhost:9000"
    company_name: Optional[str] = None
    timeout_seconds: int = 20
    max_items_per_module: int = 25

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        value = value.strip().lower()
        if not value:
            raise ValueError("ERP provider is required")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Tally URL is required")
        return value

    @field_validator("timeout_seconds")
    @classmethod
    def validate_timeout(cls, value: int) -> int:
        return max(3, min(value, 60))

    @field_validator("max_items_per_module")
    @classmethod
    def validate_max_items(cls, value: int) -> int:
        return max(1, min(value, 100))


class OrganizationERPConnectResponse(BaseModel):
    provider: str
    account_name: str
    status: str
    secret_ref: str
    folder_path: str
    indexed_points: int
    module_count: int
    action_count: int
    modules: list[OrganizationERPModuleResponse]
    actions: list[OrganizationERPActionResponse]


class OrganizationERPStatusResponse(BaseModel):
    provider: Optional[str] = None
    status: str
    secret_ref: Optional[str] = None
    account_name: Optional[str] = None
    indexed_points: int = 0
    module_count: int = 0
    action_count: int = 0
    folder_path: Optional[str] = None
    last_error: Optional[str] = None


class OrganizationTallyXMLRequest(BaseModel):
    xml_payload: str

    @field_validator("xml_payload")
    @classmethod
    def validate_xml_payload(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("XML payload is required")
        if "<ENVELOPE" not in value.upper():
            raise ValueError("Tally XML payload must include an ENVELOPE")
        return value


class OrganizationTallyXMLResponse(BaseModel):
    response_xml: str


TOOLKIT_INTEGRATION_TYPES = {"database", "crm", "zoho_desk", "erp", "shipping", "ecommerce", "his", "payments", "custom_api"}


class OrganizationToolkitGenerateRequest(BaseModel):
    integration_type: str
    provider: str
    nlp_prompt: str
    system_prompt: Optional[str] = None

    @field_validator("integration_type")
    @classmethod
    def validate_integration_type(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in TOOLKIT_INTEGRATION_TYPES:
            raise ValueError(f"Integration type must be one of: {', '.join(sorted(TOOLKIT_INTEGRATION_TYPES))}")
        return value

    @field_validator("provider", "nlp_prompt")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field is required")
        return value


class OrganizationToolkitDraftResponse(BaseModel):
    id: str
    status: str
    integration_type: str
    provider: str
    nlp_prompt: str
    tool: dict[str, Any]
    context_summary: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    reviewed_at: Optional[str] = None
    reviewed_by: Optional[str] = None
    review_notes: Optional[str] = None


class OrganizationToolkitReviewRequest(BaseModel):
    notes: Optional[str] = None


class OrganizationToolkitRegistryResponse(BaseModel):
    integration_type: str
    provider: str
    tools: list[dict[str, Any]] = Field(default_factory=list)
    drafts: list[OrganizationToolkitDraftResponse] = Field(default_factory=list)


class OrganizationShippingProviderResponse(BaseModel):
    value: str
    label: str
    group: str
    supported: bool


class OrganizationShippingModuleResponse(BaseModel):
    api_name: str
    label: str
    fields: list[str]
    description: str


class OrganizationShippingActionResponse(BaseModel):
    module: str
    name: str
    method: str
    endpoint: str
    description: str


class OrganizationShippingConnectRequest(BaseModel):
    provider: str
    email: EmailStr
    password: str
    base_url: str = "https://apiv2.shiprocket.in/v1/external"

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        value = value.strip().lower()
        if not value:
            raise ValueError("Shipping provider is required")
        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Shiprocket API password is required")
        return value

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Shiprocket API base URL is required")
        return value


class OrganizationShippingConnectResponse(BaseModel):
    provider: str
    account_name: str
    status: str
    secret_ref: str
    folder_path: str
    indexed_points: int
    module_count: int
    action_count: int
    modules: list[OrganizationShippingModuleResponse]
    actions: list[OrganizationShippingActionResponse]


class OrganizationShippingStatusResponse(BaseModel):
    provider: Optional[str] = None
    status: str
    secret_ref: Optional[str] = None
    account_name: Optional[str] = None
    indexed_points: int = 0
    module_count: int = 0
    action_count: int = 0
    folder_path: Optional[str] = None
    last_error: Optional[str] = None


class OrganizationShiprocketServiceabilityRequest(BaseModel):
    pickup_postcode: int
    delivery_postcode: int
    weight: Optional[float] = None
    cod: Optional[int] = None
    order_id: Optional[str] = None


class OrganizationShiprocketCreateOrderRequest(BaseModel):
    payload: dict[str, Any]


class OrganizationShiprocketAssignAWBRequest(BaseModel):
    shipment_id: int
    courier_id: Optional[int] = None
    status: Optional[str] = None


class OrganizationShiprocketPickupRequest(BaseModel):
    shipment_id: list[int] | int


class OrganizationShiprocketTrackRequest(BaseModel):
    order_id: Optional[str] = None
    awb_code: Optional[str] = None


class OrganizationShiprocketAPIResponse(BaseModel):
    raw: dict[str, Any] = Field(default_factory=dict)


class OrganizationZohoDeskConnectResponse(BaseModel):
    status: str
    account_name: str
    org_id: Optional[str] = None
    indexed_points: int
    module_count: int
    action_count: int
    folder_path: str


class OrganizationZohoDeskStatusResponse(BaseModel):
    status: str
    account_name: Optional[str] = None
    org_id: Optional[str] = None
    indexed_points: int = 0
    module_count: int = 0
    action_count: int = 0
    folder_path: Optional[str] = None


class OrganizationZohoDeskTicketCreateRequest(BaseModel):
    subject: str
    department_id: str
    description: Optional[str] = None
    contact_id: Optional[str] = None
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    custom_fields: dict[str, Any] = Field(default_factory=dict)

    @field_validator("subject", "department_id")
    @classmethod
    def validate_required_strings(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field is required")
        return value


class OrganizationZohoDeskTicketUpdateRequest(BaseModel):
    subject: Optional[str] = None
    description: Optional[str] = None
    contact_id: Optional[str] = None
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    custom_fields: dict[str, Any] = Field(default_factory=dict)


class OrganizationZohoDeskTicketResponse(BaseModel):
    id: str
    status: Optional[str] = None
    subject: Optional[str] = None
    department_id: Optional[str] = None
    web_url: Optional[str] = None
    raw: dict[str, Any] = Field(default_factory=dict)
