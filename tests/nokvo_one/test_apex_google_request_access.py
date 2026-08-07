"""APEX is request-gated: an UNREGISTERED user must never reach the payment screen.

Regression cover for the bug where signing in with Google using an email that has no
APEX account silently CREATED a ``pending_payment`` org and handed back a payment token,
dropping a stranger straight onto the payment page. The request-access gate existed but
was conditional on ``ENABLE_APEX_PLANS``, which defaulted to OFF (and was unset in prod), so
the gate never ran.

Both entry points are covered — Google OAuth and the legacy self-serve signup endpoint —
because they reached the same payment screen.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text

from app.api import nokvo_one_apex_auth as apex_auth
from app.core.config import settings
from app.db import session as db_session
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser

GOOGLE_LOGIN_URL = "/api/nokvo-one/apex/google/login"
SIGNUP_URL = "/api/nokvo-one/apex/signup"


def _fake_google(email: str, full_name: str | None = "Stranger Person"):
    async def _verify(_id_token):
        return {"email": email, "full_name": full_name, "email_verified": True}

    return _verify


async def _org_count_for(email: str) -> int:
    async with db_session.AsyncSessionLocal() as db:
        res = await db.execute(select(Organization).where(Organization.admin_email == email))
        return len(res.scalars().all())


async def _cleanup(email: str) -> None:
    async with db_session.AsyncSessionLocal() as db:
        await db.execute(
            text(
                "DELETE FROM organization_users WHERE organization_id IN "
                "(SELECT id FROM organizations WHERE admin_email=:e)"
            ),
            {"e": email},
        )
        await db.execute(text("DELETE FROM organizations WHERE admin_email=:e"), {"e": email})
        await db.commit()


@pytest.mark.asyncio
async def test_google_login_unregistered_goes_to_request_access(client, monkeypatch):
    """No APEX account → request-access signal, NOT a payment token, and NO org created."""
    email = f"stranger-{uuid.uuid4().hex[:10]}@gmail.com"
    monkeypatch.setattr(apex_auth.GoogleOAuthService, "verify_id_token", _fake_google(email))
    try:
        resp = await client.post(GOOGLE_LOGIN_URL, json={"id_token": "fake-token"})
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # The actual bug: this used to be "payment_required" with a live payment_token.
        assert body["code"] == "request_access_required"
        assert "payment_token" not in body
        assert body["email"] == email
        assert body["full_name"] == "Stranger Person"

        # And it must not have provisioned anything behind the scenes.
        assert await _org_count_for(email) == 0
    finally:
        await _cleanup(email)


@pytest.mark.asyncio
async def test_google_gate_holds_with_apex_plans_flag_off(client, monkeypatch):
    """The gate must NOT depend on ENABLE_APEX_PLANS — that flag being off was the bug."""
    email = f"stranger-{uuid.uuid4().hex[:10]}@gmail.com"
    monkeypatch.setattr(settings, "ENABLE_APEX_PLANS", False)
    monkeypatch.setattr(apex_auth.GoogleOAuthService, "verify_id_token", _fake_google(email))
    try:
        resp = await client.post(GOOGLE_LOGIN_URL, json={"id_token": "fake-token"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["code"] == "request_access_required"
        assert await _org_count_for(email) == 0
    finally:
        await _cleanup(email)


@pytest.mark.asyncio
async def test_existing_pending_payment_account_still_reaches_payment(client, monkeypatch):
    """Guard against over-fixing: a REGISTERED org still gets its payment screen."""
    email = f"registered-{uuid.uuid4().hex[:10]}@gmail.com"
    org_id = uuid.uuid4()
    async with db_session.AsyncSessionLocal() as db:
        db.add(
            Organization(
                id=org_id, name="registered-test", region="southindia", environment="staging",
                status="pending_payment", product_tier="nokvo_apex", calling_enabled=True,
                admin_email=email,
            )
        )
        await db.flush()
        db.add(
            OrganizationUser(
                id=uuid.uuid4(), organization_id=org_id, email=email, full_name="Registered",
                role="admin", status="active", auth_provider="google", email_verified=True,
            )
        )
        await db.commit()

    monkeypatch.setattr(apex_auth.GoogleOAuthService, "verify_id_token", _fake_google(email))
    try:
        resp = await client.post(GOOGLE_LOGIN_URL, json={"id_token": "fake-token"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == "payment_required"
        assert body.get("payment_token")
    finally:
        await _cleanup(email)


@pytest.mark.asyncio
async def test_self_serve_signup_is_closed(client, monkeypatch):
    """Same payment screen was reachable by POSTing the legacy signup endpoint directly."""
    # /apex/signup is rate-limited to 5/hour, and the limiter's counter is process-wide, so
    # repeated runs would return 429 and mask what this test is actually asserting.
    from app.core.rate_limit import limiter

    monkeypatch.setattr(limiter, "enabled", False)
    email = f"stranger-{uuid.uuid4().hex[:10]}@gmail.com"
    try:
        resp = await client.post(
            SIGNUP_URL,
            json={
                "org_name": "Walk In Co",
                "admin_name": "Walk In",
                "admin_email": email,
                "password": "StrongPassword123!",
                "country_code": "IN",
            },
        )
        assert resp.status_code == 403, resp.text
        assert await _org_count_for(email) == 0
    finally:
        await _cleanup(email)
