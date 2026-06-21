"""DB-backed LLM pool members (managed from the SuperAdmin console).

The shared gpt-5-mini / nano pools are normally configured from environment
(``AZURE_OPENAI_POOL_JSON`` etc.). This table lets an operator add or change
pool keys at runtime — each enabled row is merged into the live pool (de-duped
by endpoint) by ``llm_pool``'s background refresher, so a new Azure OpenAI
account starts taking traffic without a redeploy. ``api_key`` is stored
encrypted (Fernet, like the Plivo secrets).
"""
from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.db.session import Base


class LlmPoolKey(Base):
    __tablename__ = "llm_pool_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # "mini" (gpt-5-mini, the main agent pool) | "nano" (summary/condenser pool).
    pool = Column(String, nullable=False, server_default="mini")
    label = Column(String, nullable=True)
    endpoint = Column(String, nullable=False)
    api_key_enc = Column(String, nullable=False)  # Fernet-encrypted
    deployment = Column(String, nullable=True)
    tpm = Column(Integer, nullable=True)
    enabled = Column(Boolean, nullable=False, server_default="true")

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
