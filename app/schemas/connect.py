"""Pydantic schemas for the Nokvo Connect public API + admin key management."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


# ────────────────────────────────────────────────────────────────────────────
# Admin — API key management
# ────────────────────────────────────────────────────────────────────────────


class ApiKeyCreateRequest(BaseModel):
    label: str = Field(..., min_length=1, max_length=120)
    mode: Literal["live", "test"] = "live"
    scopes: list[str] = Field(default_factory=lambda: ["voice:sessions"])
    allowed_origins: list[str] = Field(default_factory=list)
    rate_limit_rpm: int = Field(default=60, ge=1, le=10_000)
    max_concurrent_sessions: int = Field(default=5, ge=1, le=200)
    webhook_url: HttpUrl | None = None
    expires_at: datetime | None = None

    @field_validator("scopes")
    @classmethod
    def _scopes_subset(cls, v: list[str]) -> list[str]:
        allowed = {"voice:sessions", "voice:stream", "voice:text"}
        unknown = set(v) - allowed
        if unknown:
            raise ValueError(f"Unknown scopes: {sorted(unknown)}")
        return list(dict.fromkeys(v))


class ApiKeyUpdateRequest(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    scopes: list[str] | None = None
    allowed_origins: list[str] | None = None
    rate_limit_rpm: int | None = Field(default=None, ge=1, le=10_000)
    max_concurrent_sessions: int | None = Field(default=None, ge=1, le=200)
    webhook_url: HttpUrl | None = None


class ApiKeyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    label: str
    key_prefix: str
    mode: str
    scopes: list[str]
    allowed_origins: list[str]
    rate_limit_rpm: int
    max_concurrent_sessions: int
    webhook_url: str | None
    status: str
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    revoke_reason: str | None
    created_at: datetime


class ApiKeyCreatedResponse(BaseModel):
    """One-shot response that exposes the raw key. Show it once, then forget it."""

    api_key: ApiKeyResponse
    secret: str = Field(..., description="The raw key. Show to the user once; we cannot recover it later.")
    webhook_secret: str | None = Field(default=None, description="HMAC signing secret for webhooks. Same one-shot disclosure rule.")


# ────────────────────────────────────────────────────────────────────────────
# Public — sessions
# ────────────────────────────────────────────────────────────────────────────


class SessionCreateRequest(BaseModel):
    channel: Literal["voice", "text"] = "voice"
    language: str = Field(default="en", max_length=8)
    client_session_id: str | None = Field(default=None, max_length=120)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    channel: str
    status: str
    language: str
    client_session_id: str | None
    transcript: list[dict[str, Any]]
    usage: dict[str, Any]
    metadata_: dict[str, Any] = Field(default_factory=dict, alias="metadata")
    started_at: datetime | None
    ended_at: datetime | None
    created_at: datetime


class SessionCreateResponse(BaseModel):
    session: SessionResponse
    websocket_url: str | None = Field(
        default=None,
        description="WebSocket endpoint to stream audio. Only set when channel=voice.",
    )
    idle_timeout_seconds: int = 90


class TextMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=4000)


class TextMessageResponse(BaseModel):
    session_id: UUID
    user_message: dict[str, Any]
    assistant_message: dict[str, Any]
    usage: dict[str, Any]
