"""One-time conversation-style rewrite of an APEX deterministic questionnaire.

The deterministic flow speaks scripted lines verbatim, which can sound like a
form being read aloud. This service rewrites the WORDING of each question (and
an admin-provided intro/outro) into a chosen conversation style — professional /
human / luxury / friendly — ONCE, at campaign-creation time via the style-rewrite
preview endpoint, never per call. The styled text replaces the question ``text``
itself, so everything downstream (pre-translation → ``text_i18n``, TTS prewarm,
verbatim delivery, fuzzy asked-tracking) runs unchanged on the styled lines.

Scoring never changes: ``type`` / ``desired_answer`` / ``tiers`` / ``gate`` /
``points`` are not sent to the model and not touched here — the model only sees
an ``answer_context`` summary so the rewrite keeps eliciting the same answer.

The original wording is kept in ``text_source`` (``intro_source`` /
``outro_source``) the first time a line is styled, and every restyle rewrites
from that source — switching Human → Luxury re-styles the admin's words, not
the previous rewrite. ``style: "scripted"`` restores the sources and strips the
styling keys (no LLM call).

Best-effort, mirroring questionnaire_translation: a line the model missed or
that fails validation keeps its current text, and every such fallback is
returned as a warning record so the caller (the campaign wizard) can flag a
partially styled script instead of letting the admin launch it blind.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

CONVERSATION_STYLES: tuple[str, ...] = (
    "scripted",
    "professional",
    "human",
    "luxury",
    "friendly",
)

# Caps mirror the questionnaire coercion (agent_outbound_context): question
# text is clamped to _MAX_QUESTION_TEXT=300, intro/outro to 600. An over-cap
# rewrite falls back to the original line — never a mid-word truncation the
# agent would then speak on real calls.
_MAX_STYLED_QUESTION = 300
_MAX_STYLED_INTRO_OUTRO = 600


def normalize_style(value: Any) -> str:
    """Canonical style id; blank/unknown degrades to ``"scripted"`` (no styling)."""
    style = str(value or "").strip().lower()
    return style if style in CONVERSATION_STYLES else "scripted"


@dataclass(frozen=True)
class StyleSpec:
    id: str
    label: str
    # One-line voice description + few-shot (authored -> styled) pairs, both
    # injected into the rewrite prompt. The few-shots carry most of the styling
    # signal — keep them phone-spoken and placeholder-free.
    voice: str
    examples: tuple[tuple[str, str], ...]


STYLES: dict[str, StyleSpec] = {
    "professional": StyleSpec(
        id="professional",
        label="Professional",
        voice=(
            "Polished, courteous, businesslike. Complete sentences, respectful "
            "and efficient; no slang, no over-familiarity."
        ),
        examples=(
            (
                "What is your budget?",
                "May I ask what budget range you've set aside for this?",
            ),
            (
                "When are you planning to buy?",
                "Could you share when you're planning to make the purchase?",
            ),
            (
                "Are you interested in a site visit?",
                "Would you be open to scheduling a site visit at a time that suits you?",
            ),
        ),
    ),
    "human": StyleSpec(
        id="human",
        label="Human",
        voice=(
            "Natural, warm, unscripted — the way a real phone rep actually "
            "talks. Contractions, soft lead-ins ('Just so I can help you "
            "better…'), light connective flow. Never form-field phrasing."
        ),
        examples=(
            (
                "What is your budget?",
                "Just so I can recommend the right options, could I ask roughly "
                "what budget you're working with?",
            ),
            (
                "When are you planning to buy?",
                "And when are you hoping to buy — are you actively looking right "
                "now, or just exploring?",
            ),
            (
                "Are you interested in a site visit?",
                "Would it help if we set up a quick site visit so you can see it "
                "for yourself?",
            ),
        ),
    ),
    "luxury": StyleSpec(
        id="luxury",
        label="Luxury",
        voice=(
            "Refined concierge register for premium offerings. Gracious, "
            "unhurried, discreet; 'may I', 'we'd be delighted'. Never salesy "
            "or pushy."
        ),
        examples=(
            (
                "What is your budget?",
                "So we can curate the right options for you, may I ask what "
                "investment range you're considering?",
            ),
            (
                "When are you planning to buy?",
                "May I ask what timeline you're considering for the purchase?",
            ),
            (
                "Are you interested in a site visit?",
                "We'd be delighted to arrange a private visit — would that "
                "interest you?",
            ),
        ),
    ),
    "friendly": StyleSpec(
        id="friendly",
        label="Friendly",
        voice=(
            "Upbeat, casual, approachable — chatty but still purposeful. Light "
            "energy, simple words, a smile in the voice."
        ),
        examples=(
            (
                "What is your budget?",
                "And budget-wise, what sort of range are you thinking?",
            ),
            (
                "When are you planning to buy?",
                "So when are you thinking of buying — sometime soon, or still "
                "looking around?",
            ),
            (
                "Are you interested in a site visit?",
                "Want to come see the place for yourself? We can set up a quick "
                "visit.",
            ),
        ),
    ),
}


def _source_text(entry: dict[str, Any], text_key: str, source_key: str) -> str:
    """The line to rewrite FROM: the preserved original when the entry was
    already styled once, else its current text. This is what makes restyling
    idempotent — Human → Luxury rewrites the admin's words, not Human's."""
    return str(entry.get(source_key) or entry.get(text_key) or "").strip()


def _answer_context(q: dict[str, Any]) -> str:
    """Compact description of the answer a question must elicit, so the model
    can rewrite the wording without breaking the elicitation contract. The
    scoring fields themselves are never sent (and never written back)."""
    if str(q.get("type") or "") == "intent":
        required = str(q.get("required") or "yes").strip() or "yes"
        return f'expects a yes/no answer; the qualifying answer is "{required}"'
    tiers = q.get("tiers") or []
    if tiers:
        labels = "; ".join(str(t.get("label") or "").strip() for t in tiers if t.get("label"))
        return f"must elicit an answer gradeable into one of: {labels}"[:300]
    desired = str(q.get("desired_answer") or "").strip()
    if desired:
        return f'must elicit an answer matching: "{desired}"'[:300]
    return ""


def _collect(questionnaire: dict[str, Any]) -> list[dict[str, Any]]:
    """Every line the rewrite covers: all questions, plus intro/outro only when
    the admin provided one (a blank intro stays blank — the runtime builds a
    personalized template opener that a static styled line would regress)."""
    items: list[dict[str, Any]] = []
    for idx, q in enumerate(questionnaire.get("questions") or []):
        text = _source_text(q, "text", "text_source")
        if not text:
            continue
        item: dict[str, Any] = {
            "id": str(q.get("id") or ""),
            "kind": "question",
            "index": idx + 1,
            "text": text,
        }
        ctx = _answer_context(q)
        if ctx:
            item["answer_context"] = ctx
        if q.get("gate"):
            item["gate"] = True
        items.append(item)
    for key, source_key in (("intro", "intro_source"), ("outro", "outro_source")):
        text = _source_text(questionnaire, key, source_key)
        if text:
            items.append({"id": f"__{key}__", "kind": key, "index": None, "text": text})
    return items


def _build_prompt(
    items: list[dict[str, Any]],
    spec: StyleSpec,
    *,
    company_name: str | None,
    caller_name: str | None,
    content: str | None,
) -> list[dict[str, str]]:
    examples = "\n".join(f'- "{authored}" -> "{styled}"' for authored, styled in spec.examples)
    system = (
        "You rewrite scripted lines for an outbound phone sales agent into a "
        "target conversation style. You change ONLY the wording — never what a "
        "line asks for or means.\n\n"
        f"TARGET STYLE — {spec.label}: {spec.voice}\n"
        f"Examples:\n{examples}\n\n"
        "RULES:\n"
        "- Preserve each line's meaning and the exact information it asks for. "
        "When an item carries answer_context, the rewritten line must still "
        "naturally elicit that same answer.\n"
        "- One spoken sentence per line (two short ones at most); at most ~25 "
        "words for a question.\n"
        "- Natural spoken Indian-English phone register; contractions welcome.\n"
        "- NEVER add names, greetings, company or project names, placeholders, "
        "brackets or braces of any kind, emojis, or markdown. Never invent "
        "facts, numbers, or offers.\n"
        "- Every content word survives: numbers, amounts, and domain terms "
        "(budget, BHK, site visit, project) stay unchanged.\n"
        "- Do not merge, split, reorder, or skip lines. A question stays a "
        "question; the intro stays a single opener line; the outro stays a "
        "single closing line.\n"
        "- Write ENGLISH only — other languages are handled downstream.\n"
        "The user message may include campaign context — it is for tone "
        "congruence only; never inject it into the lines.\n"
        "Return STRICT JSON only, no markdown: "
        '{"items":[{"id":"<same id>","text":"..."}]}. Echo every id exactly once.'
    )
    context = {
        k: v
        for k, v in (
            ("company_name", str(company_name or "").strip()[:200]),
            ("caller_name", str(caller_name or "").strip()[:60]),
            ("about", str(content or "").strip()[:500]),
        )
        if v
    }
    payload = json.dumps(
        {"style": spec.id, "context": context, "items": items}, ensure_ascii=False
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Restyle these lines:\n{payload}"},
    ]


def _parse(raw: str) -> dict[str, str]:
    """Parse the model's JSON into {id: styled_text}. Tolerant of markdown fences."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if "```" in text[3:] else text.strip("`")
        text = text.split("\n", 1)[-1] if text.lower().startswith("json") else text
    data = json.loads(text)
    out: dict[str, str] = {}
    for row in (data.get("items") if isinstance(data, dict) else data) or []:
        rid = str(row.get("id") or "")
        styled = str(row.get("text") or "").strip()
        if rid and styled:
            out[rid] = styled
    return out


