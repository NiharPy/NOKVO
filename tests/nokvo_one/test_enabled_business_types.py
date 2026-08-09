"""P3 staged sector enablement: env-driven allowlist, default = real_estate only."""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.services.nokvo_one_business_templates import (
    business_type_options,
    enabled_business_types,
    validate_business_type,
)


def test_default_is_real_estate_only(monkeypatch):
    monkeypatch.setattr(settings, "ENABLED_BUSINESS_TYPES", "")
    assert enabled_business_types() == {"real_estate"}
    assert len(business_type_options()) == 1
    assert validate_business_type("real_estate") == "real_estate"
    with pytest.raises(ValueError):
        validate_business_type("clinics")


def test_env_enables_sectors_staged(monkeypatch):
    monkeypatch.setattr(settings, "ENABLED_BUSINESS_TYPES", "clinics, ecommerce")
    assert enabled_business_types() == {"real_estate", "clinics", "ecommerce"}
    assert len(business_type_options()) == 3  # real_estate + clinics + ecommerce
    assert validate_business_type("clinics") == "clinics"
    assert validate_business_type("ecommerce") == "ecommerce"
    # hospitality was NOT enabled → still rejected
    with pytest.raises(ValueError):
        validate_business_type("hospitality")


def test_unknown_type_rejected_and_normalization(monkeypatch):
    monkeypatch.setattr(settings, "ENABLED_BUSINESS_TYPES", "clinics")
    with pytest.raises(ValueError):
        validate_business_type("bakery")
    # normalization still works for enabled types
    assert validate_business_type("Real Estate") == "real_estate"
    assert validate_business_type("real-estate") == "real_estate"
