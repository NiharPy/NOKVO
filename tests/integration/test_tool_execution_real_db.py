"""Integration smoke tests against a real Postgres.

These exercise the exact code path that surfaced the ``greenlet_spawn``
class of bugs (which the FakeDB-based unit tests don't catch). One test
per critical tool flow is enough — the goal is to prove the async
session lifecycle works end-to-end against a real connection, not to
test every tool variant (the unit tests already do that exhaustively).

Skips when ``TEST_DATABASE_URL`` isn't set — see ``conftest.py`` for
the env-driven setup.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services.dynamic_tool_resolver import resolve_index
from app.services.predefined_tools_service import PredefinedToolsService


@pytest.mark.asyncio
async def test_qualify_lead_and_schedule_visit_against_real_db(
    integration_db, seeded_org
) -> None:
    """The qualify_lead_and_schedule_visit macro must complete a full
    create-ticket + create-callback + assign cycle against a real async
    session without raising ``greenlet_spawn has not been called``."""
    catalog = resolve_index("real_estate")
    tool = catalog.get("qualify_lead_and_schedule_visit")
    assert tool is not None, "real_estate catalog must expose the site-visit macro"

    visit_at = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
        hour=10, minute=0, second=0, microsecond=0
    ).isoformat()
    result = await PredefinedToolsService.execute(
        integration_db,
        seeded_org["organization_id"],
        None,
        tool,
        {
            "name": "Integration Test Buyer",
            "phone": "9999999999",
            "visit_at": visit_at,
            "record_data": {
                "name": "Integration Test Buyer",
                "phone": "9999999999",
                "visit_date": visit_at,
                "issue_type": "site_visit",
                "priority": "normal",
            },
        },
    )
    await integration_db.commit()

    # The macro should have created a ticket + a callback and either
    # assigned to the seeded agent or flagged "no_available_member"
    # (acceptable — the test cares about no exception, not about which
    # member wins).
    assert result["ok"] is True
    assert "ticket_id" in result
    assert "callback_id" in result
    assert result["record_type"] == "ticket"
    assignment_status = result.get("assignment_status")
    assert assignment_status in (
        "assigned",
        "no_available_member",
    ), f"unexpected assignment_status: {assignment_status!r}"


@pytest.mark.asyncio
async def test_leads_create_against_real_db(integration_db, seeded_org) -> None:
    """leads_create — the simpler path — must also round-trip cleanly.
    This catches session-corruption bugs that only show up at commit
    time after multiple flushes within a session."""
    catalog = resolve_index("real_estate")
    tool = catalog.get("leads_create")
    if tool is None:
        pytest.skip("leads_create tool not registered for real_estate")
    result = await PredefinedToolsService.execute(
        integration_db,
        seeded_org["organization_id"],
        None,
        tool,
        {
            "name": "Integration Test Lead",
            "phone": "8888888888",
        },
    )
    await integration_db.commit()
    assert result.get("ok") is True
    assert isinstance(result.get("id"), str)
