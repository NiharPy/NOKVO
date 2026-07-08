"""Prepaid RUPEE balance + the call-start gate + FIFO bundle selection.

``has_balance`` is the gate inbound (plivo_inbound_webhook) and non-deterministic
outbound (_require_outbound_enabled + dialer) check. The DB sums are monkeypatched
so this is fast pure-logic over the GRANDFATHER rule (never-purchased org isn't
gated) and the rupee math.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import app.services.minute_balance_service as mb


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _patch(monkeypatch, purchased, consumed):
    async def _p(db, org):
        return Decimal(str(purchased))

    async def _c(db, org):
        return Decimal(str(consumed))

    monkeypatch.setattr(mb, "purchased_rupees", _p)
    monkeypatch.setattr(mb, "consumed_rupees", _c)


def test_legacy_org_never_purchased_is_not_gated(monkeypatch):
    _patch(monkeypatch, purchased=0, consumed=0)
    assert _run(mb.has_balance(None, "org")) is True
    _patch(monkeypatch, purchased=0, consumed=500)  # consumption w/o purchase → still open
    assert _run(mb.has_balance(None, "org")) is True


def test_prepaid_org_gated_at_zero(monkeypatch):
    _patch(monkeypatch, purchased=54000, consumed=53999)
    assert _run(mb.has_balance(None, "org")) is True       # ₹1 left
    _patch(monkeypatch, purchased=54000, consumed=54000)
    assert _run(mb.has_balance(None, "org")) is False      # exactly out
    _patch(monkeypatch, purchased=54000, consumed=54100)
    assert _run(mb.has_balance(None, "org")) is False      # overran


def test_apex_no_grandfather_blocks_at_zero(monkeypatch):
    # APEX passes grandfather=False — a prepaid-only product with no legacy orgs, so
    # a never-purchased / comp-lost / bypassed org (purchased<=0) MUST be blocked,
    # never fail open to free unmetered dialing.
    _patch(monkeypatch, purchased=0, consumed=0)
    assert _run(mb.has_balance(None, "org", grandfather=False)) is False
    _patch(monkeypatch, purchased=0, consumed=500)
    assert _run(mb.has_balance(None, "org", grandfather=False)) is False
    # A real balance still allows; a drained one still blocks.
    _patch(monkeypatch, purchased=54000, consumed=10000)
    assert _run(mb.has_balance(None, "org", grandfather=False)) is True
    _patch(monkeypatch, purchased=54000, consumed=54000)
    assert _run(mb.has_balance(None, "org", grandfather=False)) is False
    # The default (Nokvo One) keeps the grandfather — never-purchased is allowed.
    _patch(monkeypatch, purchased=0, consumed=0)
    assert _run(mb.has_balance(None, "org")) is True


def test_balance_summary(monkeypatch):
    _patch(monkeypatch, purchased=54000, consumed=13200)
    s = _run(mb.balance_summary(None, "org"))
    assert s == {
        "purchased_rupees": 54000.0,
        "consumed_rupees": 13200.0,
        "remaining_rupees": 40800.0,
    }
    assert _run(mb.balance_rupees(None, "org")) == Decimal("40800")


def test_current_bundle_minutes_fifo(monkeypatch):
    # Two bundles: 6000 min (₹54,000 @ ₹9) bought first, then 2000 min
    # (₹19,000 @ ₹9.5). The rate follows whichever bundle the next rupee is in.
    rows = [(6000, Decimal("54000")), (2000, Decimal("19000"))]

    async def _rows_exec(stmt):
        class _R:
            def all(self_inner):
                return rows
        return _R()

    # current_bundle_minutes calls consumed_rupees then db.execute(select...).all()
    class _DB:
        async def execute(self, stmt):
            return await _rows_exec(stmt)

    async def _consumed(db, org):
        return _consumed.value
    monkeypatch.setattr(mb, "consumed_rupees", _consumed)

    _consumed.value = Decimal("0")          # nothing spent → first (6000) bundle
    assert _run(mb.current_bundle_minutes(_DB(), "org")) == 6000
    _consumed.value = Decimal("53999")      # still inside the first bundle
    assert _run(mb.current_bundle_minutes(_DB(), "org")) == 6000
    _consumed.value = Decimal("54000")      # first bundle exactly drained → second (2000)
    assert _run(mb.current_bundle_minutes(_DB(), "org")) == 2000
    _consumed.value = Decimal("60000")      # into the second bundle
    assert _run(mb.current_bundle_minutes(_DB(), "org")) == 2000
    _consumed.value = Decimal("999999")     # everything spent → last bundle's size
    assert _run(mb.current_bundle_minutes(_DB(), "org")) == 2000


def test_current_bundle_minutes_no_purchases(monkeypatch):
    class _DB:
        async def execute(self, stmt):
            class _R:
                def all(self_inner):
                    return []
            return _R()

    async def _consumed(db, org):
        return Decimal("0")
    monkeypatch.setattr(mb, "consumed_rupees", _consumed)
    # legacy/no-purchase → safe default 1000 (the ₹10 entry rate)
    assert _run(mb.current_bundle_minutes(_DB(), "org")) == 1000


# ── APEX tester calls never deplete the wallet ────────────────────────────────


def test_apex_tester_call_never_deducts(monkeypatch):
    """record_call_cost deducts for EVERY connected APEX call — except browser
    tester sessions (kind="tester"): the stream service deliberately passes
    deducts_prepaid=False for those and the tier override must not undo it."""
    import uuid
    from datetime import datetime, timedelta, timezone

    import app.services.call_cost_recorder as rec

    priced = {"apex": 0}

    async def _bundle(db, org):
        return 1000

    def _apex_cost(seconds, bundle_minutes):
        priced["apex"] += 1
        from decimal import Decimal

        return Decimal("3.5")

    monkeypatch.setattr(mb, "current_bundle_minutes", _bundle)
    import app.services.minute_pricing as mp

    monkeypatch.setattr(mp, "apex_call_cost", _apex_cost)

    async def _mtd(db, org, now):
        return 0

    monkeypatch.setattr(rec, "_month_to_date_billed_minutes", _mtd)

    class _DB:
        async def execute(self, stmt, params=None):
            class _R:
                def scalar_one_or_none(self):
                    return "nokvo_apex"  # the org-tier probe

                def first(self):
                    return None  # insert: ON CONFLICT path → early return

            return _R()

        async def commit(self):
            pass

        async def rollback(self):
            pass

    start = datetime.now(timezone.utc)

    # Tester session on an APEX org: the deduction pricing must never run.
    _run(rec.record_call_cost(
        _DB(), organization_id=uuid.uuid4(), tenant_id="t1", call_id="tester:1",
        started_at=start, ended_at=start + timedelta(seconds=60),
        kind="tester", deducts_prepaid=False,
    ))
    assert priced["apex"] == 0

    # A real outbound APEX call DOES price the deduction (tier override).
    _run(rec.record_call_cost(
        _DB(), organization_id=uuid.uuid4(), tenant_id="t1", call_id="call:1",
        started_at=start, ended_at=start + timedelta(seconds=60),
        kind="outbound", deducts_prepaid=False,
    ))
    assert priced["apex"] == 1
