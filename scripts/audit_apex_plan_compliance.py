"""Audit every organization against the NOKVO APEX plan rules (read-only).

APEX pricing is plan-driven: the catalog in :mod:`app.services.apex_plans` is stamped onto
the org's columns at creation, and every runtime path (billing rate, concurrency cap,
wallet bonus, monthly charge) reads those columns. Nothing re-checks them afterwards, so a
hand-edited row, a half-applied migration or an account created before plans existed can
quietly bill at the wrong rate. This script re-derives what each row SHOULD look like and
reports every deviation.

Checks per APEX org:
  P1  plan code present and in the catalog
  P2  no NULL stamped column (a NULL forces the catalog fallback — wrong for enterprise)
  P3  fixed fields (included minutes, platform fee, bonuses, support tier) match the catalog
  P4  rate/concurrency match the catalog, allowing the two sanctioned deviations:
      enterprise (negotiated: 0 < rate < ceiling, concurrency >= floor) and Free Trial
      (operator-adjustable concurrency 1..TRIAL_MAX_CONCURRENCY)
  P5  rate >= the per-call connect fee (below it a call bills less than it costs)
  S1  chargeable plan has an apex subscription charged at platform fee + minutes x rate;
      Free Trial has no chargeable subscription
  W1  every wallet grant equals minutes x rate x (1 + the bonus that source earns)
  T1  status/plan coherence (trial never awaits payment, active orgs are stamped activated,
      pending_activation orgs have a funded wallet)
  X1  non-APEX orgs carry no stray apex_* config

Exit code is 0 when every account is compliant, 1 when any violation was found (so it can
gate a deploy). Read-only: it never writes.

Run from repo root:
    source venv/bin/activate
    python3 scripts/audit_apex_plan_compliance.py                  # uses .env
    python3 scripts/audit_apex_plan_compliance.py --env .env.prod  # audit production
    python3 scripts/audit_apex_plan_compliance.py --json           # machine-readable
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_env(env_file: str | None) -> None:
    """Load an alternate env file BEFORE app.core.config reads it (settings is a module
    -level singleton, so this has to happen at import time)."""
    if not env_file:
        return
    from dotenv import load_dotenv

    path = Path(env_file)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / env_file
    if not path.exists():
        raise SystemExit(f"env file not found: {path}")
    load_dotenv(path, override=True)


_pre = argparse.ArgumentParser(add_help=False)
_pre.add_argument("--env")
_load_env(_pre.parse_known_args()[0].env)

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.models.minute_purchase import MinutePurchase  # noqa: E402
from app.models.organization import Organization  # noqa: E402
from app.models.subscription import Subscription  # noqa: E402
from app.services.apex_plans import (  # noqa: E402
    APEX_PLANS,
    ENTERPRISE_MIN_CONCURRENCY,
    ENTERPRISE_RATE_CEILING,
    TRIAL_MAX_CONCURRENCY,
    monthly_subscription_paise,
    wallet_credit_for,
)
from app.services.minute_pricing import APEX_CONNECT_FEE  # noqa: E402

APEX_TIER = "nokvo_apex"

# Which bonus each wallet-credit source earns. The monthly/trial grant earns the plan's
# BILLED bonus, a dashboard top-up the TOPUP bonus, and a goodwill grant earns none.
_GRANT_BONUS_FIELD = {
    "trial_grant": "billed",
    "monthly_grant": "billed",
    "topup": "topup",
    "complimentary_grant": "none",
}

# Stamped columns that must never be NULL on a plan-driven APEX org.
_STAMPED = (
    "apex_rate_per_minute",
    "apex_concurrency",
    "apex_included_minutes",
    "apex_platform_fee_paise",
    "apex_billed_bonus_pct",
    "apex_topup_bonus_pct",
    "apex_support_tier",
)


def _dec(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


class Finding:
    __slots__ = ("code", "severity", "org_id", "org_name", "message")

    def __init__(self, code: str, severity: str, org, message: str):
        self.code = code
        self.severity = severity  # "violation" | "warning"
        self.org_id = str(getattr(org, "id", org))
        self.org_name = getattr(org, "name", "—")
        self.message = message

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "organization_id": self.org_id,
            "organization_name": self.org_name,
            "message": self.message,
        }


def check_plan_stamp(org) -> list[Finding]:
    """P1–P5: the org's stamped config vs. what its plan allows."""
    out: list[Finding] = []
    code = (org.apex_plan_code or "").strip().lower()

    if not code:
        out.append(Finding(
            "P1", "violation", org,
            "APEX org has no apex_plan_code — it is billed by the LEGACY slab ladder and the "
            "1.5x wallet, not by any plan (concurrency falls back to Core's 1).",
        ))
        return out
    if code not in APEX_PLANS:
        out.append(Finding("P1", "violation", org, f"unknown plan code {code!r} (catalog: {sorted(APEX_PLANS)})"))
        return out

    plan = APEX_PLANS[code]

    missing = [c for c in _STAMPED if getattr(org, c, None) is None]
    if missing:
        out.append(Finding(
            "P2", "violation", org,
            f"plan '{code}' but NULL stamped column(s): {', '.join(missing)} — runtime falls back to the "
            "catalog (and enterprise has no catalog rate at all)",
        ))

    # P3 — fields that are identical for every org on the plan.
    for column, expected, label in (
        ("apex_included_minutes", plan.included_minutes, "included minutes"),
        ("apex_platform_fee_paise", plan.platform_fee_paise, "platform fee (paise)"),
        ("apex_support_tier", plan.support_tier, "support tier"),
    ):
        actual = getattr(org, column, None)
        if actual is not None and str(actual) != str(expected):
            out.append(Finding("P3", "violation", org, f"{label}: row={actual} but '{code}' is {expected}"))
    for column, expected, label in (
        ("apex_billed_bonus_pct", plan.billed_bonus_pct, "billed bonus %"),
        ("apex_topup_bonus_pct", plan.topup_bonus_pct, "top-up bonus %"),
    ):
        actual = _dec(getattr(org, column, None))
        if actual is not None and actual != Decimal(str(expected)):
            out.append(Finding("P3", "violation", org, f"{label}: row={actual} but '{code}' is {expected}"))

    # P4 — rate/concurrency, with the two sanctioned per-account deviations.
    rate = _dec(org.apex_rate_per_minute)
    conc = org.apex_concurrency
    if code == "enterprise":
        if rate is not None and not (Decimal("0") < rate < ENTERPRISE_RATE_CEILING):
            out.append(Finding("P4", "violation", org, f"enterprise rate ₹{rate} is outside (0, ₹{ENTERPRISE_RATE_CEILING})"))
        if conc is not None and int(conc) < ENTERPRISE_MIN_CONCURRENCY:
            out.append(Finding("P4", "violation", org, f"enterprise concurrency {conc} < {ENTERPRISE_MIN_CONCURRENCY}"))
    else:
        if rate is not None and rate != Decimal(str(plan.rate_per_minute)):
            out.append(Finding("P4", "violation", org, f"rate ₹{rate} but '{code}' is ₹{plan.rate_per_minute}"))
        if conc is not None:
            if code == "free_trial":
                if not (1 <= int(conc) <= TRIAL_MAX_CONCURRENCY):
                    out.append(Finding("P4", "violation", org, f"trial concurrency {conc} outside 1..{TRIAL_MAX_CONCURRENCY}"))
            elif int(conc) != int(plan.concurrency):
                out.append(Finding("P4", "violation", org, f"concurrency {conc} but '{code}' is {plan.concurrency}"))

    # P5 — a rate under the connect fee bills less than the call costs.
    if rate is not None and rate < APEX_CONNECT_FEE:
        out.append(Finding("P5", "violation", org, f"rate ₹{rate} is below the ₹{APEX_CONNECT_FEE} connect fee"))
    return out


