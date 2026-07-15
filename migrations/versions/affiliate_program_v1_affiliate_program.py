"""Affiliate program — affiliates, commission ledger, settlement batches.

Public affiliate signup (18+, TOTP-only login via affiliate number);
APEX customers enter the number at payment; the affiliate earns 5% of the
first invoice (platform fee + minutes addon) and 2% of every later monthly
subscription charge (top-ups excluded). Settlement is operator-marked manual
payout (T+2 due queue in the SuperAdmin console, UTR recorded) gated on an
operator KYC approval (no document collected — verified out-of-band).

Adds ``affiliates``, ``affiliate_settlements``, ``affiliate_commissions`` and
attribution columns on ``organizations``.

Revision ID: affiliate_program_v1
Revises: platform_settings_v1
Create Date: 2026-07-13 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "affiliate_program_v1"
down_revision: Union[str, Sequence[str], None] = "platform_settings_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    insp = inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    if not _has_table("affiliates"):
        op.create_table(
            "affiliates",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("affiliate_number", sa.String(), nullable=False),
            sa.Column("full_name", sa.String(), nullable=False),
            sa.Column("date_of_birth", sa.Date(), nullable=False),
            sa.Column("email", sa.String(), nullable=False),
            sa.Column("totp_secret_encrypted_v2", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="pending_totp"),
            sa.Column("kyc_verified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("kyc_verified_by", sa.String(), nullable=True),
            sa.Column("bank_account_holder", sa.String(), nullable=True),
            sa.Column("bank_account_number", sa.String(), nullable=True),
            sa.Column("bank_ifsc", sa.String(), nullable=True),
            sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index(
            "ix_affiliates_affiliate_number", "affiliates", ["affiliate_number"], unique=True
        )
        op.create_index("ix_affiliates_email", "affiliates", ["email"], unique=True)

    if not _has_table("affiliate_settlements"):
        op.create_table(
            "affiliate_settlements",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "affiliate_id",
                UUID(as_uuid=True),
                sa.ForeignKey("affiliates.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("amount_rupees", sa.Numeric(14, 4), nullable=False),
            sa.Column("commission_count", sa.Integer(), nullable=False),
            sa.Column("utr_reference", sa.String(), nullable=False),
            sa.Column("settled_by", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index(
            "ix_affiliate_settlements_affiliate_id", "affiliate_settlements", ["affiliate_id"]
        )

    if not _has_table("affiliate_commissions"):
        op.create_table(
            "affiliate_commissions",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "affiliate_id",
                UUID(as_uuid=True),
                sa.ForeignKey("affiliates.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "organization_id",
                UUID(as_uuid=True),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("commission_type", sa.String(), nullable=False),
            sa.Column("billed_paise", sa.Integer(), nullable=False),
            sa.Column("rate", sa.Numeric(5, 4), nullable=False),
            sa.Column("amount_rupees", sa.Numeric(14, 4), nullable=False),
            sa.Column("razorpay_payment_id", sa.String(), nullable=False),
            sa.Column("razorpay_subscription_id", sa.String(), nullable=False),
            sa.Column("raw_event", JSONB(), nullable=True),
            sa.Column(
                "settlement_id",
                UUID(as_uuid=True),
                sa.ForeignKey("affiliate_settlements.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint(
                "razorpay_payment_id", name="uq_affiliate_commissions_rzp_payment_id"
            ),
        )
        op.create_index(
            "ix_affiliate_commissions_affiliate_id", "affiliate_commissions", ["affiliate_id"]
        )
        op.create_index(
            "ix_affiliate_commissions_organization_id",
            "affiliate_commissions",
            ["organization_id"],
        )
        op.create_index(
            "ix_affiliate_commissions_razorpay_subscription_id",
            "affiliate_commissions",
            ["razorpay_subscription_id"],
        )
        op.create_index(
            "ix_affiliate_commissions_settlement_id", "affiliate_commissions", ["settlement_id"]
        )
        op.create_index(
            "ix_affiliate_commissions_affiliate_created",
            "affiliate_commissions",
            ["affiliate_id", "created_at"],
        )
        op.create_index(
            "ix_affiliate_commissions_affiliate_unsettled",
            "affiliate_commissions",
            ["affiliate_id", "created_at"],
            postgresql_where=sa.text("settlement_id IS NULL"),
        )

    if not _has_column("organizations", "affiliate_id"):
        op.add_column(
            "organizations",
            sa.Column(
                "affiliate_id",
                UUID(as_uuid=True),
                sa.ForeignKey("affiliates.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
        op.create_index("ix_organizations_affiliate_id", "organizations", ["affiliate_id"])
    if not _has_column("organizations", "affiliate_code_used"):
        op.add_column("organizations", sa.Column("affiliate_code_used", sa.String(), nullable=True))


def downgrade() -> None:
    if _has_column("organizations", "affiliate_code_used"):
        op.drop_column("organizations", "affiliate_code_used")
    if _has_column("organizations", "affiliate_id"):
        op.drop_index("ix_organizations_affiliate_id", table_name="organizations")
        op.drop_column("organizations", "affiliate_id")
    if _has_table("affiliate_commissions"):
        op.drop_table("affiliate_commissions")
    if _has_table("affiliate_settlements"):
        op.drop_table("affiliate_settlements")
    if _has_table("affiliates"):
        op.drop_table("affiliates")
