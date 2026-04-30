from datetime import datetime
from typing import Optional
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
