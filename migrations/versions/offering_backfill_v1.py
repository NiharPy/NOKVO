"""offering_backfill_v1 — mirror existing projects/services into `offerings`.

Stage A of the P2 Option-B rollout: copies every ``real_estate_projects`` row
(kind='project') and ``clinic_services`` row (kind='service') into the generic
``offerings`` catalog, sharing the source UUID so the adapters round-trip by id.
Idempotent (``ON CONFLICT (id) DO NOTHING``) and guarded on table existence.
The column/JSONB mapping mirrors ``offering_adapters.*_to_offering_row`` exactly
(``price_*::text`` matches ``str(Decimal)``) so the byte-parity tests hold.

Revision ID: offering_backfill_v1
Revises: offering_catalog_v1
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "offering_backfill_v1"
down_revision: Union[str, Sequence[str], None] = "offering_catalog_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_RE_SQL = """
INSERT INTO offerings (
  id, organization_id, kind, name, category, description, price, price_display, duration_min,
  attributes, media, availability, provider_ids, status, created_by_user_id, created_at, updated_at
)
SELECT
  id, organization_id, 'project', name, property_type, description, price_min, price_display, NULL,
  jsonb_build_object(
    'location', location,
    'rera_number', rera_number,
    'property_type', property_type,
    'price_min', CASE WHEN price_min IS NULL THEN NULL ELSE price_min::text END,
    'price_max', CASE WHEN price_max IS NULL THEN NULL ELSE price_max::text END,
    'configurations', COALESCE(configurations, '[]'::jsonb),
    'amenities', COALESCE(amenities, '[]'::jsonb),
    'possession_date', possession_date,
    'builder_name', builder_name,
    'contact_phone', contact_phone,
    'extra', COALESCE(extra, '{}'::jsonb)
  ),
  jsonb_build_object('brochure_url', brochure_url, 'whatsapp', COALESCE(whatsapp, '{}'::jsonb)),
  '{}'::jsonb, '[]'::jsonb, status, created_by_user_id, created_at, updated_at
FROM real_estate_projects
ON CONFLICT (id) DO NOTHING;
"""

_CLINIC_SQL = """
INSERT INTO offerings (
  id, organization_id, kind, name, category, description, price, price_display, duration_min,
  attributes, media, availability, provider_ids, status, created_by_user_id, created_at, updated_at
)
SELECT
  id, organization_id, 'service', name, department, description, price, price_display, duration_minutes,
  '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, '[]'::jsonb,
  CASE WHEN is_active THEN 'active' ELSE 'inactive' END,
  created_by_user_id, created_at, updated_at
FROM clinic_services
ON CONFLICT (id) DO NOTHING;
"""


def upgrade() -> None:
    tables = set(inspect(op.get_bind()).get_table_names())
    if "offerings" not in tables:
        return
    if "real_estate_projects" in tables:
        op.execute(sa.text(_RE_SQL))
    if "clinic_services" in tables:
        op.execute(sa.text(_CLINIC_SQL))


def downgrade() -> None:
    # Revert the mirror; source tables are untouched.
    if "offerings" in set(inspect(op.get_bind()).get_table_names()):
        op.execute(sa.text("DELETE FROM offerings WHERE kind IN ('project', 'service');"))
