"""Switch Plivo columns to Twilio

Revision ID: f13d1f7c9a21
Revises: d41a9d9d2ef0
Create Date: 2026-04-30 11:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = "f13d1f7c9a21"
down_revision: Union[str, Sequence[str], None] = "d41a9d9d2ef0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    cols = {col["name"] for col in inspector.get_columns(table_name)}
    return column_name in cols


def upgrade() -> None:
    if _has_column("organizations", "plivo_auto_provision") and not _has_column("organizations", "twilio_auto_provision"):
        op.add_column(
            "organizations",
            sa.Column("twilio_auto_provision", sa.Boolean(), nullable=True, server_default=sa.false()),
        )
        op.execute(
            "UPDATE organizations SET twilio_auto_provision = COALESCE(plivo_auto_provision, false)"
        )
        op.alter_column("organizations", "twilio_auto_provision", nullable=False, server_default=sa.false())
        op.drop_column("organizations", "plivo_auto_provision")
    elif not _has_column("organizations", "twilio_auto_provision"):
        op.add_column(
            "organizations",
            sa.Column("twilio_auto_provision", sa.Boolean(), nullable=False, server_default=sa.false()),
        )

    if _has_column("tenant_resources", "plivo_status") and not _has_column("tenant_resources", "twilio_status"):
        op.add_column("tenant_resources", sa.Column("twilio_status", sa.String(), nullable=True))
        op.execute("UPDATE tenant_resources SET twilio_status = plivo_status WHERE plivo_status IS NOT NULL")
        op.drop_column("tenant_resources", "plivo_status")
    elif not _has_column("tenant_resources", "twilio_status"):
        op.add_column("tenant_resources", sa.Column("twilio_status", sa.String(), nullable=True))

    if _has_column("tenant_resources", "plivo_phone_number") and not _has_column("tenant_resources", "twilio_phone_number"):
        op.add_column("tenant_resources", sa.Column("twilio_phone_number", sa.String(), nullable=True))
        op.execute(
            "UPDATE tenant_resources SET twilio_phone_number = plivo_phone_number WHERE plivo_phone_number IS NOT NULL"
        )
        op.drop_column("tenant_resources", "plivo_phone_number")
    elif not _has_column("tenant_resources", "twilio_phone_number"):
        op.add_column("tenant_resources", sa.Column("twilio_phone_number", sa.String(), nullable=True))


def downgrade() -> None:
    if _has_column("organizations", "twilio_auto_provision") and not _has_column("organizations", "plivo_auto_provision"):
        op.add_column(
            "organizations",
            sa.Column("plivo_auto_provision", sa.Boolean(), nullable=True, server_default=sa.false()),
        )
        op.execute(
            "UPDATE organizations SET plivo_auto_provision = COALESCE(twilio_auto_provision, false)"
        )
        op.drop_column("organizations", "twilio_auto_provision")

    if _has_column("tenant_resources", "twilio_status") and not _has_column("tenant_resources", "plivo_status"):
        op.add_column("tenant_resources", sa.Column("plivo_status", sa.String(), nullable=True))
        op.execute("UPDATE tenant_resources SET plivo_status = twilio_status WHERE twilio_status IS NOT NULL")
        op.drop_column("tenant_resources", "twilio_status")

    if _has_column("tenant_resources", "twilio_phone_number") and not _has_column("tenant_resources", "plivo_phone_number"):
        op.add_column("tenant_resources", sa.Column("plivo_phone_number", sa.String(), nullable=True))
        op.execute(
            "UPDATE tenant_resources SET plivo_phone_number = twilio_phone_number WHERE twilio_phone_number IS NOT NULL"
        )
        op.drop_column("tenant_resources", "twilio_phone_number")
