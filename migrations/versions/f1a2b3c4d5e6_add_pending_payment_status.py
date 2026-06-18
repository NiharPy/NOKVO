"""Allow organizations.status = 'pending_payment' (payment-gated onboarding).

Revision ID: f1a2b3c4d5e6
Revises: e7c4f1a9b2d3
Create Date: 2026-06-18 00:30:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "e7c4f1a9b2d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALL = [
    "pending_payment",
    "pending_email_verification",
    "pending_totp",
    "pending_approval",
    "active",
    "suspended",
]
_OLD = _ALL[1:]  # without pending_payment


def _set_check(values: list[str]) -> None:
    joined = ", ".join(f"'{v}'" for v in values)
    op.execute("ALTER TABLE organizations DROP CONSTRAINT IF EXISTS ck_organizations_status")
    op.execute(
        f"ALTER TABLE organizations ADD CONSTRAINT ck_organizations_status "
        f"CHECK (status IN ({joined}))"
    )


def upgrade() -> None:
    _set_check(_ALL)


def downgrade() -> None:
    _set_check(_OLD)
