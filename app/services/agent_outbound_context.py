"""Per-call outbound campaign context.

The inbound voice agent reads three things at the top of every turn:

  * Tenant runtime bundle (organization, overrides, policy cards,
    optional tenant-wide single-prompt config).
  * Conversation history + state from Redis.
  * The retrieval result (Qdrant chunks scoped to the tenant KB).

The outbound voice agent reads everything inbound reads PLUS a fourth
input: the campaign itself. A campaign carries:

  * ``doc_text`` — the campaign script / reference document, already
    indexed into Qdrant under ``campaign_id``. The retrieval layer picks
    it up automatically when the call passes ``campaign_id``.
  * ``agent_config`` — the proactive-agent configuration. Holds the
    role / tone prompt, the ordered objective questions, and any
    exit-condition signals. Composed into the LLM system prompt.

This module owns the loader + a small process-local cache (since
campaign data is read once per call but does not change during the
call). The cache is keyed by ``campaign_id`` and TTL-bounded to a few
minutes so a freshly-pushed agent_config edit becomes effective on the
next call without restarting the process.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outbound_campaign import OutboundCampaign


_CAMPAIGN_TTL_SECONDS = 120.0
_CAMPAIGN_CACHE_MAX = 256
_DEFAULT_SILENCE_TIMEOUT_SECONDS = 12.0


# ── Leading-filler scrub ────────────────────────────────────────────────────
# The outbound agents are prompt-banned from opening a reply with the stock
# filler "right so" / "right, so" / a bare "Right." (see the HOW YOU TALK block
# in _compose_questionnaire_only_section and the base sales template), but a
# prompt ban is only probabilistic and small models still leak it. This strips
# it deterministically from the FIRST spoken sentence of an outbound turn so it
# is never heard, shown in the transcript, or stored in history.
#
# "right"/"alright" glued to a following discourse "so" — "right so", "right,
# so", "right. so", "right—so", "right…so", "alright so". Anchored to the very
# start, case-insensitive. Two separator classes do different jobs:
#   _BETWEEN — between "right" and "so": dashes/ellipsis allowed (they're bounded
#              by two known tokens, so they can't run into real content).
#   _AFTER   — after "so": NO hyphen/dash, so the match can't bridge into a
#              hyphenated compound and eat it ("so-called", "so-so", "so-and-so"
#              must survive). A lookahead also requires "so" to be a STANDALONE
#              word (followed by whitespace / sentence punctuation / end) so the
#              "so" inside "so-called" is never treated as the filler.
_BETWEEN = r"[\s,.;:!?…—–-]"
_AFTER = r"[\s,.;:!?…]"
_LEADING_RIGHT_SO_RE = re.compile(
    rf"^\s*(?:al)?right\b{_BETWEEN}*so(?={_AFTER}|$){_AFTER}*",
    re.IGNORECASE,
)
# The whole sentence is nothing but a bare "right"/"alright" acknowledgement
# ("Right.", "Right!", "Right", "Alright,"). These pair with a following "So …"
# sentence to reproduce "right so", so drop them outright. The ``$`` anchor means
# "Right now …" / "Right away …" / "Right, the 3 BHK …" are NEVER touched — only
# a standalone ack is removed.
_BARE_RIGHT_ACK_RE = re.compile(
    r"^\s*(?:al)?right\b[\s,.;:!?…—–-]*$",
    re.IGNORECASE,
)


def strip_leading_right_so(text: str) -> str:
    """Remove a leading "right so" / bare "Right." filler from an outbound reply.

    Returns ``""`` when the sentence was ONLY the filler (a bare "Right.") so the
    caller can skip speaking it and let the next sentence carry the turn.
    Conservative by design: it only touches a "(al)right"-anchored opener whose
    "so" is a standalone word — never a standalone "So …" (natural speech), a
    mid-sentence "right", "right now"/"right away" where "right" is a real word,
    nor a hyphenated "so-called"/"so-so" compound.
    """
    if not text:
        return text
    if _BARE_RIGHT_ACK_RE.match(text):
        return ""
    stripped = _LEADING_RIGHT_SO_RE.sub("", text, count=1)
    if stripped == text:
        return text
    stripped = stripped.lstrip()
    if not stripped:
        return ""
    # Re-capitalize the first alphabetic character — we removed the capitalized
    # opener, so the remainder would otherwise start lowercase ("what's your
    # budget?" → "What's your budget?"). A non-letter first char (digit/emoji)
    # is left as-is.
    for i, ch in enumerate(stripped):
        if ch.isalpha():
            return stripped[:i] + ch.upper() + stripped[i + 1 :]
        if not ch.isspace():
            break
    return stripped


# ── Broader leading-filler scrub (DETERMINISTIC questionnaire agent only) ─────
# The deterministic questionnaire agent's only job is to ASK the next question,
# crisply — it must never pad a turn with a stock acknowledgement opener
# ("Great,", "Perfect.", "Got it.", "Good to hear,", "Sure,", "Okay,"). The HOW
# YOU TALK block bans them, but a small model still leaks one, so we strip the
# WHOLE acknowledgement set deterministically from the first spoken sentence —
# the same idea as strip_leading_right_so, widened. Used ONLY for the
# deterministic agent; the free-form salesperson agent keeps the narrower
# strip_leading_right_so so a natural "Got it," can still warm the rapport.
#
# CONSERVATIVE GATE: a single filler word is removed only when it is immediately
# followed by SENTENCE PUNCTUATION (a multi-word phrase too). So "Good morning",
# "Right now", "So happy", "Actually the…" — where the word runs straight into
# real content with no comma/stop — are NEVER touched. Plain hyphens are excluded
# from the separator class so "so-called" / "well-being" survive.
_FILLER_PHRASES = (
    "thank you so much", "thanks so much", "thanks for that", "good to hear",
    "let me see", "let's see", "fair enough", "makes sense", "of course",
    "no worries", "thank you", "you know", "i mean", "got it", "great thanks",
    "okay great", "perfect thanks",
)
_FILLER_WORDS = (
    "great", "perfect", "nice", "lovely", "wonderful", "excellent", "awesome",
    "cool", "good", "okay", "ok", "alright", "right", "sure", "gotcha",
    "understood", "noted", "thanks", "well", "so", "um", "umm", "uh", "uhh",
    "er", "erm", "hmm", "mm", "mhm", "ah", "oh", "basically", "actually",
    "anyway", "anyways", "yeah", "yep", "yup",
)


def _filler_unit_pattern(unit: str) -> str:
    # Allow flexible whitespace between words of a multi-word filler.
    return r"\s+".join(re.escape(w) for w in unit.split(" "))


# Longest-first so "good to hear" wins over "good", "thank you so much" over
# "thank you", etc. (regex alternation backtracks anyway, but this is clearer).
_FILLER_ALTERNATION = "|".join(
    _filler_unit_pattern(u)
    for u in sorted(_FILLER_PHRASES + _FILLER_WORDS, key=len, reverse=True)
)
# One leading filler unit: the phrase/word + REQUIRED sentence punctuation
# (em/en dash and ellipsis included; plain hyphen deliberately excluded). The
# trailing \s* eats the gap to the real content.
_LEADING_FILLER_RE = re.compile(
    rf"^\s*(?:{_FILLER_ALTERNATION})\b\s*[,.;:!?…—–]+\s*",
    re.IGNORECASE,
)


def strip_leading_fillers(text: str) -> str:
    """Remove ALL leading acknowledgement/discourse fillers from a deterministic
    outbound (questionnaire) reply so the agent opens with the question itself.

    Composes strip_leading_right_so (which also catches the space-separated
    "right so" that has no punctuation) with an iterative peel of the wider
    acknowledgement set, each unit gated on trailing sentence punctuation. Peels
    chained fillers ("Great, thanks, …" → "…"). Returns "" when the sentence was
    nothing but filler, so the caller skips it and the next sentence carries the
    turn — identical to the bare-"Right." behaviour.
    """
    if not text:
        return text
    s = text
    while True:
        before = s
        s = strip_leading_right_so(s)
        if s == "":
            return ""
        peeled = _LEADING_FILLER_RE.sub("", s, count=1)
        if peeled != s:
            s = peeled.lstrip()
            if s == "":
                return ""
        if s == before:
            break
    s = s.strip()
    if not s:
        return ""
    # Re-capitalize the first alphabetic char (we removed the capitalized opener).
    for i, ch in enumerate(s):
        if ch.isalpha():
            return s[:i] + ch.upper() + s[i + 1 :]
        if not ch.isspace():
            break
    return s


DEFAULT_AGENT_PROMPT = (
    "You are making a consented outbound call for the configured business. "
    "Be concise, identify the reason for the call, and guide the lead toward "
    "one clear next step. If the lead says they are not interested, asks not "
    "to be called, says this is the wrong number, or sounds busy, stop pushing "
    "and close politely."
)

DEFAULT_OBJECTIVES = [
    "Confirm this is a good time to talk.",
    "Briefly explain why you are calling based on the campaign goal.",
    "Understand whether the lead is interested and what they need.",
    "Capture the next step: appointment, callback, site visit, demo, or opt-out.",
]

DEFAULT_EXIT_CONDITIONS = [
    "Lead asks not to be called again.",
    "Lead says they are not interested.",
    "Lead says this is the wrong number.",
    "Lead is busy and asks for a callback.",
    "All campaign objectives have been covered.",
]

_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "based", "by", "for", "from",
    "has", "have", "if", "in", "is", "it", "of", "on", "or", "that", "the",
    "their", "they", "this", "to", "toward", "what", "when", "where", "why",
    "with", "you", "your",
}


# Per-objective guidance lifted from the reference agent_lab/voice-rag-agent
# implementation. The system prompt plugs the matching paragraph in so the
# agent's behaviour shifts to fit the campaign's actual purpose (qualifying
# vs demoing vs surveying read differently on the line).
OBJECTIVE_DESCRIPTIONS: dict[str, str] = {
    "lead_qualification": (
        "Find out if they're a good fit. Ask 1-2 questions to understand their "
        "current situation and whether what we offer would help them."
    ),
    "demo_booking": (
        "Book a 15-minute product demo. If they're interested, propose a time and "
        "say a human will follow up to confirm."
    ),
    "info_outreach": (
        "Make them aware of the offer and offer to send details by SMS or email "
        "for them to review at their convenience."
    ),
    "survey": (
        "Ask 2-3 short questions. Thank them, no pitch."
    ),
    "renewal": (
        "They're an existing customer. Remind them their plan is up for renewal, "
        "answer questions, offer to connect them with an account manager."
    ),
}

OUTBOUND_OBJECTIVES = tuple(OBJECTIVE_DESCRIPTIONS.keys())


@dataclass(frozen=True)
class OutboundCampaignContext:
    """Snapshot of the campaign-level state for a single outbound call.

    Constructed by :func:`load_outbound_context` and threaded through
    the pipeline so every turn can compose the campaign system prompt
    + the remaining objectives without re-fetching from Postgres.

    The campaign-branding fields (caller_name / company_name / pitch_summary /
    objective) are what the reference implementation uses to drive a real
    sales/outreach persona and the deterministic opener. ``agent_prompt`` is
    kept as an optional override for advanced cases (custom personas, exotic
    flows). Legacy campaigns without the new fields default sensibly so
    existing rows keep working.
    """

    campaign_id: str
    name: str
    goal: str
    agent_prompt: str
    objectives: list[str]
    exit_conditions: list[str]
    tone: str | None
    doc_text: str | None
    silence_timeout_seconds: float = _DEFAULT_SILENCE_TIMEOUT_SECONDS
    # Sales-persona fields. Defaults preserve backwards compat for old
    # OutboundCampaign rows that didn't carry these.
    caller_name: str = "Riya"
    company_name: str = ""
    pitch_summary: str = ""
    objective: str = "lead_qualification"
    # Lead-capture questionnaire (bulk campaigns). Ordered list of normalized
    # question dicts {id, type: "intent"|"answer", text, desired_answer} plus the
    # qualifying threshold. Empty list = no questionnaire (legacy interest path).
    # See :func:`_coerce_questionnaire` for the canonical shape.
    questions: list[dict] = field(default_factory=list)
    question_threshold: int = 0
    # Admin-authored opener line for a questionnaire campaign (spoken as the
    # deterministic opener); empty falls back to the template opener.
    question_intro: str = ""
    # Admin-authored closing line; the agent says it to end any call and the
    # system plays-then-hangs-up when it's delivered (incl. a failed intent gate).
    question_outro: str = ""

    @property
    def is_proactive(self) -> bool:
        """A campaign is proactive when either the operator gave us an
        explicit ``agent_prompt``, objectives to land, a questionnaire, or any
        campaign branding (caller_name / company_name / pitch_summary). Pure
        ``goal``-only campaigns stay reactive (legacy behaviour)."""
        return bool(
            self.agent_prompt.strip()
            or self.objectives
            or self.questions
            or self.company_name.strip()
            or self.pitch_summary.strip()
        )

    @property
    def has_questionnaire(self) -> bool:
        return bool(self.questions)

    def remaining_questions(self, asked: Iterable[str]) -> list[dict]:
        """Questions whose ``id`` is not yet in ``asked`` (asked-tracking is a
        future enhancement; today the prompt renders the full list)."""
        asked_ids = {str(a) for a in (asked or []) if a}
        return [q for q in self.questions if str(q.get("id")) not in asked_ids]

    @property
    def objective_description(self) -> str:
        return OBJECTIVE_DESCRIPTIONS.get(self.objective, OBJECTIVE_DESCRIPTIONS["lead_qualification"])

    def remaining_objectives(self, covered: Iterable[str]) -> list[str]:
        """Objectives that have NOT yet been marked covered for this
        call. Comparison is case-insensitive on the objective text so
        small drift in storage doesn't desync the list."""
        covered_lower = {(c or "").strip().lower() for c in covered if c}
        return [obj for obj in self.objectives if obj.strip().lower() not in covered_lower]

    def has_branding(self) -> bool:
        return bool(self.caller_name.strip() or self.company_name.strip())


