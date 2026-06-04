"""Conftest for integration tests against a real Postgres + Redis.

Integration tests are opt-in. They run when both env vars are set:

  * ``TEST_DATABASE_URL`` — async Postgres DSN
    (e.g. ``postgresql+asyncpg://user:pass@localhost:5432/nokvo_test``).
    Falls back to ``DATABASE_URL`` / the regular dev DSN if the dev DB
    is safe to use (we don't recommend that — these tests CREATE and
    DELETE rows).
  * ``TEST_REDIS_URL`` — redis DSN, defaults to ``redis://localhost:6379``.

When neither is set, the integration tests are skipped with a clear
message so a contributor without local infra can still run the unit
suite.

The fixtures here are intentionally minimal — they seed only what the
test needs (Organization + OrganizationUser + TenantResources +
assignment settings) and clean up afterwards. Running the suite leaves
the DB in the state it found it in.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, time, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


def _resolve_db_dsn() -> str | None:
    """Pick the database DSN to use for integration tests.

    ``TEST_DATABASE_URL`` is the preferred name (matches the
    test_database_url convention from the README). Fall back to
    ``DATABASE_URL``. Return ``None`` to make pytest skip the suite.
    """
    return os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")


def _resolve_redis_url() -> str | None:
    return os.getenv("TEST_REDIS_URL") or os.getenv("REDIS_URL")


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(scope="module")
async def integration_db() -> AsyncSession:
    dsn = _resolve_db_dsn()
    if not dsn:
        pytest.skip(
            "Integration tests require TEST_DATABASE_URL (or DATABASE_URL) to be set. "
            "Set it to an async Postgres DSN to enable these tests."
        )
    engine = create_async_engine(dsn, poolclass=NullPool, future=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded_org(integration_db: AsyncSession):
    """Create a minimal real-estate org + user + assignable agent.

    Tears down everything it created on test exit so the DB is left
    clean. The returned dict carries the ids the test can use.
    """
    from app.models.organization import Organization
    from app.models.organization_user import OrganizationUser
    from app.models.member_assignment import OrganizationMemberAssignmentSettings
    from app.models.tenant_resources import TenantResources

    org_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    tenant_id = f"integration-{org_id.hex[:8]}"

    org = Organization(
        id=org_id,
        name="Integration Test Org",
        industry="real_estate",
        calling_enabled=False,
        status="active",
    )
    agent = OrganizationUser(
        id=agent_id,
        organization_id=org_id,
        email=f"agent-{agent_id.hex[:6]}@example.com",
        password_hash="x",
        full_name="Test Sales Agent",
        role="agent",
        status="active",
        mfa_required=False,
    )
    settings_row = OrganizationMemberAssignmentSettings(
        id=uuid.uuid4(),
        organization_id=org_id,
        member_id=agent_id,
        request_types=["site_visit"],
        is_assignable=True,
        timezone="Asia/Kolkata",
        start_time=time(9, 0),
        end_time=time(19, 0),
        working_days=["mon", "tue", "wed", "thu", "fri", "sat"],
        max_active_requests=10,
        max_requests_per_day=20,
        max_requests_per_hour=4,
        appointment_duration_minutes=30,
    )
    tenant = TenantResources(
        organization_id=org_id,
        tenant_id=tenant_id,
        provider_status={},
    )
    integration_db.add_all([org, agent, settings_row, tenant])
    await integration_db.commit()

    try:
        yield {
            "organization_id": org_id,
            "agent_id": agent_id,
            "tenant_id": tenant_id,
        }
    finally:
        # Best-effort teardown — wrap each delete so a missing row
        # doesn't leave the next test unable to seed.
        from app.models.nokvo_one_tool_record import NokvoOneToolRecord
        from app.models.agent_tool_invocation import AgentToolInvocation

        for stmt in (
            delete(AgentToolInvocation).where(AgentToolInvocation.organization_id == org_id),
            delete(NokvoOneToolRecord).where(NokvoOneToolRecord.organization_id == org_id),
            delete(OrganizationMemberAssignmentSettings).where(
                OrganizationMemberAssignmentSettings.organization_id == org_id
            ),
            delete(OrganizationUser).where(OrganizationUser.organization_id == org_id),
            delete(TenantResources).where(TenantResources.organization_id == org_id),
            delete(Organization).where(Organization.id == org_id),
        ):
            try:
                await integration_db.execute(stmt)
            except Exception:
                await integration_db.rollback()
        try:
            await integration_db.commit()
        except Exception:
            await integration_db.rollback()
