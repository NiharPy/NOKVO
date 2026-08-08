"""The APEX plan-compliance audit's rules (``scripts/audit_apex_plan_compliance.py``).

The audit is what tells us whether live accounts still match the catalog, so its checks
need their own tests: a rule that silently stops firing would report "all compliant" over a
mis-priced account. Each test drives one rule with a hand-built row — no DB needed.
"""
from __future__ import annotations

import sys
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.audit_apex_plan_compliance import (  # noqa: E402
    check_non_apex,
    check_plan_stamp,
    check_status,
    check_subscription,
    check_wallet_grants,
)


class _Row:
    """Any attribute the checks read; unset ones read as None."""

    def __init__(self, **kw):
        self.id = kw.pop("id", uuid.uuid4())
        self.name = kw.pop("name", "audit-test")
        self.product_tier = kw.pop("product_tier", "nokvo_apex")
        self.status = kw.pop("status", "active")
        self.apex_activated_at = kw.pop("apex_activated_at", object())
        for k, v in kw.items():
            setattr(self, k, v)

    def __getattr__(self, _name):  # only reached for attributes never set
        return None


def _core(**overrides) -> _Row:
    base = dict(
        apex_plan_code="core", apex_rate_per_minute=Decimal("9"), apex_concurrency=1,
        apex_included_minutes=1000, apex_platform_fee_paise=449900,
        apex_billed_bonus_pct=Decimal("0"), apex_topup_bonus_pct=Decimal("0"),
        apex_support_tier="basic_onboarding",
    )
    base.update(overrides)
    return _Row(**base)


def _codes(findings) -> set[str]:
    return {f.code for f in findings}


def test_a_correctly_stamped_row_is_clean():
    assert check_plan_stamp(_core()) == []


def test_missing_plan_code_is_flagged_as_legacy_billing():
    findings = check_plan_stamp(_Row(apex_plan_code=None))
    assert _codes(findings) == {"P1"}
    assert "LEGACY" in findings[0].message


@pytest.mark.parametrize(
    "override, expect",
    [
        ({"apex_rate_per_minute": Decimal("5")}, "P4"),        # wrong rate for Core
        ({"apex_concurrency": 4}, "P4"),                        # Growth's lines on Core
        ({"apex_included_minutes": 5000}, "P3"),
        ({"apex_platform_fee_paise": 999900}, "P3"),
        ({"apex_topup_bonus_pct": Decimal("10")}, "P3"),
        ({"apex_support_tier": "level_a"}, "P3"),
        ({"apex_rate_per_minute": None}, "P2"),
    ],
)
def test_each_drifted_column_is_caught(override, expect):
    assert expect in _codes(check_plan_stamp(_core(**override)))


def test_rate_below_the_connect_fee_is_caught():
    # Enterprise is the only plan whose rate is negotiated, so it's the only way a
    # sub-connect-fee rate can reach a row.
    row = _core(
        apex_plan_code="enterprise", apex_rate_per_minute=Decimal("1"), apex_concurrency=8,
        apex_included_minutes=100000, apex_platform_fee_paise=2000000,
        apex_billed_bonus_pct=Decimal("50"), apex_topup_bonus_pct=Decimal("20"),
        apex_support_tier="rm_level_a",
    )
    assert "P5" in _codes(check_plan_stamp(row))


def test_enterprise_bounds_are_enforced_but_a_negotiated_row_passes():
    ent = dict(
        apex_plan_code="enterprise", apex_included_minutes=100000, apex_platform_fee_paise=2000000,
        apex_billed_bonus_pct=Decimal("50"), apex_topup_bonus_pct=Decimal("20"),
        apex_support_tier="rm_level_a",
    )
    ok = _core(**ent, apex_rate_per_minute=Decimal("4.5"), apex_concurrency=8)
    assert check_plan_stamp(ok) == []
    over_ceiling = _core(**ent, apex_rate_per_minute=Decimal("6"), apex_concurrency=8)
    assert "P4" in _codes(check_plan_stamp(over_ceiling))
    too_few_lines = _core(**ent, apex_rate_per_minute=Decimal("4.5"), apex_concurrency=2)
    assert "P4" in _codes(check_plan_stamp(too_few_lines))


