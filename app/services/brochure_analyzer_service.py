"""Brochure Analyzer — extract real-estate project fields from a brochure.

An admin uploads a brochure (PDF/DOCX/TXT) on the Projects page; this service
extracts the text and uses a NANO-pool LLM to pull the structured project
fields, which then prefill the Add-Project form for review. Mirrors the
deterministic extraction pattern in ``REAgentScheduler._extract`` (nano + strict
JSON + regex parse + safe-default). Never invents facts; ``null`` for anything
the brochure doesn't state. The description is capped at 700 tokens.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.services.document_text import extract_document_text
from app.services.real_estate_project_service import (
    PROJECT_DESCRIPTION_TOKEN_CAP,
    cap_text_to_tokens,
)

logger = logging.getLogger(__name__)

_JSON_OBJ = re.compile(r"\{[\s\S]*\}")

# Cap the brochure text fed to the nano model so a long multi-page PDF can't blow
# the context window. The salient facts are almost always on the first pages.
_MAX_INPUT_CHARS = 8000

_EXTRACT_SYSTEM = (
    "You read a real-estate project brochure and extract its details as STRICT "
    "JSON with exactly these keys: name (string), location (string), rera_number "
    "(string), property_type (e.g. 'Apartments' / 'Villas' / 'Plots'), price_min "
    "(number, in rupees, no commas/symbols), price_max (number, in rupees), "
    "price_display (string as written, e.g. '₹95L - ₹1.45Cr'), configurations "
    "(array of strings, e.g. ['2 BHK','3 BHK']), amenities (array of strings), "
    "description (a concise marketing summary, AT MOST 120 words), "
    "possession_date (string as written, e.g. 'Dec 2027'), builder_name (string), "
    "contact_phone (string). Use null for any field the brochure does not state — "
    "NEVER invent a value. For prices, convert crore/lakh to plain rupees "
    "(1 crore = 10000000, 1 lakh = 100000). Output ONLY the JSON object."
)

_STR_FIELDS = (
    "name", "location", "rera_number", "property_type", "price_display",
    "possession_date", "builder_name", "contact_phone",
)
_LIST_FIELDS = ("configurations", "amenities")


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if item is None:
            continue
        text = str(item).strip()[:120]
        if text and text not in out:
            out.append(text)
    return out


def _clean_price(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value >= 0 else None
    # Strip currency symbols / commas a model might still emit.
    digits = re.sub(r"[^\d.]", "", str(value))
    if not digits:
        return None
    try:
        num = float(digits)
    except ValueError:
        return None
    return num if num >= 0 else None


# Below this many characters of extracted text we treat the brochure as having
# no readable text (empty file, or a scanned / image-only PDF).
_MIN_USABLE_CHARS = 25


async def analyze_brochure_text(text: str) -> dict[str, Any]:
    """Nano-extract project fields from already-extracted brochure TEXT. Returns
    a project-shaped dict, or ``{}`` when the model couldn't produce usable
    fields. The caller is expected to have verified the text is non-empty."""
    text = (text or "").strip()
    if not text:
        return {}
    text = text[:_MAX_INPUT_CHARS]
    try:
        from app.services.nokvo_one_voice_pipeline import AzureGroundedLLM

        raw = await AzureGroundedLLM.complete_nano(
            [
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {"role": "user", "content": f"Brochure:\n{text}"},
            ],
            max_tokens=600,
        )
    except Exception:
        logger.warning("BROCHURE-ANALYZER: nano extraction call failed", exc_info=True)
        return {}

    match = _JSON_OBJ.search((raw or "").strip())
    if not match:
        return {}
    try:
        obj = json.loads(match.group(0))
    except Exception:
        return {}
    if not isinstance(obj, dict):
        return {}

    result: dict[str, Any] = {}
    for key in _STR_FIELDS:
        cleaned = _clean_str(obj.get(key))
        if cleaned is not None:
            result[key] = cleaned
    for key in _LIST_FIELDS:
        result[key] = _clean_list(obj.get(key))
    for key in ("price_min", "price_max"):
        price = _clean_price(obj.get(key))
        if price is not None:
            result[key] = price
    description = cap_text_to_tokens(_clean_str(obj.get("description")), PROJECT_DESCRIPTION_TOKEN_CAP)
    if description:
        result["description"] = description
    return result


async def analyze_brochure(filename: str, content: bytes) -> dict[str, Any]:
    """Extract project fields from a brochure file (extract text → nano extract).
    Convenience wrapper; the API endpoint calls the two stages separately so it
    can tell 'no readable text' apart from 'text but no fields'. ``{}`` on
    either failure."""
    text = extract_document_text(filename, content)
    if len((text or "").strip()) < _MIN_USABLE_CHARS:
        return {}
    return await analyze_brochure_text(text)
