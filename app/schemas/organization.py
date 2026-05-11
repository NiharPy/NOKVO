from pydantic import AliasChoices, BaseModel, ConfigDict, Field, EmailStr, field_validator
from typing import Optional
from uuid import UUID

from app.core.email_policy import validate_work_email

class OrganizationCreate(BaseModel):
    organization_name: str = Field(
        validation_alias=AliasChoices("organization_name", "name"),
        serialization_alias="organization_name",
    )
    admin_email: EmailStr
    admin_name: Optional[str] = None
    region: str
    environment: str
    call_type: str = "inbound"
    language: str = "en-IN"
    plan_type: str = "pilot"
    product_tier: str = "nokvo_prime"
    stores_pii: bool = True
    record_calls: bool = True
    create_resource_group: bool = True
    twilio_auto_provision: bool = Field(
        default=False,
        validation_alias=AliasChoices("twilio_auto_provision", "plivo_auto_provision"),
        serialization_alias="twilio_auto_provision",
    )
    industry: Optional[str] = "Customer Support"
    country_code: Optional[str] = "IN"

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("organization_name")
    @classmethod
    def validate_organization_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Organization name is required")
        return value

    @field_validator("admin_email")
    @classmethod
    def validate_admin_email(cls, value: EmailStr) -> str:
        return validate_work_email(value)

    @field_validator("product_tier")
    @classmethod
    def validate_product_tier(cls, value: str) -> str:
        normalized = (value or "nokvo_prime").strip().lower()
        if normalized not in {"nokvo_one", "nokvo_prime", "nokvo_apex"}:
            raise ValueError("product_tier must be one of: nokvo_one, nokvo_prime, nokvo_apex")
        return normalized

class OrganizationResponse(OrganizationCreate):
    id: UUID
    email_domain: Optional[str] = None
    
    model_config = ConfigDict(from_attributes=True)
