"""One campaign per tenant at a time.

Locks in the two halves of the rule:
  * ``_assert_no_other_active_campaign`` — every launch/rerun/append path 4xxes
    while ANOTHER campaign of the tenant is running/ingesting (relaunching the
    same campaign is allowed via ``exclude_id``);
  * ``campaign_contacts_v2.maybe_complete`` — a drained V2 campaign flips
    ``running`` → ``completed`` so the slot actually frees up (V2 previously
    stayed ``running`` forever).
"""
import pytest

from app.services.outbound_campaign_service import OutboundCampaignService
from app.services import campaign_contacts_v2 as v2


class _Result:
    def __init__(self, scalar=None, rowcount=0):
        self._scalar = scalar
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        return self._scalar


class _GuardDB:
    """Answers the guard's two statements: the advisory lock, then the
    active-campaign name lookup."""

    def __init__(self, active_name=None):
        self.active_name = active_name
        self.lock_keys = []

    async def execute(self, stmt, params=None):
        if params and "campaign-slot" in str((params or {}).get("k", "")):
            self.lock_keys.append(params["k"])
            return _Result()
        return _Result(scalar=self.active_name)


@pytest.mark.asyncio
async def test_guard_allows_when_no_active_campaign():
    db = _GuardDB(active_name=None)
    await OutboundCampaignService._assert_no_other_active_campaign(db, "t1")
    # The tenant-scoped advisory lock was taken (that's what serializes racing launches).
    assert db.lock_keys == ["campaign-slot:t1"]


@pytest.mark.asyncio
async def test_guard_blocks_when_another_campaign_is_active():
    db = _GuardDB(active_name="June blast")
    with pytest.raises(ValueError, match="June blast.*one campaign"):
        await OutboundCampaignService._assert_no_other_active_campaign(db, "t1")


class _CompleteDB:
    """Answers maybe_complete's two statements: the remaining count, then the
    guarded running→completed UPDATE."""

    def __init__(self, remaining, update_rowcount=1):
        self.remaining = remaining
        self.update_rowcount = update_rowcount
        self.updated = False
        self.committed = False

    async def execute(self, stmt, params=None):
        if "count(*)" in str(stmt):
            return _Result(scalar=self.remaining)
        self.updated = True
        return _Result(rowcount=self.update_rowcount)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_maybe_complete_noop_while_work_remains():
    db = _CompleteDB(remaining=3)
    assert await v2.maybe_complete(db, "c1") is False
    assert db.updated is False  # never touches the campaign row


@pytest.mark.asyncio
async def test_maybe_complete_flips_drained_running_campaign():
    db = _CompleteDB(remaining=0, update_rowcount=1)
    assert await v2.maybe_complete(db, "c1") is True
    assert db.updated and db.committed


@pytest.mark.asyncio
async def test_maybe_complete_leaves_non_running_statuses_alone():
    # Drained but the guarded UPDATE matched nothing (e.g. cancelled) → False.
    db = _CompleteDB(remaining=0, update_rowcount=0)
    assert await v2.maybe_complete(db, "c1") is False


# ── cancel frees the slot ─────────────────────────────────────────────────────

class _CancelDB:
    def __init__(self):
        self.committed = False

    def add(self, obj):
        pass

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("status_name", ["draft", "running", "ingesting"])
async def test_cancel_allowed_for_active_statuses(status_name):
    from app.models.outbound_campaign import CampaignStatus, OutboundCampaign

    camp = OutboundCampaign(tenant_id="t1", name="c", status=getattr(CampaignStatus, status_name))
    db = _CancelDB()
    out = await OutboundCampaignService.cancel_campaign(camp, db)
    assert out.status == CampaignStatus.cancelled
    assert out.completed_at is not None
    assert db.committed


@pytest.mark.asyncio
@pytest.mark.parametrize("status_name", ["completed", "cancelled", "failed", "ingest_failed"])
async def test_cancel_refused_for_terminal_statuses(status_name):
    from app.models.outbound_campaign import CampaignStatus, OutboundCampaign

    camp = OutboundCampaign(tenant_id="t1", name="c", status=getattr(CampaignStatus, status_name))
    with pytest.raises(ValueError, match="Cannot cancel"):
        await OutboundCampaignService.cancel_campaign(camp, _CancelDB())


# ── stuck-row reaper (guarantees exhausted campaigns actually complete) ──────

class _ReapDB:
    """Returns preset campaign-id rows for the two reap UPDATEs, in order."""

    def __init__(self, placing_ids, answered_ids):
        self._results = [placing_ids, answered_ids]
        self.sql = []
        self.committed = False

    async def execute(self, stmt, params=None):
        self.sql.append(str(stmt))
        ids = self._results.pop(0) if self._results else []

        class _R:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return [(i,) for i in self._rows]

        return _R(ids)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_reap_returns_distinct_touched_campaigns():
    db = _ReapDB(placing_ids=["c1", "c2", "c1"], answered_ids=["c2", "c3"])
    touched = await v2.reap_stale_rows(db)
    assert sorted(touched) == ["c1", "c2", "c3"]
    assert db.committed
    # dialing/ringing → no_answer (re-armable); answered → completed (never re-dialed).
    assert "IN ('dialing', 'ringing')" in db.sql[0] and "'no_answer'" in db.sql[0]
    assert "= 'answered'" in db.sql[1] and "'completed'" in db.sql[1]
    # Only RUNNING campaigns are swept — terminal/cancelled rows stay untouched.
    assert all("c.status = 'running'" in s for s in db.sql)


@pytest.mark.asyncio
async def test_reap_noop_when_nothing_stale():
    db = _ReapDB(placing_ids=[], answered_ids=[])
    assert await v2.reap_stale_rows(db) == []
    assert db.committed


# ── delete orphans follow-up schedules (the FK that 500'd deletes) ───────────

class _DeleteDB:
    def __init__(self):
        self.sql = []
        self.deleted = []
        self.committed = False

    async def execute(self, stmt, params=None):
        self.sql.append(str(stmt))

        class _R:
            def scalars(self):
                return self

            def all(self):
                return []  # no contact rows in this unit

        return _R()

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_delete_campaign_nulls_followup_schedule_fk():
    """lead_followup_schedules.campaign_id has a FK with no CASCADE; deleting a
    campaign that had follow-up rows raised IntegrityError → 500. The delete
    must orphan those rows (NULL the fk) before removing the campaign."""
    from app.models.outbound_campaign import CampaignStatus, OutboundCampaign

    camp = OutboundCampaign(tenant_id="t1", name="del-me", status=CampaignStatus.completed)
    db = _DeleteDB()
    await OutboundCampaignService.delete_campaign(camp, db)
    # Contacts must go via ONE bulk raw DELETE issued BEFORE the campaign
    # delete — the old ORM loop had no relationship() dependency edge, so the
    # flush ordered the campaign DELETE first and hit the contacts FK in prod.
    assert any("DELETE FROM outbound_campaign_contacts" in s for s in db.sql), db.sql
    assert any(
        "lead_followup_schedules" in s and "campaign_id = NULL" in s for s in db.sql
    ), db.sql
    assert db.deleted == [camp] and db.committed


@pytest.mark.asyncio
async def test_delete_campaign_still_refuses_running():
    from app.models.outbound_campaign import CampaignStatus, OutboundCampaign

    camp = OutboundCampaign(tenant_id="t1", name="live", status=CampaignStatus.running)
    with pytest.raises(ValueError, match="Cancel the campaign"):
        await OutboundCampaignService.delete_campaign(camp, _DeleteDB())
