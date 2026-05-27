import asyncio
from types import SimpleNamespace

import jwt
import pytest

from app.core import security
from app.core.config import settings
from app.models.audit import VoiceDataAccessAuditLog
from app.models.organization_session import OrganizationSession
from app.models.session import SuperAdminSession
from app.services.agent_session_store import AgentSessionStore
from app.services.mcp_toolkit.execution_generator import ExecutionGenerator
from app.services.provider_status_schema import sanitize_provider_status
from app.services.qdrant_service import QdrantService


def test_jwt_tiers_use_isolated_signing_keys_with_legacy_decode_only():
    super_token = security.create_access_token(
        subject="super-1",
        extra_claims={"principal_type": "superadmin"},
        token_tier=security.JWT_TIER_SUPERADMIN,
    )
    org_token = security.create_access_token(
        subject="org-user-1",
        extra_claims={"principal_type": "organization_user", "organization_id": "org-1"},
        token_tier=security.JWT_TIER_ORGANIZATION,
    )

    assert security.decode_access_token(
        super_token, expected_tiers=[security.JWT_TIER_SUPERADMIN]
    )["sub"] == "super-1"
    assert security.decode_access_token(
        org_token, expected_tiers=[security.JWT_TIER_ORGANIZATION]
    )["sub"] == "org-user-1"

    with pytest.raises(jwt.PyJWTError):
        security.decode_access_token(org_token, expected_tiers=[security.JWT_TIER_SUPERADMIN])
    with pytest.raises(jwt.PyJWTError):
        jwt.decode(super_token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])

    legacy = jwt.encode({"sub": "legacy-super"}, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    assert security.decode_access_token(
        legacy, expected_tiers=[security.JWT_TIER_SUPERADMIN]
    )["sub"] == "legacy-super"


def test_oauth_state_is_hmac_verified_and_tamper_rejected():
    state = security.create_oauth_state({"tenant_id": "t1"}, purpose="lead_source_oauth")
    assert security.decode_oauth_state(state, expected_purpose="lead_source_oauth")["tenant_id"] == "t1"
    with pytest.raises(jwt.PyJWTError):
        security.decode_oauth_state(state[:-1] + ("0" if state[-1] != "0" else "1"), expected_purpose="lead_source_oauth")


def test_refresh_token_hash_columns_are_indexed():
    assert SuperAdminSession.__table__.c.refresh_token_hash.index is True
    assert OrganizationSession.__table__.c.refresh_token_hash.index is True


def test_provider_status_is_bounded_json_schema():
    sanitized = sanitize_provider_status(
        {
            "agent_knowledge_documents": [{"id": i} for i in range(600)],
            "single_prompt_voice_agent": {"enabled": True, "prompt": "x" * 9000},
            "business_template_custom_tabs": [
                {
                    "slug": "site_visits",
                    "label": "Site Visits",
                    "fields": [{"key": "phone", "label": "Phone", "type": "phone"}],
                    "search_keys": ["phone"],
                }
            ],
            "unknown_nested": {"value": object()},
        }
    )
    assert len(sanitized["agent_knowledge_documents"]) == 500
    assert len(sanitized["single_prompt_voice_agent"]["prompt"]) == 8000
    assert sanitized["business_template_custom_tabs"][0]["slug"] == "site_visits"
    assert isinstance(sanitized["unknown_nested"]["value"], str)
    with pytest.raises(ValueError):
        sanitize_provider_status([])


def test_semantic_cache_key_is_scoped_by_call_context():
    tenant = SimpleNamespace(tenant_id="tenant-a", redis_namespace="tenant:a", provider_status={})
    key_a = AgentSessionStore.semantic_cache_key(tenant, "what is the price", "en", call_context="call-a")
    key_b = AgentSessionStore.semantic_cache_key(tenant, "what is the price", "en", call_context="call-b")
    assert key_a != key_b


@pytest.mark.asyncio
async def test_async_tester_stash_uses_event_loop_lock():
    from app.api import nokvo_one_voice

    assert isinstance(nokvo_one_voice._tester_stash_lock, asyncio.Lock)
    token = await nokvo_one_voice._tester_stash_put({"agent_prompt": "hello"})
    assert await nokvo_one_voice._tester_stash_pop(token) == {"agent_prompt": "hello"}
    assert await nokvo_one_voice._tester_stash_pop(token) is None


def test_generated_sql_limits_include_db_enforcement():
    limits = ExecutionGenerator.limits()
    assert limits["max_total_rows"] == settings.MCP_SQL_MAX_ROWS
    assert limits["db_enforced"]["statement_timeout_ms"] == settings.MCP_SQL_STATEMENT_TIMEOUT_MS
    assert limits["db_enforced"]["limit_parameter"] == "limit"


@pytest.mark.asyncio
async def test_qdrant_tenant_quota_blocks_oversized_upsert(monkeypatch):
    class CountResponse:
        count = 10

    class FakeClient:
        def count(self, **_kwargs):
            return CountResponse()

    tenant = SimpleNamespace(tenant_id="tenant-a", organization_id="org-a", provider_status={})
    monkeypatch.setattr(settings, "QDRANT_MAX_POINTS_PER_TENANT", 11)
    monkeypatch.setattr(settings, "QDRANT_MAX_UPSERT_POINTS", 10)

    with pytest.raises(RuntimeError, match="storage quota"):
        await QdrantService._enforce_tenant_quota(
            "collection",
            tenant,
            FakeClient(),
            [SimpleNamespace(id="1"), SimpleNamespace(id="2")],
        )


def test_voice_data_audit_log_is_formal_table():
    columns = {column.name for column in VoiceDataAccessAuditLog.__table__.columns}
    assert VoiceDataAccessAuditLog.__tablename__ == "voice_data_access_audit_logs"
    assert {"organization_id", "actor_type", "access_type", "resource_type", "call_id", "metadata"}.issubset(columns)