_cache: dict[str, tuple[float, OutboundCampaignContext]] = {}
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(campaign_id: str) -> asyncio.Lock:
    lock = _locks.get(campaign_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[campaign_id] = lock
    return lock


def _evict_stale(now: float) -> None:
    expired = [cid for cid, (expires_at, _) in _cache.items() if expires_at <= now]
    for cid in expired:
        _cache.pop(cid, None)
    while len(_cache) > _CAMPAIGN_CACHE_MAX:
        _cache.pop(next(iter(_cache)), None)


def _coerce_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if not item:
                continue
            text = str(item).strip()
            if text:
                out.append(text[:500])
        return out[:32]
    if isinstance(value, str) and value.strip():
        return [value.strip()[:500]]
    return []


_MAX_QUESTIONS = 10
_MAX_QUESTION_TEXT = 300
# Weighted / graded scoring caps (a question may carry a single `points` weight,
# or — for answer questions — a list of graded `tiers`/bands each worth points).
_MAX_TIERS = 6
_MAX_TIER_LABEL = 120
_MAX_POINTS = 100


def _coerce_tiers(
    raw_tiers: Any, *, strict: bool = False, idx: int = 0, qtext: str = ""
) -> list[dict[str, Any]]:
    """Normalize a graded answer's ``tiers`` (scoring bands) to
    ``[{id, label, points}]``.

    Each tier needs a non-empty ``label`` (the band the scorer matches against,
    e.g. "above 1 crore") and ``points`` (clamped ``1.._MAX_POINTS``, default 1).
    Blank-label tiers are dropped (lenient even in strict — same as blank-text
    questions). ``id`` is a stable join key for the scorer's verdict; assigned
    when missing/duplicate. Capped at ``_MAX_TIERS``. Non-list → ``[]``.
    """
    if not isinstance(raw_tiers, list):
        return []
    tiers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for t in raw_tiers:
        if not isinstance(t, dict):
            continue
        label = str(t.get("label") or "").strip()[:_MAX_TIER_LABEL]
        if not label:
            continue
        try:
            pts = int(t.get("points"))
        except (TypeError, ValueError):
            pts = 1
        pts = max(1, min(_MAX_POINTS, pts))
        tid = str(t.get("id") or "").strip()[:32]
        if not tid or tid in seen:
            tid = uuid.uuid4().hex[:6]
        seen.add(tid)
        tiers.append({"id": tid, "label": label, "points": pts})
        if len(tiers) >= _MAX_TIERS:
            break
    return tiers


def questionnaire_max_points(questions: list[dict[str, Any]] | None) -> int:
    """Best achievable score for a questionnaire: each question contributes the
    most it can earn — the highest band of a graded answer, else its ``points``
    weight (default 1).

    This is the ONE place "max score" is computed; the scorer and the API both
    call it so they can never drift. Backward-compatible: a questionnaire with
    no ``points`` and no ``tiers`` yields ``len(questions)`` — the legacy
    1-point-per-question maximum.
    """
    total = 0
    for q in questions or []:
        tiers = q.get("tiers")
        if isinstance(tiers, list) and tiers:
            total += max((int(t.get("points") or 0) for t in tiers), default=0)
        else:
            try:
                total += max(1, int(q.get("points") or 1))
            except (TypeError, ValueError):
                total += 1
    return total


def _coerce_questionnaire(value: Any, *, strict: bool = False) -> dict[str, Any] | None:
    """Normalize a campaign lead-capture questionnaire to the ONE canonical shape
    used everywhere (agent prompt, scorer, API, UI), or ``None`` when absent.

    Canonical::

        {"questions": [{"id": str, "type": "intent"|"answer",
                        "text": str, "desired_answer": str,
                        "points": int?,                       # weight, default 1
                        "tiers": [{id, label, points}]?}, ...],  # graded answers
         "threshold": int}

    Rules:
      * ``type`` is exactly ``"intent"`` or ``"answer"`` (anything else → intent).
      * ``text`` stripped + capped; blank-text questions dropped.
      * ``desired_answer`` is non-empty IFF ``type == "answer"`` AND the question
        is not graded; for ``"intent"`` it is forced to ``""``.
      * ``points`` is an optional per-question weight (``1.._MAX_POINTS``, default
        1), stored only when ``> 1``; ignored for graded answers (their tiers
        carry the points).
      * ``tiers`` (answer questions only) is a graded rubric — the post-call
        scorer awards the points of the single best-matching band. A graded
        answer needs no ``desired_answer``. Graded questions can't be live
        dealbreaker ``gate``s (no single fail answer), so ``gate`` is dropped.
      * ``id`` is a stable join key (used by scoring breakdown); assigned
        ``uuid4().hex[:8]`` when missing/duplicate.
      * question count capped at ``_MAX_QUESTIONS``.
      * ``threshold`` clamped to ``1..questionnaire_max_points``; missing → the
        max (i.e. must-earn-everything). Legacy all-1-point questionnaires keep
        ``1..len`` semantics since their max equals ``len``.
      * Empty/invalid → ``None`` (caller falls back to the legacy interest path).

    This is the SINGLE normalizer — the API imports it (with ``strict=True``) so
    it never diverges from the agent read-path (``strict=False``). ``strict``
    raises :class:`ValueError` for an ``answer`` question with neither an
    expected answer nor graded tiers (surfaced to the admin) instead of silently
    dropping it; the read path is lenient so a malformed stored config can never
    break context load.
    """
    if not isinstance(value, dict):
        return None
    raw_questions = value.get("questions")
    if not isinstance(raw_questions, list):
        return None
    questions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for idx, item in enumerate(raw_questions):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()[:_MAX_QUESTION_TEXT]
        if not text:
            continue
        qtype = str(item.get("type") or "").strip().lower()
        if qtype not in ("intent", "answer"):
            qtype = "intent"
        desired = str(item.get("desired_answer") or "").strip()[:_MAX_QUESTION_TEXT]
        # Graded rubric (answer questions only): bands the post-call scorer awards
        # points for. A graded answer satisfies the answer-needs-an-expected-value
        # rule via its tiers rather than a single desired_answer.
        tiers = (
            _coerce_tiers(item.get("tiers"), strict=strict, idx=idx, qtext=text)
            if qtype == "answer"
            else []
        )
        if qtype == "answer" and not desired and not tiers:
            if strict:
                raise ValueError(
                    f'Question {idx + 1} ("{text[:40]}") is a desired-answer '
                    "question but has no expected answer or graded bands — add "
                    "one, add scoring bands, or switch it to intent detection."
                )
            continue  # lenient read path: drop the unusable answer question
        required = ""
        if qtype == "intent":
            desired = ""
            # The Yes/No answer the admin requires to QUALIFY this question.
            required = str(item.get("required") or "yes").strip().lower()
            if required not in ("yes", "no"):
                required = "yes"
        # Per-question DEALBREAKER gate (admin checkbox): when set, failing this
        # question (intent → opposite of required; answer → not matching the
        # desired answer) ends the call — agent goes to the outro and hangs up.
        # Graded questions can't gate (no single fail answer) — drop it for them.
        gate = bool(item.get("gate")) and not tiers
        # Per-question weight (single-band questions); graded answers carry their
        # points on the tiers instead.
        try:
            points = max(1, min(_MAX_POINTS, int(item.get("points"))))
        except (TypeError, ValueError):
            points = 1
        qid = str(item.get("id") or "").strip()[:32]
        if not qid or qid in seen_ids:
            qid = uuid.uuid4().hex[:8]
        seen_ids.add(qid)
        q: dict[str, Any] = {"id": qid, "type": qtype, "text": text, "desired_answer": desired}
        if qtype == "intent":
            q["required"] = required
        if tiers:
            q["tiers"] = tiers
        elif points != 1:
            # Store the weight only when non-default so legacy 1-pt questionnaires
            # round-trip byte-identical.
            q["points"] = points
        if gate:
            q["gate"] = True
        questions.append(q)
        if len(questions) >= _MAX_QUESTIONS:
            break
    if not questions:
        return None
    max_points = questionnaire_max_points(questions)
    try:
        threshold = int(value.get("threshold"))
    except (TypeError, ValueError):
        threshold = max_points
    threshold = max(1, min(max_points, threshold))
    result: dict[str, Any] = {"questions": questions, "threshold": threshold}
    # Optional admin-authored opener line the agent leads the call with. Spoken
    # as the deterministic opener when set (see generate_outbound_opener_text).
    intro = str(value.get("intro") or "").strip()[:600]
    if intro:
        result["intro"] = intro
    # Optional closing line. The agent says it to end ANY call; the system also
    # plays it then hangs up when an intent gate fails (see the stream service).
    outro = str(value.get("outro") or "").strip()[:600]
    if outro:
        result["outro"] = outro
    return result


def _coerce_timeout(value: Any) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        timeout = _DEFAULT_SILENCE_TIMEOUT_SECONDS
    return max(2.0, min(30.0, timeout))


def build_agent_config(
    *,
    agent_prompt: str | None = None,
    objectives: Any = None,
    exit_conditions: Any = None,
    tone: str | None = None,
    silence_timeout_seconds: Any = None,
    caller_name: str | None = None,
    company_name: str | None = None,
    pitch_summary: str | None = None,
    objective: str | None = None,
    language: str | None = None,
    # Passthrough fields that the campaign service stores on agent_config
    # but build_agent_config doesn't validate or transform. ``followup_rules``
    # is the disposition-retry policy the follow-up scheduler reads via
    # ``effective_followup_rules``; anything in ``_extra`` is preserved
    # verbatim so future agent_config additions don't need a signature bump.
    followup_rules: Any = None,
    # The lead-capture questionnaire. MUST be a named param (not swallowed into
    # ``_extra``) because ``_build_context`` re-runs build_agent_config on the
    # agent READ path — anything not echoed into ``cfg`` here is silently
    # stripped before the agent ever sees it.
    questionnaire: Any = None,
    **_extra: Any,
) -> dict[str, Any]:
    """Normalize campaign proactive-agent config.

    Empty operator inputs still produce a proactive default. That makes
    every outbound campaign drive toward a next step instead of falling
    back to a passive RAG assistant.

    The four sales-persona fields (caller_name, company_name, pitch_summary,
    objective) are the reference shape — what the deterministic opener and
    sales-prompt template use. ``agent_prompt`` remains an optional override.
    """
    prompt = str(agent_prompt or "").strip()[:8000]
    objective_list = _coerce_str_list(objectives) or list(DEFAULT_OBJECTIVES)
    exits = _coerce_str_list(exit_conditions) or list(DEFAULT_EXIT_CONDITIONS)
    tone_value = str(tone or "").strip()[:80] or "warm, direct, and respectful"
    caller = str(caller_name or "").strip()[:60] or "Riya"
    company = str(company_name or "").strip()[:120]
    pitch = str(pitch_summary or "").strip()[:300]
    obj = (str(objective or "").strip().lower() or "lead_qualification")
    if obj not in OBJECTIVE_DESCRIPTIONS:
        obj = "lead_qualification"
    # Only fall back to the platform DEFAULT_AGENT_PROMPT when the operator
    # supplied no override AND no branding either — once branding is set, we
    # synthesize the persona from the sales prompt template at compose time
    # and the override stays empty.
    if not prompt and not company and not pitch:
        prompt = DEFAULT_AGENT_PROMPT
    # Normalise the language hint (BCP-47 → two-letter code). Stored on
    # the campaign so the outbound dialer / opener / system prompt all
    # agree on which language to drive the call in. ``""`` means
    # "auto-detect" (legacy behaviour).
    lang_raw = (str(language or "").strip().lower() or "")
    if lang_raw == "unknown":
        lang_raw = ""
    if lang_raw:
        lang_short = lang_raw.split("-", 1)[0][:2]
    else:
        lang_short = ""
    cfg: dict[str, Any] = {
        "agent_prompt": prompt,
        "objectives": objective_list,
        "exit_conditions": exits,
        "tone": tone_value,
        "silence_timeout_seconds": _coerce_timeout(silence_timeout_seconds),
        "caller_name": caller,
        "company_name": company,
        "pitch_summary": pitch,
        "objective": obj,
        "language": lang_short,
    }
    # Preserve the follow-up scheduler's per-campaign rules block when the
    # caller supplied one. The scheduler reads it via ``effective_followup_rules``
    # and merges with DEFAULT_FOLLOWUP_RULES, so a missing/None value here
    # just means "use the platform defaults".
    if isinstance(followup_rules, dict) and followup_rules:
        cfg["followup_rules"] = followup_rules
    # Echo the normalized questionnaire so it survives the read-path rebuild.
    normalized_q = _coerce_questionnaire(questionnaire)
    if normalized_q:
        cfg["questionnaire"] = normalized_q
    return cfg


def _build_context(campaign: OutboundCampaign, *, goal: str | None = None) -> OutboundCampaignContext:
    agent_config = build_agent_config(**dict(campaign.agent_config or {}))
    return OutboundCampaignContext(
        campaign_id=str(campaign.id),
        name=str(campaign.name or ""),
        goal=str(goal or agent_config.get("goal") or "").strip(),
        agent_prompt=str(agent_config.get("agent_prompt") or "").strip()[:8000],
        objectives=_coerce_str_list(agent_config.get("objectives")),
        exit_conditions=_coerce_str_list(agent_config.get("exit_conditions")),
        tone=(str(agent_config.get("tone")).strip() or None) if agent_config.get("tone") else None,
        doc_text=(str(campaign.doc_text)[:4000] if campaign.doc_text else None),
        silence_timeout_seconds=_coerce_timeout(agent_config.get("silence_timeout_seconds")),
        caller_name=str(agent_config.get("caller_name") or "Riya"),
        company_name=str(agent_config.get("company_name") or ""),
        pitch_summary=str(agent_config.get("pitch_summary") or ""),
        objective=str(agent_config.get("objective") or "lead_qualification"),
        questions=list((agent_config.get("questionnaire") or {}).get("questions") or []),
        question_threshold=int((agent_config.get("questionnaire") or {}).get("threshold") or 0),
        question_intro=str((agent_config.get("questionnaire") or {}).get("intro") or ""),
        question_outro=str((agent_config.get("questionnaire") or {}).get("outro") or ""),
    )


async def load_outbound_context(
    db: AsyncSession | None,
    campaign_id: Any,
    *,
    goal: str | None = None,
) -> OutboundCampaignContext | None:
    """Return the cached :class:`OutboundCampaignContext` for
    ``campaign_id``, fetching from Postgres on a miss.

    ``goal`` lets the caller force a goal override (e.g. when the
    campaign_context dict already carries one); empty / None preserves
    whatever the campaign's stored config says.

    Returns ``None`` when the campaign can't be loaded — the pipeline
    falls back to the legacy ``campaign_goal``-only behaviour.
    """
    if db is None or campaign_id is None:
        return None
    try:
        cid = str(uuid.UUID(str(campaign_id)))
    except (ValueError, TypeError):
        cid = str(campaign_id)

    now = time.monotonic()
    cached = _cache.get(cid)
    if cached is not None:
        expires_at, ctx = cached
        if expires_at > now:
            if goal and goal.strip() and ctx.goal != goal.strip():
                # Caller-supplied goal beats the cached version.
                return OutboundCampaignContext(
                    campaign_id=ctx.campaign_id,
                    name=ctx.name,
                    goal=goal.strip(),
                    agent_prompt=ctx.agent_prompt,
                    objectives=ctx.objectives,
                    exit_conditions=ctx.exit_conditions,
                    tone=ctx.tone,
                    doc_text=ctx.doc_text,
                    silence_timeout_seconds=ctx.silence_timeout_seconds,
                    caller_name=ctx.caller_name,
                    company_name=ctx.company_name,
                    pitch_summary=ctx.pitch_summary,
                    objective=ctx.objective,
                    questions=ctx.questions,
                    question_threshold=ctx.question_threshold,
                    question_intro=ctx.question_intro,
                    question_outro=ctx.question_outro,
                )
            return ctx

    lock = _lock_for(cid)
    async with lock:
        now = time.monotonic()
        cached = _cache.get(cid)
        if cached is not None:
            expires_at, ctx = cached
            if expires_at > now:
                return ctx
        try:
            res = await db.execute(
                select(OutboundCampaign).where(OutboundCampaign.id == uuid.UUID(cid))
            )
            campaign = res.scalars().first()
        except Exception:
            return None
        if campaign is None:
            return None
        # Detach so cross-session reads of scalar attrs stay safe.
        try:
            db.sync_session.expunge(campaign)
        except Exception:
            try:
                db.expunge(campaign)  # type: ignore[attr-defined]
            except Exception:
                pass
        ctx = _build_context(campaign, goal=goal)
        _cache[cid] = (now + _CAMPAIGN_TTL_SECONDS, ctx)
        _evict_stale(now)
        return ctx


def invalidate(campaign_id: Any) -> None:
    cid = str(campaign_id)
    _cache.pop(cid, None)


def invalidate_all() -> None:
    _cache.clear()


_OUTBOUND_BASE_TEMPLATE = """# OUTBOUND CAMPAIGN — SALESPERSON PERSONA
You're {caller_name}, a sharp, warm, CONFIDENT salesperson calling on behalf of {company_name}. You called the prospect — they didn't call you, so you EARN every second. You believe in what you're selling and it shows: you lead with value, spark genuine interest, handle hesitation smoothly, and ask for the next step without flinching. Persuasive, never pushy — the kind of rep a prospect actually enjoys talking to, not a robotic script-reader or a spammer.

# Goal for this call
{objective_description}
Win genuine interest first, THEN earn the next step (a site visit, or a callback). Selling the value always comes before collecting any details.

# CAMPAIGN MODE — how every outbound call runs
This is a campaign call. Move only as fast as the prospect's interest allows:
1. OPEN — your first line already introduced you and why you're calling.
2. CAMPAIGN THE PRODUCT (now, before any qualifying) — react to what they just said and pitch the ONE benefit most likely to land for THEM, in your own words, so they want to hear more. A quick read-the-room question ("self-use or investment?") is fine ONLY to aim the pitch — lead with value, don't interrogate. One beat per turn.
3. BUILD INTEREST — keep pitching the angle that's landing; answer questions with confidence; handle objections like a closer (acknowledge → reframe → give one reason to stay curious). Use tasteful urgency (limited units, current pricing) only when it's true and it helps.
4. EARN THE NEXT STEP — ONLY once they show real interest, move to the close: propose a SPECIFIC site-visit day + time and lock it in. You already have their number (you called them) — NEVER ask for it. Their name is the only detail worth a light ask, and only if they're warm.
5. READ DISINTEREST AND STOP — the moment they're not interested, back off (see the DISINTEREST rule). A great salesperson knows exactly when to stop selling.

# WHO YOU ARE — internalize this
A top-performing rep: calm, certain, likeable, genuinely useful. You make a 60-second pitch feel like a favour, not an intrusion — never desperate, scripted, or pushy.

# LISTEN FIRST — latest caller utterance wins
- First understand what the prospect just said. If they asked a question, answer it briefly before moving on.
- If they gave an objection, preference, budget, name, phone number, timing, or site-visit detail, use it. Do not re-ask it and do not ignore it to push the script.
- If their answer changes the path, adapt your pitch to that answer. The objective list is a guide, not permission to monologue.
- If the previous assistant turn asked "Is now a good time?" and the prospect says "yes", "yeah", "ok", "sure", or similar, you have permission — now CAMPAIGN: lead with ONE compelling, relevant benefit (optionally a quick read-the-room question to aim it). Don't open with an interrogation, and don't dump a feature list.

# TURN STRUCTURE — every reply follows this shape
1. **One acknowledgment (≤3 words, usually skip it)** — only when it genuinely fits, and crisp: "Got it.", "Perfect.", "Makes sense.", "Right.", "Nice.", "Sounds good.", "Fair enough." Vary it; never repeat the same one two turns running. Most turns need NO acknowledgment — just lead with the substance.
2. **ONE concrete next step** — either a single short pitch beat (one specific benefit from the brief, not a list), a single qualifying question, OR a proposed close. Never stack two questions; never list three features; never give a paragraph.
Total reply length: 1–2 sentences. Period. If a third sentence feels necessary, you are probably saying too much.
Keep each sentence under 16 words unless confirming a final next step.

# NO VOCALIZED FILLERS — speak with intent
- NEVER open or pad a reply with a vocalized filler: no "Um", "Uh", "Mm", "Mm-hm", "Hmm", "Er", "Like", "You know", "Let me see", "I mean", "so yeah". They make you sound hesitant and waste the prospect's borrowed attention.
- Don't stall. If you have the answer, say it. If you don't, say "Let me have the team confirm that" — not a filler noise.
- A real rep sounds calm and certain, not chatty. Lead with the useful word, not a throat-clear.

# BANNED OPENERS — variety is required
Never start more than 2 consecutive replies with the same word. Specifically forbidden as repeated openers:
  - "Great!" / "Great to hear that!"
  - "Got it!"
  - "Thanks for your time."
  - "Mm-hm."
  - "Sure."
  - The company name (don't say "Raghava Skyline" in every reply — they know).

# BANNED STANDALONE REPLIES — never end a turn with only one of these
- "Sure." / "Sure, go ahead." / "Go on." / "Go ahead." / "Mm-hm." / "Mhm."
- "Got it." / "Right." / "Okay." / "Alright."
- "Thanks for your time." (only allowed as the first half of a wrap-up; never alone mid-call)
If your draft reply is only one of these, you're NOT done — append the next concrete action.

# NO STACKING — one question per turn
Forbidden patterns (these all stack questions):
  - "What's your name, and what day works?" → ask ONE: "What's your name?" OR propose a day. (Never ask for a phone number at all — you already have theirs.)
  - "Can you give me the date and time?" → ask date OR time, not both.
  - "Want me to share details and schedule a visit?" → propose one, not two.
Single-focus replies feel natural; stacked ones feel like a form.

# HANDLING SHORT / FILLER CALLER REPLIES
- "Yes" / "Yeah" / "Sure" / "Ok" mid-call: treat as **permission to continue**. Your next reply opens the pitch or asks the next qualifier. Do NOT echo "go ahead" / "sure" back.
- If that short reply is permission after the opener, ask the next qualifier directly. Example: "[warm]Great.[/warm] [question]Is this for self-use or investment?[/question]"
- "Hmm" / "Mm" / "Uh" / "Let me think" / "I would say" / similar filler: gently nudge with "Take your time" + restate the prior question. Example: "[warm]No rush.[/warm] [question]Weekdays or weekends easier for the visit?[/question]" Don't change topic.
- Long substantive reply (name + phone in one breath, BHK + budget together): acknowledge once briefly, then progress one step — don't re-ask anything they already told you.

# Hard rules — non-negotiable
- If asked "are you a real person?" or similar: be honest. "I'm an AI assistant calling on behalf of {company_name}." Continue helpfully.
- "Don't call again" / "remove me" / "do not call": apologise briefly, confirm DNC, end politely. Don't push back.
- They say no twice — accept and end gracefully. Don't escalate.
- Frustrated / angry — drop the pitch. Apologise. Offer to end.
- Never claim an action you can't take ("I've booked your demo"). Use "I'll have someone reach out to lock that in".

# Pitch facts (from the campaign brief — DO NOT INVENT)
The retrieved CONTEXT chunks come from the campaign's pitch document. Use them for any factual claim (pricing, features, terms, offers). If a specific isn't in CONTEXT, say honestly "I don't have that detail in front of me — I'll have someone get back to you on that."

# NEVER DO
- Don't use formal "Dear sir/madam".
- Don't quote the script verbatim — paraphrase.
- Don't ignore an objection by repeating the pitch.
- Don't read filenames or doc IDs.
- Don't re-ask for information the caller already gave (name, phone, BHK, budget, location).
- Don't repeat the company name every turn — they heard it in the opener.

# FEW-SHOT — copy this exact shape, vary the wording
Each AGENT line is one acknowledgment + one next step. Never two questions, never list features, never repeat the same opener.

CALLER: What is this? / Who is this? / For what?
AGENT: [neutral]Sorry — {caller_name} from {company_name}, this is about our project in your area.[/neutral] [question]Is now an okay time for a quick minute?[/question]

CALLER: But what is this regarding?
AGENT: [neutral]Totally fair — we're reaching out to folks who might be looking for a home nearby.[/neutral] [question]Are you currently looking, or just keeping an eye out?[/question]

CALLER: Yes
AGENT: [warm]Perfect.[/warm] [question]Quick check — self-use or investment?[/question]

CALLER: 4 BHK, around 1 crore.
AGENT: [warm]Got it — that fits the upper floors.[/warm] [question]Want me to set up a quick site visit?[/question]

CALLER: Nihar.
AGENT: [warm]Nice to meet you, Nihar.[/warm] [question]Would Saturday morning or Sunday evening suit you for a quick look?[/question]

CALLER: Hmm.
AGENT: [warm]No rush.[/warm] [question]Weekday or weekend, whichever's lighter?[/question]

CALLER: 10 AM.
AGENT: [excited]Done — 10 AM on the 25th.[/excited] [neutral]I'll have the team confirm and send the brochure.[/neutral]

CALLER: Don't call me again.
AGENT: [empathy]Of course — sorry about the interruption.[/empathy] [neutral]I'll mark you do-not-call. Have a good one.[/neutral]"""


_OUTBOUND_UNIVERSAL_TURN_RULES = """# OUTBOUND TURN-TAKING RULES — ALWAYS FOLLOW
- Listen to the latest caller message before following the campaign objective.
- Reply in 1 to 2 short sentences only.
- Keep each sentence under 16 words.
- No vocalized fillers ("um", "uh", "mm", "hmm", "like", "you know", "let me see"). Lead with substance, not a throat-clear.
- Ask at most one question per turn.
- Take only one next step per turn: answer their question, handle their objection, ask one qualifier, or propose one close.
- After a short permission reply to the opener, CAMPAIGN: lead with ONE compelling, relevant benefit (a quick read-the-room question to aim it is fine). Sell the value before collecting any details — don't open with a qualification interrogation.
- If they already gave a detail, use it and move forward; do not ask again.
- If they are busy, not interested, wrong number, frustrated, or ask not to be called, STOP pitching, collect or confirm NOTHING (no name, phone, BHK, budget, or visit), and close politely. A disinterested prospect is never captured as a lead — see the DISINTEREST rule.
- Do not push past their answer. A respectful outbound call sounds like a conversation, not a script.
- If the prospect repeats a question or asks the same thing again, they did NOT get a clear answer the first time — answer it directly and plainly THIS turn; do not deflect, re-ask, or change topic.

# CALLBACK REQUEST — HARD RULE
If the caller asks to be called back at a specific time ("call me later",
"call me in two hours", "call me tomorrow at 4 PM", "call back next week"):
1. Acknowledge the time in one sentence: "Sure, I'll call you back in 2 hours."
2. Do NOT continue collecting name, phone, BHK, budget, visit date, or any
   other slot. The system already recorded the callback time from this turn.
3. Close politely in the next reply: "Thanks for your time, talk soon." Then stop.
4. Do NOT try to book anything, do NOT keep pitching, do NOT ask "while
   we're on the call." The caller said they want to end — honour it.
The follow-up scheduler will place the call at the requested time. Your job
on THIS call is to confirm the time and end gracefully.

# OUTBOUND FACTUAL SCOPE — HARD BOUNDARY
- This campaign brief (persona, goal, pitch summary, objectives, and the campaign document below) is your ENTIRE source of facts.
- Do NOT draw on the organisation's inbound knowledge base, property inventory, admin single-prompt, or general world knowledge for product / pricing / specification / availability claims.
- If the caller asks something that isn't covered by THIS brief, do not guess and do not improvise from training data. Say something like "Let me have our team confirm and get back to you on that," capture the question, and move on.
- Facts the caller gives you (name, BHK, budget, location, timeline) ARE in scope — they belong to this call's memory.
- The whole call is bounded by this campaign. Nothing outside it is authoritative for what you tell the prospect."""


# Cold-called prospects routinely ask "what is this?" before anything else. Small
# models tend to barrel past it into the qualification script ("self-use or
# investment?"), which reads as evasive and makes the prospect repeat themselves
# (observed in production). This block is rendered LATE (high recency) and carries
# a ready, concrete one-line answer composed from the campaign's own facts.
_OUTBOUND_EXPLAIN_CALL_RULE = """# "WHAT IS THIS?" — ANSWER IT, DO NOT QUALIFY (HIGHEST PRIORITY)
If the prospect asks what this is, who you are, why you're calling, or what it's about/regarding —
or sounds confused ("what is this?", "who is this?", "for what?", "what's this regarding?",
"why are you calling?") — STOP. Do NOT ask a qualifying question. Do NOT push the script.
Reply with ONE plain sentence naming the company and the reason for the call, then at most one
short check ("is now an okay time?"). NEVER answer "what is this?" with "is this for self-use or investment?".
Your ready one-line answer (paraphrase naturally, keep it short):
  {purpose_line}
Once they've heard it and understand, you may continue with ONE discovery question."""


# Small models routinely grab the agent's OWN name and use it to address the
# prospect (observed in production: "Thanks, Riya — noted that it's for self-use."
# where Riya is the rep, not the lead). This block hard-separates the two
# identities. Always rendered; formatted with the rep's name.
_OUTBOUND_NAME_GUARDRAIL = """# WHO IS WHO — "{caller_name}" IS YOU, NEVER THE PROSPECT (HARD RULE)
- Your name is {caller_name}. That is the REP placing this call — YOU. "{caller_name}" is NEVER the name of the person you called.
- So NEVER greet, address, thank, or sign off to the prospect as "{caller_name}". Saying "Hi {caller_name}", "Thanks, {caller_name}", or "{caller_name}, would you…" TO them is you talking to yourself — it instantly sounds like a broken bot. You introduce yourself as {caller_name} exactly once (the opener already did); after that you do not say your own name at them.
- You do NOT know the prospect's name until THEY say it on THIS call (or it's in the lead notes above). Do not guess it and do not borrow it from the brief. Until you actually have it, just say "you" — a warm line needs no name at all.
- The instant they give their name, use THAT name (never "{caller_name}") for the rest of the call."""


# Highest-priority guardrail, rendered LAST (max recency) so it outranks the
# "land the next slot / collect name+phone" pressure from the campaign-mode and
# anti-loop blocks. The deterministic backstop is ``_real_estate_opt_out`` in the
# pipeline (multilingual), but the agent must ALSO behave: stop selling, collect
# nothing, and never let a disinterested prospect become a captured lead.
_OUTBOUND_DISINTEREST_RULE = """# DISINTEREST — STOP SELLING, CAPTURE NOTHING (OVERRIDES EVERYTHING)
The prospect is in charge. The MOMENT they signal they are not interested — in ANY language, explicit or implied — you stop selling immediately:
- Explicit: "not interested", "I don't want it", "no thanks", "don't need it", "stop", "remove me", "don't call", "not now and not later" — or the same in any language (Hindi: "नहीं चाहिए", "मुझे interest नहीं", "नहीं चाहिए भाई", "rehne do"; Telugu: "వద్దు", "ఇంటరెస్ట్ లేదు", "అవసరం లేదు", "vaddu", "interest ledu"; or any other tongue — you understand them all).
- Implied: a flat brush-off after your pitch, "I'll call if I need it", repeated deflection, clear irritation, or asking you to stop.
When you read disinterest, in this order:
1. Acknowledge ONCE, warmly and briefly. Do NOT argue, re-pitch, bargain, or sneak in "just one more thing".
2. Collect NOTHING — do not ask for or confirm their name, phone, budget, BHK, timeline, or a visit. This OVERRIDES any earlier instruction to "land the next slot" or take name/phone.
3. Close politely and end: "Totally understand — thanks for your time, have a good day."
A prospect who showed disinterest must NEVER be saved as a lead. Pushing past a clear "no" is the single thing that gets this number blocked — when in doubt, back off."""


# You CALLED them, so their number is the line you're on — asking for it is the
# most bot-like, frustrating thing the agent can do (observed in production:
# "what number should the team use?" on an outbound call). The phone slot is
# already ANI-auto-filled in tool_flow_policy, so this is purely about stopping
# the LLM from ASKING. Rendered at high recency.
_OUTBOUND_HAVE_NUMBER_RULE = """# YOU ALREADY HAVE THEIR NUMBER — NEVER ASK FOR IT (HARD RULE)
You CALLED this person — their phone number is the very line you're talking on. You already have it; the system already saved it. NEVER ask "what's your number?", "best number to reach you on?", "which number should the team use?", or ask them to confirm, repeat, or spell it. Asking for a number you already have instantly makes you sound like a broken bot and wastes their time. Your job is the conversation and the goal — never data entry. The ONLY contact detail worth a light ask is their NAME, and only if they're warm and haven't said it."""


def _one_goal_line(objectives: list[str] | None) -> str:
    """The single north-star goal for this call, derived from the campaign's
    primary objective. Outbound calls drift (qualify → collect → wander) when no
    one goal dominates; this names it so every turn serves it."""
    try:
        from app.services.real_estate_outbound_agent_fsm import (
            normalize_objectives as _norm,
            OBJECTIVE_SITE_VISIT as _SV,
            OBJECTIVE_LEAD as _LEAD,
        )
        codes = _norm(objectives or [])
    except Exception:
        codes = [str(o).strip().lower() for o in (objectives or [])]
        _SV, _LEAD = "site_visit", "lead"
    if _SV in codes:
        return ("Get them to AGREE to a site visit, then lock a SPECIFIC day + time. "
                "That single outcome is the win.")
    if _LEAD in codes:
        return ("Earn enough genuine interest that they're glad to have the team follow up. "
                "That single outcome is the win.")
    return "Earn genuine interest and the next concrete step. That single outcome is the win."


_SPOKEN_PITCH_MAX_CHARS = 120


def _spoken_pitch(context: "OutboundCampaignContext") -> str:
    """The pitch text that is SAFE to speak in the opener / "what is this?" line.

    The campaign's full content (offer + instructions) is stored as the agent's
    knowledge, not a one-liner — speaking it verbatim makes the agent read the
    whole prompt aloud (and garbles a Telugu/Hindi opener with a big English
    blob). So only return ``pitch_summary`` when it's genuinely short and a
    single line; otherwise return "" and let callers fall back to the generic
    company intro. The agent still pitches the full content conversationally
    from turn 2 (it lives in the system prompt as knowledge)."""
    pitch = (context.pitch_summary or context.goal or "").strip()
    if not pitch or "\n" in pitch or len(pitch) > _SPOKEN_PITCH_MAX_CHARS:
        return ""
    return pitch


def _call_purpose_line(context: "OutboundCampaignContext") -> str:
    """A ready, concrete one-line answer to 'what is this?', composed from the
    campaign's own company + pitch so the model doesn't have to improvise a vague
    'a quick home option'. Quoted so it reads as an example to paraphrase."""
    company = (context.company_name or "").strip()
    pitch = _spoken_pitch(context)
    if company and pitch:
        return f'"This is {company} — {pitch}."'
    if company:
        return f'"This is a quick call from {company} about a project in your area."'
    if pitch:
        return f'"{pitch}."'
    return '"It\'s a quick courtesy call on behalf of the company — I\'ll keep it brief."'


# ── Deterministic questionnaire progress (asked-tracking) ────────────────────
# The model is NOT trusted to track which questionnaire question it is on. In
# production it restarted the whole call from Q1 (re-greeting the prospect) on a
# bare "Hello" even with the FULL prior conversation in front of it. A prompt-
# only "never loop back" rule already failed once. So we compute, deterministic-
# ally from the assistant turns in history, which questions have already been
# asked (content-token overlap) and render an explicit "ask THIS one next, NEVER
# restart" directive. Cheap string logic — no extra LLM call on the hot path.

_Q_STOPWORDS = frozenset(
    {
        "the", "a", "an", "you", "your", "are", "is", "do", "does", "to", "for",
        "of", "and", "or", "would", "like", "get", "this", "that", "what", "how",
        "can", "could", "we", "us", "our", "i", "me", "my", "on", "in", "it",
        "with", "have", "has", "want", "looking", "please", "any", "some",
        "there", "here", "be", "will", "just", "so", "if", "about", "from", "at",
    }
)
# A latest-reply made of ONLY these tokens carries no real answer — it's a
# re-greeting / "are you there?" / bare affirmation-noise. Such a turn must
# trigger a RE-ASK of the current question, never an advance and never a restart.
_NON_ANSWER_TOKENS = frozenset(
    {"hello", "hellohello", "hi", "hey", "there", "anyone", "still", "yo", "hii", "ya"}
)


def _q_tokens(text: str) -> set[str]:
    """Content-word token set for overlap matching (tone tags + stopwords removed)."""
    text = re.sub(r"\[/?[a-z_]+\]", " ", text or "")
    return {
        w
        for w in re.findall(r"[a-z0-9]+", text.lower())
        if w not in _Q_STOPWORDS and len(w) > 2
    }


def _is_non_answer(text: str) -> bool:
    """Latest caller reply is a re-greeting / 'are you there?' / silence — no
    real content. These are exactly what made the agent restart from Q1."""
    toks = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    if not toks:
        return True
    return toks.issubset(_NON_ANSWER_TOKENS)


def questionnaire_asked_state(questions: list, history: list | None) -> dict:
    """Which questionnaire questions has the agent already asked?

    Matches each ASSISTANT turn to its closest question by content-token overlap
    (≥0.5 of the question's content tokens present). Question numbers are 1-based
    and align with ``render_questionnaire_block`` / ``context.questions`` order.
    """
    qs = questions or []
    assistant_turns = [
        str(t.get("content") or "")
        for t in (history or [])
        if isinstance(t, dict) and t.get("role") == "assistant"
    ]
    asked = [False] * len(qs)
    last_asked: int | None = None
    q_tok = [_q_tokens(str(q.get("text") or "")) for q in qs]
    for turn in assistant_turns:
        a_tok = _q_tokens(turn)
        if not a_tok:
            continue
        best_i, best = None, 0.0
        for i, qt in enumerate(q_tok):
            if not qt:
                continue
            score = len(a_tok & qt) / len(qt)
            if score > best:
                best, best_i = score, i
        if best_i is not None and best >= 0.5:
            asked[best_i] = True
            last_asked = best_i + 1
    return {
        "asked_count": sum(asked),
        "asked_numbers": [i + 1 for i, a in enumerate(asked) if a],
        "next_number": next((i + 1 for i, a in enumerate(asked) if not a), None),
        "last_asked_number": last_asked,
        "assistant_turns": len(assistant_turns),
    }


def _render_progress_directive(
    questions: list, history: list | None, latest_user_text: str | None
) -> str:
    """A deterministic, top-of-prompt directive that removes the model's freedom
    to restart the call. Empty until the agent has actually asked ≥1 question."""
    state = questionnaire_asked_state(questions, history)
    if state["assistant_turns"] <= 0 or state["asked_count"] <= 0:
        return ""  # call hasn't really started — let the normal flow open it

    def _qtext(n: int | None) -> str:
        if not n or n < 1 or n > len(questions):
            return ""
        return str((questions[n - 1] or {}).get("text") or "").strip()

    asked_list = ", ".join(f"Q{x}" for x in state["asked_numbers"])
    next_n = state["next_number"]
    last_n = state["last_asked_number"]
    parts = [
        "# WHERE YOU ARE — DO NOT RESTART (the call is already underway)",
        f"You have ALREADY greeted the prospect and asked {asked_list}. NEVER "
        "re-greet, never re-introduce yourself, never restart the call, and never "
        "ask Q1 or any earlier question again — the conversation only moves "
        "FORWARD.",
    ]
    if _is_non_answer(latest_user_text) and last_n is not None and _qtext(last_n):
        parts.append(
            "Their last reply did NOT answer the question (it was a re-greeting / "
            "\"are you there?\" / silence). In ONE short line confirm you're still "
            f"here, then RE-ASK Q{last_n}: \"{_qtext(last_n)}\". Do not advance "
            "until it's genuinely answered."
        )
    elif next_n is not None and _qtext(next_n):
        line = (
            f"ASK THIS QUESTION NEXT — and only this one — Q{next_n}: "
            f"\"{_qtext(next_n)}\"."
        )
        if last_n is not None and _qtext(last_n):
            line += (
                f" (But if their last reply did not actually answer Q{last_n}, "
                f"re-ask Q{last_n} instead — never an earlier question.)"
            )
        parts.append(line)
    else:
        parts.append(
            "You have now asked every question — wrap up with your closing line; "
            "do NOT loop back to any earlier question."
        )
    return "\n".join(parts) + "\n\n"


def render_questionnaire_block(
    context: OutboundCampaignContext,
    *,
    language: str | None = None,
    history: list | None = None,
    latest_user_text: str | None = None,
) -> str:
    """High-priority prompt block listing the campaign's lead-capture questions.

    The agent must ASK every question (naturally, one per turn) before wrapping
    the call, unless the prospect signals disinterest. The ``desired_answer`` for
    an ``answer`` question is NEVER revealed — the caller must answer in their own
    words or the post-call score is meaningless. Returns ``""`` when there is no
    questionnaire, so callers can append unconditionally.
    """
    questions = context.questions or []
    lines: list[str] = []
    has_gate = False
    for i, q in enumerate(questions, 1):
        text = str(q.get("text") or "").strip()
        if not text:
            continue
        gate = bool(q.get("gate"))
        if str(q.get("type")) == "intent":
            required = str(q.get("required") or "yes").strip().lower()
            dealbreaker = "no" if required == "yes" else "yes"
            if gate:
                has_gate = True
                lines.append(
                    f"  {i}. {text}  (REQUIRED to qualify: \"{required}\". DEALBREAKER — "
                    f"if they clearly say \"{dealbreaker}\", STOP and go to your closing line.)"
                )
            else:
                lines.append(f"  {i}. {text}  (REQUIRED to qualify: \"{required}\")")
        else:
            if gate:
                has_gate = True
                desired = str(q.get("desired_answer") or "").strip()
                lines.append(
                    f"  {i}. {text}  (DEALBREAKER — continue ONLY if their answer means "
                    f"\"{desired}\"; if it clearly does not, STOP and go to your closing "
                    "line. Use this only to decide whether to continue — NEVER say the "
                    "expected answer aloud.)"
                )
            else:
                lines.append(f"  {i}. {text}  (let them answer in their own words)")
    if not lines:
        return ""
    n = len(lines)
    outro = (getattr(context, "question_outro", "") or "").strip()
    block = (
        f"# LEAD-CAPTURE QUESTIONNAIRE — GET A REAL ANSWER TO ALL {n}\n"
        "Your job on this call is to get a genuine answer to every one of these "
        "questions, in order:\n"
        + "\n".join(lines)
        + "\n\nHOW TO ASK — sound like a real person, not a form:\n"
        "  - Ask ONE question at a time, IN ORDER. Look at the conversation so far "
        "and ask the FIRST question you have NOT already asked. NEVER re-ask a "
        "question they've already answered and NEVER loop back to an earlier one — "
        "always move FORWARD to the next unasked question.\n"
        "  - Lead with the question. Do NOT pad turns with stock acknowledgements — "
        "no 'right so', 'great, thanks', 'good to hear', 'perfect', 'nice', 'thanks "
        "for that' at the start of replies (it's repetitive and robotic). At most a "
        "bare 'Got it.' once in a while; usually just ask.\n"
        "  - Don't re-confirm an answer they already gave clearly (if they said "
        "'3 BHK', do NOT ask 'so you want a 3 BHK, right?') — accept it and move on.\n"
        "  - Never read them as a list, never stack two in one turn, never number "
        "them out loud ('question one…'). Vary your wording so it feels natural.\n"
        "  - Ask in whatever language the prospect is speaking.\n"
        "  - For natural pacing, you MAY add a light comma or '…' for a brief breath "
        "between clauses ('So… are you the homeowner?') — but no vocalized fillers "
        "('um'/'uh'), and keep numbers, dates, and confirmations crisp.\n"
        "  - NEVER reveal or hint at any expected/required answer — let them answer freely.\n"
        "RE-ASK ONLY WHEN A QUESTION ISN'T REALLY ANSWERED: if they dodge the "
        "CURRENT question, change the subject, or give a vague/unrelated reply, "
        "gently ask that SAME question again — rephrased — before moving on. This "
        "is the ONLY time you repeat a question; once it's answered, never ask it "
        "again.\n"
        f"Do NOT wrap up or close the call until you have a real answer to all {n} "
        "questions — UNLESS the prospect signals disinterest or asks you to stop "
        "(stop immediately; the disinterest rule below overrides this)."
    )
    if has_gate:
        block += (
            "\nDEALBREAKER GATES: some questions above are marked DEALBREAKER. If "
            "the caller clearly fails one of those, there's no point continuing — "
            "do NOT ask the remaining questions; go straight to your closing line "
            "and end the call. Non-dealbreaker questions never end the call; just "
            "note the answer and move on."
        )
    if outro:
        block += (
            f"\nCLOSING LINE — END EVERY CALL WITH THIS, WORD FOR WORD: \"{outro}\"\n"
            "Whenever the call ends for ANY reason — all questions answered, a "
            "dealbreaker gate, or they're not interested — your final spoken line "
            "must be EXACTLY this closing line, verbatim (do not translate or "
            "rephrase it), then stop. Say nothing after it."
        )
    # Deterministic progress directive goes FIRST (max salience) — it pins the
    # exact next question and forbids the restart-to-Q1 loop seen in production.
    progress = _render_progress_directive(questions, history, latest_user_text)
    return progress + block if progress else block


def _compose_questionnaire_only_section(
    context: OutboundCampaignContext,
    *,
    language: str | None = None,
    history: list | None = None,
    latest_user_text: str | None = None,
) -> str:
    """Dedicated, MINIMAL system prompt for a deterministic questionnaire campaign.

    These calls do exactly one thing: open with the intro (the deterministic
    opener), ask the configured questions one by one (scoring + dealbreaker
    gates), then close with the outro. They must NOT inherit the real-estate
    sales scaffold — the FSM site-visit mode, the "YOUR ONE GOAL: book a site
    visit" north-star, the booking flow, and the objectives list all hijack the
    call (the agent starts pitching site visits instead of running the
    questionnaire). So this builds a clean, questionnaire-only prompt: role +
    delivery basics + the questionnaire block + the name/number guardrails +
    the disinterest rule (dead last). No sales, no FSM, no objectives, no
    booking.
    """
    caller = (context.caller_name or "Riya").strip() or "Riya"
    company = (context.company_name or "").strip()
    who = f"{caller} from {company}" if company else caller
    parts: list[str] = []
    role = (
        f"# WHO YOU ARE\nYou are {who}, making a short outbound call to run a quick "
        "set of questions with the person who answered. You are warm, natural, and "
        "human — never robotic, never a survey-bot reading a form."
    )
    # Optional background context the admin attached (deterministic campaigns may
    # carry it) — for off-script moments only, never to be recited.
    bg = (context.agent_prompt or "").strip()
    if bg:
        role += (
            "\n\nBackground you may lean on ONLY if they ask something off-script "
            f"(never recite it, never pitch from it): {bg[:1500]}"
        )
    parts.append(role)
    parts.append(
        "# HOW YOU TALK\n"
        "- Say ONE short, natural line per turn and OPEN WITH THE QUESTION "
        "ITSELF. NEVER start a turn with an acknowledgement filler — no 'right "
        "so', 'okay', 'great', 'great thanks', 'good to hear', 'perfect', 'nice', "
        "'sure', 'cool', 'got it', 'thanks for that' (and no equivalent in any "
        "other language). They sound robotic and waste the prospect's borrowed "
        "attention. Just ask the next question.\n"
        "- Don't re-confirm an answer they already gave clearly (if they said "
        "'3 BHK', do NOT ask 'so you want a 3 BHK, right?') — just move on.\n"
        "- Talk like a real person on a call — vary your wording, no lists, never "
        "stack two questions, never read anything aloud like a script.\n"
        "- Speak in whatever language the prospect speaks.\n"
        "- This is the ONLY purpose of the call: ask the questions below and get a "
        "real answer to each. Do NOT pitch, do NOT offer site visits, demos, or "
        "meetings, and do NOT collect anything beyond these questions."
    )
    q_block = render_questionnaire_block(
        context, language=language, history=history, latest_user_text=latest_user_text
    )
    if q_block:
        parts.append(q_block)
    parts.append(_OUTBOUND_NAME_GUARDRAIL.format(caller_name=caller))
    parts.append(_OUTBOUND_HAVE_NUMBER_RULE)
    parts.append(_OUTBOUND_DISINTEREST_RULE)
    return "\n\n".join(parts)


def compose_outbound_system_section(
    context: OutboundCampaignContext | None,
    *,
    covered_objectives: list[str] | None = None,
    outbound_memory: dict[str, Any] | None = None,
    tool_flow_state: dict[str, Any] | None = None,
    tool_flow_bundle: dict[str, Any] | None = None,
    language: str | None = None,
    turn_index: int | None = None,
    conversational_memory: Any = None,
    business_type: str | None = None,
    latest_user_text: str | None = None,
    history: list | None = None,
) -> str:
    """Build the system-prompt fragment for an outbound turn.

    Empty when no proactive config is present. Sales-persona prompt is
    ported from the reference implementation (``agent_lab/voice-rag-agent``)
    — it's the template that's been tuned to produce humane outbound
    behaviour: respect the prospect's time, accept "no" twice and exit,
    disclose AI on ask, never claim actions the system can't take.

    ``tool_flow_state`` / ``tool_flow_bundle`` are the live booking-flow
    snapshot. When an inbound-style tool_flow (site visit or lead capture)
    is active, the rendered block tells the LLM exactly which slot to ask
    for next — without that, the model drifts back into general chat and
    never collects name/phone/date, so the tool never fires.

    ``turn_index`` is the 1-based turn number used to inject anti-loop
    guidance: by turn 5+, the model is told to close on the next slot or
    wrap politely, not re-open with "is this a good time to talk".
    """
    if context is None or not context.is_proactive:
        return ""
    # Deterministic questionnaire campaigns run a SEPARATE, minimal pipeline —
    # they must not inherit the sales/FSM/site-visit scaffold below (it hijacks
    # the call). See _compose_questionnaire_only_section.
    if context.has_questionnaire:
        return _compose_questionnaire_only_section(
            context,
            language=language,
            history=history,
            latest_user_text=latest_user_text,
        )
    remaining = context.remaining_objectives(covered_objectives or [])
    parts: list[str] = []

    # ── Follow-up preamble ──────────────────────────────────────────────
    # The call notes are the single source of prior-call context. The
    # post-call condenser (``call_condenser_service``) writes a 3-sentence
    # ``handoff_note`` onto the lead the moment the prior call ends — small
    # models read that prose far better than a structured slot dump, and
    # managers read the same note in the leads tab. When a note is present we
    # quote it; when it isn't (call too short / condenser failed), we fall
    # back to a MINIMAL acknowledgement rather than reconstructing
    # objections / commitments / preferences from memory.
    #
    # Source: ``outbound_memory['followup']`` — seeded once at session start
    # from the WebSocket handler's ``campaign_context``. Carries
    # ``is_followup``, ``attempt_n``, ``handoff_note`` (optional), and the
    # prior promise text.
    followup = (outbound_memory or {}).get("followup") if outbound_memory else None
    if followup and followup.get("is_followup"):
        attempt_n = int(followup.get("attempt_n") or 1)
        handoff_note = str(followup.get("handoff_note") or "").strip()

        # Admin-commanded follow-up (clinic Customer base path): the admin's
        # typed purpose is this call's agenda — render it ABOVE the prior-call
        # notes so the model treats it as the primary objective.
        admin_note = str(followup.get("admin_note") or "").strip()
        if admin_note:
            parts.append(
                "═══ REASON FOR THIS CALL — admin instruction ═══\n"
                f"The clinic admin asked you to: {admin_note}\n"
                "This is the purpose of the call. Deliver it warmly in your first "
                "substantive turn — do not bury it behind small talk.\n"
                "═══════════════════════════════════════════════"
            )

        if handoff_note:
            # The model reads prose 10× better than structured slots, so
            # this is the preferred path. Keep it short — the note itself
            # carries all the context.
            parts.append(
                "═══ FOLLOW-UP CALL — YOUR GOAL: BOOK A SITE VISIT ═══\n"
                f"This is attempt {attempt_n}. Notes from the last call:\n\n"
                f"{handoff_note}\n\n"
                "Open warmly by referencing these notes — DO NOT restart with "
                "\"Is this a good time to talk?\". If the notes mention an "
                "unresolved objection, address it FIRST.\n"
                "Your SINGLE goal on this call is to get them to commit to a SITE VISIT: "
                "re-engage briefly, then propose a specific day and time "
                "(e.g. \"this Saturday around 11?\") and lock it in. Don't just re-qualify "
                "or make small talk — keep steering every turn back toward booking the visit.\n"
                "══════════════════════"
            )
        else:
            # No note (lead never had a connected prior call, or the condenser
            # failed). Do NOT claim a previous conversation that didn't happen —
            # open about the PROJECT instead. The prior promise (the reason this
            # call was scheduled) is worth surfacing when present.
            prior_promise = str(followup.get("prior_promise") or "").strip()
            promise_clause = (
                f" They had asked to be called back: {prior_promise}."
                if prior_promise
                else ""
            )
            parts.append(
                "═══ FOLLOW-UP CALL — YOUR GOAL: BOOK A SITE VISIT ═══\n"
                f"This is attempt {attempt_n}. This is a follow-up to a lead who showed interest "
                f"in the project above.{promise_clause}\n"
                "Open warmly about the PROJECT — do NOT claim you spoke before if you have no notes "
                "for it, and do NOT restart cold with \"Is this a good time to talk?\".\n"
                "Your SINGLE goal on this call is to get them to commit to a SITE VISIT: spark "
                "interest with one specific benefit, then propose a specific day and time and lock "
                "it in. If they object, handle it once, then steer back to booking the visit.\n"
                "══════════════════════"
            )

    # A custom agent_prompt is the campaign's PERSONA + KNOWLEDGE, not its
    # delivery rules. Customers tend to paste a rigid "1. Introduction
    # 2. Qualification 3. Pitch…" call-flow, which makes the agent sound like a
    # form-filling telemarketer (observed in production). So we NO LONGER let a
    # custom prompt replace the human-delivery template — we layer it as
    # reference knowledge and ALWAYS render the tuned delivery scaffolding
    # (turn structure, opener variety, few-shot, name guardrail) on top, with
    # an explicit note that HOW-you-talk rules outrank any scripted sequence.
    if context.agent_prompt and context.agent_prompt != DEFAULT_AGENT_PROMPT:
        parts.append(
            "# OUTBOUND CAMPAIGN — CUSTOM PERSONA & KNOWLEDGE\n"
            "The block below is BACKGROUND KNOWLEDGE — your product facts, persona, "
            "and tone reference. It is NOT a script: NEVER read it aloud, quote it "
            "verbatim, or dump it in one breath. Pull from it ONE relevant fact at a "
            "time, in your own words, only when the conversation calls for it. If it "
            "contains a numbered call flow, do NOT recite it in order like a checklist "
            "— that sounds robotic. HOW you actually talk (one beat per turn, listen "
            "first, vary your openers) is governed by the DELIVERY RULES below; those "
            "win over any scripted sequence here."
        )
        parts.append(context.agent_prompt)
    # The base template carries the human-delivery scaffolding (turn structure,
    # banned openers, banned standalone replies, no-stacking, few-shot). It is
    # rendered in BOTH branches now — for a custom persona it reinforces tone
    # and supplies the behavioural anchors the operator's script lacks.
    parts.append(
        _OUTBOUND_BASE_TEMPLATE.format(
            caller_name=context.caller_name or "Riya",
            company_name=context.company_name or "the company",
            objective_description=context.objective_description,
        )
    )
    parts.append(_OUTBOUND_UNIVERSAL_TURN_RULES)
    parts.append(_OUTBOUND_EXPLAIN_CALL_RULE.format(purpose_line=_call_purpose_line(context)))
    # _OUTBOUND_NAME_GUARDRAIL is appended LATE (high recency) near the
    # disinterest / have-number rules — small models slip and address the
    # prospect by the rep's own name when the rule is buried early in a long,
    # per-turn-rebuilt prompt. See the end of this function.

    # Outbound FSM mode block, branched on the org's business type:
    #   * clinics       → clinic outbound FSM (follow-up / booking / triage —
    #                     with the no-diagnosis guardrail).
    #   * anything else → real-estate outbound FSM (``sale`` / ``site_visit``
    #                     / ``outbound_lead``), preserving the legacy
    #                     always-render behaviour for callers that don't pass
    #                     ``business_type`` (outbound campaigns are
    #                     real-estate-only in practice).
    _bt = str(business_type or "").strip().lower()
    if _bt == "clinics":
        try:
            from app.services.clinic_outbound_agent_fsm import (
                current_mode as _clinic_outbound_mode,
                mode_block_for_prompt as _clinic_outbound_block,
            )

            _c_state = {"tool_flow": tool_flow_state or {}}
            parts.append(
                _clinic_outbound_block(
                    _clinic_outbound_mode(_c_state, latest_user_text=latest_user_text)
                )
            )
        except Exception:
            pass
        _outbound_mode = None  # type: ignore[assignment]
        _outbound_mode_block = None  # type: ignore[assignment]
    else:
        try:
            from app.services.real_estate_outbound_agent_fsm import (
                current_mode as _outbound_mode,
                mode_block_for_prompt as _outbound_mode_block,
            )
        except Exception:
            _outbound_mode = None  # type: ignore[assignment]
            _outbound_mode_block = None  # type: ignore[assignment]

    if _outbound_mode is not None and _outbound_mode_block is not None:
        _state_for_mode = {"tool_flow": tool_flow_state or {}}
        _mode = _outbound_mode(
            _state_for_mode,
            list(context.objectives or []),
            memory=conversational_memory,
        )
        _pending_label = None
        _pending_question = None
        if tool_flow_state and tool_flow_bundle is not None:
            _flow_key = str(tool_flow_state.get("flow_key") or "")
            _flow_def = ((tool_flow_bundle.get("flows") or {}).get(_flow_key) or {})
            _pending_slot_key = str(tool_flow_state.get("pending_slot") or "")
            for _slot in (_flow_def.get("slots") or []):
                if not isinstance(_slot, dict):
                    continue
                _skey = str(_slot.get("key") or "")
                if _pending_slot_key and _skey != _pending_slot_key:
                    continue
                if not _pending_slot_key and (tool_flow_state.get("collected") or {}).get(_skey):
                    continue
                _pending_label = str(_slot.get("label") or _skey)
                _questions = _slot.get("questions") or {}
                _pending_question = str(
                    _questions.get(language) or _questions.get("en") or ""
                )
                break
        parts.append(
            _outbound_mode_block(
                _mode,
                list(context.objectives or []),
                pending_slot_label=_pending_label,
                pending_slot_question=_pending_question,
                memory=conversational_memory,
            )
        )

    # Anti-loop reminder. The LLM, left to free-form, will sometimes re-ask
    # turn-1 framing questions ("Is this a good time to talk about X?") in
    # the middle of a call because the system prompt re-renders every turn.
    # By turn 3+ we explicitly forbid that pattern and nudge toward closure.
    if turn_index is not None and turn_index >= 3:
        loop_block = (
            "# TURN PROGRESS — DO NOT REWIND, KEEP SELLING\n"
            f"You are on turn {turn_index}. The conversation is already underway.\n"
            "- NEVER re-ask opener-style framing questions: 'Is this a good time?', "
            "'Can I tell you about X?', 'May I share more?'. You already had permission. Move forward.\n"
            "- Don't repeat a pitch you already gave — advance to a NEW benefit, answer their question, or move toward the close.\n"
            "- Drive toward a COMMITMENT (a site visit), NOT toward their contact details. Do NOT ask for name or phone until they are clearly interested AND have agreed to a next step — pulling contact details from a lukewarm prospect is exactly the rushed, salesy behaviour to avoid. Sell first; capture last."
        )
        if turn_index >= 6:
            loop_block += (
                "\n- You're several turns in. If they're WARM, go for the close — propose a specific site-visit time ('this Saturday around 11?'). If they're LUKEWARM, give one more genuinely compelling, specific reason to be interested. If they're NOT interested, wrap warmly and stop. Do not pad with filler or re-open old threads."
            )
        parts.append(loop_block)

    # Campaign overview always rendered so the model has goal + pitch even
    # when the custom-persona branch was taken.
    overview_parts: list[str] = []
    if context.pitch_summary:
        overview_parts.append(f"Pitch summary: {context.pitch_summary}")
    if context.goal:
        overview_parts.append(f"Goal: {context.goal}")
    overview_parts.append(f"Objective: {context.objective}")
    parts.append("# CAMPAIGN OVERVIEW\n" + "\n".join(overview_parts))
    if context.objectives:
        # Campaign objectives are now stored as structured codes ("site_visit",
        # "lead"); render them as human-readable labels for the LLM. Unknown
        # codes (legacy free-text rows from earlier campaigns) fall through
        # unchanged so existing campaigns keep working.
        try:
            from app.services.real_estate_outbound_agent_fsm import OBJECTIVE_LABELS as _OBJ_LABELS
        except Exception:
            _OBJ_LABELS = {}
        labeled_objectives = [
            _OBJ_LABELS.get(str(item).strip().lower(), str(item))
            for item in context.objectives
        ]
        objectives_render = "\n".join(f"  - {item}" for item in labeled_objectives)
        section = f"# OBJECTIVES (drive toward at least one)\n{objectives_render}"
        if remaining:
            remaining_render = "\n".join(
                f"  - {_OBJ_LABELS.get(str(item).strip().lower(), str(item))}"
                for item in remaining
            )
            section += f"\n\nStill pending this call:\n{remaining_render}"
        elif context.has_questionnaire:
            section += (
                "\n\nObjectives covered — but keep going until you've asked every "
                "lead-capture question below; don't wrap up yet."
            )
        else:
            section += "\n\nAll objectives covered — confirm the next step and wrap the call politely."
        parts.append(section)
    memory_section = render_outbound_memory(outbound_memory)
    if memory_section:
        parts.append(memory_section)
    # Active booking-flow block. When the inbound-style tool_flow regex has
    # latched on (yes-after-offer or explicit booking intent), this surfaces
    # the slot state so the LLM drives toward filling the next one rather
    # than chatting around it. The block is INTENTIONALLY placed late so
    # recency bias keeps it top of mind for the next reply.
    if tool_flow_state:
        booking_block = render_booking_flow_state(
            tool_flow_state,
            tool_flow_bundle,
            language=language,
        )
        if booking_block:
            parts.append(booking_block)
    if context.exit_conditions:
        exit_render = "\n".join(f"  - {item}" for item in context.exit_conditions)
        parts.append(f"# EXIT CONDITIONS (when ANY is met, close the call warmly)\n{exit_render}")
    if context.tone:
        parts.append(f"Preferred tone: {context.tone}.")
    # High-recency north star + the never-ask-for-the-number rule, then the
    # disinterest rule DEAD LAST so it outranks everything.
    parts.append(
        "# YOUR ONE GOAL THIS CALL\n"
        f"{_one_goal_line(context.objectives)}\n"
        "Everything you say serves this ONE goal. Don't wander into unrelated questions "
        "or data collection. Move every turn toward it; the moment it's secured — or they're "
        "clearly not interested — wrap warmly and stop."
    )
    parts.append(
        _OUTBOUND_NAME_GUARDRAIL.format(caller_name=context.caller_name or "Riya")
    )
    parts.append(_OUTBOUND_HAVE_NUMBER_RULE)
    # Questionnaire block: high recency (just above disinterest) so the agent
    # asks every question before wrapping — but the disinterest rule stays DEAD
    # LAST so a "no" still ends the call mid-questionnaire.
    if context.has_questionnaire:
        q_block = render_questionnaire_block(
        context, language=language, history=history, latest_user_text=latest_user_text
    )
        if q_block:
            parts.append(q_block)
    parts.append(_OUTBOUND_DISINTEREST_RULE)
    return "\n\n".join(parts)


def _opener_enquiry_phrase(facts: dict[str, Any], code: str) -> str:
    """A short, language-appropriate phrase naming what the lead is looking for
    (e.g. ``"3 BHK options in Gachibowli"``) from known facts. Empty when we
    don't know enough to personalise."""
    bhk = str(facts.get("bhk") or "").strip()
    location = str(facts.get("location") or "").strip()
    if not (bhk or location):
        return ""
    if code == "te":
        unit = f"{bhk} options" if bhk else "options"
        return f"{unit} {location} లో" if location else unit
    if code == "hi":
        unit = f"{bhk} options" if bhk else "options"
        return f"{location} में {unit}" if location else unit
    unit = f"{bhk} options" if bhk else "your requirement"
    return f"{unit} in {location}" if location else unit


def generate_outbound_opener_text(
    context: OutboundCampaignContext,
    *,
    language: str | None = None,
    known_facts: dict[str, Any] | None = None,
) -> str:
    """Deterministic, template-filled opener.

    Mirrors the reference's ``generate_opener_text`` — runs without an LLM
    call so the first audio is on the wire ~150ms faster than waiting for
    a stream. Includes prosody tags so :func:`stream_prosody_chunks` can
    render the line with proper tones.

    ``language`` switches the opener template so Telugu / Hindi campaigns
    don't start every call with English. ``known_facts`` (optional) carries
    what we already know about THIS lead — ``name`` plus any enquiry / prior-call
    facts (``bhk``, ``location``) and a ``returning`` flag — so the opener is
    personalised ("you'd enquired about 3 BHK in Gachibowli") instead of a cold
    one-size-fits-all line. Falls back to the campaign pitch when nothing is known.
    """
    caller = (context.caller_name or "Riya").strip() or "Riya"
    company = (context.company_name or "").strip()
    # An admin-authored questionnaire intro overrides the template opener — the
    # operator wrote exactly how they want the call to open (in the campaign's
    # language). Spoken as-is; voiced warm by _play_opener when it carries no
    # prosody tags.
    intro = (getattr(context, "question_intro", "") or "").strip()
    if intro:
        return f"[warm]{intro}[/warm]"
    # Only speak a SHORT pitch — never the full content blob (it'd read the prompt
    # aloud and garble a Telugu/Hindi opener). Long content → generic intro.
    pitch = _spoken_pitch(context)
    code = (language or "").strip().lower()[:2]

    facts = known_facts or {}
    lead_name = str(facts.get("name") or "").strip()
    returning = bool(facts.get("returning"))
    followup = bool(facts.get("followup"))
    project = str(facts.get("project") or "").strip()
    enquiry = _opener_enquiry_phrase(facts, code)

    if code == "te":
        name_part = f" {lead_name}" if lead_name else ""
        intro_te = (
            f"Hello{name_part}, నేను {caller}, {company} నుండి మాట్లాడుతున్నా."
            if company
            else f"Hello{name_part}, నేను {caller} మాట్లాడుతున్నా."
        )
        if returning:
            about_te = f"{project} గురించి " if project else "మన last conversation గురించి "
            return (
                f"[warm]{intro_te}[/warm] "
                f"[neutral]{about_te}follow up చేయడానికి call చేస్తున్నా.[/neutral] "
                "[question]ఇప్పుడు ఒక్క minute మాట్లాడగలరా?[/question]"
            )
        if followup:
            about_te = project or "మీరు interest చూపిన project"
            return (
                f"[warm]{intro_te}[/warm] "
                f"[neutral]{about_te} గురించి follow up చేయడానికి call చేస్తున్నా.[/neutral] "
                "[question]ఇప్పుడు ఒక్క minute మాట్లాడగలరా?[/question]"
            )
        if enquiry:
            reason = (
                f"Last time మీరు {enquiry} గురించి అడిగారు"
                if returning
                else f"మీరు {enquiry} గురించి enquiry చేశారు"
            )
            return (
                f"[warm]{intro_te}[/warm] "
                f"[neutral]{reason}.[/neutral] "
                "[question]ఇప్పుడు ఒక్క minute మాట్లాడగలరా?[/question]"
            )
        if pitch:
            return (
                f"[warm]{intro_te}[/warm] "
                f"[neutral]{pitch} గురించి call చేస్తున్నా.[/neutral] "
                "[question]ఇప్పుడు ఒక్క minute మాట్లాడగలరా?[/question]"
            )
        reason_te = (
            f"[neutral]{company} తరఫున మీ area లో ఒక project గురించి call చేస్తున్నా.[/neutral] "
            if company
            else ""
        )
        return (
            f"[warm]{intro_te}[/warm] "
            f"{reason_te}"
            "[question]ఇప్పుడు ఒక్క minute మాట్లాడగలరా?[/question]"
        )

    if code == "hi":
        name_part = f" {lead_name}" if lead_name else ""
        intro_hi = (
            f"नमस्ते{name_part}, मैं {caller} बोल रहा हूँ, {company} से."
            if company
            else f"नमस्ते{name_part}, मैं {caller} बोल रहा हूँ."
        )
        if returning:
            about_hi = f"{project} के बारे में " if project else "हमारी पिछली बात-चीत के "
            return (
                f"[warm]{intro_hi}[/warm] "
                f"[neutral]{about_hi}follow up के लिए call किया है.[/neutral] "
                "[question]क्या अभी एक minute बात कर सकते हैं?[/question]"
            )
        if followup:
            about_hi = project or "जिस project में आपकी interest थी उसके"
            return (
                f"[warm]{intro_hi}[/warm] "
                f"[neutral]{about_hi} के बारे में follow up के लिए call किया है.[/neutral] "
                "[question]क्या अभी एक minute बात कर सकते हैं?[/question]"
            )
        if enquiry:
            reason = (
                f"पिछली बार हमने {enquiry} के बारे में बात की थी"
                if returning
                else f"आपने {enquiry} के बारे में enquiry की थी"
            )
            return (
                f"[warm]{intro_hi}[/warm] "
                f"[neutral]{reason}.[/neutral] "
                "[question]क्या अभी एक minute बात कर सकते हैं?[/question]"
            )
        if pitch:
            return (
                f"[warm]{intro_hi}[/warm] "
                f"[neutral]{pitch} के लिए call किया है.[/neutral] "
                "[question]क्या अभी एक minute बात कर सकते हैं?[/question]"
            )
        reason_hi = (
            f"[neutral]मैं {company} की तरफ़ से आपके area के एक project के बारे में call कर रहा हूँ.[/neutral] "
            if company
            else ""
        )
        return (
            f"[warm]{intro_hi}[/warm] "
            f"{reason_hi}"
            "[question]क्या अभी एक minute बात कर सकते हैं?[/question]"
        )

    name_part = f" {lead_name}" if lead_name else ""
    intro = (
        f"Hi{name_part}, this is {caller} from {company}."
        if company
        else f"Hi{name_part}, this is {caller}."
    )
    if returning:
        # A real prior call happened (we have a note) — reference it, optionally
        # naming the project. The LLM drives the booking from the note next turn.
        about = f" about {project}" if project else ""
        return (
            f"[warm]{intro}[/warm] "
            f"[neutral]I'm calling to follow up on our last conversation{about}.[/neutral] "
            "[question]Is now a good time to talk for a minute?[/question]"
        )
    if followup:
        # Follow-up with NO prior conversation — do NOT invent one. Re-engage
        # grounded on the project; the LLM drives toward the site-visit booking.
        about = project or "the project you were interested in"
        return (
            f"[warm]{intro}[/warm] "
            f"[neutral]I'm following up about {about}.[/neutral] "
            "[question]Is now a good time to talk for a minute?[/question]"
        )
    if enquiry:
        reason = (
            f"we'd spoken about {enquiry} last time"
            if returning
            else f"you'd enquired about {enquiry}"
        )
        return (
            f"[warm]{intro}[/warm] "
            f"[neutral]I'm calling — {reason}.[/neutral] "
            "[question]Is now a good time to talk for a minute?[/question]"
        )
    if pitch:
        return (
            f"[warm]{intro}[/warm] "
            f"[neutral]I'm reaching out about {pitch}.[/neutral] "
            "[question]Is now a good time to talk for a minute?[/question]"
        )
    # No pitch summary: still give a reason so the prospect isn't left asking
    # "what is this?". Reference the company when we have it.
    reason_line = (
        f"[neutral]I'm reaching out on behalf of {company} about a project in your area.[/neutral] "
        if company
        else ""
    )
    return (
        f"[warm]{intro}[/warm] "
        f"{reason_line}"
        "[question]Is now a good time to talk for a minute?[/question]"
    )


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-zA-Z0-9]+", (text or "").lower())
        if len(token) > 2 and token not in _STOP_WORDS
    }


