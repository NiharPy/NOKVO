"""Scalable per-row campaign-contact path (V2) — the pure, DB-free units.

The DB-bound engine (COPY ingest, advisory-lock claim, O(1) webhooks, keyset
pagination, claim pool) is exercised against a live Postgres in the load harness;
here we lock the pure logic: the streaming CSV parser's canonicalization +
header-skip + dedupe shape, the lead-row mapping (result as dict OR json string),
and the bucket predicate table.
"""
from __future__ import annotations

import pytest

from app.services import campaign_contacts_v2 as v2
from app.services.outbound_campaign_service import _iter_parsed_contacts


# ── streaming parser: canonicalize + header-skip + name fallback ────────────
def test_iter_parsed_contacts_canonicalizes_and_skips_header():
    csv = (
        b"Phone,Name\n"
        b"7569672503,Alice\n"          # bare 10-digit -> 91 prefix
        b"+91 98765 43210,Bob\n"       # formatted -> stripped + kept
        b"09000000001,Carol\n"         # leading-0 trunk -> 91
        b"917569672503,\n"             # duplicate of Alice (canonical) + no name
        b",skipme\n"                   # no phone -> dropped
    )
    out = list(_iter_parsed_contacts("list.csv", csv))
    phones = [c["phone"] for c in out]
    assert phones == ["917569672503", "919876543210", "919000000001", "917569672503"]
    # header row never becomes a contact
    assert all(c["phone"].startswith("91") for c in out)
    # name falls back to the phone when the cell is blank
    assert out[3]["name"] == "917569672503"
    assert out[0]["name"] == "Alice"


def test_iter_parsed_contacts_no_header_row():
    # First row is already a number → not treated as a header (not dropped).
    csv = b"9000000001,First\n9000000002,Second\n"
    out = list(_iter_parsed_contacts("x.csv", csv))
    assert [c["phone"] for c in out] == ["919000000001", "919000000002"]


# ── lead-row mapping (result as dict OR json string) ────────────────────────
def test_lead_row_from_rec_result_as_dict():
    rec = {
        "campaign_id": "c1", "campaign_name": "Diwali", "call_link_id": "abc",
        "phone": "919000000001", "name": "Zoya", "lead_score": 250,
        "agent_config": {"questionnaire": {"questions": [{}, {}]}, "max_score": 300},
        "result": {"call_note": "hot", "score_breakdown": [{"awarded": True}],
                   "lead_score_reason": "Scored 250/300"},
        "claimed_at": None,
    }
    row = v2._lead_row_from_rec(rec)
    assert row["name"] == "Zoya" and row["phone"] == "919000000001"
    assert row["lead_score"] == 250 and row["max_score"] == 300
    assert row["call_note"] == "hot" and row["score_breakdown"] == [{"awarded": True}]
    assert row["lead_score_reason"] == "Scored 250/300"


def test_lead_row_from_rec_result_as_json_string():
    # asyncpg may hand back JSONB as a string — the mapper must parse it.
    rec = {
        "campaign_id": "c1", "campaign_name": "Diwali", "call_link_id": "abc",
        "phone": "919000000001", "name": None, "lead_score": None,
        "agent_config": {}, "result": '{"call_note": "note", "claim_status": "won"}',
        "claimed_at": None,
    }
    row = v2._lead_row_from_rec(rec)
    assert row["name"] == "919000000001"   # name falls back to phone
    assert row["call_note"] == "note" and row["claim_status"] == "won"
    assert row["max_score"] is None        # no questionnaire


# ── bucket predicates ────────────────────────────────────────────────────────
def test_bucket_sql_covers_the_ui_buckets():
    for b in ("qualified", "not_interested", "no_pickup", "pending"):
        assert b in v2.BUCKET_SQL and v2.BUCKET_SQL[b]
    assert "qualified = true" in v2.BUCKET_SQL["qualified"]
    assert "dnd_dropped" in v2.BUCKET_SQL["no_pickup"]


# ── legacy blob → V2 migration (unblocks add-contacts on exhausted campaigns) ─