def check_subscription(org, subs: list[Subscription]) -> list[Finding]:
    """S1: the recurring charge matches platform fee + included minutes x rate."""
    out: list[Finding] = []
    code = (org.apex_plan_code or "").strip().lower()
    plan = APEX_PLANS.get(code)
    if plan is None:
        return out

    apex_subs = [s for s in subs if (s.plan or "") == "apex"]
    live = [s for s in apex_subs if (s.status or "") in {"created", "active", "authenticated"}]

    if not plan.chargeable:
        billing = [s for s in live if int(s.amount_paise or 0) > 0]
        if billing:
            out.append(Finding(
                "S1", "violation", org,
                f"Free Trial but has a live chargeable subscription "
                f"({', '.join(f'{s.razorpay_subscription_id}=₹{int(s.amount_paise or 0) / 100:,.0f}' for s in billing)})",
            ))
        return out

    if org.apex_rate_per_minute is None:
        return out  # already reported by P2
    expected = monthly_subscription_paise(
        _dec(org.apex_rate_per_minute),
        int(org.apex_included_minutes or plan.included_minutes),
        int(org.apex_platform_fee_paise if org.apex_platform_fee_paise is not None else plan.platform_fee_paise),
    )
    if not apex_subs:
        # No subscription at all is only expected before the payment link is opened, and
        # never once the account is live.
        if (org.status or "") in {"active", "pending_activation"}:
            out.append(Finding(
                "S1", "warning", org,
                f"chargeable plan '{code}' is {org.status} but has no apex subscription row "
                f"(expected ₹{expected / 100:,.0f}/mo — comp'd or dev-created account?)",
            ))
        return out
    for sub in live or apex_subs:
        actual = int(sub.amount_paise or 0)
        if actual != expected:
            out.append(Finding(
                "S1", "violation", org,
                f"subscription {sub.razorpay_subscription_id} ({sub.status}) charges "
                f"₹{actual / 100:,.2f}/mo but '{code}' at the row's rate is ₹{expected / 100:,.2f}/mo",
            ))
        if int(sub.minutes or 0) and int(sub.minutes) != int(org.apex_included_minutes or 0):
            out.append(Finding(
                "S1", "violation", org,
                f"subscription {sub.razorpay_subscription_id} carries {sub.minutes} minutes but the org "
                f"is stamped for {org.apex_included_minutes}",
            ))
    return out