def test_free_trial_concurrency_is_allowed_within_the_cap():
    trial = dict(
        apex_plan_code="free_trial", apex_rate_per_minute=Decimal("9"),
        apex_included_minutes=1000, apex_platform_fee_paise=0,
        apex_billed_bonus_pct=Decimal("0"), apex_topup_bonus_pct=Decimal("0"),
        apex_support_tier="basic",
    )
    assert check_plan_stamp(_core(**trial, apex_concurrency=6)) == []
    assert "P4" in _codes(check_plan_stamp(_core(**trial, apex_concurrency=25)))


class _Sub:
    def __init__(self, amount_paise, *, status="active", minutes=1000, sub_id="sub_x", plan="apex"):
        self.amount_paise = amount_paise
        self.status = status
        self.minutes = minutes
        self.razorpay_subscription_id = sub_id
        self.plan = plan


def test_subscription_amount_must_match_the_plan():
    org = _core()
    # Core = ₹4,499 fee + 1000 × ₹9 = ₹13,499/mo.
    assert check_subscription(org, [_Sub(1349900)]) == []
    findings = check_subscription(org, [_Sub(449900)])
    assert _codes(findings) == {"S1"}


def test_free_trial_must_not_carry_a_chargeable_subscription():
    org = _core(
        apex_plan_code="free_trial", apex_platform_fee_paise=0, apex_support_tier="basic",
    )
    assert "S1" in _codes(check_subscription(org, [_Sub(1349900)]))
    assert check_subscription(org, []) == []


def test_a_live_account_with_no_subscription_is_a_warning_not_a_violation():
    findings = check_subscription(_core(status="active"), [])
    assert [f.severity for f in findings] == ["warning"]


class _Purchase:
    def __init__(self, source, minutes, rupees, rate):
        self.id = uuid.uuid4()
        self.source = source
        self.minutes = minutes
        self.rupees = Decimal(str(rupees))
        self.rate_per_minute = Decimal(str(rate))


def test_wallet_credits_must_equal_minutes_times_rate_plus_the_source_bonus():
    org = _core(
        apex_plan_code="pinnacle", apex_rate_per_minute=Decimal("5.5"), apex_concurrency=4,
        apex_included_minutes=25000, apex_platform_fee_paise=999900,
        apex_billed_bonus_pct=Decimal("20"), apex_topup_bonus_pct=Decimal("10"),
        apex_support_tier="level_a",
    )
    # 25000 × 5.5 × 1.20 = 165,000 (monthly grant earns the BILLED bonus).
    assert check_wallet_grants(org, [_Purchase("monthly_grant", 25000, "165000.0000", "5.5")]) == []
    # A top-up earns the smaller top-up bonus: 1000 × 5.5 × 1.10 = 6,050.
    assert check_wallet_grants(org, [_Purchase("topup", 1000, "6050.0000", "5.5")]) == []
    # ...crediting a top-up at the billed bonus over-grants and must be caught.
    assert "W1" in _codes(check_wallet_grants(org, [_Purchase("topup", 1000, "6600.0000", "5.5")]))
    # A goodwill grant earns no bonus at all.
    assert check_wallet_grants(org, [_Purchase("complimentary_grant", 100, "550.0000", "5.5")]) == []
    assert "W1" in _codes(check_wallet_grants(org, [_Purchase("complimentary_grant", 100, "660.0000", "5.5")]))


def test_legacy_credit_source_is_reported_as_a_warning():
    findings = check_wallet_grants(_core(), [_Purchase("onboarding", 1000, "9000.0000", "9")])
    assert [(f.code, f.severity) for f in findings] == [("W1", "warning")]


def test_status_rules():
    # A live account that was never credited can dial on someone else's dime.
    assert "T1" in _codes(check_status(_core(status="active"), []))
    assert check_status(_core(status="active"), [_Purchase("monthly_grant", 1000, "9000", "9")]) == []
    # A trial is never charged, so it must never sit in pending_payment.
    trial = _core(apex_plan_code="free_trial", apex_platform_fee_paise=0,
                  apex_support_tier="basic", status="pending_payment")
    assert "T1" in _codes(check_status(trial, []))


def test_stray_apex_config_on_a_nokvo_one_org_is_flagged():
    clean = _Row(product_tier="nokvo_one")
    assert check_non_apex(clean) == []
    stray = _Row(product_tier="nokvo_one", apex_plan_code="core")
    assert _codes(check_non_apex(stray)) == {"X1"}
