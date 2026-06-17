"""Brochure Analyzer — nano extraction + the 700-token description cap."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import tiktoken

from app.services import brochure_analyzer_service as bas
from app.services.real_estate_project_service import cap_text_to_tokens


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeProjectDB:
    def __init__(self, organization):
        self.organization = organization
        self.added = []

    async def execute(self, _stmt):
        org = self.organization

        class _Result:
            def scalars(self):
                return self

            def first(self):
                return org

        return _Result()

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def refresh(self, obj):
        obj.created_at = obj.created_at or datetime.now(timezone.utc)
        obj.updated_at = obj.updated_at or datetime.now(timezone.utc)


def test_create_project_caps_description_at_700_tokens():
    from app.api.nokvo_one_projects import create_project
    from app.schemas.nokvo_one import RealEstateProjectCreateRequest

    org = SimpleNamespace(id=uuid.uuid4(), industry="real_estate")
    user = SimpleNamespace(id=uuid.uuid4(), organization_id=org.id)
    db = _FakeProjectDB(org)

    # Within the schema's 8000-char limit but well over 700 tokens (~1500).
    long_desc = "Luxury gated community living. " * 240
    enc = tiktoken.get_encoding("o200k_base")
    assert len(enc.encode(long_desc)) > 700  # precondition

    payload = RealEstateProjectCreateRequest(name="Skyline Heights", description=long_desc)
    resp = _run(create_project(payload, user=user, db=db))

    assert len(enc.encode(resp.description)) <= 700
    # The persisted ORM object is capped too (not just the response).
    assert len(enc.encode(db.added[0].description)) <= 700


def test_cap_text_to_tokens_truncates_and_preserves_short():
    enc = tiktoken.get_encoding("o200k_base")
    long = "word " * 2000
    capped = cap_text_to_tokens(long, 700)
    assert len(enc.encode(capped)) <= 700
    # Already-short text is returned unchanged; None passes through.
    assert cap_text_to_tokens("Premium gated community.", 700) == "Premium gated community."
    assert cap_text_to_tokens(None) is None


def test_analyze_brochure_maps_fields_and_caps_description(monkeypatch):
    long_desc = "Luxury living. " * 400  # well over 700 tokens

    async def fake_nano(messages, *, max_tokens=600):
        # The model only sees the brochure text; return strict JSON.
        return json.dumps({
            "name": "  Skyline Heights ",
            "location": "Tukkuguda, Hyderabad",
            "rera_number": "P02400001234",
            "property_type": "Apartments",
            "price_min": 24500000,
            "price_max": "₹4,10,00,000",          # messy → coerced
            "price_display": "₹2.45Cr - ₹4.1Cr",
            "configurations": ["3 BHK", "4 BHK", "3 BHK"],  # dup dropped
            "amenities": ["Clubhouse", "Gym", None],
            "description": long_desc,
            "possession_date": "Dec 2026",
            "builder_name": "Raghava Constructions",
            "contact_phone": "+91 98765 43210",
        })

    from app.services.nokvo_one_voice_pipeline import AzureGroundedLLM
    monkeypatch.setattr(AzureGroundedLLM, "complete_nano", staticmethod(fake_nano))

    out = _run(bas.analyze_brochure("skyline.txt", b"Skyline Heights brochure text..."))

    assert out["name"] == "Skyline Heights"          # trimmed
    assert out["location"] == "Tukkuguda, Hyderabad"
    assert out["rera_number"] == "P02400001234"
    assert out["price_min"] == 24500000.0
    assert out["price_max"] == 41000000.0            # messy string coerced
    assert out["configurations"] == ["3 BHK", "4 BHK"]  # deduped
    assert out["amenities"] == ["Clubhouse", "Gym"]     # None dropped
    assert out["possession_date"] == "Dec 2026"
    # Description capped at 700 tokens.
    enc = tiktoken.get_encoding("o200k_base")
    assert len(enc.encode(out["description"])) <= 700


def test_analyze_brochure_empty_on_unusable(monkeypatch):
    async def junk_nano(messages, *, max_tokens=600):
        return "I could not find any project details."

    from app.services.nokvo_one_voice_pipeline import AzureGroundedLLM
    monkeypatch.setattr(AzureGroundedLLM, "complete_nano", staticmethod(junk_nano))

    # No JSON object in the reply → safe-default {}.
    assert _run(bas.analyze_brochure("x.txt", b"some text")) == {}
    # Empty file → {} without even calling the model.
    assert _run(bas.analyze_brochure("x.txt", b"")) == {}