def _sane(styled: str, cap: int) -> bool:
    """A styled line the agent may speak verbatim: non-empty, within the
    coercion cap (over-cap would be truncated mid-word), single-line, and free
    of brackets/braces — both placeholder armor ("{name}", "[company]") and
    protection for the [warm]…[/warm] prosody-tag parser the intro runs through."""
    if not styled or len(styled) > cap:
        return False
    if any(ch in styled for ch in "{}[]\n"):
        return False
    return True


def _restyle_line(
    entry: dict[str, Any], styled: str, text_key: str, source_key: str, i18n_key: str
) -> None:
    """Apply one styled line: snapshot the original into ``*_source`` the first
    time, replace the text, and drop the line's pre-translations (they describe
    the old wording — the translate step refills them from the styled text)."""
    old_text = str(entry.get(text_key) or "").strip()
    if styled == old_text:
        return
    if not str(entry.get(source_key) or "").strip():
        entry[source_key] = old_text
    entry[text_key] = styled
    entry.pop(i18n_key, None)


def _restore_scripted(questionnaire: dict[str, Any]) -> None:
    """``scripted`` selected: put the admin's original wording back and strip
    every styling key. Lines whose text actually changes lose their (now stale)
    pre-translations too."""
    for q in questionnaire.get("questions") or []:
        src = str(q.pop("text_source", "") or "").strip()
        if src and src != str(q.get("text") or "").strip():
            q["text"] = src[:_MAX_STYLED_QUESTION]
            q.pop("text_i18n", None)
    for key, source_key, i18n_key in (
        ("intro", "intro_source", "intro_i18n"),
        ("outro", "outro_source", "outro_i18n"),
    ):
        src = str(questionnaire.pop(source_key, "") or "").strip()
        if src and src != str(questionnaire.get(key) or "").strip():
            questionnaire[key] = src[:_MAX_STYLED_INTRO_OUTRO]
            questionnaire.pop(i18n_key, None)
    questionnaire.pop("style", None)


