"""Legal-doc grounding for Nova — ToS / Privacy Policy excerpt lookup.

The source of truth for the APEX legal documents is
``frontend/src/content/apexLegalDocs.js``; plain-text copies live in
``app/assets/legal/apex_tos.txt`` / ``apex_privacy.txt`` (a unit test alarms on
heading drift). Two documents, ~30 sections total — grounding is heading-chunked
keyword scoring, no embeddings needed. Excerpts are returned as UNTRUSTED DATA
for the model to quote/summarise, never as instructions.
"""
from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_LEGAL_DIR = Path(__file__).resolve().parent.parent / "assets" / "legal"
_DOCS = {"tos": "apex_tos.txt", "privacy": "apex_privacy.txt"}
_MAX_EXCERPTS = 3
_EXCERPT_CAP = 1600  # chars per excerpt fed back to the model

_WORD_RE = re.compile(r"[a-z0-9]+")
# Common words that carry no signal for section matching.
_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are",
    "what", "does", "do", "can", "i", "my", "me", "you", "your", "about", "it",
    "policy", "terms", "apex", "nokvo",
}


def _tokens(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text.lower()) if w not in _STOP]


@lru_cache(maxsize=4)
def _chunks(doc: str) -> list[tuple[str, str]]:
    """[(heading, body)] per ``## `` section of a legal text file."""
    path = _LEGAL_DIR / _DOCS[doc]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.exception("NOVA-LEGAL: missing legal asset %s", path)
        return []
    # Drop the sync-header comment lines.
    text = "\n".join(l for l in text.splitlines() if not l.startswith("# SYNCED") and not l.startswith("# Regenerate"))
    parts = re.split(r"^## ", text, flags=re.M)
    out: list[tuple[str, str]] = []
    for part in parts[1:]:
        heading, _, body = part.partition("\n")
        out.append((heading.strip(), body.strip()))
    return out


def lookup_legal(query: str, doc: str = "both") -> list[dict[str, str]]:
    """Top-scoring sections for ``query``. Returns
    ``[{doc, heading, excerpt}]`` (may be empty)."""
    docs = ["tos", "privacy"] if doc not in _DOCS else [doc]
    q_tokens = set(_tokens(query or ""))
    scored: list[tuple[float, str, str, str]] = []
    for d in docs:
        for heading, body in _chunks(d):
            body_tokens = _tokens(heading + " " + body)
            if not body_tokens:
                continue
            overlap = sum(1 for t in body_tokens if t in q_tokens)
            # Heading hits weigh extra — users ask "refunds", the heading says "No Refunds".
            heading_hits = sum(1 for t in _tokens(heading) if t in q_tokens)
            score = overlap + 3 * heading_hits
            if score > 0:
                scored.append((score, d, heading, body))
    scored.sort(key=lambda s: -s[0])
    return [
        {"doc": d, "heading": h, "excerpt": b[:_EXCERPT_CAP]}
        for _, d, h, b in scored[:_MAX_EXCERPTS]
    ]
