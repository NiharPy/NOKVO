"""APEX automated bulk DID provisioning — cost-safe top-up to exactly 5.

PlivoService is fully mocked (no network). These pin the idempotent, cost-safe buy
logic: never over-buy past 5, gate purchases on compliance APPROVAL, and the
enabled/active transitions. The sub-account + compliance-reuse wiring is exercised
through ``start_auto_provision``.
"""
from __future__ import annotations

import pytest

from app.core.secret_crypto import encrypt_secret
from app.services.plivo_bulk_provisioning_service import (
    BULK_DID_COUNT,
    PlivoBulkProvisioningService,
)
from app.services.plivo_service import PlivoError, PlivoService


class _FakeDB:
    async def commit(self):
        pass


class _FakeTR:
    def __init__(self, provider_status):
        self.provider_status = provider_status
        self.tenant_id = "tnt-abcd1234"


def _tr(app_id="app-123", bulk=None, sub_auth="ACCTSUB", sub_token="acct-token"):
    # plivo.subaccount_* mirrors what provisioning Step 4 writes at APEX account
    # creation — the bulk pool reuses THIS sub-account (no new one is created).
    plivo = {"compliance": {"application_id": app_id} if app_id else {}}
    if sub_auth:
        plivo["subaccount_auth_id"] = sub_auth
        plivo["subaccount_auth_token_enc"] = encrypt_secret(sub_token)
    ps = {"plivo": plivo}
    if bulk is not None:
        ps["bulk_calling"] = bulk
    return _FakeTR(ps)


@pytest.fixture
def plivo(monkeypatch):
    """Mock the Plivo primitives + a configurable compliance status / sell behaviour."""
    state = {"owned": [], "compliance_status": "approved", "rent_calls": 0, "create_calls": 0, "rent_raises": False}

    async def create_subaccount(name):
        state["create_calls"] += 1
        return {"auth_id": "SUBAUTH", "auth_token": "subtoken"}

    async def list_account_numbers(auth):
        return list(state["owned"])

    async def rent_number(**kwargs):
        if state["rent_raises"]:
            raise PlivoError("no rentable DIDs (KYC/regulatory)")
        state["rent_calls"] += 1
        num = PlivoService.normalize_number(f"+9199900{1000 + len(state['owned'])}")
        state["owned"].append(num)
        return {"number": num, "status": "active"}

    async def _request(method, url, **kwargs):
        return {"status": state["compliance_status"]}

    monkeypatch.setattr(PlivoService, "create_subaccount", staticmethod(create_subaccount))
    monkeypatch.setattr(PlivoService, "list_account_numbers", staticmethod(list_account_numbers))
    monkeypatch.setattr(PlivoService, "rent_number", staticmethod(rent_number))
    monkeypatch.setattr(PlivoService, "_master_auth", staticmethod(lambda: ("M", "T")))
    monkeypatch.setattr(PlivoService, "_base", staticmethod(lambda a: "https://api.plivo.test"))
    monkeypatch.setattr(PlivoService, "_request", staticmethod(_request))
    return state


def test_bulk_did_count_is_5():
    assert BULK_DID_COUNT == 5


@pytest.mark.asyncio
async def test_buys_exactly_5_when_approved(plivo):
    cfg = await PlivoBulkProvisioningService.start_auto_provision(_tr(), _FakeDB(), superadmin_id="sa")
    assert plivo["create_calls"] == 0          # NO new sub-account — reuses account-creation one
    assert plivo["rent_calls"] == 5            # exactly 5 DIDs bought on it
    assert cfg["status"] == "active"
    assert cfg["enabled"] is True
    assert len(cfg["numbers"]) == 5
    assert cfg["number"] == cfg["numbers"][0]  # primary caller id = first DID
    assert cfg["auth_id"] == "ACCTSUB"         # the account-creation sub-account
    assert cfg["auto_provisioned"] is True
    assert cfg["uses_account_subaccount"] is True


@pytest.mark.asyncio
async def test_no_overbuy_when_some_owned(plivo):
    plivo["owned"] = ["919990001000", "919990001001"]  # 2 already on the sub-account
    cfg = await PlivoBulkProvisioningService.start_auto_provision(_tr(), _FakeDB(), superadmin_id="sa")
    assert plivo["rent_calls"] == 3            # only tops up the remaining 3
    assert len(cfg["numbers"]) == 5
    assert cfg["status"] == "active"


@pytest.mark.asyncio
async def test_already_at_target_buys_nothing(plivo):
    plivo["owned"] = [f"91999000{i}" for i in range(5)]  # already 5
    cfg = await PlivoBulkProvisioningService.start_auto_provision(_tr(), _FakeDB(), superadmin_id="sa")
    assert plivo["rent_calls"] == 0
    assert cfg["status"] == "active"
    assert len(cfg["numbers"]) == 5


@pytest.mark.asyncio
async def test_compliance_pending_buys_nothing(plivo):
    plivo["compliance_status"] = "in-review"
    cfg = await PlivoBulkProvisioningService.start_auto_provision(_tr(), _FakeDB(), superadmin_id="sa")
    assert plivo["rent_calls"] == 0            # NO DIDs bought pre-approval
    assert cfg["status"] == "provisioning"
    assert cfg["enabled"] is False