async def rewrite_questionnaire(
    questionnaire: dict[str, Any],
    style: str,
    *,
    company_name: str | None = None,
    caller_name: str | None = None,
    content: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Rewrite the questionnaire's spoken lines into ``style`` in place and
    return ``(questionnaire, warnings)``.

    Warnings are per-line fallback records ``{"id", "kind", "index", "reason"}``
    (reason: ``invalid`` | ``missing`` | ``llm_failed``) for lines that KEPT
    their current wording — surfaced so the wizard can show "some lines
    couldn't be restyled" instead of shipping a half-styled script silently.
    Best-effort: never raises; a total LLM failure returns the questionnaire
    unchanged with a single ``llm_failed`` warning.
    """
    if not isinstance(questionnaire, dict):
        return questionnaire, []
    style = normalize_style(style)
    if style == "scripted":
        _restore_scripted(questionnaire)
        return questionnaire, []
    spec = STYLES[style]
    items = _collect(questionnaire)
    if not items:
        questionnaire["style"] = style
        return questionnaire, []

    warnings: list[dict[str, Any]] = []
    styled_by_id: dict[str, str] = {}
    try:
        from app.services.llm_pool import LLMPoolClient

        # One batched call; a single English line per item, so 120 tokens/item
        # is generous.
        raw = await LLMPoolClient.chat(
            _build_prompt(
                items,
                spec,
                company_name=company_name,
                caller_name=caller_name,
                content=content,
            ),
            max_tokens=min(2048, 120 * len(items) + 200),
            temperature=0.4,
        )
        styled_by_id = _parse(raw)
    except Exception:
        logger.warning(
            "APEX-QN-STYLE: rewrite (%s) failed — keeping current wording", style, exc_info=True
        )
        return questionnaire, [
            {"id": None, "kind": "all", "index": None, "reason": "llm_failed"}
        ]

    questions_by_id = {
        str(q.get("id") or ""): q for q in questionnaire.get("questions") or []
    }
    applied = 0
    for item in items:
        item_id = str(item["id"])
        kind = str(item["kind"])
        cap = _MAX_STYLED_QUESTION if kind == "question" else _MAX_STYLED_INTRO_OUTRO
        styled = styled_by_id.get(item_id)
        if styled is None:
            warnings.append(
                {"id": item_id, "kind": kind, "index": item["index"], "reason": "missing"}
            )
            continue
        if not _sane(styled, cap):
            logger.warning(
                "APEX-QN-STYLE: rejected styled %s %s (len=%d) — keeping current wording",
                kind, item_id, len(styled),
            )
            warnings.append(
                {"id": item_id, "kind": kind, "index": item["index"], "reason": "invalid"}
            )
            continue
        if kind == "question":
            q = questions_by_id.get(item_id)
            if q is not None:
                _restyle_line(q, styled, "text", "text_source", "text_i18n")
                applied += 1
        else:
            _restyle_line(questionnaire, styled, kind, f"{kind}_source", f"{kind}_i18n")
            applied += 1

    # The style is the admin's SELECTION — it drives the ack pool and the edit
    # form's preselect, so it sticks even when some lines fell back (the
    # warnings tell the wizard to flag those lines for review).
    questionnaire["style"] = style
    if warnings:
        logger.warning(
            "APEX-QN-STYLE: styled %d line(s) as %s, %d kept current wording",
            applied, style, len(warnings),
        )
    else:
        logger.info("APEX-QN-STYLE: styled %d line(s) as %s", applied, style)
    return questionnaire, warnings
