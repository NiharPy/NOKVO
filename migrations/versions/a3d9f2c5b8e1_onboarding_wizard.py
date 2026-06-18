"""Post-payment onboarding wizard: business/legal fields, consent timestamps,
onboarding_step, and the 'onboarding' organization status.

Revision ID: a3d9f2c5b8e1
Revises: f1a2b3c4d5e6
Create Date: 2026-06-18 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a3d9f2c5b8e1"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Allowed organization.status values, now including the wizard stage.
_ALL = [
    "pending_payment",
    "onboarding",
    "pending_email_verification",
    "pending_totp",
    "pending_approval",
    "active",
    "suspended",
]
_OLD = [v for v in _ALL if v != "onboarding"]

_COLUMNS = [
    ("legal_name", sa.String()),
    ("alias_name", sa.String()),
    ("business_pan", sa.String()),
    ("cin", sa.String()),
    ("terms_accepted_at", sa.DateTime(timezone=True)),
    ("privacy_accepted_at", sa.DateTime(timezone=True)),
    ("terms_version", sa.String()),
    ("onboarding_step", sa.String()),
]


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return any(c["name"] == column for c in insp.get_columns(table))


def _set_check(values: list[str]) -> None:
    joined = ", ".join(f"'{v}'" for v in values)
    op.execute("ALTER TABLE organizations DROP CONSTRAINT IF EXISTS ck_organizations_status")
    op.execute(
        f"ALTER TABLE organizations ADD CONSTRAINT ck_organizations_status "
        f"CHECK (status IN ({joined}))"
    )


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        if not _has_column("organizations", name):
            op.add_column("organizations", sa.Column(name, type_, nullable=True))
    _set_check(_ALL)


def downgrade() -> None:
    _set_check(_OLD)
    for name, _ in _COLUMNS:
        if _has_column("organizations", name):
            op.drop_column("organizations", name)
