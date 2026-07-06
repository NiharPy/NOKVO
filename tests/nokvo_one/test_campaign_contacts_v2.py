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