def test_blob_row_status_mapping():
    from app.services.campaign_contacts_v2 import _blob_row_status

    # Answered people are never re-dialed.
    assert _blob_row_status({"status": "answered", "ended": True}) == "completed"
    assert _blob_row_status({"status": "answered"}) == "answered"  # live call keeps resolving
    # Terminal states carry over.
    for s in ("completed", "no_answer", "failed", "dnd_dropped", "pending"):
        assert _blob_row_status({"status": s}) == s
    # In-flight placements stay live (reaper resolves strays); ended ones are misses.
    assert _blob_row_status({"status": "calling"}) == "ringing"
    assert _blob_row_status({"status": "calling", "ended": True}) == "no_answer"
    # Unknown status: dialed → miss, never-dialed → pending.
    assert _blob_row_status({"status": "weird", "call_id": "C1"}) == "no_answer"
    assert _blob_row_status({"status": "weird"}) == "pending"


class _MigrateDB:
    def __init__(self, total_after=3):
        self.inserts = []
        self.committed = False
        self._total = total_after
        self.added = []

    async def execute(self, stmt, params=None):
        s = str(stmt)

        class _R:
            def __init__(self, rowcount=0, scalar=None):
                self.rowcount = rowcount
                self._scalar = scalar

            def scalar_one(self):
                return self._scalar

        if "INSERT INTO outbound_campaign_contacts" in s:
            self.inserts.append(params)
            return _R(rowcount=1)
        if "count(*)" in s:
            return _R(scalar=self._total)
        return _R()

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_migrate_blob_campaign_preserves_identity_and_claims():
    import uuid as _uuid

    from app.models.outbound_campaign import CampaignStatus, OutboundCampaign
    from app.services.campaign_contacts_v2 import migrate_blob_campaign

    claimer = str(_uuid.uuid4())
    camp = OutboundCampaign(
        id=_uuid.uuid4(), tenant_id="t1", name="legacy", status=CampaignStatus.completed,
        total_count=2,
        contacts=[
            {"phone": "7569672503", "name": "Asha", "status": "answered", "ended": True,
             "call_id": "C1", "call_link_id": "L1", "lead_score": 260, "qualified": True,
             "claimed_by": claimer, "claim_status": "contacted", "call_note": "hot lead"},
            {"phone": "+91 98765 43210", "name": "Ravi", "status": "pending", "call_link_id": "L2"},
        ],
    )
    db = _MigrateDB(total_after=2)
    inserted = await migrate_blob_campaign(db, camp)

    assert inserted == 2
    by_link = {p["clid"]: p for p in db.inserts}
    a = by_link["L1"]
    assert a["phone"] == "917569672503"          # canonicalized for the dedupe key
    assert a["status"] == "completed"            # answered+ended → never re-dialed
    assert a["qualified"] is True and a["lead_score"] == 260
    assert a["claimed_by"] == claimer            # claim survives into the V2 pool
    assert "hot lead" in a["result"]             # extras parked in result JSONB
    b = by_link["L2"]
    assert b["phone"] == "919876543210" and b["status"] == "pending"
    # One-way: blob gone, campaign reads as V2 from here on.
    assert camp.contacts is None
    assert camp.total_count == 2
    assert db.committed


@pytest.mark.asyncio
async def test_migrate_empty_blob_just_nulls():
    import uuid as _uuid

    from app.models.outbound_campaign import CampaignStatus, OutboundCampaign
    from app.services.campaign_contacts_v2 import migrate_blob_campaign

    camp = OutboundCampaign(
        id=_uuid.uuid4(), tenant_id="t1", name="empty-blob", status=CampaignStatus.completed,
        contacts=[],
    )
    db = _MigrateDB(total_after=5)  # rows already exist in the V2 store
    inserted = await migrate_blob_campaign(db, camp)
    assert inserted == 0
    assert camp.contacts is None
    assert camp.total_count == 5
    assert db.committed


def test_dialed_bucket_selects_placed_contacts():
    """Call Logs reads the 'dialed' bucket — every contact a call was PLACED for.
    Without it the APEX Call Logs tab (which used to read the empty V2 blob)
    showed zero calls forever."""
    assert v2.BUCKET_SQL["dialed"] == "call_id IS NOT NULL"


