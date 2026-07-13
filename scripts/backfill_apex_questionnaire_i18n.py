"""One-off backfill: pre-translate existing campaign questionnaires into en/hi/te.

APEX deterministic campaigns speak each question VERBATIM from a per-language
string stored on the questionnaire (``text_i18n`` / ``outro_i18n``). Those are
written once at campaign CREATION (see ``questionnaire_translation`` wired in
``nokvo_one_voice.py``). Campaigns created before that — or where the one-shot
translation failed — have no i18n, so at call time ``next_verbatim_question``
falls back to the LLM-rephrase flow (functionally fine, but not verbatim and no
TTS-cache hit). Run this ONCE at the ``APEX_VERBATIM_QUESTIONS_ENABLED`` rollout
to fill them in.

Idempotent + best-effort: ``translate_questionnaire`` skips questionnaires that
already carry i18n and never raises, so re-runs are cheap and safe. A campaign is
only written back when its questionnaire actually changed.

Run from repo root:
    source venv/bin/activate
    python3 scripts/backfill_apex_questionnaire_i18n.py            # apply
    python3 scripts/backfill_apex_questionnaire_i18n.py --dry-run  # report only

``--force-retranslate`` clears every machine-stored i18n first and re-runs the
translation — for adopting the spoken-register translation prompt
(APEX_I18N_SPOKEN_REGISTER) on campaigns translated before it existed. WARNING:
hand-edited translations are indistinguishable from machine ones and are LOST
under --force. Combine with ``--campaign <id>`` to scope it. After a forced
re-translation run scripts/apex_reprewarm.py — new strings mean cold TTS keys.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.models.outbound_campaign import OutboundCampaign
from app.services.questionnaire_translation import translate_questionnaire

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("apex-i18n-backfill")


def _canon(questionnaire: dict) -> str:
    return json.dumps(questionnaire, sort_keys=True, ensure_ascii=False)


def _clear_i18n(questionnaire: dict) -> None:
    """--force-retranslate: drop every stored translation so
    ``translate_questionnaire`` regenerates all of them (hand-edits included —
    see the module docstring warning)."""
    for q in questionnaire.get("questions") or []:
        q.pop("text_i18n", None)
    questionnaire.pop("intro_i18n", None)
    questionnaire.pop("outro_i18n", None)


async def main() -> int:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    force = "--force-retranslate" in args
    wanted_ids = {args[i + 1] for i, a in enumerate(args) if a == "--campaign" and i + 1 < len(args)}
    eng = create_async_engine(settings.SQLALCHEMY_DATABASE_URI)
    Session = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    scanned = updated = skipped = failed = 0
    async with Session() as db:
        rows = (await db.execute(select(OutboundCampaign))).scalars().all()
        for c in rows:
            if wanted_ids and str(c.id) not in wanted_ids:
                continue
            cfg = dict(c.agent_config or {})
            questionnaire = cfg.get("questionnaire")
            if not isinstance(questionnaire, dict) or not questionnaire.get("questions"):
                continue
            scanned += 1
            before = _canon(questionnaire)
            try:
                if force:
                    _clear_i18n(questionnaire)
                # Mutates in place (fills text_i18n / outro_i18n where missing).
                await translate_questionnaire(questionnaire)
            except Exception:
                failed += 1
                log.warning("campaign %s: translation failed", c.id, exc_info=True)
                continue
            if _canon(questionnaire) == before:
                skipped += 1  # already fully translated → nothing to write
                continue
            updated += 1
            log.info("campaign %s (%s): filled i18n", c.id, c.name)
            if not dry_run:
                cfg["questionnaire"] = questionnaire
                c.agent_config = cfg
                flag_modified(c, "agent_config")
        if dry_run:
            log.info("DRY RUN — rolling back, no writes")
            await db.rollback()
        else:
            await db.commit()
    await eng.dispose()

    log.info(
        "done: scanned=%d updated=%d skipped(already-translated)=%d failed=%d%s",
        scanned, updated, skipped, failed, " [DRY RUN]" if dry_run else "",
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
