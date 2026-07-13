"""One-time pre-translation of an APEX deterministic questionnaire.

The deterministic flow speaks scripted lines. To make each line cacheable AND
multilingual, we translate every question ``text``, the ``intro`` (the opener the
agent leads the call with) and the ``outro`` into en/hi/te ONCE at campaign
creation (not per call) and store the static strings on the questionnaire
(``text_i18n`` per question, ``intro_i18n``, ``outro_i18n``). At call time the
flow can then speak the exact per-language string verbatim → it recurs across
calls → TTS cache hit, while hi/te stay native.

Best-effort: any failure (no LLM pool, malformed JSON, timeout) leaves the string
un-translated — the call-time flow falls back to the authored ``text``. Idempotent:
items that already carry i18n are skipped, so re-runs are cheap.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# The languages APEX supports. Hindi = Devanagari, Telugu = native Telugu script
# (romanised script makes Sarvam TTS mispronounce — see feedback_telugu_native_script).
_TARGET_LANGS = ("en", "hi", "te")


def _needs(entry: dict[str, Any], key: str) -> bool:
    i18n = entry.get(key)
    return not (isinstance(i18n, dict) and all(i18n.get(l) for l in _TARGET_LANGS))


def _collect(questionnaire: dict[str, Any]) -> list[dict[str, str]]:
    """The {id, text} strings still needing translation (questions + intro + outro)."""
    items: list[dict[str, str]] = []
    for q in questionnaire.get("questions") or []:
        text = str(q.get("text") or "").strip()
        if text and _needs(q, "text_i18n"):
            items.append({"id": str(q.get("id") or ""), "text": text})
    intro = str(questionnaire.get("intro") or "").strip()
    if intro and _needs(questionnaire, "intro_i18n"):
        items.append({"id": "__intro__", "text": intro})
    outro = str(questionnaire.get("outro") or "").strip()
    if outro and _needs(questionnaire, "outro_i18n"):
        items.append({"id": "__outro__", "text": outro})
    return items


def drop_stale_i18n(new_q: dict[str, Any], old_q: dict[str, Any] | None) -> None:
    """Campaign EDIT support: when a line's authored text changed, its
    pre-translated languages describe the OLD text — speaking them would say the
    wrong thing in hi/te. Drop the i18n for exactly those lines (matched by
    question ``id``; intro/outro by value) so ``translate_questionnaire`` refills
    them, while unchanged lines keep their (possibly hand-edited) translations."""
    old_by_id = {
        str(q.get("id") or ""): q for q in ((old_q or {}).get("questions") or [])
    }
    for q in new_q.get("questions") or []:
        old = old_by_id.get(str(q.get("id") or ""))
        if old is not None and (
            str(old.get("text") or "").strip() != str(q.get("text") or "").strip()
        ):
            q.pop("text_i18n", None)
    for key, i18n_key in (("intro", "intro_i18n"), ("outro", "outro_i18n")):
        if str((old_q or {}).get(key) or "").strip() != str(new_q.get(key) or "").strip():
            new_q.pop(i18n_key, None)


def _merge_i18n(existing: Any, tr: dict[str, str]) -> dict[str, str]:
    """Fill missing languages from the fresh translation WITHOUT overwriting a
    non-empty existing value. An admin who hand-edited one language (leaving
    another blank) re-enters translation via ``_needs`` — the edit must survive,
    only the blanks get filled."""
    keep = {k: v for k, v in existing.items() if v} if isinstance(existing, dict) else {}
    return {**tr, **keep}


# Spoken-register rules distilled from language_style.py (the LLM turns'
# style guide) so VERBATIM lines carry the same register as the live agent —
# without this the questions sound textbook-formal while the LLM's objection
# handling sounds like a real metro salesperson: two voices in one call.
# Kept short on purpose (a translation prompt, not the full few-shot blocks).
_SPOKEN_REGISTER_RULES = (
    "REGISTER — these lines are SPOKEN on a phone call by a salesperson, not "
    "printed. Write them the way a real Indian phone rep talks:\n"
    "- en: natural spoken English — contractions, direct address, light "
    "connective flow ('And what budget do you have in mind?'), never form-"
    "field phrasing ('Please specify your budget range'). PRESERVE every "
    "content word: names, numbers, amounts, and domain terms (project, BHK, "
    "site visit, budget) must survive the rewrite unchanged.\n"
    "- hi: ~60% English / 40% Hindi — real urban metro talk, not formal or "
    "Sanskritised. Hindi (Devanagari) is the grammatical frame (है, चाहिए, "
    "आप, के लिए, जी); English carries the content nouns and domain terms in "
    "LATIN letters (project, budget, location, site visit, BHK). NEVER "
    "transliterate English words into Devanagari (WhatsApp, not व्हाट्सएप). "
    "Polite आप form always.\n"
    "- te: ~60% English / 40% Telugu — real Hyderabad metro talk, not "
    "textbook Telugu. Telugu script only for genuine Telugu grammar words "
    "(ఉంది, కావాలి, మీరు, కోసం, అండి); content nouns and domain terms stay "
    "English in LATIN letters. NEVER transliterate English into Telugu "
    "script (WhatsApp, not వాట్సాప్). Polite మీరు form always.\n"
    "- All languages: numbers, ₹ amounts, phone numbers, dates and times "
    "stay as digits (₹50L, 3 BHK, 10 AM) — the TTS layer voices them.\n"
    "Example — authored: 'What is your budget for the apartment?' → "
    "en: 'And what budget do you have in mind for the apartment?' | "
    "hi: 'Apartment के लिए आपका budget कितना है?' | "
    "te: 'Apartment కోసం మీ budget ఎంత అనుకుంటున్నారు?'"
)


def _build_prompt(items: list[dict[str, str]]) -> list[dict[str, str]]:
    from app.core.config import settings

    payload = json.dumps([{"id": it["id"], "text": it["text"]} for it in items], ensure_ascii=False)
    system = (
        "You translate short phone-call lines for a voice agent into English (en), "
        "Hindi (hi, in Devanagari script), and Telugu (te, in native Telugu script). "
        "Keep each translation natural and concise for speech — no transliteration, "
        "no romanisation, no extra commentary. Return STRICT JSON only, no markdown: "
        '{"items":[{"id":"<same id>","en":"...","hi":"...","te":"..."}]}. '
        "Preserve every id exactly. If a line is already in a target language, still "
        "provide all three."
    )
    if settings.APEX_I18N_SPOKEN_REGISTER:
        system = f"{system}\n\n{_SPOKEN_REGISTER_RULES}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Translate these lines:\n{payload}"},
    ]


def _parse(raw: str) -> dict[str, dict[str, str]]:
    """Parse the model's JSON into {id: {en, hi, te}}. Tolerant of markdown fences."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if "```" in text[3:] else text.strip("`")
        text = text.split("\n", 1)[-1] if text.lower().startswith("json") else text
    data = json.loads(text)
    out: dict[str, dict[str, str]] = {}
    for row in (data.get("items") if isinstance(data, dict) else data) or []:
        rid = str(row.get("id") or "")
        tr = {l: str(row.get(l) or "").strip() for l in _TARGET_LANGS}
        if rid and all(tr.values()):
            out[rid] = tr
    return out


