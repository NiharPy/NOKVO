from pydantic import BaseModel, ConfigDict, Field
from typing import Any, Optional


class TenantUsageEventCreate(BaseModel):
    event_type: str = "call_session"
    minutes: int = Field(default=0, ge=0)
    stt_minutes: float = Field(default=0, ge=0)
    telephony_minutes: float = Field(default=0, ge=0)
    tts_characters: int = Field(default=0, ge=0)
    llm_api_calls: int = Field(default=0, ge=0)
    llm_input_tokens: int = Field(default=0, ge=0)
    llm_output_tokens: int = Field(default=0, ge=0)
    storage_gb: float = Field(default=0, ge=0)
    infrastructure_units: int = Field(default=0, ge=0)
    metadata: Optional[dict[str, Any]] = None

    model_config = ConfigDict(extra="forbid")
