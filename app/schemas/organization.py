from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from typing import Optional
from uuid import UUID

class OrganizationCreate(BaseModel):
    organization_name: str = Field(
        validation_alias=AliasChoices("organization_name", "name"),
        serialization_alias="organization_name",
    )
    admin_email: Optional[str] = None
    admin_name: Optional[str] = None
    region: str
    environment: str
    call_type: str = "inbound"
    language: str = "en-IN"
    plan_type: str = "pilot"
    stores_pii: bool = True
    record_calls: bool = True
    create_resource_group: bool = True
    plivo_auto_provision: bool = False
    industry: Optional[str] = "Customer Support"
    country_code: Optional[str] = "IN"

    model_config = ConfigDict(populate_by_name=True)

class OrganizationResponse(OrganizationCreate):
    id: UUID
    
    model_config = ConfigDict(from_attributes=True)
