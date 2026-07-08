"""Platform-wide runtime settings (managed from the SuperAdmin console).

A tiny key/value table for the handful of operator-tunable knobs that must
change WITHOUT a redeploy — the first being ``usd_to_inr``, the FX rate every
per-call COGS calculation converts vendor USD list prices with. Values are
stored as strings and parsed by the reading service
(:mod:`app.services.platform_settings`); a missing row means "use the
``settings`` default". Each instance's background refresher folds changes into
the in-process ``settings`` object, mirroring how ``llm_pool_keys`` changes
propagate.
"""
from __future__ import annotations

from sqlalchemy import Column, DateTime, String
from sqlalchemy.sql import func

from app.db.session import Base


class PlatformSetting(Base):
    __tablename__ = "platform_settings"

    key = Column(String(64), primary_key=True)
    value = Column(String(256), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    # Audit: which superadmin changed it last (email, not FK — survives user deletion).
    updated_by = Column(String(320), nullable=True)
