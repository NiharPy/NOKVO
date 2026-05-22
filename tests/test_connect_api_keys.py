"""Unit tests for app.core.api_keys mint/parse/verify cycle."""
from __future__ import annotations

import pytest

from app.core import api_keys


def test_mint_returns_well_shaped_key():
    minted = api_keys.mint("live")
    assert minted.raw.startswith("nk_live_")
    assert minted.mode == "live"
    assert minted.key_prefix.startswith("nk_live_")
    assert minted.secret_hash and minted.secret_hash != minted.raw
    # 6-char prefix + 36-char secret + "nk_live_"
    assert len(minted.raw) == len("nk_live_") + 6 + 36


def test_mint_test_mode():
    minted = api_keys.mint("test")
    assert minted.raw.startswith("nk_test_")
    assert minted.key_prefix.startswith("nk_test_")
    assert minted.mode == "test"


def test_parse_round_trip():
    minted = api_keys.mint("live")
    parsed = api_keys.parse(minted.raw)
    assert parsed is not None
    assert parsed.mode == "live"
    assert parsed.key_prefix == minted.key_prefix
    assert api_keys.verify(parsed, minted.secret_hash) is True


def test_parse_rejects_malformed():
    for bad in (None, "", "garbage", "nk_live_short", "nk_other_" + "x" * 42):
        assert api_keys.parse(bad) is None


def test_verify_rejects_wrong_secret():
    a = api_keys.mint("live")
    b = api_keys.mint("live")
    parsed_a = api_keys.parse(a.raw)
    assert parsed_a is not None
    # Mismatched secret hash should not verify.
    assert api_keys.verify(parsed_a, b.secret_hash) is False


def test_mint_webhook_secret_round_trip():
    raw, h = api_keys.mint_webhook_secret()
    assert raw.startswith("whsec_")
    assert h != raw


@pytest.mark.parametrize("mode", ["live", "test"])
def test_each_mint_is_unique(mode):
    seen = set()
    for _ in range(20):
        m = api_keys.mint(mode)
        assert m.raw not in seen
        seen.add(m.raw)