@pytest.mark.asyncio
async def test_page_contacts_returns_call_identity_columns():
    """The page SELECT must carry call_link_id (the transcript/ledger join key)
    and call_id/answered_at/duration_s for the Call Logs rows."""
    captured = {}

    class _R:
        def mappings(self):
            return self

        def all(self):
            return []

    class _DB:
        async def execute(self, stmt, params=None):
            captured["sql"] = str(stmt)
            return _R()

    import uuid as _uuid

    await v2.page_contacts(_DB(), _uuid.uuid4(), "dialed", limit=10)
    for col in ("call_link_id", "call_id", "answered_at", "duration_s"):
        assert col in captured["sql"], captured["sql"]
    assert "call_id IS NOT NULL" in captured["sql"]


# ── guarded transitions (stamp_answered / finalize_terminal) ──────────────────
# One-statement guarded UPDATEs: only the call that actually flips the row gets
# a non-None return, so counters bump exactly once under concurrent webhooks.


class _GuardDB:
    """Captures the statement + params; returns a configured .first() row."""

    def __init__(self, row=None):
        self.row = row
        self.sql = ""
        self.params = None

    async def execute(self, stmt, params=None):
        self.sql = str(stmt)
        self.params = params

        class _R:
            def __init__(self, row):
                self._row = row

            def first(self):
                return self._row

        return _R(self.row)


@pytest.mark.asyncio
async def test_stamp_answered_guarded_and_idempotent():
    import uuid as _uuid
    from datetime import datetime, timezone

    camp_id = _uuid.uuid4()
    db = _GuardDB(row=(camp_id,))
    got = await v2.stamp_answered_by_link(db, "L1", datetime.now(timezone.utc))
    assert got == camp_id
    # Guard: only a live pre-answer row transitions; answered_at never overwritten.
    assert "status IN ('dialing', 'ringing')" in db.sql
    assert "COALESCE(answered_at" in db.sql
    # Late/duplicate event → no row matched → None (no counter bump).
    assert await v2.stamp_answered_by_link(_GuardDB(row=None), "L1", datetime.now(timezone.utc)) is None


@pytest.mark.asyncio
async def test_finalize_terminal_completes_answered_and_reports_old_state():
    import uuid as _uuid

    camp_id = _uuid.uuid4()
    # Row was 'answered' → the CASE lands it on 'completed' (returned status).
    db = _GuardDB(row=("completed", camp_id))
    got = await v2.finalize_terminal_by_link(db, "L1", "no_answer", duration_s=12.0)
    assert got == ("completed", camp_id)
    assert "CASE WHEN status = 'answered' THEN 'completed'" in db.sql
    assert "status IN ('dialing', 'ringing', 'answered')" in db.sql  # terminal rows excluded
    # Never-answered row → lands on the hangup-cause status.
    db2 = _GuardDB(row=("no_answer", camp_id))
    assert (await v2.finalize_terminal_by_link(db2, "L2", "no_answer")) == ("no_answer", camp_id)
    # Already terminal (duplicate webhook) → None, caller skips counters.
    assert await v2.finalize_terminal_by_link(_GuardDB(row=None), "L3", "failed") is None


# ── claim_pending: status guard + daily-cap budget inside the locked txn ──────


