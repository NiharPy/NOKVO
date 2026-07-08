"""APEX scheduled-dialer window — pure window/day/cap decision helpers.

The dialer only places calls inside 09:00–19:00 Asia/Kolkata, on the first
``working_days`` weekdays (5→Mon–Fri), up to ``calls_per_day`` placements/day
(a runtime counter that resets on the IST date rollover). These pin that
decision without a DB / event loop.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.services.outbound_campaign_service import (
    _allowed_weekdays,
    _dialed_today_count,
    call_window_allows,
)

IST = ZoneInfo("Asia/Kolkata")
# 2026-06-22 is a Monday; 06-27 is a Saturday.
MON = datetime(2026, 6, 22, 10, 0, tzinfo=IST)
SAT = datetime(2026, 6, 27, 12, 0, tzinfo=IST)
WINDOW = {"working_days": 5, "calls_per_day": 200, "window_local": "09:00-19:00"}


def test_no_window_always_allowed():
    # Legacy / Nokvo One campaigns (no call_window) dial anytime.
    assert call_window_allows(None, datetime(2026, 6, 22, 3, 0, tzinfo=IST)) == (True, "no_window")
    assert call_window_allows({}, MON)[0] is True


def test_hour_band_boundaries():
    assert call_window_allows(WINDOW, MON.replace(hour=9, minute=0))[0] is True    # opens at 09:00
    assert call_window_allows(WINDOW, MON.replace(hour=18, minute=59))[0] is True  # last minute
    assert call_window_allows(WINDOW, MON.replace(hour=8, minute=59)) == (False, "outside_hours")
    assert call_window_allows(WINDOW, MON.replace(hour=19, minute=0)) == (False, "outside_hours")
    assert call_window_allows(WINDOW, MON.replace(hour=23, minute=30)) == (False, "outside_hours")


def test_weekday_gate():
    # 5-day week → Mon–Fri allowed, Sat/Sun paused.
    assert call_window_allows(WINDOW, MON)[0] is True
    assert call_window_allows(WINDOW, SAT) == (False, "outside_days")
    # 6-day week → Saturday now allowed.
    assert call_window_allows({**WINDOW, "working_days": 6}, SAT)[0] is True
    # 7-day week → Sunday allowed too.
    sun = datetime(2026, 6, 28, 12, 0, tzinfo=IST)
    assert call_window_allows({**WINDOW, "working_days": 7}, sun)[0] is True


def test_allowed_weekdays_mapping():
    assert _allowed_weekdays(5) == {0, 1, 2, 3, 4}        # Mon–Fri
    assert _allowed_weekdays(6) == {0, 1, 2, 3, 4, 5}     # Mon–Sat
    assert _allowed_weekdays(7) == {0, 1, 2, 3, 4, 5, 6}  # all
    # clamp out-of-range / junk to 1..7
    assert _allowed_weekdays(0) == {0}
    assert _allowed_weekdays(99) == {0, 1, 2, 3, 4, 5, 6}
    assert _allowed_weekdays(None) == {0, 1, 2, 3, 4, 5, 6}


def test_daily_counter_reset_on_rollover():
    cw = {"dialed_today": {"date": "2026-06-26", "count": 137}}
    assert _dialed_today_count(cw, "2026-06-26") == 137   # same day → running count
    assert _dialed_today_count(cw, "2026-06-27") == 0     # new day → reset
    assert _dialed_today_count({}, "2026-06-27") == 0     # never dialed → 0
    assert _dialed_today_count({"dialed_today": {"date": "2026-06-27", "count": "x"}}, "2026-06-27") == 0


# ── build_agent_config echoes the FULL call_window ────────────────────────────
# The normalizer re-runs on every create path; anything it doesn't echo is
# silently stripped from the stored config. Dropping calls_per_day here is
# exactly what disabled the daily cap for every form-created APEX campaign.


def test_build_agent_config_echoes_calls_per_day_and_dialed_today():
    from app.services.agent_outbound_context import build_agent_config

    cfg = build_agent_config(
        agent_prompt="pitch",
        call_window={
            "working_days": 5,
            "hours_per_day": 10,
            "calls_per_day": 200,
            "dialed_today": {"date": "2026-07-08", "count": 37},
        },
    )
    w = cfg["call_window"]
    assert w["calls_per_day"] == 200           # the daily cap SURVIVES normalization
    assert w["dialed_today"] == {"date": "2026-07-08", "count": 37}  # runtime counter too
    assert w["working_days"] == 5 and w["hours_per_day"] == 10
    assert w["window_local"] == "09:00-19:00"


def test_build_agent_config_call_window_clamps_and_omits_zero_cap():
    from app.services.agent_outbound_context import build_agent_config

    cfg = build_agent_config(
        agent_prompt="pitch",
        call_window={"working_days": 99, "hours_per_day": 24, "calls_per_day": 0},
    )
    w = cfg["call_window"]
    assert w["working_days"] == 7              # days/week clamp (was min(60) — a bug)
    assert w["hours_per_day"] == 10            # 9AM–7PM band
    assert "calls_per_day" not in w            # 0/absent → no cap key
    assert "dialed_today" not in w
