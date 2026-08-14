"""Campaign diagnostics: shape, derived arithmetic, and fail-soft isolation.

The SQL itself is validated against a real Postgres (every statement, plus the
claim query's EXPLAIN plan) — see the P4 verification notes. What matters here is
the behaviour around it: the rates the endpoint derives, and the guarantee that
one slow or broken aggregate degrades its own block instead of blanking the page
an operator opened to diagnose a live campaign.
"""
from __future__ import annotations

import uuid

import pytest

from app.services import campaign_diagnostics as cd


class _Rows:
    """Fake session returning a canned row set for every execute()."""

    def __init__(self, rows=(), one=None):
        self._rows = list(rows)
        self._one = one

    async def execute(self, stmt, params=None):
        rows, one = self._rows, self._one

        class _R:
            def all(self):
                return rows

            def first(self):
                return one

        return _R()


@pytest.mark.asyncio
async def test_by_hour_derives_answer_rate():
    db = _Rows(rows=[(11, 100, 20), (16, 50, 25)])
    out = await cd._by_hour(db, uuid.uuid4())
    assert out[0] == {"hour": 11, "dialed": 100, "answered": 20, "answer_rate": 0.2}
    assert out[1]["answer_rate"] == 0.5   # 4pm is twice as good as 11am


@pytest.mark.asyncio
async def test_by_hour_never_divides_by_zero():
    out = await cd._by_hour(_Rows(rows=[(9, 0, 0)]), uuid.uuid4())
    assert out[0]["answer_rate"] == 0.0


@pytest.mark.asyncio
async def test_hangup_causes_flag_permanently_dead_numbers():
    """The whole point of keeping the cause: a number that will never work must
    be distinguishable from one that simply didn't answer this time."""
    db = _Rows(rows=[("NO_ANSWER", 400), ("INVALID_NUMBER", 90), ("USER_BUSY", 30)])
    out = await cd._hangup_causes(db, uuid.uuid4())
    by_cause = {r["cause"]: r["likely_permanent"] for r in out}
    assert by_cause["INVALID_NUMBER"] is True
    assert by_cause["NO_ANSWER"] is False
    assert by_cause["USER_BUSY"] is False   # busy people are worth another try


@pytest.mark.asyncio
async def test_talk_time_reports_the_early_hangup_share():
    """A high early-hangup rate is an OPENER problem, not a list problem — the
    distinction the five summary buckets could never make."""
    db = _Rows(one=(200, 80, 120, 42.5, 30.0))
    out = await cd._talk_time(db, uuid.uuid4())
    assert out["connected"] == 200
    assert out["hung_up_under_10s"] == 80
    assert out["early_hangup_rate"] == 0.4
    assert out["median_seconds"] == 30.0


@pytest.mark.asyncio
async def test_talk_time_on_a_campaign_with_no_connects():
    out = await cd._talk_time(_Rows(one=(0, 0, 0, None, None)), uuid.uuid4())
    assert out["early_hangup_rate"] == 0.0
    assert out["avg_seconds"] is None


@pytest.mark.asyncio
async def test_question_dropoff_tracks_who_is_still_engaged():
    """The curve an operator reads to find which question loses them callers."""
    db = _Rows(rows=[(0, 10), (1, 30), (2, 20), (3, 40)])
    out = await cd._question_dropoff(db, uuid.uuid4())
    assert [r["questions_reached"] for r in out] == [0, 1, 2, 3]
    assert out[0]["still_engaged"] == 100     # everyone
    assert out[1]["still_engaged"] == 90      # after the 10 who reached none
    assert out[3]["still_engaged"] == 40
    assert out[1]["share"] == 0.3


@pytest.mark.asyncio
async def test_retry_readiness_compares_first_and_repeat_attempts(monkeypatch):
    """Estimates retry lift from the manual Re-run traffic that already exists,
    so the cadence's offsets are set from evidence rather than taste."""
    async def fake_causes(db, cid):
        return [{"cause": "NO_ANSWER", "count": 300, "likely_permanent": False}]

    monkeypatch.setattr(cd, "_hangup_causes", fake_causes)
    db = _Rows(one=(1000, 250, 200, 60))
    out = await cd._retry_readiness(db, uuid.uuid4())
    assert out["first_attempt"]["answer_rate"] == 0.25
    assert out["repeat_attempts"]["answer_rate"] == 0.3   # retries do connect
    assert out["sample_sufficient"] is True


@pytest.mark.asyncio
async def test_retry_readiness_flags_a_thin_sample():
    db = _Rows(one=(1000, 250, 4, 1))
    out = await cd._retry_readiness(db, uuid.uuid4())
    assert out["sample_sufficient"] is False   # 4 repeat dials proves nothing


@pytest.mark.asyncio
async def test_one_broken_aggregate_does_not_blank_the_page(monkeypatch):
    """An operator opening diagnostics is already debugging something. A failing
    block must degrade to null, not take the other six with it."""
    async def boom(db, cid):
        raise RuntimeError("bad plan")

    async def fine(db, cid):
        return {"ok": True}

    monkeypatch.setattr(cd, "_by_hour", boom)
    monkeypatch.setattr(cd, "_talk_time", fine)
    out = await cd.campaign_diagnostics(_Rows(), uuid.uuid4())
    assert out["answer_rate_by_hour"] is None
    assert out["talk_time"] == {"ok": True}
    assert out["campaign_id"]
