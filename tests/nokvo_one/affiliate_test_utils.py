"""Shared fakes for the affiliate-program unit tests (no live Postgres).

FakeDB evaluates the simple equality / IS NULL / <= predicates the affiliate
service and endpoints actually issue, against in-memory lists of REAL model
instances — so the queries under test run unmodified.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql import operators as sqlops
from sqlalchemy.sql.elements import BinaryExpression, BooleanClauseList

from app.models.affiliate import Affiliate
from app.models.affiliate_commission import AffiliateCommission, AffiliateSettlement
from app.models.organization import Organization


class FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


def _predicates(stmt):
    preds = []

    def walk(node):
        if node is None:
            return
        if isinstance(node, BooleanClauseList):
            for clause in node.clauses:
                walk(clause)
        elif isinstance(node, BinaryExpression):
            preds.append(node)

    walk(getattr(stmt, "whereclause", None))
    return preds


def _matches(row, pred) -> bool:
    key = pred.left.key
    value = getattr(row, key, None)
    rv = getattr(pred.right, "value", None)
    op = pred.operator
    if op is sqlops.is_:
        return value is None
    if op is sqlops.is_not:
        return value is not None
    if op is sqlops.eq:
        return value == rv
    if op is sqlops.le:
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value is not None and value <= rv
    if op is sqlops.ge:
        return value is not None and value >= rv
    raise NotImplementedError(f"FakeDB can't evaluate operator {op}")


class FakeDB:
    def __init__(self, *, affiliates=None, orgs=None, commissions=None, pools=None):
        self.affiliates = list(affiliates or [])
        self.orgs = list(orgs or [])
        self.commissions = list(commissions or [])
        self.settlements = []
        # Extra {Model: [rows]} pools for tests outside the affiliate models
        # (e.g. Subscription / MinutePurchase in the payment-dedupe tests).
        self.pools = {k: list(v) for k, v in (pools or {}).items()}
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self.raise_integrity_on_commit = 0

    def _pool_for(self, model):
        if model in self.pools:
            return self.pools[model]
        return {
            Affiliate: self.affiliates,
            Organization: self.orgs,
            AffiliateCommission: self.commissions,
            AffiliateSettlement: self.settlements,
        }.get(model)

    async def get(self, model, pk):
        for row in self._pool_for(model) or []:
            if getattr(row, "id", None) == pk:
                return row
        return None

    async def execute(self, stmt):
        desc = stmt.column_descriptions[0]
        entity = desc.get("entity")
        preds = _predicates(stmt)
        is_count = "count(" in str(stmt).lower()
        pool = self._pool_for(entity)
        if pool is None:
            raise NotImplementedError(f"FakeDB has no pool for {entity}")
        rows = [r for r in pool if all(_matches(r, p) for p in preds)]
        if is_count:
            return FakeResult([len(rows)])
        return FakeResult(rows)

    def add(self, obj):
        self.added.append(obj)
        pool = self._pool_for(type(obj))
        if pool is not None and obj not in pool:
            pool.append(obj)

    async def commit(self):
        if self.raise_integrity_on_commit > 0:
            self.raise_integrity_on_commit -= 1
            raise IntegrityError("duplicate", None, Exception("duplicate"))
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def refresh(self, obj):
        return None

    async def flush(self):
        return None


def make_affiliate(**overrides) -> Affiliate:
    fields = dict(
        id=uuid.uuid4(),
        affiliate_number="NKV7XQ2MRT",
        full_name="Priya Sharma",
        date_of_birth=datetime(1995, 5, 5).date(),
        email="priya@example.com",
        totp_secret_encrypted_v2=None,
        status="active",
        kyc_verified_at=None,
        kyc_verified_by=None,
        bank_account_holder=None,
        bank_account_number=None,
        bank_ifsc=None,
        created_at=datetime.now(timezone.utc),
    )
    fields.update(overrides)
    return Affiliate(**fields)


def make_org(**overrides) -> Organization:
    fields = dict(
        id=uuid.uuid4(),
        name="Acme Realty",
        region="southindia",
        environment="prod",
        product_tier="nokvo_apex",
        status="pending_payment",
        affiliate_id=None,
        created_at=datetime.now(timezone.utc),
    )
    fields.update(overrides)
    return Organization(**fields)


def make_commission(**overrides) -> AffiliateCommission:
    fields = dict(
        id=uuid.uuid4(),
        affiliate_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        commission_type="first_month",
        billed_paise=649900,
        rate=0.05,
        amount_rupees=324.95,
        razorpay_payment_id=f"pay_{uuid.uuid4().hex[:12]}",
        razorpay_subscription_id="sub_test",
        settlement_id=None,
        created_at=datetime.now(timezone.utc),
    )
    fields.update(overrides)
    return AffiliateCommission(**fields)


class FakeRequest:
    """Enough of a Request for endpoints whose limiter is disabled in tests."""

    client = None
    headers: dict = {}
