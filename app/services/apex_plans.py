"""NOKVO APEX plan catalog — the single source of truth for plan-driven pricing.

APEX pricing is **plan-driven** (this module), not quantity-driven (the slab ladder
in :mod:`app.services.minute_pricing`, which stays for Nokvo One). Each plan fixes the
per-minute rate, concurrency, monthly platform fee, included minutes, wallet bonus
(different for the monthly *billed* grant vs. dashboard *top-ups*), support tier and
the activation SLA.

An org's concrete config is **stamped onto its columns at creation** (via
:func:`stamp_org_from_plan`) so a later catalog edit never silently re-prices an existing
account, and the ``enterprise`` plan — whose rate/concurrency are negotiated per deal —
always lands as concrete values on the row. ``get_apex_rate`` / ``get_apex_concurrency``
read those columns and fall back to the catalog only for a legacy row that predates them,
so no runtime path can ever hit a ``None`` rate.

Money is ``Decimal``/``int`` end to end (paise for Razorpay); never float.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.services.call_cost_calculator import _LEDGER_Q
from app.services.minute_pricing import APEX_CONNECT_FEE


@dataclass(frozen=True)
class ApexPlan:
    code: str
    label: str
    # ``None`` in the catalog for enterprise (negotiated per deal); always concrete
    # once stamped onto an org row.
    concurrency: int | None
    rate_per_minute: Decimal | None
    platform_fee_paise: int
    included_minutes: int
    # Wallet bonus as a whole-number PERCENT. "billed" = the monthly included-minutes
    # grant; "topup" = dashboard top-ups. 0 = no bonus.
    billed_bonus_pct: Decimal
    topup_bonus_pct: Decimal
    support_tier: str
    # Communicated SLA only (activation is a manual SuperAdmin action). ``None`` =
    # enterprise (per deal, longer than 6h).
    activation_sla_hours: int | None
    chargeable: bool


APEX_PLANS: dict[str, ApexPlan] = {
    "free_trial": ApexPlan(
        code="free_trial", label="Free Trial", concurrency=1,
        rate_per_minute=Decimal("9"), platform_fee_paise=0, included_minutes=1000,
        billed_bonus_pct=Decimal("0"), topup_bonus_pct=Decimal("0"),
        support_tier="basic", activation_sla_hours=24, chargeable=False,
    ),
    "core": ApexPlan(
        code="core", label="Core", concurrency=1,
        rate_per_minute=Decimal("9"), platform_fee_paise=449900, included_minutes=1000,
        billed_bonus_pct=Decimal("0"), topup_bonus_pct=Decimal("0"),
        support_tier="basic_onboarding", activation_sla_hours=6, chargeable=True,
    ),
    "growth": ApexPlan(
        code="growth", label="Growth", concurrency=2,
        rate_per_minute=Decimal("7.5"), platform_fee_paise=749900, included_minutes=5000,
        billed_bonus_pct=Decimal("0"), topup_bonus_pct=Decimal("0"),
        support_tier="level_b", activation_sla_hours=6, chargeable=True,
    ),
    "pinnacle": ApexPlan(
        code="pinnacle", label="Pinnacle", concurrency=4,
        rate_per_minute=Decimal("5.5"), platform_fee_paise=999900, included_minutes=25000,
        billed_bonus_pct=Decimal("20"), topup_bonus_pct=Decimal("10"),
        support_tier="level_a", activation_sla_hours=6, chargeable=True,
    ),
    "enterprise": ApexPlan(
        code="enterprise", label="Enterprise", concurrency=None,
        rate_per_minute=None, platform_fee_paise=2000000, included_minutes=100000,
        billed_bonus_pct=Decimal("50"), topup_bonus_pct=Decimal("20"),
        support_tier="rm_level_a", activation_sla_hours=None, chargeable=True,
    ),
}

# Used by the DB CHECK constraint and by request/create validation.
VALID_PLAN_CODES: frozenset[str] = frozenset(APEX_PLANS)
DEFAULT_PLAN_CODE = "core"

# Enterprise is "under ₹5/min" and "5+ concurrency" — the negotiated values must fall
# within these bounds (and the rate must at least cover the per-call connect fee).
ENTERPRISE_RATE_CEILING = Decimal("5")
ENTERPRISE_MIN_CONCURRENCY = 5


def resolve_plan(code: str | None) -> ApexPlan:
    """The :class:`ApexPlan` for ``code`` (defaults to Core for an unknown/None code)."""
    return APEX_PLANS.get((code or "").strip().lower(), APEX_PLANS[DEFAULT_PLAN_CODE])


def _q_pct(pct) -> Decimal:
    return Decimal(str(pct))


def wallet_credit_for(minutes, rate, bonus_pct) -> Decimal:
    """Spendable Call Credits (1 credit ≡ ₹1) granted for ``minutes`` at ``rate`` with a
    ``bonus_pct``% bonus: ``minutes × rate × (1 + bonus_pct/100)``, quantised to the 4-dp
    ledger. Raises if ``rate``/``bonus_pct`` is None — a chargeable plan must always resolve
    a concrete rate before crediting (guards a mis-stamped enterprise row)."""
    if rate is None or bonus_pct is None:
        raise ValueError("wallet_credit_for requires a concrete rate and bonus_pct")
    billed = Decimal(int(minutes)) * Decimal(str(rate))
    credit = billed * (Decimal("1") + _q_pct(bonus_pct) / Decimal("100"))
    return credit.quantize(_LEDGER_Q, rounding=ROUND_HALF_UP)


def monthly_subscription_paise(rate, included_minutes, platform_fee_paise) -> int:
    """Recurring monthly charge in paise = platform fee + (included minutes × rate).
    ``rate`` may be ₹-with-≤2dp, so the product converts to paise exactly."""
    if rate is None:
        raise ValueError("monthly_subscription_paise requires a concrete rate")
    minutes_paise = int(
        (Decimal(int(included_minutes)) * Decimal(str(rate)) * Decimal("100")).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )
    return int(platform_fee_paise) + minutes_paise


def stamp_org_from_plan(
    org,
    code: str,
    *,
    enterprise_rate=None,
    enterprise_concurrency=None,
) -> ApexPlan:
    """Resolve ``code`` and write the concrete plan config onto ``org``'s columns.

    For ``enterprise`` the caller MUST pass ``enterprise_rate`` (< ₹5, ≥ the connect fee)
    and ``enterprise_concurrency`` (≥ 5) — they are negotiated per deal and there is no
    catalog default. Every stamped row therefore has a non-null rate/concurrency. Returns
    the resolved plan (with enterprise overrides applied) for the caller's convenience."""
    code = (code or "").strip().lower()
    if code not in APEX_PLANS:
        raise ValueError(f"Unknown APEX plan: {code!r}")
    plan = APEX_PLANS[code]

    rate = plan.rate_per_minute
    concurrency = plan.concurrency
    if code == "enterprise":
        if enterprise_rate is None or enterprise_concurrency is None:
            raise ValueError("Enterprise requires an explicit rate_per_minute and concurrency")
        rate = Decimal(str(enterprise_rate))
        concurrency = int(enterprise_concurrency)
        if not (Decimal("0") < rate < ENTERPRISE_RATE_CEILING):
            raise ValueError(f"Enterprise rate must be > 0 and < ₹{ENTERPRISE_RATE_CEILING}")
        if concurrency < ENTERPRISE_MIN_CONCURRENCY:
            raise ValueError(f"Enterprise concurrency must be ≥ {ENTERPRISE_MIN_CONCURRENCY}")

    if rate is None or rate < APEX_CONNECT_FEE:
        raise ValueError(f"APEX rate must be ≥ the connect fee (₹{APEX_CONNECT_FEE})")

    org.apex_plan_code = code
    org.apex_rate_per_minute = rate
    org.apex_concurrency = int(concurrency)
    org.apex_included_minutes = int(plan.included_minutes)
    org.apex_platform_fee_paise = int(plan.platform_fee_paise)
    org.apex_billed_bonus_pct = plan.billed_bonus_pct
    org.apex_topup_bonus_pct = plan.topup_bonus_pct
    org.apex_support_tier = plan.support_tier
    return ApexPlan(
        code=plan.code, label=plan.label, concurrency=int(concurrency), rate_per_minute=rate,
        platform_fee_paise=plan.platform_fee_paise, included_minutes=plan.included_minutes,
        billed_bonus_pct=plan.billed_bonus_pct, topup_bonus_pct=plan.topup_bonus_pct,
        support_tier=plan.support_tier, activation_sla_hours=plan.activation_sla_hours,
        chargeable=plan.chargeable,
    )


