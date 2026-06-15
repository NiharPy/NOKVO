"""Helpers for surfacing real-estate project inventory to the agent.

Used by both the chat runtime and the voice pipeline to inject project
context into the system prompt and tool inputs. The list is small in
practice (orgs typically market <50 projects), so we read it directly
from Postgres each time rather than caching — staleness here would hurt
sales conversations more than the query latency.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.real_estate_project import RealEstateProject


async def load_active_projects(
    db: AsyncSession | None,
    organization_id: uuid.UUID | str | None,
) -> list[RealEstateProject]:
    if db is None or organization_id is None:
        return []
    try:
        res = await db.execute(
            select(RealEstateProject)
            .where(
                RealEstateProject.organization_id == organization_id,
                RealEstateProject.status == "active",
            )
            .order_by(RealEstateProject.name.asc())
        )
    except Exception:
        return []
    return list(res.scalars().all())


def _indian_group(n: int) -> str:
    """Group an integer rupee amount the Indian way (e.g. 62000 -> '62,000')."""
    s = str(abs(int(n)))
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts: list[str] = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def _trim_decimal(value: float) -> str:
    """'1.18' stays, '2.00' -> '2', '1.50' -> '1.5'."""
    return f"{value:.2f}".rstrip("0").rstrip(".")


def format_inr(amount: Any) -> str:
    """Speakable Indian-format rupee string: crore / lakh / grouped rupees.

    Indian buyers (and TTS) expect "₹1.18 crore" / "₹82 lakh", not the
    Western grouping ``₹11,800,000`` the previous formatter produced.
    """
    try:
        rupees = float(amount)
    except (TypeError, ValueError):
        return ""
    if rupees <= 0:
        return ""
    if rupees >= 1e7:
        return f"₹{_trim_decimal(rupees / 1e7)} crore"
    if rupees >= 1e5:
        return f"₹{_trim_decimal(rupees / 1e5)} lakh"
    return f"₹{_indian_group(round(rupees))}"


def _format_price(project: RealEstateProject) -> str:
    if project.price_display:
        return project.price_display
    low = format_inr(project.price_min) if project.price_min else ""
    high = format_inr(project.price_max) if project.price_max else ""
    if low and high:
        return low if low == high else f"{low} – {high}"
    if low:
        return f"from {low}"
    if high:
        return f"up to {high}"
    return ""


def project_summary_lines(projects: list[RealEstateProject]) -> list[str]:
    """One human-readable line per project, capped to keep the prompt sane."""
    lines: list[str] = []
    for project in projects[:40]:
        bits: list[str] = [project.name]
        if project.builder_name:
            bits.append(f"by {project.builder_name}")
        if project.location:
            bits.append(f"in {project.location}")
        if project.property_type:
            bits.append(f"({project.property_type})")
        configs = list(project.configurations or [])
        if configs:
            bits.append(f"configs: {', '.join(configs[:6])}")
        price = _format_price(project)
        if price:
            bits.append(f"price: {price}")
        if project.rera_number:
            bits.append(f"RERA {project.rera_number}")
        if project.possession_date:
            bits.append(f"possession {project.possession_date}")
        amenities = list(project.amenities or [])
        if amenities:
            bits.append("amenities: " + ", ".join(amenities[:6]))
        if project.contact_phone:
            bits.append(f"site contact: {project.contact_phone}")
        if project.brochure_url:
            # The agent should OFFER the brochure (handled via the
            # brochure-request flow); it never reads the raw URL aloud.
            bits.append("brochure available on request")
        lines.append(" — ".join(bits))
        description = (project.description or "").strip()
        if description:
            lines.append(f"    notes: {description[:280]}")
    return lines


def projects_prompt_section(projects: list[RealEstateProject]) -> str:
    """Block that gets injected as a system-prompt section.

    Returns empty string if there are no active projects so we don't
    pollute the prompt with a "no projects" footnote.

    Phrased as an authoritative source so the LLM prefers this list
    over any project names that may also appear in the admin's free-text
    single-prompt guidance. The admin's prompt is for tone, persona, and
    qualification rules — this section is the ground truth for inventory.
    """
    if not projects:
        return ""
    lines = project_summary_lines(projects)
    body = "\n".join(f"- {line}" if not line.startswith("    ") else line for line in lines)
    return (
        "THIS LIST IS THE AUTHORITATIVE SOURCE OF TRUTH FOR THE CURRENT PROJECT "
        "INVENTORY. If the admin's prompt mentions different project names, "
        "prices, configurations, locations, or RERA numbers, IGNORE those and "
        "use the facts below instead.\n"
        "When the caller asks about projects, properties, what you offer, "
        "available homes, or anything similar, base your answer ONLY on this "
        "list. Never invent, infer, or carry over projects that aren't in this "
        "list. If the list is empty, say the team will share the latest "
        "inventory and offer to take their details.\n"
        "When creating a lead or scheduling a site visit, set the "
        "``project_name`` (or ``project_id``) field to the matching project "
        "below so the team can route the work correctly.\n\n"
        f"{body}"
    )


def project_choices_for_tool_schema(projects: list[RealEstateProject]) -> dict[str, Any]:
    """JSONSchema fragment describing project_id/project_name fields."""
    names = [project.name for project in projects if project.name]
    project_id_field = {
        "type": "string",
        "description": "UUID of the project this record relates to.",
    }
    project_name_field: dict[str, Any] = {
        "type": "string",
        "description": "Name of the project this record relates to.",
        "maxLength": 200,
    }
    if names:
        capped = names[:80]
        project_name_field["enum"] = capped
        project_name_field["description"] = (
            "Name of the project this record relates to. Use the EXACT name of "
            "one of the org's projects: " + "; ".join(capped) + "."
        )
    return {"project_id": project_id_field, "project_name": project_name_field}


# Builder prefixes and project-category suffixes carry little disambiguating
# signal ("Raghava", "Residences"), so they're dropped before token matching —
# otherwise a caller who just says the builder name matches every project.
_PROJECT_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "project", "projects", "residences", "residence", "apartments",
        "apartment", "villa", "villas", "homes", "home", "towers", "tower",
        "heights", "enclave", "phase", "block", "by",
    }
)


def _normalize_project_text(value: str | None) -> str:
    text = re.sub(r"[^a-z0-9\s]", " ", (value or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def _project_tokens(value: str | None) -> set[str]:
    return {t for t in _normalize_project_text(value).split() if t and t not in _PROJECT_STOPWORDS}


def _project_match_score(spoken: str, spoken_tokens: set[str], project: RealEstateProject) -> float:
    name_norm = _normalize_project_text(project.name)
    if not name_norm:
        return 0.0
    if name_norm == spoken:
        return 1.0
    score = 0.0
    if spoken and (spoken in name_norm or name_norm in spoken):
        score = 0.8
    # De-spaced comparison so STT word-splitting / concatenation still matches:
    # "sky line heights" ≈ "Skyline Heights", "Greenacres" ≈ "Green Acres".
    spoken_ns = spoken.replace(" ", "")
    name_ns = name_norm.replace(" ", "")
    if spoken_ns and name_ns and (spoken_ns == name_ns or spoken_ns in name_ns or name_ns in spoken_ns):
        score = max(score, 0.85)
    name_tokens = _project_tokens(project.name)
    if spoken_tokens and name_tokens:
        overlap = spoken_tokens & name_tokens
        if overlap:
            jaccard = len(overlap) / len(spoken_tokens | name_tokens)
            coverage = len(overlap) / len(spoken_tokens)
            score = max(score, 0.5 * jaccard + 0.5 * coverage)
    return score


def find_project_match(
    projects: list[RealEstateProject],
    *,
    project_id: str | None = None,
    project_name: str | None = None,
) -> RealEstateProject | None:
    """Resolve a spoken/typed project reference to a real project record.

    Matching is fuzzy on purpose: voice STT rarely returns the exact
    registered name ("Skyline" vs "Raghava Skyline Residences"), so exact
    matching used to silently fail and leave the booking unlinked. We score
    every project on substring containment + significant-token overlap and
    take the best — but bail to ``None`` when the top two are too close
    (e.g. a bare shared builder prefix), since guessing the wrong project is
    worse than leaving it for a human to pick.
    """
    if project_id:
        for project in projects:
            if str(project.id) == str(project_id):
                return project
    if not project_name or not projects:
        return None
    spoken = _normalize_project_text(project_name)
    if not spoken:
        return None
    spoken_tokens = _project_tokens(project_name)
    scored = sorted(
        ((_project_match_score(spoken, spoken_tokens, p), p) for p in projects),
        key=lambda pair: pair[0],
        reverse=True,
    )
    top_score, top_project = scored[0]
    if top_score < 0.5:
        return None
    if (
        len(scored) > 1
        and top_score < 0.9
        and (top_score - scored[1][0]) < 0.15
    ):
        # Ambiguous — the spoken text wasn't distinctive enough.
        return None
    return top_project


# Generic configuration words that shouldn't match on their own — every
# residential listing has "BHK", so a "2 BHK" search must hinge on the "2".
_CONFIG_GENERIC_TOKENS: frozenset[str] = frozenset(
    {"bhk", "bed", "beds", "bedroom", "bedrooms", "rk", "br"}
)


def _configuration_matches(
    project: RealEstateProject,
    want_config: str,
    want_config_tokens: set[str],
) -> bool:
    haystacks = [_normalize_project_text(str(c)) for c in (project.configurations or [])]
    haystacks.append(_normalize_project_text(project.property_type))
    for hay in haystacks:
        if not hay:
            continue
        if want_config in hay or hay in want_config:
            return True
        hay_tokens = set(hay.split()) - _CONFIG_GENERIC_TOKENS
        if want_config_tokens and (want_config_tokens & hay_tokens):
            return True
    return False


def filter_projects(
    projects: list[RealEstateProject],
    *,
    budget_max: float | None = None,
    budget_min: float | None = None,
    configuration: str | None = None,
    location: str | None = None,
) -> list[RealEstateProject]:
    """Filter the structured catalog so the agent can answer 'show me a 2BHK
    under 1cr in Kokapet' precisely instead of eyeballing a prose blob.

    Each filter is best-effort: a project with no price is not excluded by a
    budget filter (we'd rather over-include than hide inventory). Budget uses
    the project's lower bound so 'under X' keeps anything that *starts* at or
    below X.
    """
    # Configuration matching does NOT use the project-name tokenizer — that one
    # strips "villa"/"apartment" as stopwords, which are exactly the terms a
    # caller searches by. "bhk"/"bed" etc. are too generic to match on alone.
    want_config = _normalize_project_text(configuration) if configuration else ""
    want_config_tokens = set(want_config.split()) - _CONFIG_GENERIC_TOKENS
    location_norm = _normalize_project_text(location) if location else ""
    out: list[RealEstateProject] = []
    for project in projects:
        if budget_max is not None and project.price_min is not None:
            if float(project.price_min) > float(budget_max):
                continue
        if budget_min is not None and project.price_max is not None:
            if float(project.price_max) < float(budget_min):
                continue
        if want_config and not _configuration_matches(project, want_config, want_config_tokens):
            continue
        if location_norm:
            loc = _normalize_project_text(project.location)
            if location_norm not in loc and loc not in location_norm:
                continue
        out.append(project)
    return out


# ─────────── WhatsApp pre-set message resolvers ───────────
# Per-project Meta-approved template config lives in ``project.whatsapp``. The
# template's variable positions are defined by the admin when they register the
# template with Meta; we use a FIXED convention so the values line up:
#   Location template: {{1}} = project name, {{2}} = maps link.
#   Brochure template: {{1}} = project name; the brochure URL is the document
#                      header (media) param.
# Both return ``None`` when no template is configured → caller sends nothing.


def project_whatsapp_location(project: RealEstateProject) -> dict[str, Any] | None:
    """Resolved location-message send config, or ``None`` if not configured.

    Sent automatically when a site visit is booked for this project."""
    cfg = (getattr(project, "whatsapp", None) or {}).get("location") or {}
    template = str(cfg.get("template") or "").strip()
    if not template:
        return None
    maps_url = str(cfg.get("maps_url") or "").strip()
    return {
        "template": template,
        "language": str(cfg.get("language") or "en").strip() or "en",
        # {{1}} project name, {{2}} maps link (falls back to the display location).
        "body_params": [project.name, maps_url or (project.location or "")],
        "media_url": None,
        # Per-project WhatsApp sender — overrides the tenant WABA number when set,
        # so this project's location goes out from its own number.
        "sender": str((getattr(project, "whatsapp", None) or {}).get("sender_number") or "").strip() or None,
    }


def project_whatsapp_brochure(project: RealEstateProject) -> dict[str, Any] | None:
    """Resolved brochure-message send config, or ``None`` if not configured.

    Sent from whatsapp_mode when the caller asks for the brochure."""
    cfg = (getattr(project, "whatsapp", None) or {}).get("brochure") or {}
    template = str(cfg.get("template") or "").strip()
    if not template:
        return None
    return {
        "template": template,
        "language": str(cfg.get("language") or "en").strip() or "en",
        # {{1}} project name; the brochure URL rides as the document header.
        "body_params": [project.name],
        "media_url": (project.brochure_url or None),
        # Per-project WhatsApp sender — overrides the tenant WABA number when set.
        "sender": str((getattr(project, "whatsapp", None) or {}).get("sender_number") or "").strip() or None,
    }