@pytest.mark.asyncio
async def test_no_provisioned_subaccount_errors(plivo):
    # No sub-account from account creation → can't tie a pool to it.
    cfg = await PlivoBulkProvisioningService.start_auto_provision(
        _tr(sub_auth=None), _FakeDB(), superadmin_id="sa"
    )
    assert plivo["rent_calls"] == 0
    assert "sub-account" in (cfg.get("error") or "").lower()


@pytest.mark.asyncio
async def test_no_compliance_application_errors(plivo):
    cfg = await PlivoBulkProvisioningService.start_auto_provision(_tr(app_id=None), _FakeDB(), superadmin_id="sa")
    assert plivo["rent_calls"] == 0
    assert cfg["status"] == "provisioning"
    assert "compliance" in (cfg.get("error") or "").lower()


@pytest.mark.asyncio
async def test_partial_then_retry_tops_up_without_exceeding(plivo):
    tr = _tr()
    # 1st run: approved but Plivo won't sell yet → 0 bought, stays provisioning.
    plivo["rent_raises"] = True
    cfg1 = await PlivoBulkProvisioningService.start_auto_provision(tr, _FakeDB(), superadmin_id="sa")
    assert plivo["rent_calls"] == 0 and cfg1["status"] == "provisioning"
    # 2nd run (poller): DIDs now available → tops up to exactly 5, active.
    plivo["rent_raises"] = False
    cfg2 = await PlivoBulkProvisioningService._buy_pending_numbers(tr, _FakeDB())
    assert len(cfg2["numbers"]) == 5 and cfg2["status"] == "active"
    # 3rd run: already active → no further buys (idempotent, no over-buy).
    before = plivo["rent_calls"]
    cfg3 = await PlivoBulkProvisioningService._buy_pending_numbers(tr, _FakeDB())
    assert plivo["rent_calls"] == before and len(cfg3["numbers"]) == 5


# ── manual path: enable on the existing sub-account (no buying, no creds pasted) ──


@pytest.mark.asyncio
async def test_existing_subaccount_auto_detects_numbers(plivo):
    plivo["owned"] = ["919990001000", "919990001001", "919990001002"]
    cfg = await PlivoBulkProvisioningService.grant_with_existing_subaccount(
        _tr(), _FakeDB(), superadmin_id="sa"
    )
    assert plivo["rent_calls"] == 0                       # NEVER buys
    assert cfg["enabled"] is True and cfg["status"] == "active"
    assert cfg["numbers"] == ["919990001000", "919990001001", "919990001002"]
    assert cfg["number"] == "919990001000"                # primary caller id = first
    assert cfg["auth_id"] == "ACCTSUB"                    # the account-creation sub-account
    assert cfg["auto_provisioned"] is False
    assert cfg["uses_account_subaccount"] is True


@pytest.mark.asyncio
async def test_existing_subaccount_keeps_only_owned_entered_numbers(plivo):
    plivo["owned"] = ["919990001000", "919990001001", "919990001002"]
    # Operator enters 2 owned (one formatted) + 1 typo; typo dropped, owned kept.
    cfg = await PlivoBulkProvisioningService.grant_with_existing_subaccount(
        _tr(), _FakeDB(),
        numbers=["+91 99900 01001", "91 99900 01002", "919999999999"],
        superadmin_id="sa",
    )
    assert plivo["rent_calls"] == 0
    assert cfg["numbers"] == ["919990001001", "919990001002"]


@pytest.mark.asyncio
async def test_existing_subaccount_errors_when_entered_none_owned(plivo):
    plivo["owned"] = ["919990001000"]
    cfg = await PlivoBulkProvisioningService.grant_with_existing_subaccount(
        _tr(), _FakeDB(), numbers=["919999999999"], superadmin_id="sa"
    )
    assert "none of those" in (cfg.get("error") or "").lower()


@pytest.mark.asyncio
async def test_existing_subaccount_errors_without_subaccount(plivo):
    cfg = await PlivoBulkProvisioningService.grant_with_existing_subaccount(
        _tr(sub_auth=None), _FakeDB(), superadmin_id="sa"
    )
    assert "provisioned" in (cfg.get("error") or "").lower()


@pytest.mark.asyncio
async def test_existing_subaccount_errors_when_nothing_to_enable(plivo):
    plivo["owned"] = []  # nothing rented yet, and no numbers entered
    cfg = await PlivoBulkProvisioningService.grant_with_existing_subaccount(
        _tr(), _FakeDB(), superadmin_id="sa"
    )
    assert "no dids" in (cfg.get("error") or "").lower()


@pytest.mark.asyncio
async def test_existing_subaccount_trusts_entry_when_listing_fails(plivo, monkeypatch):
    async def boom(auth):
        raise PlivoError("listing down")

    monkeypatch.setattr(PlivoService, "list_account_numbers", staticmethod(boom))
    cfg = await PlivoBulkProvisioningService.grant_with_existing_subaccount(
        _tr(), _FakeDB(), numbers=["+919990001234"], superadmin_id="sa"
    )
    assert cfg.get("error") is None
    assert cfg["numbers"] == ["919990001234"] and cfg["enabled"] is True