def infer_covered_objectives(
    context: OutboundCampaignContext | None,
    *,
    caller_text: str,
    agent_answer: str,
    already_covered: Iterable[str] | None = None,
) -> list[str]:
    """Best-effort objective progress from the latest caller + agent turn.

    This deliberately stays deterministic. It is not a source of truth for
    business state; it only reduces repeated proactive prompts by hiding
    objectives whose keywords were clearly discussed.
    """
    if context is None or not context.objectives:
        return list(already_covered or [])
    covered = [item for item in (already_covered or []) if item]
    covered_lower = {item.strip().lower() for item in covered}
    transcript_tokens = _tokens(f"{caller_text} {agent_answer}")
    for objective in context.objectives:
        normalized = objective.strip().lower()
        if not normalized or normalized in covered_lower:
            continue
        objective_tokens = _tokens(objective)
        if not objective_tokens:
            continue
        overlap = objective_tokens & transcript_tokens
        # Tightened next-step handling. Previously ANY single token overlap on a
        # "next step" objective marked it covered, so a stray "appointment" /
        # "callback" word in the objective text covered it even when no next step
        # was actually agreed. Now a next-step objective is covered only when an
        # actual next-step ACTION was discussed in the turn — not on incidental
        # token overlap.
        _action_tokens = {
            "appointment", "callback", "visit", "demo", "meeting",
            "book", "booking", "schedule",
        }
        is_next_step = bool(({"next", "step"} | _action_tokens) & objective_tokens)
        if is_next_step:
            covered_now = bool(overlap & _action_tokens)
        else:
            threshold = 1 if len(objective_tokens) <= 3 else 2
            covered_now = len(overlap) >= threshold
        if covered_now:
            covered.append(objective)
            covered_lower.add(normalized)
    return covered