def check_wallet_grants(org, purchases: list[MinutePurchase]) -> list[Finding]:
    """W1: every credit equals minutes x rate x (1 + the bonus that source earns)."""
    out: list[Finding] = []
    code = (org.apex_plan_code or "").strip().lower()
    plan = APEX_PLANS.get(code)
    if plan is None or org.apex_rate_per_minute is None:
        return out

    billed_bonus = _dec(org.apex_billed_bonus_pct)
    topup_bonus = _dec(org.apex_topup_bonus_pct)
    if billed_bonus is None:
        billed_bonus = Decimal(str(plan.billed_bonus_pct))
    if topup_bonus is None:
        topup_bonus = Decimal(str(plan.topup_bonus_pct))

    for p in purchases:
        which = _GRANT_BONUS_FIELD.get((p.source or "").strip().lower())
        if which is None:
            # A legacy source ("onboarding") on a plan-driven org means it was credited by
            # the pre-plan path.
            out.append(Finding(
                "W1", "warning", org,
                f"wallet credit {p.id} has legacy source '{p.source}' ({p.minutes} min, ₹{p.rupees}) — "
                "credited outside the plan-driven path",
            ))
            continue
        bonus = {"billed": billed_bonus, "topup": topup_bonus, "none": Decimal("0")}[which]
        # Priced at the rate stamped ON THE PURCHASE (what the customer was actually
        # charged); flag when that rate no longer matches the org's plan rate.
        purchase_rate = _dec(p.rate_per_minute)
        expected = wallet_credit_for(int(p.minutes or 0), purchase_rate, bonus)
        actual = _dec(p.rupees)
        if actual is not None and actual != expected:
            out.append(Finding(
                "W1", "violation", org,
                f"{p.source} credit {p.id}: {p.minutes} min @ ₹{purchase_rate} +{bonus}% should be "
                f"₹{expected} but the ledger holds ₹{actual}",
            ))
        org_rate = _dec(org.apex_rate_per_minute)
        if purchase_rate is not None and org_rate is not None and purchase_rate != org_rate:
            out.append(Finding(
                "W1", "warning", org,
                f"{p.source} credit {p.id} was priced at ₹{purchase_rate}/min but the org is now on "
                f"₹{org_rate}/min (plan changed after the credit?)",
            ))
    return out


