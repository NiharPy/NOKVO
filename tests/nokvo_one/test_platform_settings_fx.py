"""SuperAdmin-tunable USD→INR FX — validation, apply-to-process, and the fact
that changing it actually changes COGS pricing.

DB-free: the session is faked; the key behavior pinned is that a saved rate
mutates ``settings.USD_TO_INR`` in-process, which every ``compute_cogs_inr`` /
``llm_cost_inr`` call reads live — so pricing changes take effect immediately
without touching the pricing functions.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

import app.services.platform_settings as ps
from app.core.config import settings
from app.services.call_usage import CallUsage, compute_cogs_inr, llm_cost_inr


@pytest.fixture(autouse=True)
def _restore_fx():
    """Every test leaves the process rate exactly as it found it."""
    before = settings.USD_TO_INR
    yield
    settings.USD_TO_INR = before


def test_validate_fx_bounds_and_junk():
    assert ps.validate_fx("86.5") == 86.5
    assert ps.validate_fx(90) == 90.0
    for bad in ("abc", None, "", 0, 9.9, 501, -5):
        with pytest.raises(ValueError):
            ps.validate_fx(bad)


class _FxDB:
    """Fake session: db.get returns the configured row; execute/commit recorded."""

    def __init__(self, row=None):
        self.row = row
        self.executed = []
        self.committed = 0
        self.deleted = []

    async def get(self, model, key):
        return self.row

    async def execute(self, stmt):
        self.executed.append(stmt)

    async def commit(self):
        self.committed += 1

    async def delete(self, row):
        self.deleted.append(row)


@pytest.mark.asyncio
async def test_set_fx_persists_and_applies_in_process():
    db = _FxDB(row=None)
    out = await ps.set_usd_to_inr(db, 92.5, updated_by="ops@nokvo.com")
    assert settings.USD_TO_INR == 92.5          # calculations change immediately
    assert db.committed == 1 and len(db.executed) == 1
    sql = str(db.executed[0])
    assert "platform_settings" in sql and "ON CONFLICT" in sql
    assert out["usd_to_inr"] in (92.5, ps.DEFAULT_USD_TO_INR)  # read-back is via db.get(row=None fake)


@pytest.mark.asyncio
async def test_set_fx_rejects_out_of_range_without_touching_process():
    before = settings.USD_TO_INR
    with pytest.raises(ValueError):
        await ps.set_usd_to_inr(_FxDB(), 5000, updated_by="ops@nokvo.com")
    assert settings.USD_TO_INR == before


@pytest.mark.asyncio
async def test_clear_fx_restores_default():
    settings.USD_TO_INR = 120.0  # pretend an override is live
    row = SimpleNamespace(value="120", updated_at=None, updated_by="ops")
    db = _FxDB(row=row)
    await ps.clear_usd_to_inr(db, updated_by="ops@nokvo.com")
    assert settings.USD_TO_INR == ps.DEFAULT_USD_TO_INR
    assert db.deleted == [row]


@pytest.mark.asyncio
async def test_get_fx_reports_override_metadata():
    row = SimpleNamespace(value="95", updated_at=None, updated_by="ops@nokvo.com")
    out = await ps.get_usd_to_inr(_FxDB(row=row))
    assert out == {
        "usd_to_inr": 95.0,
        "default": ps.DEFAULT_USD_TO_INR,
        "is_override": True,
        "updated_at": None,
        "updated_by": "ops@nokvo.com",
    }
    out_default = await ps.get_usd_to_inr(_FxDB(row=None))
    assert out_default["is_override"] is False
    assert out_default["usd_to_inr"] == ps.DEFAULT_USD_TO_INR


@pytest.mark.asyncio
async def test_junk_persisted_rate_is_ignored():
    row = SimpleNamespace(value="not-a-number", updated_at=None, updated_by=None)
    out = await ps.get_usd_to_inr(_FxDB(row=row))
    assert out["usd_to_inr"] == ps.DEFAULT_USD_TO_INR and out["is_override"] is False


def test_changed_rate_changes_cogs_pricing():
    """The whole point: the SAME usage prices differently after a rate change —
    linearly in the FX for every USD-based component — while the INR-native
    Plivo flat fee stays fixed."""
    usage = CallUsage(
        llm_input_tokens=1000, llm_output_tokens=500, llm_cached_tokens=200,
        stt_seconds=120, tts_characters=600,
    )
    settings.USD_TO_INR = 86.0
    at_86 = compute_cogs_inr(usage, telephony_seconds=60)
    llm_at_86 = llm_cost_inr(1000, 500, 200)
    settings.USD_TO_INR = 172.0  # double the rate
    at_172 = compute_cogs_inr(usage, telephony_seconds=60)

    # Each USD-based component doubles (±1 quantization ulp — each figure is
    # independently rounded to 4dp, so double-then-round ≠ round-then-double).
    eps = Decimal("0.0002")
    assert abs(at_172.cost_stt_inr - at_86.cost_stt_inr * 2) <= eps
    assert abs(at_172.cost_llm_inr - at_86.cost_llm_inr * 2) <= eps
    assert abs(at_172.cost_tts_inr - at_86.cost_tts_inr * 2) <= eps
    assert at_172.cost_telephony_inr == at_86.cost_telephony_inr  # INR-native, FX-immune
    assert abs(llm_cost_inr(1000, 500, 200) - llm_at_86 * 2) <= eps  # post-call attribution too


@pytest.mark.asyncio
async def test_apply_persisted_fx_folds_db_value_into_process(monkeypatch):
    row = SimpleNamespace(value="99.5", updated_at=None, updated_by=None)

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, model, key):
            return row

    import app.db.session as dbs

    monkeypatch.setattr(dbs, "AsyncSessionLocal", lambda: _Session())
    settings.USD_TO_INR = 86.0
    await ps.apply_persisted_fx()
    assert settings.USD_TO_INR == 99.5
    # No row → back to the default (an override cleared elsewhere converges).
    row = None  # noqa: F841 — rebind captured via closure

    class _Empty(_Session):
        async def get(self, model, key):
            return None

    monkeypatch.setattr(dbs, "AsyncSessionLocal", lambda: _Empty())
    await ps.apply_persisted_fx()
    assert settings.USD_TO_INR == ps.DEFAULT_USD_TO_INR
