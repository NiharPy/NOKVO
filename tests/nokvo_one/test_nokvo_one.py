"""Unit tests for Nokvo One: TOTP crypto, predefined tools, schema validators, tier guard."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from pydantic import ValidationError

from app.core.totp_crypto import (
    TOTPDecryptionError,
    decrypt_totp_secret,
    encrypt_totp_secret,
)
from app.schemas.nokvo_one import (
    NokvoOneInvitationAcceptRequest,
    NokvoOneSignupRequest,
)
from app.services.predefined_tools_service import (
    CATALOG,
    PredefinedToolsService,
    get_tool,
    list_tools,
    validate_tool_keys,
)


# ─────────── TOTP crypto ───────────


def test_totp_encryption_roundtrip():
    plaintext = "JBSWY3DPEHPK3PXP"
    ciphertext = encrypt_totp_secret(plaintext)
    assert ciphertext != plaintext
    assert decrypt_totp_secret(ciphertext) == plaintext


def test_totp_encryption_rejects_empty():
    with pytest.raises(ValueError):
        encrypt_totp_secret("")


def test_totp_decryption_rejects_garbage():
    with pytest.raises(TOTPDecryptionError):
        decrypt_totp_secret("not-a-valid-fernet-ciphertext")


def test_totp_ciphertexts_are_unique_per_invocation():
    """Fernet adds a nonce, so the same plaintext encrypts to different ciphertexts."""
    plaintext = "JBSWY3DPEHPK3PXP"
    a = encrypt_totp_secret(plaintext)
    b = encrypt_totp_secret(plaintext)
    assert a != b
    assert decrypt_totp_secret(a) == plaintext
    assert decrypt_totp_secret(b) == plaintext


# ─────────── Predefined tools catalog ───────────


def test_catalog_has_exactly_v1_tools():
    expected = {
        "lead_tracker_create_lead",
        "lead_tracker_update_status",
        "lead_tracker_add_note",
        "call_logger_create_entry",
        "call_logger_get_history",
        "create_ticket",
        "schedule_callback",
        "send_email_draft",
    }
    actual = {tool.key for tool in CATALOG}
    assert actual == expected, f"V1 catalog drift: missing={expected - actual} extra={actual - expected}"


def test_catalog_excludes_dangerous_tools():
    """V1 must not ship web_search, direct DB writes, or payment/refund tools."""
    forbidden = {"web_search", "execute_sql", "refund_payment", "modify_order", "create_payment"}
    actual = {tool.key for tool in CATALOG}
    assert not (forbidden & actual), f"Dangerous tools must not be present: {forbidden & actual}"


def test_send_email_draft_requires_confirmation():
    tool = get_tool("send_email_draft")
    assert tool is not None
    assert tool.requires_confirmation is True, "send_email_draft must require human confirmation"


def test_send_email_draft_description_explains_no_direct_send():
    tool = get_tool("send_email_draft")
    assert tool is not None
    desc = tool.description.lower()
    assert "draft" in desc
    assert "human" in desc or "confirmation" in desc


def test_list_tools_returns_serialisable_dicts():
    items = list_tools()
    assert len(items) == 8
    for item in items:
        assert {"key", "display_name", "description", "input_schema", "requires_confirmation"} <= set(item.keys())


def test_validate_tool_keys_rejects_unknown():
    with pytest.raises(ValueError):
        validate_tool_keys(["lead_tracker_create_lead", "web_search"])


def test_validate_tool_keys_accepts_known():
    keys = ["lead_tracker_create_lead", "create_ticket"]
    assert validate_tool_keys(keys) == keys


# ─────────── Schema validators ───────────


def test_signup_password_validator_rejects_short():
    with pytest.raises(ValidationError):
        NokvoOneSignupRequest(
            org_name="Acme",
            admin_name="A",
            admin_email="a@acmecorp.com",
            password="short1",
        )


def test_signup_password_validator_requires_letter_and_digit():
    with pytest.raises(ValidationError):
        NokvoOneSignupRequest(
            org_name="Acme",
            admin_name="A",
            admin_email="a@acmecorp.com",
            password="onlyletters",
        )
    with pytest.raises(ValidationError):
        NokvoOneSignupRequest(
            org_name="Acme",
            admin_name="A",
            admin_email="a@acmecorp.com",
            password="1234567890",
        )


def test_signup_rejects_personal_email():
    with pytest.raises(ValidationError):
        NokvoOneSignupRequest(
            org_name="Acme",
            admin_name="A",
            admin_email="a@gmail.com",
            password="ValidPass123",
        )


def test_signup_accepts_valid_payload():
    req = NokvoOneSignupRequest(
        org_name="Acme Inc",
        admin_name="Alice",
        admin_email="alice@acmecorp.com",
        password="ValidPass123",
    )
    assert req.admin_email == "alice@acmecorp.com"
    assert req.org_name == "Acme Inc"


def test_invite_accept_password_validator():
    with pytest.raises(ValidationError):
        NokvoOneInvitationAcceptRequest(token="abc", password="weak")
    ok = NokvoOneInvitationAcceptRequest(token="abc", password="StrongPass1")
    assert ok.password == "StrongPass1"


# ─────────── Predefined tools dispatcher (DB-mocked) ───────────


class _FakeDB:
    def __init__(self):
        self.added = []
        self.flushed = False
        self.rolled_back = False

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        pass

    async def rollback(self):
        self.rolled_back = True

    async def execute(self, _stmt):
        class _Result:
            def scalars(self):
                return self

            def first(self):
                return None

            def all(self):
                return []

        return _Result()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_send_email_draft_dispatch_creates_pending_confirmation():
    db = _FakeDB()
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    result = _run(
        PredefinedToolsService.execute(
            db,
            org_id,
            user_id,
            "send_email_draft",
            {
                "to_email": "customer@example.com",
                "subject": "Follow-up",
                "body": "Hi there, thanks for reaching out.",
            },
        )
    )
    assert result["ok"] is True
    assert result["status"] == "pending_confirmation"
    assert "human" in result["message"].lower() or "confirm" in result["message"].lower()
    assert len(db.added) == 1
    record = db.added[0]
    assert record.record_type == "email_draft"
    assert record.status == "pending_confirmation"


def test_unknown_tool_rejected_by_dispatcher():
    db = _FakeDB()
    with pytest.raises(ValueError):
        _run(
            PredefinedToolsService.execute(
                db, uuid.uuid4(), uuid.uuid4(), "web_search", {"query": "x"}
            )
        )


def test_create_ticket_dispatch():
    db = _FakeDB()
    result = _run(
        PredefinedToolsService.execute(
            db,
            uuid.uuid4(),
            uuid.uuid4(),
            "create_ticket",
            {
                "subject": "Login broken",
                "description": "Cannot log in since this morning.",
                "priority": "high",
            },
        )
    )
    assert result["ok"] is True
    assert "ticket_id" in result
    assert db.added[0].record_type == "ticket"
    assert db.added[0].status == "open"