def _floor_missing_i18n(questionnaire: dict[str, Any]) -> int:
    """Guarantee every question (and the intro/outro) carries a non-empty i18n
    dict: anything the translation didn't cover is floored to
    ``{"en": authored_text}``.

    WHY: ``next_verbatim_question`` hard-requires a non-empty ``text_i18n`` — a
    question without one silently falls back to LLM rephrasing for EVERY call,
    which costs latency/credits AND breaks the fuzzy asked-state tracking (the
    prod questionnaire-loop incident). A floored question is spoken verbatim in
    the authored language; hi/te degrade via verbatim_line_for_language's
    lang→en→authored chain. Returns how many entries were floored."""
    floored = 0
    for q in questionnaire.get("questions") or []:
        text = str(q.get("text") or "").strip()
        i18n = q.get("text_i18n")
        if text and not (isinstance(i18n, dict) and any(i18n.values())):
            q["text_i18n"] = {"en": text}
            floored += 1
    for key, i18n_key in (("intro", "intro_i18n"), ("outro", "outro_i18n")):
        line = str(questionnaire.get(key) or "").strip()
        i18n = questionnaire.get(i18n_key)
        if line and not (isinstance(i18n, dict) and any(i18n.values())):
            questionnaire[i18n_key] = {"en": line}
            floored += 1
    return floored


async def translate_questionnaire(questionnaire: dict[str, Any]) -> dict[str, Any]:
    """Fill ``text_i18n`` / ``intro_i18n`` / ``outro_i18n`` on a questionnaire in place (and return it).
    Best-effort + idempotent — never raises. Whatever the translation outcome,
    every entry leaves with at least a floored ``{"en": authored}`` i18n so
    verbatim delivery always fires."""
    if not isinstance(questionnaire, dict):
        return questionnaire
    items = _collect(questionnaire)
    if not items:
        return questionnaire
    translated: dict[str, dict[str, str]] = {}
    try:
        from app.services.llm_pool import LLMPoolClient

        # One batched call. ~200 tokens/line is generous for three short translations.
        raw = await LLMPoolClient.chat(
            _build_prompt(items),
            max_tokens=min(2048, 220 * len(items) + 200),
            temperature=0.0,
        )
        translated = _parse(raw)
        if not translated:
            logger.warning(
                "APEX-QN-I18N: model output parsed to ZERO usable rows for %d line(s)", len(items)
            )
        elif len(translated) < len(items):
            logger.warning(
                "APEX-QN-I18N: partial translation — %d of %d line(s) usable "
                "(rows missing a language are dropped); the rest will be floored to authored text",
                len(translated), len(items),
            )
    except Exception:
        logger.warning("APEX-QN-I18N: translation failed — flooring to authored text", exc_info=True)

    for q in questionnaire.get("questions") or []:
        tr = translated.get(str(q.get("id") or ""))
        if tr and _needs(q, "text_i18n"):
            q["text_i18n"] = _merge_i18n(q.get("text_i18n"), tr)
    for line_id, i18n_key in (("__intro__", "intro_i18n"), ("__outro__", "outro_i18n")):
        tr = translated.get(line_id)
        if tr and _needs(questionnaire, i18n_key):
            questionnaire[i18n_key] = _merge_i18n(questionnaire.get(i18n_key), tr)
    if translated:
        logger.info("APEX-QN-I18N: translated %d line(s) into en/hi/te", len(translated))
    floored = _floor_missing_i18n(questionnaire)
    if floored:
        logger.info("APEX-QN-I18N: floored %d untranslated line(s) to authored text", floored)
    return questionnaire
