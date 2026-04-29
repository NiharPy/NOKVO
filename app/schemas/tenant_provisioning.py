from pydantic import BaseModel, ConfigDict
from typing import Optional, List, Dict, Any
from uuid import UUID

class StepResult(BaseModel):
    name: str
    status: str
    message: Optional[str] = None
    resource_id: Optional[str] = None

class AzureResourcesResponse(BaseModel):
    resource_group: Optional[str] = None
    region: Optional[str] = None

class TenantResourcesResponse(BaseModel):
    qdrant_collection: Optional[str] = None
    redis_namespace: Optional[str] = None
    blob_prefix: Optional[str] = None
    key_vault: Optional[str] = None
    stt_provider: Optional[str] = None
    stt_model: Optional[str] = None
    stt_endpoint: Optional[str] = None
    stt_status: Optional[str] = None
    tts_provider: Optional[str] = None
    tts_model: Optional[str] = None
    tts_status: Optional[str] = None
    tts_speaker: Optional[str] = None
    twilio_status: Optional[str] = None
    twilio_provider: Optional[str] = None
    twilio_subaccount_id: Optional[str] = None

class TenantProvisioningResult(BaseModel):
    tenant_id: str
    organization_id: UUID
    status: str
    azure: AzureResourcesResponse
    resources: TenantResourcesResponse
    steps: List[StepResult]
    next_steps: List[str]
    
    model_config = ConfigDict(from_attributes=True)
