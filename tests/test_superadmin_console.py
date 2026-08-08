"""SuperAdmin console API — list, detail, and plan upgrade.

Integration tests against the test DB. Seeds an org with an active subscription
and two CallCost rows (one instrumented with the STT/LLM/TTS/Plivo COGS
breakdown, one historical with the COGS columns NULL) and exercises the three
console endpoints. Reuses the founder superadmin created by conftest.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.core.security import create_access_token
from app.db import session as db_session
from app.models.call_cost import CallCost
from app.models.organization import Organization
from app.models.subscription import Subscription
from app.models.tenant_resources import TenantResources
from app.models.user import SuperAdminUser

pytestmark = pytest.mark.asyncio

FOUNDER_EMAIL = "test_superadmin@nokvo.ai"  # created by conftest's setup fixture


async def _founder_headers() -> dict:
    async with db_session.AsyncSessionLocal() as db:
        founder = (
            await db.execute(select(SuperAdminUser).where(SuperAdminUser.email == FOUNDER_EMAIL))
        ).scalars().first()
    token = create_access_token(
        data={"sub": str(founder.id), "role": founder.role, "mfa_completed": True, "principal_type": "superadmin"}
    )
    return {"Authorization": f"Bearer {token}"}


async def _seed_org() -> tuple[uuid.UUID, str]:
    org_id = uuid.uuid4()
    tenant_id = f"tenant-cogs-{org_id.hex[:8]}"
    now = datetime.now(timezone.utc)
    async with db_session.AsyncSessionLocal() as db:
        db.add(Organization(
            id=org_id, name=f"COGS Test Org {org_id.hex[:6]}",
            admin_email="cogs@test.com", admin_name="COGS Admin",
            region="centralindia", environment="production",
            product_tier="nokvo_one", status="active",
            plan_type="inbound_only", calling_enabled=False,
        ))
        await db.flush()
        db.add(TenantResources(organization_id=org_id, tenant_id=tenant_id, provisioning_status="success"))
        await db.flush()  # tenant_resources row must exist before call_costs (FK on tenant_id)
        db.add(Subscription(
            organization_id=org_id, tenant_id=tenant_id, plan="inbound_only",
            amount_paise=449900, razorpay_subscription_id=f"sub_{org_id.hex[:12]}", status="active",
        ))
        # Instrumented call: full COGS breakdown.
        db.add(CallCost(
            organization_id=org_id, tenant_id=tenant_id, call_id=f"call-{org_id.hex[:8]}-1",
            kind="inbound", duration_seconds=Decimal("120"), rupees=Decimal("20.0000"),
            rate_per_second=Decimal("0.166667"), started_at=now, ended_at=now + timedelta(seconds=120),
            cost_stt_inr=Decimal("2.0000"), cost_llm_inr=Decimal("0.5000"),
            cost_tts_inr=Decimal("1.0000"), cost_telephony_inr=Decimal("2.0000"),
            cost_total_inr=Decimal("5.5000"), llm_input_tokens=1000, llm_output_tokens=500,
            llm_cached_tokens=100, stt_seconds=Decimal("120"), tts_characters=600,
        ))
        # Historical call: COGS columns NULL (pre-instrumentation).
        db.add(CallCost(
            organization_id=org_id, tenant_id=tenant_id, call_id=f"call-{org_id.hex[:8]}-2",
            kind="inbound", duration_seconds=Decimal("60"), rupees=Decimal("10.0000"),
            rate_per_second=Decimal("0.166667"), started_at=now, ended_at=now + timedelta(seconds=60),
        ))
        await db.commit()
    return org_id, tenant_id


async def _cleanup(org_id: uuid.UUID) -> None:
    async with db_session.AsyncSessionLocal() as db:
        await db.execute(delete(CallCost).where(CallCost.organization_id == org_id))
        await db.execute(delete(Subscription).where(Subscription.organization_id == org_id))
        await db.execute(delete(TenantResources).where(TenantResources.organization_id == org_id))
        await db.execute(delete(Organization).where(Organization.id == org_id))
        await db.commit()


async def test_list_tenants_reports_minutes_revenue_cogs_margin(client):
    headers = await _founder_headers()
    org_id, _ = await _seed_org()
    try:
        res = await client.get("/superadmin/tenants", headers=headers)
        assert res.status_code == 200
        row = next((o for o in res.json()["organizations"] if o["organization_id"] == str(org_id)), None)
        assert row is not None
        assert row["minutes_used"] == pytest.approx(3.0)          # 180s
        assert row["revenue"]["subscription_monthly_inr"] == pytest.approx(4499.0)
        assert row["revenue"]["usage_inr"] == pytest.approx(30.0)  # 20 + 10
        assert row["revenue"]["total_inr"] == pytest.approx(4529.0)
        assert row["cogs_inr"] == pytest.approx(5.5)               # only the instrumented call
        assert row["margin_inr"] == pytest.approx(4523.5)
        assert row["calling_enabled"] is False
    finally:
        await _cleanup(org_id)


async def test_detail_returns_per_call_breakdown(client):
    headers = await _founder_headers()
    org_id, _ = await _seed_org()
    try:
        res = await client.get(f"/superadmin/tenants/{org_id}", headers=headers)
        assert res.status_code == 200
        body = res.json()
        assert body["totals"]["cogs"]["stt_inr"] == pytest.approx(2.0)
        assert body["totals"]["cogs"]["llm_inr"] == pytest.approx(0.5)
        assert body["totals"]["cogs"]["tts_inr"] == pytest.approx(1.0)
        assert body["totals"]["cogs"]["telephony_inr"] == pytest.approx(2.0)
        assert body["totals"]["cogs"]["total_inr"] == pytest.approx(5.5)
        assert len(body["recent_calls"]) == 2
        instrumented = [c for c in body["recent_calls"] if c["instrumented"]]
        historical = [c for c in body["recent_calls"] if not c["instrumented"]]
        assert len(instrumented) == 1 and len(historical) == 1
        assert instrumented[0]["cost_total_inr"] == pytest.approx(5.5)
        assert instrumented[0]["llm_input_tokens"] == 1000
    finally:
        await _cleanup(org_id)


async def test_upgrade_and_downgrade_toggle_capability(client):
    headers = await _founder_headers()
    org_id, _ = await _seed_org()
    try:
        up = await client.post(
            f"/superadmin/tenants/{org_id}/upgrade", headers=headers,
            json={"plan": "inbound_outbound"},
        )
        assert up.status_code == 200
        assert up.json()["calling_enabled"] is True
        assert up.json()["plan_type"] == "inbound_outbound"

        async with db_session.AsyncSessionLocal() as db:
            org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalars().first()
            assert org.calling_enabled is True
            assert org.plan_type == "inbound_outbound"

        down = await client.post(
            f"/superadmin/tenants/{org_id}/upgrade", headers=headers,
            json={"plan": "inbound_only"},
        )
        assert down.status_code == 200
        assert down.json()["calling_enabled"] is False
        assert down.json()["plan_type"] == "inbound_only"
    finally:
        await _cleanup(org_id)


async def test_upgrade_rejects_unknown_plan(client):
    headers = await _founder_headers()
    org_id, _ = await _seed_org()
    try:
        res = await client.post(
            f"/superadmin/tenants/{org_id}/upgrade", headers=headers,
            json={"plan": "enterprise_unlimited"},
        )
        assert res.status_code == 400
    finally:
        await _cleanup(org_id)


async def test_endpoints_require_superadmin_auth(client):
    res = await client.get("/superadmin/tenants")
    assert res.status_code in (401, 403)


# ── APEX bulk-number assignment (sub-account pool) ──────────────────────────


async def _seed_apex_org(*, with_subaccount: bool = True) -> tuple[uuid.UUID, str]:
    """An APEX org whose TenantResources carries the account-creation sub-account
    creds the bulk pool reuses (mirrors provisioning Step 4)."""
    from app.core.secret_crypto import encrypt_secret

    org_id = uuid.uuid4()
    tenant_id = f"tenant-apex-{org_id.hex[:8]}"
    plivo_cfg: dict = {"compliance": {"application_id": f"app-{org_id.hex[:6]}"}}
    if with_subaccount:
        plivo_cfg["subaccount_auth_id"] = "ACCTSUB"
        plivo_cfg["subaccount_auth_token_enc"] = encrypt_secret("acct-token")
    async with db_session.AsyncSessionLocal() as db:
        db.add(Organization(
            id=org_id, name=f"APEX Test Org {org_id.hex[:6]}",
            admin_email="apex@test.com", admin_name="APEX Admin",
            region="centralindia", environment="production",
            product_tier="nokvo_apex", status="active",
            plan_type="apex", calling_enabled=True,
        ))
        await db.flush()
        db.add(TenantResources(
            organization_id=org_id, tenant_id=tenant_id, provisioning_status="success",
            provider_status={"plivo": plivo_cfg},
        ))
        await db.commit()
    return org_id, tenant_id


async def test_detail_reports_is_apex_and_bulk_pool(client):
    headers = await _founder_headers()
    org_id, _ = await _seed_apex_org()
    try:
        res = await client.get(f"/superadmin/tenants/{org_id}", headers=headers)
        assert res.status_code == 200
        body = res.json()
        assert body["is_apex"] is True
        assert body["product_tier"] == "nokvo_apex"
        # No pool assigned yet → not enabled, empty list.
        assert body["telephony"]["bulk_enabled"] is False
        assert body["telephony"]["bulk_numbers"] == []
    finally:
        await _cleanup(org_id)


async def test_apex_bulk_numbers_rejects_non_apex(client):
    headers = await _founder_headers()
    org_id, _ = await _seed_org()  # product_tier="nokvo_one"
    try:
        res = await client.post(
            f"/superadmin/tenants/{org_id}/apex-bulk-numbers", headers=headers,
            json={"numbers": []},
        )
        assert res.status_code == 409
    finally:
        await _cleanup(org_id)


async def test_apex_bulk_numbers_enables_pool_from_subaccount(client, monkeypatch):
    from app.services.plivo_service import PlivoService

    owned = ["919990001000", "919990001001", "919990001002"]

    async def _list(auth):
        return list(owned)

    monkeypatch.setattr(PlivoService, "list_account_numbers", staticmethod(_list))

    headers = await _founder_headers()
    org_id, tenant_id = await _seed_apex_org()
    try:
        # Blank numbers → auto-detect every DID on the sub-account.
        res = await client.post(
            f"/superadmin/tenants/{org_id}/apex-bulk-numbers", headers=headers,
            json={"numbers": []},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["enabled"] is True
        assert body["numbers"] == owned
        assert body["bulk_status"] == "active"

        # Persisted onto the tenant's provider_status["bulk_calling"] so the dialer
        # + the detail panel pick it up.
        async with db_session.AsyncSessionLocal() as db:
            tr = (await db.execute(
                select(TenantResources).where(TenantResources.organization_id == org_id)
            )).scalars().first()
            bulk = (tr.provider_status or {}).get("bulk_calling") or {}
            assert bulk.get("enabled") is True
            assert bulk.get("numbers") == owned
            assert bulk.get("auth_id") == "ACCTSUB"

        # Detail now reflects the enabled pool.
        detail = (await client.get(f"/superadmin/tenants/{org_id}", headers=headers)).json()
        assert detail["telephony"]["bulk_enabled"] is True
        assert detail["telephony"]["bulk_numbers"] == owned
    finally:
        await _cleanup(org_id)


async def test_apex_bulk_numbers_errors_without_subaccount(client):
    headers = await _founder_headers()
    org_id, _ = await _seed_apex_org(with_subaccount=False)
    try:
        res = await client.post(
            f"/superadmin/tenants/{org_id}/apex-bulk-numbers", headers=headers,
            json={"numbers": []},
        )
        assert res.status_code == 400
        assert "sub-account" in res.json()["detail"].lower()
    finally:
        await _cleanup(org_id)


# ── APEX plan change (upgrade / downgrade from the console) ─────────────────


async def test_apex_plan_catalog_is_served_to_the_console(client):
    headers = await _founder_headers()
    res = await client.get("/superadmin/tenants/apex/plans", headers=headers)
    assert res.status_code == 200
    codes = {p["code"] for p in res.json()["plans"]}
    assert {"free_trial", "core", "growth", "pinnacle", "enterprise"} <= codes


async def test_detail_exposes_the_stamped_apex_plan(client):
    """The console shows what the account is ACTUALLY billed at (its stamped columns),
    which is what the plan-change modal opens with."""
    from app.services.apex_plans import stamp_org_from_plan

    headers = await _founder_headers()
    org_id, _ = await _seed_apex_org()
    try:
        async with db_session.AsyncSessionLocal() as db:
            org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalars().first()
            stamp_org_from_plan(org, "growth")
            await db.commit()

        body = (await client.get(f"/superadmin/tenants/{org_id}", headers=headers)).json()
        assert body["apex_plan"]["plan_code"] == "growth"
        assert body["apex_plan"]["plan_label"] == "Growth"
        assert body["apex_plan"]["rate_per_minute"] == pytest.approx(7.5)
        assert body["apex_plan"]["concurrency"] == 2
        assert body["apex_plan"]["included_minutes"] == 5000
    finally:
        await _cleanup(org_id)


async def test_detail_has_no_apex_plan_for_a_nokvo_one_org(client):
    headers = await _founder_headers()
    org_id, _ = await _seed_org()
    try:
        body = (await client.get(f"/superadmin/tenants/{org_id}", headers=headers)).json()
        assert body["apex_plan"] is None
    finally:
        await _cleanup(org_id)


async def test_change_plan_requires_an_explicit_acknowledgement(client):
    from app.services.apex_plans import stamp_org_from_plan

    headers = await _founder_headers()
    org_id, _ = await _seed_apex_org()
    try:
        async with db_session.AsyncSessionLocal() as db:
            org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalars().first()
            stamp_org_from_plan(org, "core")
            await db.commit()

        res = await client.post(
            f"/superadmin/tenants/apex/accounts/{org_id}/plan", headers=headers,
            json={"plan_code": "growth", "rebill": False},
        )
        assert res.status_code == 400
        async with db_session.AsyncSessionLocal() as db:
            org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalars().first()
            assert org.apex_plan_code == "core"  # nothing moved
    finally:
        await _cleanup(org_id)


async def test_change_plan_upgrades_and_writes_an_audit_row(client, monkeypatch):
    from app.models.audit import SuperAdminAuditLog
    from app.services import apex_account_service
    from app.services.apex_plans import stamp_org_from_plan

    monkeypatch.setattr(apex_account_service, "_razorpay_configured", lambda: False)

    async def _no_mail(*a, **k):
        return None

    monkeypatch.setattr(apex_account_service.EmailService, "send_apex_plan_change_email", _no_mail)

    headers = await _founder_headers()
    org_id, _ = await _seed_apex_org()
    try:
        async with db_session.AsyncSessionLocal() as db:
            org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalars().first()
            stamp_org_from_plan(org, "core")
            await db.commit()

        res = await client.post(
            f"/superadmin/tenants/apex/accounts/{org_id}/plan", headers=headers,
            json={"plan_code": "pinnacle", "rebill": False, "acknowledge": True},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["changed"] is True
        assert body["after"]["plan_code"] == "pinnacle"
        assert body["after"]["concurrency"] == 4
        assert body["wallet_unchanged"] is True

        async with db_session.AsyncSessionLocal() as db:
            org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalars().first()
            assert org.apex_plan_code == "pinnacle"
            assert float(org.apex_rate_per_minute) == pytest.approx(5.5)
            assert org.apex_support_tier == "level_a"
            audits = (await db.execute(
                select(SuperAdminAuditLog).where(SuperAdminAuditLog.target_id == str(org_id))
            )).scalars().all()
        assert any(a.action == "apex_plan_changed" for a in audits)
    finally:
        async with db_session.AsyncSessionLocal() as db:
            await db.execute(delete(SuperAdminAuditLog).where(SuperAdminAuditLog.target_id == str(org_id)))
            await db.commit()
        await _cleanup(org_id)


async def test_change_plan_rejects_a_non_apex_org(client):
    headers = await _founder_headers()
    org_id, _ = await _seed_org()  # nokvo_one
    try:
        res = await client.post(
            f"/superadmin/tenants/apex/accounts/{org_id}/plan", headers=headers,
            json={"plan_code": "growth", "rebill": False, "acknowledge": True},
        )
        assert res.status_code == 409
    finally:
        await _cleanup(org_id)