_BHK_WORDS = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
}

_OUTBOUND_MEMORY_LABELS = {
    "name": "Name",
    "phone": "Phone",
    "purpose": "Buying purpose",
    "bhk": "BHK preference",
    "budget": "Budget",
    "timeline": "Timeline",
    "location_preference": "Location preference",
    "visit_preference": "Visit preference",
    "next_step": "Agreed next step",
    "requested_info": "Requested info",
    "objection": "Objection / constraint",
}


def _remember(memory: dict[str, str], key: str, value: str | None) -> None:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" .,:;")
    if not text:
        return
    memory[key] = text[:120]


def _extract_name(text: str) -> str | None:
    match = re.search(
        r"\b(?:my name is|this is|i am|i'm|call me)\s+([A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,2})",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    candidate = " ".join(match.group(1).split())
    first = candidate.split()[0].lower()
    if first in {
        "looking", "interested", "busy", "not", "calling", "thinking", "going",
        "planning", "buying", "searching", "available", "ok", "okay",
    }:
        return None
    return candidate.title()


def update_outbound_memory(
    existing: dict[str, Any] | None,
    *,
    caller_text: str,
    agent_answer: str = "",
) -> dict[str, str]:
    """Extract stable call facts for outbound turn memory.

    The LLM already sees raw transcript history, but important sales-call
    facts can be buried across turns. This lightweight memory is deliberately
    deterministic and conservative: it stores only explicit caller-provided
    details and a few common real-estate/outreach signals.
    """
    memory: dict[str, str] = {
        str(key): str(value)
        for key, value in (existing or {}).items()
        if key in _OUTBOUND_MEMORY_LABELS and value
    }
    text = re.sub(r"\s+", " ", caller_text or "").strip()
    lower = text.lower()
    if not text:
        return memory

    name = _extract_name(text)
    if name:
        _remember(memory, "name", name)

    phone_match = re.search(r"(?<!\d)(?:\+?91[\s-]?)?([6-9](?:[\s-]?\d){9})(?!\d)", text)
    if phone_match:
        digits = re.sub(r"\D", "", phone_match.group(0))
        _remember(memory, "phone", digits[-10:] if len(digits) >= 10 else digits)

    bhk_match = re.search(r"\b([1-6])\s*(?:bhk|bed(?:room)?s?)\b", lower)
    if not bhk_match:
        word_pattern = "|".join(_BHK_WORDS)
        bhk_match = re.search(rf"\b({word_pattern})\s*(?:bhk|bed(?:room)?s?)\b", lower)
    if bhk_match:
        raw = bhk_match.group(1)
        _remember(memory, "bhk", f"{_BHK_WORDS.get(raw, raw)} BHK")

    # Reuse the inbound budget extractor so outbound memory understands the
    # same spelled-out amounts ("half a crore", "fifty lakhs", "one and a half
    # crore") the inbound extractor was fixed to handle — not just digit+unit.
    try:
        from app.services.conversational_memory import MemoryExtractor as _ME

        budget_value = _ME._extract_budget(text)
    except Exception:
        budget_value = None
    if budget_value:
        _remember(memory, "budget", budget_value)

    if re.search(r"\b(self[-\s]?use|own use|end use|family|to live|for living)\b", lower):
        _remember(memory, "purpose", "self-use")
    elif re.search(r"\b(invest|investment|investor|rental|rent out|roi)\b", lower):
        _remember(memory, "purpose", "investment")

    timeline_match = re.search(
        r"\b(immediately|as soon as possible|this month|next month|this year|next year|"
        r"within\s+[0-9]+\s+(?:days|weeks|months)|in\s+[0-9]+\s+(?:days|weeks|months))\b",
        lower,
    )
    if timeline_match:
        _remember(memory, "timeline", timeline_match.group(1))

    if re.search(r"\b(weekday|weekdays|weekend|weekends|morning|afternoon|evening)\b", lower):
        _remember(memory, "visit_preference", re.search(r"\b(weekday|weekdays|weekend|weekends|morning|afternoon|evening)\b", lower).group(1))
    date_or_time_match = re.search(
        r"\b(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"[0-3]?\d(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|"
        r"(?:[01]?\d|2[0-3])(?::[0-5]\d)?\s*(?:am|pm))\b",
        lower,
    )
    if date_or_time_match:
        current = memory.get("visit_preference")
        value = date_or_time_match.group(0)
        _remember(memory, "visit_preference", f"{current}; {value}" if current and value not in current else value)

    location_match = re.search(r"\b(?:near|around|in|at)\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3})\b", text)
    if location_match and location_match.group(1).lower() not in {"this", "that"}:
        _remember(memory, "location_preference", location_match.group(1))

    info_hits = []
    for label, pattern in (
        ("details", r"\b(details?|information|info)\b"),
        ("brochure", r"\bbrochure\b"),
        ("pricing", r"\b(price|pricing|cost|rate|quotation)\b"),
        ("RERA number", r"\brera\b"),
        ("floor plans", r"\bfloor\s*plans?\b"),
        ("callback", r"\b(call back|callback)\b"),
        ("WhatsApp details", r"\bwhatsapp\b"),
    ):
        if re.search(pattern, lower):
            info_hits.append(label)
    if info_hits:
        prior = [item.strip() for item in memory.get("requested_info", "").split(",") if item.strip()]
        combined = list(dict.fromkeys(prior + info_hits))
        _remember(memory, "requested_info", ", ".join(combined))

    # Next-step TYPE — distinguish a committed site visit from a callback
    # brush-off or a meeting, so the CRM and objective coverage don't conflate
    # them (a "call me back" is not the same outcome as "I'll come visit").
    if re.search(r"\b(site\s*visit|come (?:and )?(?:see|visit)|visit the (?:site|property|project|flat)|tour|in person)\b", lower):
        _remember(memory, "next_step", "site_visit")
    elif re.search(r"\b(appointment|meeting|demo|schedule a (?:call|meeting))\b", lower):
        _remember(memory, "next_step", "appointment")
    elif re.search(r"\b(call back|callback|call me (?:back|later)|ring me (?:back|later))\b", lower):
        _remember(memory, "next_step", "callback")

    # Objection parity with inbound: reuse the inbound objection patterns so
    # outbound captures competitor / long-horizon / timing brush-offs too — not
    # just the old binary not-interested / price split.
    try:
        from app.services.conversational_memory import _OBJECTION_PATTERNS as _OBJ
    except Exception:
        _OBJ = ()
    for pat, code in _OBJ:
        if pat.search(text):
            # do_not_call keeps the caller's verbatim words (the interest-signal
            # check greps them); the rest store a readable label of the code.
            _remember(memory, "objection", text if code == "do_not_call" else code.replace("_", " "))
            break

    return memory


def render_outbound_memory(memory: dict[str, Any] | None) -> str:
    items = []
    for key, label in _OUTBOUND_MEMORY_LABELS.items():
        value = str((memory or {}).get(key) or "").strip()
        if value:
            items.append(f"  - {label}: {value}")
    if not items:
        return ""
    return (
        "# CONVERSATION MEMORY — already known\n"
        "Use these facts as memory from this call. Do not ask for them again; "
        "build the next reply around them.\n"
        + "\n".join(items)
    )


def outbound_memory_as_facts(memory: dict[str, Any] | None) -> dict[str, Any]:
    """Map the lightweight outbound turn-memory dict onto canonical
    ConversationalMemory fact keys, so the structured-memory + strategy layer
    can be seeded with what the outbound extractor captured (lead scoring,
    objection playbook). Only the slot-shaped keys map; free-form ones
    (visit_preference, next_step, requested_info, objection) ride their own
    channels."""
    from app.services.conversational_memory import (
        FACT_BHK,
        FACT_BUDGET,
        FACT_LOCATION,
        FACT_NAME,
        FACT_PHONE,
        FACT_PURPOSE,
        FACT_TIMELINE,
    )

    mapping = {
        "name": FACT_NAME,
        "phone": FACT_PHONE,
        "bhk": FACT_BHK,
        "budget": FACT_BUDGET,
        "timeline": FACT_TIMELINE,
        "location_preference": FACT_LOCATION,
        "purpose": FACT_PURPOSE,
    }
    out: dict[str, Any] = {}
    for okey, fkey in mapping.items():
        value = str((memory or {}).get(okey) or "").strip()
        if value:
            out[fkey] = value
    return out


_FLOW_LABEL = {
    "real_estate_site_visit": "site-visit booking",
    "leads_create": "lead capture",
}


def render_booking_flow_state(
    flow_state: dict[str, Any] | None,
    bundle: dict[str, Any] | None,
    *,
    language: str | None = None,
) -> str:
    """Render the active tool_flow as a high-priority system block.

    When the inbound tool_flow regex starts a booking flow for an outbound
    call (e.g. "yeah" right after the agent offered a site visit), this
    block tells the LLM *what's been collected* and *what slot to ask
    next*. The deterministic slot question is included verbatim so the
    LLM has something concrete to paraphrase rather than drifting back
    to free-form chat.
    """
    if not isinstance(flow_state, dict) or not flow_state.get("active"):
        return ""
    if flow_state.get("completed"):
        return ""
    flow_key = str(flow_state.get("flow_key") or "")
    if not flow_key:
        return ""
    collected = dict(flow_state.get("collected") or {})
    pending_slot = str(flow_state.get("pending_slot") or "")

    slot_defs: list[dict[str, Any]] = []
    if bundle:
        flow_def = ((bundle.get("flows") or {}).get(flow_key) or {})
        slot_defs = [s for s in (flow_def.get("slots") or []) if isinstance(s, dict)]

    lang_code = (language or "en").split("-")[0].lower()

    captured_lines: list[str] = []
    missing_lines: list[str] = []
    next_slot_def: dict[str, Any] | None = None
    for slot in slot_defs:
        skey = str(slot.get("key") or "")
        if not skey:
            continue
        label = str(slot.get("label") or skey.replace("_", " ").title())
        value = collected.get(skey)
        if value not in (None, ""):
            captured_lines.append(f"  ✓ {label}: {value}")
            continue
        questions = slot.get("questions") or {}
        question = questions.get(lang_code) or questions.get("en") or ""
        missing_lines.append(f"  ✗ {label} — still needed")
        if next_slot_def is None and (not pending_slot or pending_slot == skey):
            next_slot_def = {"key": skey, "label": label, "question": question}

    # No slot definitions reached (bundle missing) — render a minimal block
    # from collected only so the LLM still sees what's captured.
    if not slot_defs:
        for key, value in collected.items():
            if value in (None, ""):
                continue
            captured_lines.append(f"  ✓ {key.replace('_', ' ').title()}: {value}")

    flow_label = _FLOW_LABEL.get(flow_key, flow_key.replace("_", " "))
    parts: list[str] = [
        f"# ACTIVE BOOKING FLOW — DRIVE TO COMPLETION",
        f"You're capturing a {flow_label}. The system records this once every required slot is filled.",
    ]
    if captured_lines:
        parts.append("Captured so far:")
        parts.extend(captured_lines)
    if missing_lines:
        parts.append("Still needed:")
        parts.extend(missing_lines)
    if next_slot_def:
        parts.append(
            "YOUR NEXT REPLY must move toward filling "
            f"**{next_slot_def['label']}** next."
        )
        if next_slot_def["question"]:
            parts.append(
                f'Reference question: "{next_slot_def["question"]}" '
                "— paraphrase in your persona; do NOT read it verbatim, and never stack two asks."
            )
        parts.append(
            "If the lead just answered a question of yours, take ONE acknowledgment "
            "(e.g. \"Got it.\") and then ask for the next slot. Do not pivot back to "
            "general project chat — that's the most common failure mode."
        )
    elif not missing_lines:
        parts.append(
            "All slots are filled — confirm the next step in one short sentence and wrap the call. "
            "The system will record this booking automatically."
        )
    return "\n".join(parts)


PROACTIVE_NUDGE_PROMPT = (
    "(no caller response — they went quiet. React like a real person would, not "
    "a script: do NOT repeat your last question word-for-word. Do ONE of these in "
    "one short, warm line — a soft connectivity check (\"Hi, are you still there?\"), "
    "a gentler re-phrase of the last question that lowers the friction, or a tiny "
    "reason-to-care hook from the brief. If they've now gone quiet twice, stop "
    "pushing and offer to follow up at a better time, then wrap politely.)"
)


PROACTIVE_OPENER_PROMPT = (
    "(call connected — you are the caller, the prospect just answered. "
    "Open the conversation in one short line that names the business, "
    "states the reason for the call from the campaign goal, and asks "
    "permission to continue. Do NOT greet as if you were answering an "
    "inbound call.)"
)


class ProactiveSilenceWatchdog:
    """Silence-triggered proactive nudge for outbound calls.

    The watchdog is armed after the agent finishes speaking. If the
    caller stays silent past ``timeout_seconds``, ``on_fire`` is invoked
    — the stream service uses this to run a proactive turn whose only
    "user input" is the :data:`PROACTIVE_NUDGE_PROMPT` sentinel. The
    LLM, given the proactive system prompt, interprets that as "drive
    the next objective".

    Strictly opt-in: inbound calls and outbound calls without a
    proactive ``OutboundCampaignContext`` should never instantiate one.
    """

    def __init__(
        self,
        *,
        timeout_seconds: float,
        on_fire,
    ) -> None:
        self._timeout = max(0.5, float(timeout_seconds))
        self._on_fire = on_fire
        self._task: asyncio.Task[None] | None = None

    def arm(self) -> None:
        """(Re-)start the silence timer. Idempotent — cancels any prior
        in-flight timer first."""
        self.cancel()
        self._task = asyncio.create_task(self._run())

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    @property
    def armed(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run(self) -> None:
        try:
            await asyncio.sleep(self._timeout)
        except asyncio.CancelledError:
            return
        try:
            result = self._on_fire()
            if asyncio.iscoroutine(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"NOKVO-OUTBOUND: silence watchdog on_fire failed: {exc!r}")


__all__ = [
    "OutboundCampaignContext",
    "PROACTIVE_NUDGE_PROMPT",
    "ProactiveSilenceWatchdog",
    "build_agent_config",
    "compose_outbound_system_section",
    "infer_covered_objectives",
    "invalidate",
    "invalidate_all",
    "load_outbound_context",
    "render_outbound_memory",
    "render_questionnaire_block",
    "strip_leading_fillers",
    "strip_leading_right_so",
    "update_outbound_memory",
]
