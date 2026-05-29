"""Unit tests for the real-estate project helpers added to make the agent
better at linking, pricing, filtering and steering toward real inventory.

Pure-function coverage (no DB): fuzzy matching, Indian price formatting,
catalog filtering, the project-name tool enum, and the runtime helper that
constrains only project-bearing tools.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.services.predefined_tools_service import MACRO_CATALOG
from app.services.nokvo_one_agent_runtime import _with_project_choices
from app.services.real_estate_project_service import (
    filter_projects,
    find_project_match,
    format_inr,
    project_choices_for_tool_schema,
    project_summary_lines,
)


def _project(name, **kw):
    base = dict(
        id=uuid.uuid4(),
        name=name,
        location=None,
        property_type=None,
        price_min=None,
        price_max=None,
        price_display=None,
        configurations=[],
        amenities=[],
        description=None,
        possession_date=None,
        builder_name=None,
        brochure_url=None,
        contact_phone=None,
        rera_number=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _catalog():
    return [
        _project("Raghava Skyline Residences", location="Kokapet",
                 price_min=11800000, configurations=["2 BHK", "3 BHK", "3.5 BHK"]),
        _project("Raghava Green Meadows", location="Tellapur",
                 price_min=8200000, configurations=["2 BHK", "3 BHK"]),
        _project("Raghava Urban Nest", location="Bachupally",
                 price_min=6200000, configurations=["2 BHK", "3 BHK"]),
        _project("Raghava Serene Villas", location="Mokila",
                 price_min=23500000, property_type="Villa",
                 configurations=["3 BHK", "4 BHK"]),
    ]


# ─────────── Indian price formatting ───────────


def test_format_inr_crore_and_lakh():
    assert format_inr(11800000) == "₹1.18 crore"
    assert format_inr(23500000) == "₹2.35 crore"
    assert format_inr(8200000) == "₹82 lakh"
    assert format_inr(6200000) == "₹62 lakh"
    assert format_inr(7800000) == "₹78 lakh"


def test_format_inr_sub_lakh_uses_indian_grouping():
    assert format_inr(95000) == "₹95,000"
    assert format_inr(150000) == "₹1.5 lakh"


def test_format_inr_zero_and_garbage():
    assert format_inr(0) == ""
    assert format_inr(None) == ""
    assert format_inr("abc") == ""


def test_summary_line_speaks_crore_not_western_grouping():
    line = project_summary_lines([_project("X", price_min=11800000)])[0]
    assert "₹1.18 crore" in line
    assert "11,800,000" not in line


def test_summary_line_surfaces_builder_and_brochure():
    line = project_summary_lines(
        [_project("X", builder_name="Raghava Group", brochure_url="https://x/y.pdf",
                  contact_phone="04041897914")]
    )[0]
    assert "by Raghava Group" in line
    assert "brochure available on request" in line
    assert "site contact: 04041897914" in line
    # The raw URL is never put in the spoken line.
    assert "https://x/y.pdf" not in line


# ─────────── Fuzzy matching ───────────


def test_find_project_match_substring_and_tokens():
    projects = _catalog()
    assert find_project_match(projects, project_name="Skyline").name == "Raghava Skyline Residences"
    assert find_project_match(projects, project_name="skyline residences").name == "Raghava Skyline Residences"
    assert find_project_match(projects, project_name="Green Meadows").name == "Raghava Green Meadows"


def test_find_project_match_exact_id_wins():
    projects = _catalog()
    target = projects[2]
    assert find_project_match(projects, project_id=str(target.id)) is target


def test_find_project_match_ambiguous_builder_prefix_returns_none():
    # "Raghava" is shared by every project — too ambiguous to guess.
    assert find_project_match(_catalog(), project_name="Raghava") is None


def test_find_project_match_unknown_returns_none():
    assert find_project_match(_catalog(), project_name="Prestige Lakeside") is None
    assert find_project_match([], project_name="anything") is None


# ─────────── Catalog filtering ───────────


def test_filter_projects_by_budget():
    matches = filter_projects(_catalog(), budget_max=10000000)
    names = {p.name for p in matches}
    assert "Raghava Skyline Residences" not in names  # starts at ₹1.18cr
    assert "Raghava Serene Villas" not in names       # starts at ₹2.35cr
    assert {"Raghava Green Meadows", "Raghava Urban Nest"} <= names


def test_filter_projects_by_location():
    matches = filter_projects(_catalog(), location="Kokapet")
    assert [p.name for p in matches] == ["Raghava Skyline Residences"]


def test_filter_projects_by_configuration():
    matches = filter_projects(_catalog(), configuration="villa")
    assert [p.name for p in matches] == ["Raghava Serene Villas"]


# ─────────── Tool-schema enum ───────────


def test_project_choices_emits_enum_and_description():
    frag = project_choices_for_tool_schema(_catalog())
    name_field = frag["project_name"]
    assert "Raghava Skyline Residences" in name_field["enum"]
    assert "EXACT" in name_field["description"]
    assert "project_id" in frag


def test_with_project_choices_only_touches_project_tools():
    qualify = MACRO_CATALOG["real_estate"][0]
    assert qualify.key == "qualify_lead_and_schedule_visit"
    plain = MACRO_CATALOG["ecommerce"][0]  # no project fields

    enriched = _with_project_choices([qualify, plain], _catalog())
    by_key = {t.key: t for t in enriched}

    enum = by_key["qualify_lead_and_schedule_visit"].input_schema["properties"]["project_name"]["enum"]
    assert "Raghava Green Meadows" in enum
    # The non-project tool is returned untouched (same object identity).
    assert by_key[plain.key] is plain
    # Original catalog tool is not mutated.
    assert "enum" not in qualify.input_schema["properties"]["project_name"]


def test_with_project_choices_noop_without_projects():
    tools = [MACRO_CATALOG["real_estate"][0]]
    assert _with_project_choices(tools, []) is tools