def check_status(org, purchases: list[MinutePurchase]) -> list[Finding]:
    """T1: the account's status is coherent with its plan."""
    out: list[Finding] = []
    code = (org.apex_plan_code or "").strip().lower()
    plan = APEX_PLANS.get(code)
    status = (org.status or "").strip().lower()
    if plan is None:
        return out

    if not plan.chargeable and status == "pending_payment":
        out.append(Finding("T1", "violation", org, "Free Trial is stuck in 'pending_payment' — a trial is never charged"))
    if status == "active" and org.apex_activated_at is None:
        out.append(Finding("T1", "warning", org, "status is 'active' but apex_activated_at is NULL (activated outside the APEX activate path)"))
    if status in {"active", "pending_activation"} and not purchases:
        out.append(Finding(
            "T1", "violation", org,
            f"status '{status}' but the Call Credits wallet was never credited — "
            f"'{code}' includes {plan.included_minutes} minutes",
        ))
    return out


def check_non_apex(org) -> list[Finding]:
    """X1: apex_* config on a non-APEX org."""
    stray = [c for c in ("apex_plan_code", *_STAMPED) if getattr(org, c, None) is not None]
    if not stray:
        return []
    return [Finding(
        "X1", "warning", org,
        f"product_tier={org.product_tier!r} but carries APEX plan config ({', '.join(stray)})",
    )]


async def audit(db: AsyncSession) -> list[Finding]:
    orgs = (await db.execute(select(Organization).order_by(Organization.created_at.asc()))).scalars().all()

    subs_by_org: dict[str, list[Subscription]] = {}
    for s in (await db.execute(select(Subscription))).scalars().all():
        subs_by_org.setdefault(str(s.organization_id), []).append(s)
    purch_by_org: dict[str, list[MinutePurchase]] = {}
    for p in (await db.execute(select(MinutePurchase).order_by(MinutePurchase.created_at.asc()))).scalars().all():
        purch_by_org.setdefault(str(p.organization_id), []).append(p)

    findings: list[Finding] = []
    for org in orgs:
        oid = str(org.id)
        if (org.product_tier or "") != APEX_TIER:
            findings += check_non_apex(org)
            continue
        purchases = purch_by_org.get(oid, [])
        findings += check_plan_stamp(org)
        findings += check_subscription(org, subs_by_org.get(oid, []))
        findings += check_wallet_grants(org, purchases)
        findings += check_status(org, purchases)
    return findings


def _summarize(orgs_total: int, apex_total: int, findings: list[Finding], as_json: bool) -> int:
    violations = [f for f in findings if f.severity == "violation"]
    warnings = [f for f in findings if f.severity == "warning"]
    if as_json:
        print(json.dumps({
            "organizations": orgs_total,
            "apex_organizations": apex_total,
            "violations": [f.as_dict() for f in violations],
            "warnings": [f.as_dict() for f in warnings],
        }, indent=2))
        return 1 if violations else 0

    print(f"\nAPEX plan compliance — {apex_total} APEX org(s) of {orgs_total} total\n" + "─" * 78)
    if not findings:
        print("✅ every account matches its plan's rules.")
        return 0
    for label, group in (("VIOLATION", violations), ("warning", warnings)):
        if not group:
            continue
        print(f"\n{label}S ({len(group)})" if label == "VIOLATION" else f"\n{label}s ({len(group)})")
        by_org: dict[tuple[str, str], list[Finding]] = {}
        for f in group:
            by_org.setdefault((f.org_name, f.org_id), []).append(f)
        for (name, oid), items in by_org.items():
            print(f"  {name}  [{oid}]")
            for f in items:
                print(f"    [{f.code}] {f.message}")
    print("\n" + "─" * 78)
    print(f"{len(violations)} violation(s), {len(warnings)} warning(s)")
    return 1 if violations else 0


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", help="env file to load (default: the app's .env)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    eng = create_async_engine(settings.SQLALCHEMY_DATABASE_URI)
    Session = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    try:
        async with Session() as db:
            orgs = (await db.execute(select(Organization.id, Organization.product_tier))).all()
            findings = await audit(db)
    finally:
        await eng.dispose()

    if not args.json:
        print(f"database: {settings.POSTGRES_SERVER}/{settings.POSTGRES_DB}")
    return _summarize(len(orgs), sum(1 for _, t in orgs if (t or "") == APEX_TIER), findings, args.json)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
