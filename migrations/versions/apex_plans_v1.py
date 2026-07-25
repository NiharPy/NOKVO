"""APEX plan-driven pricing + request-gated onboarding.

Additive schema for the NOKVO APEX plan model:

* ``organizations`` gains the stamped plan config (plan code, rate, concurrency,
  included minutes, platform fee, bonus %s, support tier, activation timestamp).
  Existing APEX rows are back-filled to the **Core** plan so no runtime path reads
  NULL; a CHECK constraint keeps ``apex_plan_code`` to the known codes.
* ``minute_purchases.razorpay_invoice_id`` + a PARTIAL UNIQUE index on
  ``(organization_id, razorpay_invoice_id)`` — per-cycle idempotency for the APEX
  monthly minute grant (a webhook retry can't double-credit a cycle).
* ``apex_access_requests`` — the public "request access" queue (replaces signup).
* ``razorpay_webhook_events`` — durable webhook audit for replay/resync.

All steps are guarded/idempotent and additive, safe to run under live old code.

Revision ID: apex_plans_v1
Revises: apex_crm_api_v1
Create Date: 2026-07-25 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision: str = "apex_plans_v1"
down_revision: Union[str, Sequence[str], None] = "apex_crm_api_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _insp():
    return inspect(op.get_bind())


def _cols(table: str) -> set[str]:
    ins = _insp()
    if table not in ins.get_table_names():
        return set()
    return {c["name"] for c in ins.get_columns(table)}


def _indexes(table: str) -> set[str]:
    ins = _insp()
    if table not in ins.get_table_names():
        return set()
    return {i["name"] for i in ins.get_indexes(table)}


def _check_constraints(table: str) -> set[str]:
    ins = _insp()
    if table not in ins.get_table_names():
        return set()
    return {c["name"] for c in ins.get_check_constraints(table)}


# The stamped plan-config columns and their types.
_ORG_APEX_COLUMNS = [
    ("apex_plan_code", sa.String()),
    ("apex_rate_per_minute", sa.Numeric(6, 2)),
    ("apex_concurrency", sa.Integer()),
    ("apex_included_minutes", sa.Integer()),
    ("apex_platform_fee_paise", sa.Integer()),
    ("apex_billed_bonus_pct", sa.Numeric(5, 2)),
    ("apex_topup_bonus_pct", sa.Numeric(5, 2)),
    ("apex_support_tier", sa.String()),
    ("apex_activated_at", sa.DateTime(timezone=True)),
]


def upgrade() -> None:
    orgs = "organizations"
    existing = _cols(orgs)
    for name, type_ in _ORG_APEX_COLUMNS:
        if name not in existing:
            op.add_column(orgs, sa.Column(name, type_, nullable=True))

    # Back-fill any existing APEX org to the Core plan defaults (never leave NULL for a
    # live account whose code assumes a concrete rate/concurrency).
    op.execute(
        sa.text(
            """
            UPDATE organizations
            SET apex_plan_code = 'core',
                apex_rate_per_minute = 9,
                apex_concurrency = 1,
                apex_included_minutes = 1000,
                apex_platform_fee_paise = 449900,
                apex_billed_bonus_pct = 0,
                apex_topup_bonus_pct = 0,
                apex_support_tier = 'basic_onboarding'
            WHERE product_tier = 'nokvo_apex' AND apex_plan_code IS NULL
            """
        )
    )

    if "ck_organizations_apex_plan_code" not in _check_constraints(orgs):
        op.create_check_constraint(
            "ck_organizations_apex_plan_code",
            orgs,
            "apex_plan_code IS NULL OR apex_plan_code IN "
            "('free_trial','core','growth','pinnacle','enterprise')",
        )

    # Extend the org status CHECK to allow the new APEX 'pending_activation' state
    # (paid, awaiting SuperAdmin activation). Recreate the constraint with the full set.
    if "ck_organizations_status" in _check_constraints(orgs):
        op.drop_constraint("ck_organizations_status", orgs, type_="check")
    op.create_check_constraint(
        "ck_organizations_status",
        orgs,
        "status IN ('pending_payment','onboarding','pending_email_verification',"
        "'pending_totp','pending_approval','pending_activation','active','suspended')",
    )

    # ── minute_purchases: per-cycle idempotency for the APEX monthly grant ──
    mp = "minute_purchases"
    if "razorpay_invoice_id" not in _cols(mp):
        op.add_column(mp, sa.Column("razorpay_invoice_id", sa.String(), nullable=True))
    if "uq_minute_purchases_org_invoice" not in _indexes(mp):
        op.create_index(
            "uq_minute_purchases_org_invoice",
            mp,
            ["organization_id", "razorpay_invoice_id"],
            unique=True,
            postgresql_where=sa.text("razorpay_invoice_id IS NOT NULL"),
        )

    # ── apex_access_requests ──
    if "apex_access_requests" not in _insp().get_table_names():
        op.create_table(
            "apex_access_requests",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("company_name", sa.String(), nullable=True),
            sa.Column("contact_name", sa.String(), nullable=True),
            sa.Column("email", sa.String(), nullable=False),
            sa.Column("phone", sa.String(), nullable=True),
            sa.Column("requested_plan", sa.String(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="new"),
            sa.Column("status_updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "converted_org_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("organizations.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_apex_access_requests_email", "apex_access_requests", ["email"])
        op.create_index(
            "ix_apex_access_requests_status_created",
            "apex_access_requests",
            ["status", "created_at"],
        )

    # ── razorpay_webhook_events ──
    if "razorpay_webhook_events" not in _insp().get_table_names():
        op.create_table(
            "razorpay_webhook_events",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("event_id", sa.String(), nullable=True),
            sa.Column("event_type", sa.String(), nullable=True),
            sa.Column("subscription_id", sa.String(), nullable=True),
            sa.Column("payment_id", sa.String(), nullable=True),
            sa.Column("invoice_id", sa.String(), nullable=True),
            sa.Column(
                "organization_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("organizations.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("status", sa.String(), nullable=False, server_default="received"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_razorpay_webhook_events_event_id", "razorpay_webhook_events", ["event_id"])
        op.create_index("ix_razorpay_webhook_events_subscription_id", "razorpay_webhook_events", ["subscription_id"])
        op.create_index("ix_razorpay_webhook_events_organization_id", "razorpay_webhook_events", ["organization_id"])
        op.create_index(
            "ix_razorpay_webhook_events_status_created",
            "razorpay_webhook_events",
            ["status", "created_at"],
        )


def downgrade() -> None:
    if "razorpay_webhook_events" in _insp().get_table_names():
        op.drop_table("razorpay_webhook_events")
    if "apex_access_requests" in _insp().get_table_names():
        op.drop_table("apex_access_requests")

    mp = "minute_purchases"
    if "uq_minute_purchases_org_invoice" in _indexes(mp):
        op.drop_index("uq_minute_purchases_org_invoice", table_name=mp)
    if "razorpay_invoice_id" in _cols(mp):
        op.drop_column(mp, "razorpay_invoice_id")

    orgs = "organizations"
    # Restore the status CHECK to its pre-APEX set (drop 'pending_activation'). First move any
    # org still holding the new status to a value the restored constraint allows, else the
    # CHECK recreation would fail.
    if "ck_organizations_status" in _check_constraints(orgs):
        op.execute(
            sa.text("UPDATE organizations SET status='pending_payment' WHERE status='pending_activation'")
        )
        op.drop_constraint("ck_organizations_status", orgs, type_="check")
        op.create_check_constraint(
            "ck_organizations_status",
            orgs,
            "status IN ('pending_payment','onboarding','pending_email_verification',"
            "'pending_totp','pending_approval','active','suspended')",
        )
    if "ck_organizations_apex_plan_code" in _check_constraints(orgs):
        op.drop_constraint("ck_organizations_apex_plan_code", orgs, type_="check")
    existing = _cols(orgs)
    for name, _type in reversed(_ORG_APEX_COLUMNS):
        if name in existing:
            op.drop_column(orgs, name)