class _ClaimSession:
    """Fake AsyncSession for claim_pending: dispatches on statement text."""

    def __init__(
        self,
        *,
        status="running",
        date=None,
        count=None,
        live=0,
        in_conversation=0,
        pending_rows=(),
    ):
        self._status = status
        self._date = date
        self._count = count
        # The claim reads two live populations separately: rows holding a
        # CONVERSATION slot (answered) and rows still RINGING. ``live`` keeps its
        # original meaning — calls already placed and not yet connected.
        self._ringing = live
        self._in_conversation = in_conversation
        self._pending = list(pending_rows)
        self.bumped = None  # (today, n) when _bump_dialed_today ran

    def begin(self):
        session = self

        class _Tx:
            async def __aenter__(self):
                return session

            async def __aexit__(self, *exc):
                return False

        return _Tx()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, params=None):
        s = str(stmt)
        outer = self

        class _R:
            def __init__(self, first=None, scalar=None, rows=()):
                self._first = first
                self._scalar = scalar
                self._rows = rows

            def first(self):
                return self._first

            def scalar_one(self):
                return self._scalar

            def mappings(self):
                return self

            def all(self):
                return self._rows

        if "pg_advisory_xact_lock" in s:
            return _R()
        if "SELECT status" in s:
            return _R(first=(outer._status, outer._date, outer._count))
        if "in_conversation" in s:
            return _R(first=(outer._in_conversation, outer._ringing))
        if "count(*)" in s:
            return _R(scalar=outer._ringing)
        if "SET status='dialing'" in s:
            k = params["k"]
            return _R(rows=outer._pending[:k])
        if "dialed_today" in s and "jsonb_set" in s:
            outer.bumped = (params["today"], params["n"])
            return _R()
        raise AssertionError(f"unexpected stmt: {s[:80]}")


def _patch_claim_session(monkeypatch, session):
    class _Factory:
        def __call__(self):
            return session

    monkeypatch.setattr(v2, "AsyncSessionLocal", _Factory())


@pytest.mark.asyncio
async def test_claim_pending_returns_nothing_for_non_running(monkeypatch):
    """Cancel-vs-refill race: a cancel that committed after the caller's stale
    snapshot wins — the claim sees status!=running and claims nothing."""
    import uuid as _uuid

    session = _ClaimSession(status="cancelled", pending_rows=[{"id": 1, "phone": "919", "call_link_id": "a"}])
    _patch_claim_session(monkeypatch, session)
    assert await v2.claim_pending(_uuid.uuid4(), 5) == []


@pytest.mark.asyncio
async def test_claim_pending_daily_cap_budget_and_upfront_charge(monkeypatch):
    """The cap budget is computed from the FRESH in-txn counter (not the caller's
    snapshot) and the claim charges dialed_today up front — concurrent refills
    can never overshoot calls_per_day."""
    import uuid as _uuid

    rows = [{"id": i, "phone": f"91{i}", "call_link_id": f"L{i}"} for i in range(5)]
    session = _ClaimSession(status="running", date="2026-07-08", count="198", live=0, pending_rows=rows)
    _patch_claim_session(monkeypatch, session)
    claimed = await v2.claim_pending(_uuid.uuid4(), 5, daily_cap=200, today="2026-07-08")
    assert len(claimed) == 2                       # 200 − 198 remaining, not the cap of 5
    assert session.bumped == ("2026-07-08", 2)     # charged inside the locked txn


@pytest.mark.asyncio
async def test_claim_pending_cap_exhausted_claims_nothing(monkeypatch):
    import uuid as _uuid

    session = _ClaimSession(status="running", date="2026-07-08", count="200",
                            pending_rows=[{"id": 1, "phone": "919", "call_link_id": "a"}])
    _patch_claim_session(monkeypatch, session)
    assert await v2.claim_pending(_uuid.uuid4(), 5, daily_cap=200, today="2026-07-08") == []
    assert session.bumped is None


@pytest.mark.asyncio
async def test_claim_pending_date_rollover_resets_budget(monkeypatch):
    """Yesterday's counter doesn't constrain today (junk counters tolerated too)."""
    import uuid as _uuid

    rows = [{"id": i, "phone": f"91{i}", "call_link_id": f"L{i}"} for i in range(5)]
    session = _ClaimSession(status="running", date="2026-07-07", count="200", pending_rows=rows)
    _patch_claim_session(monkeypatch, session)
    claimed = await v2.claim_pending(_uuid.uuid4(), 3, daily_cap=200, today="2026-07-08")
    assert len(claimed) == 3
    assert session.bumped == ("2026-07-08", 3)


# ── weighted max_score in the member pool row ─────────────────────────────────


