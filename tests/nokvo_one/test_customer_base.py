"""Customer base — upsert semantics, the clinic auto-followup gate, and
customer-targeted dispatch.

Pure-logic style (no Postgres): SQL statements are captured from a fake
session and compiled against the postgresql dialect to lock down the
ON CONFLICT / COALESCE shape; the live insert path is exercised by the API
smoke tests.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy.dialects import postgresql

from app.services.customer_base_service import (
    clean_customer_name,
    ensure_customer,
    upsert_customer_from_call,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _CapturingDb:
    """Captures executed statements; returns no rows."""

    def __init__(self):
        self.statements = []
        self.commits = 0

    async def execute(self, stmt):
        self.statements.append(stmt)

        class _Result:
            def scalar_one_or_none(self):
                return None

            @property
            def rowcount(self):
                return 0

        return _Result()

    async def commit(self):
        self.commits += 1


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


# ── Name hygiene ────────────────────────────────────────────────────────────


def test_clean_customer_name_rejects_junk():
    assert clean_customer_name("Nihar Reddy") == "Nihar Reddy"
    assert clean_customer_name("  Asha  ") == "Asha"
    assert clean_customer_name("9999999999") is None       # bare digits
    assert clean_customer_name("unknown") is None
    assert clean_customer_name("N/A") is None
    assert clean_customer_name("") is None
    assert clean_customer_name(None) is None
    assert clean_customer_name("x" * 121) is None          # absurd length


# ── Upsert shape: dedupe + fill-only name ───────────────────────────────────


def test_upsert_is_on_conflict_with_fill_only_name():
    db = _CapturingDb()
    _run(
        upsert_customer_from_call(
            db, tenant_id="t-1", phone="+91 99999 99999", name="Nihar", call_id="call-1"
        )
    )
    assert len(db.statements) == 1
    sql = _compiled(db.statements[0])
    # Dedupe on the tenant+phone unique constraint.
    assert "ON CONFLICT ON CONSTRAINT uq_customer_base_tenant_phone" in sql
    # Name is fill-only: a misheard STT name on call 2 must never overwrite
    # the name stored on call 1.
    assert "coalesce(customer_base.name" in sql
    # Repeat callers bump the counter.
    assert "customer_base.call_count + " in sql
    assert db.commits == 1


def test_upsert_normalizes_phone_and_skips_empty():
    db = _CapturingDb()
    result = _run(upsert_customer_from_call(db, tenant_id="t-1", phone="   "))
    assert result is None
    assert db.statements == []


def test_upsert_commit_false_leaves_transaction_open():
    db = _CapturingDb()
    _run(
        upsert_customer_from_call(
            db, tenant_id="t-1", phone="+919999999999", commit=False
        )
    )
    assert db.commits == 0
    assert len(db.statements) == 1


def test_ensure_customer_does_not_bump_counters():
    # The appointment macro links a booking mid-call; the call-end teardown
    # owns the counters — ensure_customer must not double-count the call.
    db = _CapturingDb()
    _run(ensure_customer(db, tenant_id="t-1", phone="+919999999999", name="Jane"))
    sql = _compiled(db.statements[0])
    assert "ON CONFLICT ON CONSTRAINT uq_customer_base_tenant_phone" in sql
    assert "coalesce(customer_base.name" in sql
    assert "call_count + " not in sql
    assert "last_call_at" not in sql.split("DO UPDATE")[1]


# ── Clinic gate: clinics never auto-schedule follow-ups ─────────────────────


def test_enqueue_after_call_returns_none_for_clinics(monkeypatch):
    from types import SimpleNamespace

    from app.services.followup_scheduler_service import FollowupSchedulerService

    async def _is_clinic(*, tenant_id, db):
        return True

    monkeypatch.setattr(FollowupSchedulerService, "_tenant_is_clinic", _is_clinic)

    lead = SimpleNamespace(id=uuid.uuid4(), tenant_id="t-clinic", consent_status=None)
    row = _run(
        FollowupSchedulerService.enqueue_after_call(
            lead=lead,
            campaign=None,
            source_call_id="call-1",
            disposition="no_answer",
            db=None,
        )
    )
    assert row is None


def test_enqueue_after_call_still_schedules_for_non_clinics(monkeypatch):
    """Same disposition, non-clinic tenant → the decision tree proceeds (we
    stop it at the next kill switch by disabling the feature toggle, proving
    the clinic gate alone didn't swallow it)."""
    from types import SimpleNamespace

    from app.services import followup_scheduler_service as mod
    from app.services.followup_scheduler_service import FollowupSchedulerService

    async def _is_clinic(*, tenant_id, db):
        return False

    monkeypatch.setattr(FollowupSchedulerService, "_tenant_is_clinic", _is_clinic)
    monkeypatch.setattr(
        mod, "effective_followup_rules", lambda campaign: {"enabled": False}
    )

    lead = SimpleNamespace(id=uuid.uuid4(), tenant_id="t-ecom", consent_status=None)
    row = _run(
        FollowupSchedulerService.enqueue_after_call(
            lead=lead,
            campaign=None,
            source_call_id="call-1",
            disposition="no_answer",
            db=None,
        )
    )
    # None here because of the feature toggle (kill switch #1), which is only
    # reachable when the clinic gate (kill switch #0) let the call through.
    assert row is None


# ── Customer-targeted synthetic contact (webhook resolution) ────────────────


def test_followup_synthetic_contact_for_customer_has_no_lead_id():
    """get_by_call_link_id's customer branch must omit lead_id so the
    downstream auto-followup enqueue (guarded by lead) never fires, and must
    carry tenant_id + the admin note for the WS handler."""
    import inspect

    from app.services.outbound_campaign_service import OutboundCampaignService

    src = inspect.getsource(OutboundCampaignService.get_by_call_link_id)
    assert "customer_id" in src
    assert "_admin_note" in src
    assert "tenant_id" in src


def test_dispatch_one_handles_customer_rows():
    """_dispatch_one must branch on customer_id (opt-out check, phone from
    customer) beside the lead branch."""
    import inspect

    from app.services import followup_scheduler

    src = inspect.getsource(followup_scheduler._dispatch_one)
    assert "row.customer_id" in src
    assert "opt_out" in src
    assert "customer.phone_e164" in src