def get_apex_rate(org) -> Decimal:
    """The org's fixed per-minute rate (from its stamped column; catalog fallback for a
    legacy row). Never None."""
    rate = getattr(org, "apex_rate_per_minute", None)
    if rate is not None:
        return Decimal(str(rate))
    plan = resolve_plan(getattr(org, "apex_plan_code", None))
    return plan.rate_per_minute if plan.rate_per_minute is not None else APEX_PLANS[DEFAULT_PLAN_CODE].rate_per_minute


def get_apex_concurrency(org) -> int:
    """The org's concurrency cap (stamped column; catalog fallback for a legacy row).
    Never None; floors at 1."""
    c = getattr(org, "apex_concurrency", None)
    if c is not None:
        return max(1, int(c))
    plan = resolve_plan(getattr(org, "apex_plan_code", None))
    return max(1, int(plan.concurrency or 1))


def get_topup_bonus_pct(org) -> Decimal:
    """Top-up bonus % for the org's plan (stamped column; catalog fallback)."""
    pct = getattr(org, "apex_topup_bonus_pct", None)
    if pct is not None:
        return Decimal(str(pct))
    return resolve_plan(getattr(org, "apex_plan_code", None)).topup_bonus_pct


def plan_public_view(plan: ApexPlan) -> dict:
    """Catalog row for the ``GET /apex/plans`` endpoint / UI. Enterprise shows its
    rate/concurrency as null ("contact sales")."""
    return {
        "code": plan.code,
        "label": plan.label,
        "concurrency": plan.concurrency,
        "rate_per_minute": float(plan.rate_per_minute) if plan.rate_per_minute is not None else None,
        "platform_fee_inr": plan.platform_fee_paise / 100,
        "included_minutes": plan.included_minutes,
        "billed_bonus_pct": float(plan.billed_bonus_pct),
        "topup_bonus_pct": float(plan.topup_bonus_pct),
        "support_tier": plan.support_tier,
        "activation_sla_hours": plan.activation_sla_hours,
        "chargeable": plan.chargeable,
        "monthly_inr": (
            monthly_subscription_paise(plan.rate_per_minute, plan.included_minutes, plan.platform_fee_paise) / 100
            if plan.rate_per_minute is not None else None
        ),
    }