def test_lead_row_from_rec_weighted_max_score_without_stored_max():
    """No stored max_score → the weighted helper, never len(questions) (which
    showed impossible '7/4' scores on weighted questionnaires)."""
    rec = {
        "campaign_id": "c1", "campaign_name": "W", "call_link_id": "abc",
        "phone": "919000000001", "name": "Zoya", "lead_score": 7,
        "agent_config": {"questionnaire": {"questions": [
            {"points": 5}, {"tiers": [{"points": 1}, {"points": 3}]}, {},
        ]}},
        "result": {},
        "claimed_at": None,
    }
    assert v2._lead_row_from_rec(rec)["max_score"] == 9  # 5 + 3 + 1


# ── conversation slots vs ring-ahead ─────────────────────────────────────────
# The claim bounds two DIFFERENT live populations. Only `answered` rows hold a
# conversation slot; `dialing`/`ringing` rows are calls placed in the hope they
# connect. Conflating them behind one hardcoded 5 is what let the dialer place
# five calls on a plan that sold one conversation — four of every five people who
# picked up were hung up on the moment they said hello.


@pytest.mark.asyncio
async def test_claim_stops_while_a_conversation_holds_the_only_slot(monkeypatch):
    """Core (concurrency 1) with a live conversation claims NOTHING."""
    import uuid as _uuid

    session = _ClaimSession(
        status="running", in_conversation=1, live=0,
        pending_rows=[{"id": 1, "phone": "919", "call_link_id": "a"}],
    )
    _patch_claim_session(monkeypatch, session)
    assert await v2.claim_pending(_uuid.uuid4(), 1) == []


@pytest.mark.asyncio
async def test_claim_rings_one_line_per_free_slot_by_default(monkeypatch):
    """Default multiplier 1.0 → never place a call that couldn't be answered."""
    import uuid as _uuid

    rows = [{"id": i, "phone": f"91{i}", "call_link_id": f"L{i}"} for i in range(5)]
    session = _ClaimSession(status="running", in_conversation=1, live=0, pending_rows=rows)
    _patch_claim_session(monkeypatch, session)
    # Growth: 2 slots, 1 busy → exactly 1 line may ring.
    assert len(await v2.claim_pending(_uuid.uuid4(), 2)) == 1


@pytest.mark.asyncio
async def test_claim_ring_ahead_multiplies_free_slots(monkeypatch):
    """The pacer rings 1/answer_rate lines per free slot so the EXPECTED number
    of simultaneous answers is the plan concurrency."""
    import uuid as _uuid

    rows = [{"id": i, "phone": f"91{i}", "call_link_id": f"L{i}"} for i in range(10)]
    session = _ClaimSession(status="running", in_conversation=0, live=0, pending_rows=rows)
    _patch_claim_session(monkeypatch, session)
    # 1 free slot at a 25% answer rate → 4 lines ringing.
    assert len(await v2.claim_pending(_uuid.uuid4(), 1, ring_multiplier=4.0)) == 4


@pytest.mark.asyncio
async def test_claim_ring_ahead_respects_ceiling_and_lines_already_ringing(monkeypatch):
    import uuid as _uuid

    rows = [{"id": i, "phone": f"91{i}", "call_link_id": f"L{i}"} for i in range(10)]
    session = _ClaimSession(status="running", in_conversation=0, live=2, pending_rows=rows)
    _patch_claim_session(monkeypatch, session)
    # multiplier wants 8, ceiling caps at 3, two are already ringing → 1 more.
    assert len(await v2.claim_pending(
        _uuid.uuid4(), 1, ring_multiplier=8.0, max_ring_ahead=3
    )) == 1


@pytest.mark.asyncio
async def test_claim_never_exceeds_the_plan_even_with_a_huge_multiplier(monkeypatch):
    """A conversation cap of 0 free slots claims nothing no matter what the pacer
    asks for — the sold concurrency is a hard ceiling, not a hint."""
    import uuid as _uuid

    rows = [{"id": i, "phone": f"91{i}", "call_link_id": f"L{i}"} for i in range(10)]
    session = _ClaimSession(status="running", in_conversation=2, live=0, pending_rows=rows)
    _patch_claim_session(monkeypatch, session)
    assert await v2.claim_pending(
        _uuid.uuid4(), 2, ring_multiplier=99.0, max_ring_ahead=50
    ) == []
