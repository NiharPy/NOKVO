"""Re-warm the TTS byte-cache for existing APEX campaigns.

The byte-cache key includes every voice parameter that changes the audio —
text, speaker, model, sample rate, PACE, temperature, rendition variant. That
means several legitimate changes rotate the keys of every already-warmed
scripted line and the next call per line/language pays live synthesis:

  * prosody tuning (app/services/prosody.py pace deltas),
  * a tenant ``provider_status.sarvam_tts_temperature`` / speaker change,
  * enabling rendition variants (``APEX_TTS_VARIANTS`` > 1) or the pace
    spread knob,
  * enabling micro-acks (``APEX_ACK_ENABLED`` — the ack pool wants warming),
  * new/changed style ack pools (``apex_micro_acks.STYLE_ACK_POOLS`` — each
    campaign warms the pool for its ``questionnaire.style``).

Run this right after deploying any of those to re-fill the cache for every
ACTIVE campaign (running/scheduled/paused — completed ones don't dial again).
Delegates to ``prewarm_campaign_tts``, which probes the cache before each
synthesis, so re-runs only pay for keys that are actually cold. Idempotent,
best-effort, safe to re-run.

Run from repo root:
    source venv/bin/activate
    python3 scripts/apex_reprewarm.py                     # all active campaigns
    python3 scripts/apex_reprewarm.py --campaign <id> …   # specific campaign(s)
    python3 scripts/apex_reprewarm.py --all               # include completed too
"""
from __future__ import annotations

import asyncio
import logging
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.outbound_campaign import OutboundCampaign
from app.models.tenant_resources import TenantResources
from app.services.apex_tts_prewarm import prewarm_campaign_tts

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("apex-reprewarm")

_ACTIVE_STATUSES = {"running", "scheduled", "paused", "created"}


async def main() -> int:
    args = sys.argv[1:]
    include_all = "--all" in args
    wanted_ids: set[str] = set()
    for i, a in enumerate(args):
        if a == "--campaign" and i + 1 < len(args):
            wanted_ids.add(args[i + 1])

    eng = create_async_engine(settings.SQLALCHEMY_DATABASE_URI)
    Session = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    scanned = warmed = skipped = failed = 0
    async with Session() as db:
        rows = (await db.execute(select(OutboundCampaign))).scalars().all()
        tenants: dict[str, TenantResources | None] = {}
        for c in rows:
            if wanted_ids and str(c.id) not in wanted_ids:
                continue
            status = str(getattr(c, "status", "") or "").lower()
            if not include_all and not wanted_ids and status not in _ACTIVE_STATUSES:
                continue
            cfg = dict(c.agent_config or {})
            questionnaire = cfg.get("questionnaire")
            if not isinstance(questionnaire, dict) or not questionnaire.get("questions"):
                continue
            scanned += 1
            org_key = str(c.organization_id)
            if org_key not in tenants:
                tenants[org_key] = (
                    await db.execute(
                        select(TenantResources).where(
                            TenantResources.organization_id == c.organization_id
                        )
                    )
                ).scalars().first()
            tr = tenants[org_key]
            if tr is None:
                skipped += 1
                log.warning("campaign %s (%s): no TenantResources — skipped", c.id, c.name)
                continue
            try:
                # Serial + cache-probe-first inside: only cold keys hit Sarvam.
                await prewarm_campaign_tts(tr, questionnaire)
                warmed += 1
                log.info("campaign %s (%s): re-warmed", c.id, c.name)
            except Exception:
                failed += 1
                log.warning("campaign %s (%s): re-warm failed", c.id, c.name, exc_info=True)
    await eng.dispose()

    log.info(
        "done: scanned=%d warmed=%d skipped=%d failed=%d",
        scanned, warmed, skipped, failed,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
