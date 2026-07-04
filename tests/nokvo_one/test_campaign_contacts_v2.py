"""Scalable per-row campaign-contact path (V2) — the pure, DB-free units.

The DB-bound engine (COPY ingest, advisory-lock claim, O(1) webhooks, keyset
pagination, claim pool) is exercised against a live Postgres in the load harness;
here we lock the pure logic: the streaming CSV parser's canonicalization +
header-skip + dedupe shape, the lead-row mapping (result as dict OR json string),
and the bucket predicate table.
"""
from __future__ import annotations

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
