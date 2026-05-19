"""Tests for the v2 onboarding surface:

  - STARTER_PACKS shape per business_type.
  - materialize_starter_pack honors business_type, drops unsupported tools,
    falls back to default-on outcomes when nothing is selected.
  - RequireMFACompleted dep returns 403 for users with no TOTP secret.
  - mfa_pending property on OrganizationUser.

The HTTP-level integration of the dep is exercised indirectly — these tests
keep the dep deterministic and avoid spinning up a DB.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import jwt
import pytest

from app.api.deps import RequireMFACompleted
from app.core.config import settings
from app.models.organization_user import OrganizationUser
from app.services.dynamic_tool_resolver import resolve_index
from app.services.nokvo_one_outcome_starter import (
    OUTCOME_PACKS,
    default_agent_name,
    materialize_starter_pack,
    outcomes_for,
)


# ─────────── Starter packs ───────────


def test_every_business_type_has_outcomes():
    for bt in ("clinics", "real_estate", "ecommerce", "hospitality", "other"):
        outs = outcomes_for(bt)
        assert outs, f"{bt} has no outcomes"
        # At least one default-on outcome — otherwise the wizard ships with nothing checked.
        assert any(o["default_on"] for o in outs), f"{bt} has no default-on outcome"


def test_outcome_packs_only_reference_keys_the_resolver_emits():
    # For each business_type, every tool key listed must be either cross-cutting
    # or producible by the resolver for that business_type.
    for bt, packs in OUTCOME_PACKS.items():
        catalog_keys = set(resolve_index(bt).keys())
        for pack in packs:
            for key in pack.get("tool_keys", []):
                assert key in catalog_keys, (
                    f"OUTCOME_PACKS[{bt}].{pack['slug']} references unknown tool '{key}'"
                )


def test_materialize_drops_tools_not_in_org_catalog():
    # An org with no business_type has only cross-cutting tools.
    available = set(resolve_index(None).keys())
    pack = materialize_starter_pack(
        "clinics",
        ["book_appointments"],
        available_tool_keys=available,
    )
    # appointments_create is clinic-specific and won't be available for an org
    # without a business_type. The materializer should drop it silently.
    assert "appointments_create" not in pack["tool_keys"]


def test_materialize_empty_selection_falls_back_to_defaults_on():
    available = set(resolve_index("clinics").keys())
    pack = materialize_starter_pack(
        "clinics", [], available_tool_keys=available
    )
    # default_on outcomes for clinics include "Book and confirm appointments".
    assert "appointments_create" in pack["tool_keys"]
    # The starter pack always has a non-empty prompt.
    assert "outcomes:" in pack["system_prompt"]


def test_materialize_dedupes_tool_keys_across_outcomes():
    # Two clinics outcomes both reference call_log_create; verify no duplicate keys.
    available = set(resolve_index("clinics").keys())
    pack = materialize_starter_pack(
        "clinics",
        ["take_messages", "send_reminders"],
        available_tool_keys=available,
    )
    assert len(pack["tool_keys"]) == len(set(pack["tool_keys"]))


def test_default_agent_name_is_set_per_business_type():
    assert default_agent_name("clinics") == "Clinic Assistant"
    assert default_agent_name("real_estate") == "Property Assistant"
    assert default_agent_name("unknown_bt") == "Customer Assistant"


# ─────────── MFA pending property ───────────


def test_mfa_pending_true_when_no_totp_secret():
    user = OrganizationUser(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        email="test@example.com",
        role="admin",
        status="active",
        auth_provider="password",
        mfa_required=False,
    )
    assert user.mfa_pending is True


def test_mfa_pending_false_when_totp_v2_set():
    user = OrganizationUser(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        email="test@example.com",
        role="admin",
        status="active",
        auth_provider="password",
        mfa_required=False,
        totp_secret_encrypted_v2="encrypted",
    )
    assert user.mfa_pending is False


def test_mfa_pending_false_when_legacy_totp_set():
    user = OrganizationUser(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        email="test@example.com",
        role="admin",
        status="active",
        auth_provider="password",
        mfa_required=False,
        totp_secret_encrypted="encrypted_legacy",
    )
    assert user.mfa_pending is False


# ─────────── RequireMFACompleted dep ───────────


def _make_jwt(*, mfa_completed: bool) -> str:
    return jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "principal_type": "organization_user",
            "mfa_completed": mfa_completed,
        },
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_require_mfa_completed_blocks_user_without_totp():
    dep = RequireMFACompleted()
    user = SimpleNamespace(
        totp_secret_encrypted=None,
        totp_secret_encrypted_v2=None,
    )
    token = _make_jwt(mfa_completed=False)
    with pytest.raises(Exception) as exc_info:
        _run(dep(user=user, token=token))
    detail = getattr(exc_info.value, "detail", {})
    assert isinstance(detail, dict)
    assert detail.get("code") == "mfa_setup_required"


def test_require_mfa_completed_blocks_session_without_mfa_claim():
    dep = RequireMFACompleted()
    user = SimpleNamespace(
        totp_secret_encrypted=None,
        totp_secret_encrypted_v2="encrypted",
    )
    token = _make_jwt(mfa_completed=False)
    with pytest.raises(Exception) as exc_info:
        _run(dep(user=user, token=token))
    detail = getattr(exc_info.value, "detail", {})
    assert isinstance(detail, dict)
    assert detail.get("code") == "mfa_step_up_required"


def test_require_mfa_completed_passes_when_user_and_session_are_mfa_ready():
    dep = RequireMFACompleted()
    user = SimpleNamespace(
        totp_secret_encrypted=None,
        totp_secret_encrypted_v2="encrypted",
    )
    token = _make_jwt(mfa_completed=True)
    result = _run(dep(user=user, token=token))
    assert result is user


# ─────────── Skip-TOTP endpoint feature-flag gating ───────────


def test_skip_totp_endpoint_404_when_v2_flag_off():
    """The endpoint must hide itself unless NOKVO_ONBOARDING_V2 is true."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    with patch.object(settings, "NOKVO_ONBOARDING_V2", False):
        resp = client.post(
            "/api/nokvo-one/signup/skip-totp",
            json={"setup_token": "irrelevant"},
        )
    # Endpoint exists but returns 404 because the flag is off.
    assert resp.status_code == 404
    assert resp.json().get("detail") == "Endpoint not available"


def test_onboarding_v2_flag_exposed_in_config_response():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    with patch.object(settings, "NOKVO_ONBOARDING_V2", True):
        resp = client.get("/api/nokvo-one/config")
    assert resp.status_code == 200
    payload = resp.json()
    assert "onboarding_v2_enabled" in payload
    assert payload["onboarding_v2_enabled"] is True
