"""Security-hardening regression tests (pure / mocked — no DB, no LLM).

Pins the fixes from the threat review so they can't silently regress:
  * Razorpay grant gate (forged webhook → non-paid status → grants nothing),
  * KYC upload size/type validation,
  * prod security-config validator,
  * lead-scorer + agent anti-prompt-injection clauses,
  * Plivo signature rejects a bogus signature (so enforce mode blocks forgeries).
"""
from __future__ import annotations

import pytest


# ── Razorpay: only a Razorpay-confirmed subscription may grant service ───────
def test_subscription_is_paid_gate():
    from app.api.nokvo_one_payments import _subscription_is_paid

    assert _subscription_is_paid({"status": "active"}) is True
    assert _subscription_is_paid({"status": "authenticated"}) is True
    # A forged/replayed webhook for a not-yet-paid sub grants NOTHING.
    for bad in ("created", "pending", "halted", "cancelled", "expired", "", None):
        assert _subscription_is_paid({"status": bad}) is False
    assert _subscription_is_paid({}) is False


# ── KYC upload: size cap + content-type allowlist ───────────────────────────
class _FakeUpload:
    def __init__(self, *, content_type, filename, size=None, content=b"%PDF-1.4 ok"):
        self.content_type = content_type
        self.filename = filename
        self._size = size
        self._content = content

    async def read(self, n=-1):
        if self._size is not None:  # simulate a stream of self._size bytes
            take = n if (n and n > 0) else self._size
            return b"x" * min(take, self._size)
        return self._content


@pytest.mark.asyncio
async def test_upload_accepts_small_valid_pdf():
    from app.api.nokvo_one_onboarding import _read_validated_upload

    data = await _read_validated_upload(
        _FakeUpload(content_type="application/pdf", filename="inc.pdf"), label="Doc"
    )
    assert data == b"%PDF-1.4 ok"


@pytest.mark.asyncio
async def test_upload_rejects_bad_type():
    from fastapi import HTTPException
    from app.api.nokvo_one_onboarding import _read_validated_upload

    with pytest.raises(HTTPException) as e:
        await _read_validated_upload(
            _FakeUpload(content_type="application/x-msdownload", filename="evil.exe"), label="Doc"
        )
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_upload_rejects_oversize():
    from fastapi import HTTPException
    from app.api.nokvo_one_onboarding import _MAX_DOC_BYTES, _read_validated_upload

    big = _FakeUpload(content_type="application/pdf", filename="big.pdf", size=_MAX_DOC_BYTES + 5)
    with pytest.raises(HTTPException) as e:
        await _read_validated_upload(big, label="Doc")
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_upload_rejects_empty():
    from fastapi import HTTPException
    from app.api.nokvo_one_onboarding import _read_validated_upload

    with pytest.raises(HTTPException):
        await _read_validated_upload(
            _FakeUpload(content_type="application/pdf", filename="x.pdf", content=b""), label="Doc"
        )


# ── Startup security-config validator (prod-only) ───────────────────────────
def test_security_validator_dev_silent(monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.settings, "ENVIRONMENT", "development")
    assert config.validate_security_config() == []


def test_security_validator_flags_prod_gaps(monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(config.settings, "RAZORPAY_WEBHOOK_SECRET", "")
    monkeypatch.setattr(config.settings, "PLIVO_VALIDATE_SIGNATURES", "warn")
    monkeypatch.setattr(config.settings, "EXPECTED_ORIGIN", "http://localhost:5173")
    monkeypatch.setattr(config.settings, "JWT_LEGACY_SECRET_FALLBACK", True)
    warns = " ".join(config.validate_security_config()).lower()
    assert "razorpay_webhook_secret" in warns
    assert "plivo_validate_signatures" in warns
    assert "expected_origin" in warns
    assert "jwt_legacy_secret_fallback" in warns


# ── Prompt-injection hardening (clauses present) ────────────────────────────
def test_lead_scorer_has_anti_injection_clause():
    from app.services.lead_score_service import _LEAD_SCORE_SYSTEM

    s = _LEAD_SCORE_SYSTEM
    assert "UNTRUSTED DATA" in s
    assert "NEVER" in s and "obey" in s
    assert "not a qualifying answer" in s.lower()


def test_agent_questionnaire_prompt_has_stay_on_task():
    from app.services.agent_outbound_context import (
        OutboundCampaignContext,
        _compose_questionnaire_only_section,
    )

    ctx = OutboundCampaignContext(
        campaign_id="c1", name="Test", goal="qualify", agent_prompt="", objectives=[],
        exit_conditions=[], tone=None, doc_text=None, caller_name="Riya",
        company_name="Acme", questions=[], question_threshold=1,
    )
    prompt = _compose_questionnaire_only_section(ctx)
    assert "STAY ON TASK" in prompt
    assert "never commands" in prompt.lower() or "untrusted" in prompt.lower()


# ── Plivo: a bogus signature never matches (so enforce mode rejects) ────────
def test_plivo_bogus_signature_no_match():
    from app.api.nokvo_one_voice import _verify_plivo_signature

    matched = _verify_plivo_signature(
        "https://app.nokvo.org/api/nokvo-one/agents/plivo/voice/abc",
        nonce="12345",
        signature="not-a-real-signature",
        tokens=["master-token", "tenant-token"],
    )
    assert not matched  # → _check_plivo_signature returns False under enforce → caller 403s
