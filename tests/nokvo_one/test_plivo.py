"""Plivo telephony: subaccount/application/number provisioning logic, inbound
answer XML, and the phone-link (call-forwarding) response shape. The Plivo REST
layer is mocked — no network."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.plivo_service import PlivoService, PlivoError


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def plivo_creds(monkeypatch):
    monkeypatch.setattr(settings, "PLIVO_AUTH_ID", "MASTER")
    monkeypatch.setattr(settings, "PLIVO_AUTH_TOKEN", "secret")


def _mock_request(monkeypatch, handler):
    async def fake(method, url, *, auth, json_body=None):
        return handler(method, url, json_body)
    monkeypatch.setattr(PlivoService, "_request", staticmethod(fake))


def test_create_subaccount_and_application(plivo_creds, monkeypatch):
    calls = []

    def handler(method, url, body):
        calls.append((method, url, body))
        if "/Subaccount/" in url:
            return {"auth_id": "SUBxxx", "auth_token": "subtok"}
        if "/Application/" in url:
            return {"app_id": "APP123"}
        return {}

    _mock_request(monkeypatch, handler)
    sub = _run(PlivoService.create_subaccount("acme"))
    assert sub == {"auth_id": "SUBxxx", "auth_token": "subtok"}
    app_id = _run(PlivoService.create_application(app_name="nokvo-acme", answer_url="https://x/api/.../plivo/voice/L1"))
    assert app_id == "APP123"
    app_call = next(c for c in calls if "/Application/" in c[1])
    assert app_call[2]["answer_url"].endswith("/plivo/voice/L1")
    assert app_call[2]["answer_method"] == "POST"


def test_rent_number_assigns_to_app_and_subaccount(plivo_creds, monkeypatch):
    calls = []

    def handler(method, url, body):
        calls.append((method, url, body))
        if "PhoneNumber/?" in url:
            return {"objects": [{"number": "911140000000"}]}
        return {}

    _mock_request(monkeypatch, handler)
    res = _run(PlivoService.rent_number(country="IN", app_id="APP123", sub_auth_id="SUBxxx"))
    assert res == {"number": "911140000000", "status": "active"}
    # The DID was bound to the app + moved to the subaccount.
    assign = next(c for c in calls if c[1].endswith("/Number/911140000000/"))
    assert assign[2]["app_id"] == "APP123" and assign[2]["subaccount"] == "SUBxxx"


def test_assign_number_normalizes_formatted_did(plivo_creds, monkeypatch):
    # A formatted DID ("+91 22 6423 3631") must hit a BARE-digit /Number/<n>/ path —
    # otherwise Plivo 404s ("not found"), the reported re-bind failure.
    calls = []
    _mock_request(monkeypatch, lambda m, u, b: calls.append((m, u, b)) or {})
    _run(PlivoService.assign_number("+91 22 6423 3631", app_id="APP123", sub_auth_id="SUBxxx"))
    assert calls and calls[-1][1].endswith("/Number/912264233631/")
    assert "+" not in calls[-1][1] and " " not in calls[-1][1]


def test_set_tenant_number_normalizes_assign_and_storage(plivo_creds, monkeypatch):
    calls = []
    _mock_request(monkeypatch, lambda m, u, b: calls.append((m, u, b)) or {})
    tr = SimpleNamespace(
        provider_status={"plivo": {"application_id": "APP123", "subaccount_auth_id": "SUBxxx", "link_id": "L1"}},
        twilio_phone_number=None,
        tenant_id="t1",
    )

    class _DB:
        def add(self, *a, **k):
            pass

        async def commit(self):
            pass

    res = _run(PlivoService.set_tenant_number(tr, _DB(), number="+91 22 6423 3631", reassign=True, base=None))
    # Stored caller ID is bare-digit, and the assign hit the bare-digit path (no 404).
    assert res["number"] == "912264233631" and res["assigned"] is True and res.get("assign_error") is None
    assert tr.twilio_phone_number == "912264233631"
    assert tr.provider_status["plivo"]["number"] == "912264233631"
    assert any(c[1].endswith("/Number/912264233631/") for c in calls)


class _StubDB:
    def add(self, *a, **k):
        pass

    async def commit(self):
        pass


def test_set_tenant_numbers_binds_up_to_five_without_subaccount_move(plivo_creds, monkeypatch):
    calls = []
    _mock_request(monkeypatch, lambda m, u, b: calls.append((m, u, b)) or {})
    tr = SimpleNamespace(
        provider_status={"plivo": {"application_id": "APP123", "subaccount_auth_id": "SUBxxx", "link_id": "L1"}},
        twilio_phone_number=None,
        tenant_id="t1",
    )
    res = _run(PlivoService.set_tenant_numbers(
        tr, _StubDB(), numbers=["+91 22 6423 3631", "919000000001", "919000000002"], reassign=True, base=None,
    ))
    assert res["numbers"] == ["912264233631", "919000000001", "919000000002"]
    assert res["number"] == "912264233631"                     # primary = first
    assert res["assigned_count"] == 3 and res["assigned"] is True
    assert tr.provider_status["plivo"]["numbers"] == res["numbers"]
    # Each DID is bound to the app WITHOUT a subaccount move — a master app can't be
    # assigned to a subaccount number (the reported 400). So no "subaccount" in body.
    number_calls = [c for c in calls if "/Number/" in c[1]]
    assert len(number_calls) == 3
    for _m, _u, body in number_calls:
        assert body.get("app_id") == "APP123" and "subaccount" not in body


def test_set_tenant_numbers_dedupes_and_caps_at_five(plivo_creds, monkeypatch):
    _mock_request(monkeypatch, lambda m, u, b: {})
    tr = SimpleNamespace(provider_status={"plivo": {"application_id": "A"}}, twilio_phone_number=None, tenant_id="t")
    nums = ["+91 22 6423 3631", "912264233631", "9100000001", "9100000002", "9100000003", "9100000004", "9100000005"]
    res = _run(PlivoService.set_tenant_numbers(tr, _StubDB(), numbers=nums, reassign=False, base=None))
    # The formatted twin collapses onto its bare form, then capped at the first 5.
    assert res["numbers"] == ["912264233631", "9100000001", "9100000002", "9100000003", "9100000004"]


def test_set_tenant_numbers_rejects_short_fragments(plivo_creds, monkeypatch):
    # A formatted DID split on its spaces ("+91 22 6423 2977" → 91/22/6423/2977) →
    # all fragments → a clear error and NO doomed /Number/<frag>/ call.
    calls = []
    _mock_request(monkeypatch, lambda m, u, b: calls.append((m, u, b)) or {})
    tr = SimpleNamespace(provider_status={"plivo": {"application_id": "A"}}, twilio_phone_number=None, tenant_id="t")
    with pytest.raises(PlivoError) as exc:
        _run(PlivoService.set_tenant_numbers(tr, _StubDB(), numbers=["91", "22", "6423", "2977"], reassign=True, base=None))
    assert "fragment" in str(exc.value).lower()
    assert not calls


def test_set_tenant_numbers_flags_fragment_but_binds_valid(plivo_creds, monkeypatch):
    calls = []
    _mock_request(monkeypatch, lambda m, u, b: calls.append((m, u, b)) or {})
    tr = SimpleNamespace(provider_status={"plivo": {"application_id": "A"}}, twilio_phone_number=None, tenant_id="t")
    res = _run(PlivoService.set_tenant_numbers(tr, _StubDB(), numbers=["912264233631", "22"], reassign=True, base=None))
    assert res["numbers"] == ["912264233631"] and res["assigned_count"] == 1
    frag = next(e for e in res["per_number"] if e["number"] == "22")
    assert "too short" in frag["error"] and frag["assigned"] is False
    number_calls = [c for c in calls if "/Number/" in c[1]]
    assert number_calls and all("/Number/912264233631/" in c[1] for c in number_calls)


def test_rent_number_raises_when_no_india_inventory(plivo_creds, monkeypatch):
    # India KYC/regulatory → no instantly-rentable DID. Caller treats this as pending.
    _mock_request(monkeypatch, lambda m, u, b: {"objects": []} if "PhoneNumber/?" in u else {})
    with pytest.raises(PlivoError):
        _run(PlivoService.rent_number(country="IN"))


def test_missing_master_creds_raises(monkeypatch):
    monkeypatch.setattr(settings, "PLIVO_AUTH_ID", "")
    monkeypatch.setattr(settings, "PLIVO_AUTH_TOKEN", "")
    with pytest.raises(PlivoError):
        _run(PlivoService.create_subaccount("acme"))


def test_phone_link_response_shows_forwarding_did():
    tr = SimpleNamespace(
        provider_status={
            "plivo": {
                "number": "911140000000",
                "number_status": "active",
                "application_id": "APP123",
                "forward_from_number": "919876543210",
                "answer_url": "https://x/plivo/voice/L1",
            },
            "agent_phone_link": {"link_id": "L1", "status": "linked"},
        },
        twilio_phone_number=None,
    )
    r = PlivoService.phone_link_response(tr)
    assert r["provider"] == "plivo"
    assert r["plivo_number"] == "911140000000"      # tenant forwards their number HERE
    assert r["number_status"] == "active"
    assert r["forward_from_number"] == "919876543210"
    assert r["link_id"] == "L1"


def test_pending_verification_when_no_number():
    tr = SimpleNamespace(provider_status={"plivo": {"application_id": "APP", "number": None}}, twilio_phone_number=None)
    r = PlivoService.phone_link_response(tr)
    assert r["number_status"] == "pending_verification"


def test_inbound_answer_xml_is_bidirectional_stream():
    from app.api.nokvo_one_voice import _plivo_stream_xml
    xml = _plivo_stream_xml("wss://host/api/nokvo-one/agents/plivo/media/L1")
    assert "<Stream" in xml and 'bidirectional="true"' in xml
    assert "wss://host/api/nokvo-one/agents/plivo/media/L1</Stream>" in xml
    assert "audio/x-l16" in xml


def test_inbound_answer_xml_keeps_status_callback():
    from app.api.nokvo_one_voice import _plivo_stream_xml
    xml = _plivo_stream_xml(
        "wss://host/api/nokvo-one/agents/plivo/media/L1",
        "https://host/api/nokvo-one/agents/plivo/stream-status/L1",
    )
    assert 'statusCallbackUrl="https://host/api/nokvo-one/agents/plivo/stream-status/L1"' in xml
    assert 'statusCallbackMethod="POST"' in xml


# ── Public URL building (the inbound-connectivity fix) ──────────────────────


def test_public_base_url_precedence(monkeypatch):
    from app.services.public_url import public_base_url, ws_base_url

    # 1. Explicit PLIVO_WEBHOOK_BASE_URL wins over everything.
    monkeypatch.setattr(settings, "PLIVO_WEBHOOK_BASE_URL", "https://hooks.example/")
    monkeypatch.setattr(settings, "AGENT_PUBLIC_BASE_URL", "https://agent.example")
    assert public_base_url() == "https://hooks.example"
    assert ws_base_url() == "wss://hooks.example"

    # 2. AGENT_PUBLIC_BASE_URL when explicit is empty (and not the localhost default).
    monkeypatch.setattr(settings, "PLIVO_WEBHOOK_BASE_URL", "")
    assert public_base_url() == "https://agent.example"

    # 3. Request-derived, honoring X-Forwarded-* (the proxy case: internal
    # http request must still yield the public https/wss URL).
    monkeypatch.setattr(settings, "AGENT_PUBLIC_BASE_URL", "http://localhost:8000")
    request = SimpleNamespace(
        headers={"x-forwarded-proto": "https", "x-forwarded-host": "pub.example"},
        url=SimpleNamespace(scheme="http", hostname="10.0.0.5", port=8000),
    )
    assert public_base_url(request) == "https://pub.example"
    assert ws_base_url(request) == "wss://pub.example"

    # 4. No forwarded headers → the raw request URL.
    bare = SimpleNamespace(
        headers={},
        url=SimpleNamespace(scheme="https", hostname="direct.example", port=None),
    )
    assert public_base_url(bare) == "https://direct.example"


def test_validate_public_base_url_warnings(monkeypatch):
    from app.services.public_url import validate_public_base_url

    monkeypatch.setattr(settings, "PLIVO_WEBHOOK_BASE_URL", "")
    monkeypatch.setattr(settings, "AGENT_PUBLIC_BASE_URL", "http://localhost:8000")
    warnings = validate_public_base_url()
    assert any("PLIVO_WEBHOOK_BASE_URL is not set" in w for w in warnings)
    assert any("localhost" in w for w in warnings)

    monkeypatch.setattr(settings, "PLIVO_WEBHOOK_BASE_URL", "http://insecure.example")
    assert any("plain http" in w for w in validate_public_base_url())

    monkeypatch.setattr(settings, "PLIVO_WEBHOOK_BASE_URL", "https://a.example")
    monkeypatch.setattr(settings, "AGENT_PUBLIC_BASE_URL", "https://b.example")
    assert any("disagree" in w for w in validate_public_base_url())

    monkeypatch.setattr(settings, "AGENT_PUBLIC_BASE_URL", "https://a.example")
    assert validate_public_base_url() == []


# ── Application update + webhook re-sync ────────────────────────────────────


def test_update_application_posts_answer_url(plivo_creds, monkeypatch):
    calls = []

    def handler(method, url, body):
        calls.append((method, url, body))
        return {}

    _mock_request(monkeypatch, handler)
    _run(PlivoService.update_application("APP9", answer_url="https://new.example/api/nokvo-one/agents/plivo/voice/L1"))
    assert len(calls) == 1
    method, url, body = calls[0]
    assert method == "POST"
    assert url.endswith("/Application/APP9/")
    assert body["answer_url"].endswith("/plivo/voice/L1")
    assert body["answer_method"] == "POST"


def test_update_application_noop_without_urls(plivo_creds, monkeypatch):
    calls = []
    _mock_request(monkeypatch, lambda m, u, b: calls.append((m, u, b)) or {})
    _run(PlivoService.update_application("APP9"))
    assert calls == []


def test_needs_webhook_resync_matrix():
    base = "https://new.example"
    expected = PlivoService.expected_answer_url("L1", base)
    assert expected == "https://new.example/api/nokvo-one/agents/plivo/voice/L1"
    # Stale URL → needs resync.
    assert PlivoService.needs_webhook_resync(
        {"application_id": "A", "link_id": "L1", "answer_url": "https://old.example/api/nokvo-one/agents/plivo/voice/L1"},
        base,
    )
    # Already in sync → no.
    assert not PlivoService.needs_webhook_resync(
        {"application_id": "A", "link_id": "L1", "answer_url": expected}, base
    )
    # Nothing to sync (no application / no link / no base) → no.
    assert not PlivoService.needs_webhook_resync({}, base)
    assert not PlivoService.needs_webhook_resync({"application_id": "A"}, base)
    assert not PlivoService.needs_webhook_resync(
        {"application_id": "A", "link_id": "L1", "answer_url": "x"}, ""
    )


class _FakeDb:
    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def test_resync_tenant_webhook_updates_plivo_and_store(plivo_creds, monkeypatch):
    calls = []

    def handler(method, url, body):
        calls.append((method, url, body))
        return {}

    _mock_request(monkeypatch, handler)
    tr = SimpleNamespace(
        tenant_id="t-1",
        provider_status={
            "plivo": {
                "application_id": "APP1",
                "link_id": "L1",
                "answer_url": "https://old.example/api/nokvo-one/agents/plivo/voice/L1",
            }
        },
    )
    db = _FakeDb()
    result = _run(PlivoService.resync_tenant_webhook(tr, db, base="https://new.example"))
    assert result["updated"] is True
    assert calls and calls[0][1].endswith("/Application/APP1/")
    assert tr.provider_status["plivo"]["answer_url"] == (
        "https://new.example/api/nokvo-one/agents/plivo/voice/L1"
    )
    assert db.commits == 1


def test_resync_tenant_webhook_skips_in_sync(plivo_creds, monkeypatch):
    calls = []
    _mock_request(monkeypatch, lambda m, u, b: calls.append(1) or {})
    expected = PlivoService.expected_answer_url("L1", "https://same.example")
    tr = SimpleNamespace(
        tenant_id="t-1",
        provider_status={"plivo": {"application_id": "APP1", "link_id": "L1", "answer_url": expected}},
    )
    db = _FakeDb()
    result = _run(PlivoService.resync_tenant_webhook(tr, db, base="https://same.example"))
    assert result["updated"] is False and result["reason"] == "in_sync"
    assert calls == [] and db.commits == 0


# ── Plivo webhook signature validation (X-Plivo-Signature-V2) ───────────────


def _v2_signature(token: str, url: str, nonce: str) -> str:
    import base64
    import hashlib
    import hmac

    return base64.b64encode(
        hmac.new(token.encode(), (url + nonce).encode(), hashlib.sha256).digest()
    ).decode()


def test_verify_plivo_signature_known_vector():
    from app.api.nokvo_one_voice import _verify_plivo_signature

    url = "https://hooks.example/api/nokvo-one/agents/plivo/voice/L1"
    nonce = "12345"
    sig = _v2_signature("secret", url, nonce)
    assert _verify_plivo_signature(url, nonce, sig, ["secret"]) == "master"
    # Second (tenant) token also matches when the first doesn't.
    assert _verify_plivo_signature(url, nonce, sig, ["wrong", "secret"]) == "tenant"
    # Comma-separated rotation list.
    assert _verify_plivo_signature(url, nonce, f"garbage,{sig}", ["secret"]) == "master"
    # Mismatch / missing inputs → None.
    assert _verify_plivo_signature(url, nonce, "bad", ["secret"]) is None
    assert _verify_plivo_signature(url, "", sig, ["secret"]) is None
    assert _verify_plivo_signature(url, nonce, "", ["secret"]) is None


def test_check_plivo_signature_modes(monkeypatch):
    from app.api import nokvo_one_voice as mod

    url_path = "/api/nokvo-one/agents/plivo/voice/L1"
    base = "https://hooks.example"
    monkeypatch.setattr(settings, "PLIVO_WEBHOOK_BASE_URL", base)
    monkeypatch.setattr(settings, "PLIVO_AUTH_TOKEN", "secret")
    good_sig = _v2_signature("secret", base + url_path, "n-1")

    def _req(sig, nonce):
        return SimpleNamespace(
            headers={"x-plivo-signature-v2": sig, "x-plivo-signature-v2-nonce": nonce},
            url=SimpleNamespace(scheme="http", hostname="internal", port=8000, path=url_path, query=""),
        )

    monkeypatch.setattr(settings, "PLIVO_VALIDATE_SIGNATURES", "enforce")
    assert _run(mod._check_plivo_signature(_req(good_sig, "n-1"))) is True
    assert _run(mod._check_plivo_signature(_req("forged", "n-1"))) is False

    # warn mode: mismatch logs but allows.
    monkeypatch.setattr(settings, "PLIVO_VALIDATE_SIGNATURES", "warn")
    assert _run(mod._check_plivo_signature(_req("forged", "n-1"))) is True

    # off / no token: always allow.
    monkeypatch.setattr(settings, "PLIVO_VALIDATE_SIGNATURES", "off")
    assert _run(mod._check_plivo_signature(_req("forged", "n-1"))) is True
    monkeypatch.setattr(settings, "PLIVO_VALIDATE_SIGNATURES", "enforce")
    monkeypatch.setattr(settings, "PLIVO_AUTH_TOKEN", "")
    assert _run(mod._check_plivo_signature(_req("forged", "n-1"))) is True


# ── Outbound-status hardening ────────────────────────────────────────────────


def test_outbound_status_survives_unparseable_payload(monkeypatch):
    """Both form() and json() raising must not NameError — the handler still
    parses to {} and proceeds (here: down the not-found/deferred path)."""
    from app.api import nokvo_one_voice as mod

    monkeypatch.setattr(settings, "PLIVO_VALIDATE_SIGNATURES", "off")

    async def _no_rows(call_link_id, db):
        return None, None

    monkeypatch.setattr(
        mod.OutboundCampaignService, "get_by_call_link_id", staticmethod(_no_rows)
    )

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)

    class _BadRequest:
        headers = {}
        url = SimpleNamespace(scheme="https", hostname="x", port=None, path="/p", query="")

        async def form(self):
            raise RuntimeError("bad form")

        async def json(self):
            raise RuntimeError("bad json")

    result = _run(mod.plivo_outbound_status("missing-link", _BadRequest(), db=None))
    assert result["ok"] is True
    assert result.get("deferred") is True


# ── In-flight reconciliation ─────────────────────────────────────────────────


def test_is_webhook_timed_out_boundaries():
    from datetime import datetime, timedelta, timezone

    from app.models.lead_followup_schedule import FollowupStatus
    from app.services.followup_scheduler_service import FollowupSchedulerService as S

    now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)
    row = SimpleNamespace(
        status=FollowupStatus.in_flight,
        updated_at=now - timedelta(minutes=31),
        created_at=None,
    )
    assert S.is_webhook_timed_out(row, now, 30) is True
    row.updated_at = now - timedelta(minutes=29)
    assert S.is_webhook_timed_out(row, now, 30) is False
    # Non-in_flight rows never time out.
    row.status = FollowupStatus.pending
    row.updated_at = now - timedelta(days=2)
    assert S.is_webhook_timed_out(row, now, 30) is False
    # No timestamps at all → treat as timed out (can't prove freshness).
    stale = SimpleNamespace(status=FollowupStatus.in_flight, updated_at=None, created_at=None)
    assert S.is_webhook_timed_out(stale, now, 30) is True
    # Naive timestamps are coerced to UTC, not crashed on.
    naive = SimpleNamespace(
        status=FollowupStatus.in_flight,
        updated_at=(now - timedelta(minutes=40)).replace(tzinfo=None),
        created_at=None,
    )
    assert S.is_webhook_timed_out(naive, now, 30) is True


# ── list_account_numbers: rotate caller IDs over a sub-account's DID pool ──────

def test_list_account_numbers_parses_objects(monkeypatch):
    """GET /Number/ → bare-digit, voice-only, deduped list (the pool the bulk
    dialer rotates the caller ID across)."""
    def handler(method, url, body):
        assert method == "GET" and "/Number/" in url
        return {
            "objects": [
                {"number": "+91 22 6423 2977", "voice_enabled": True},
                {"number": "918031321315", "voice_enabled": True},
                {"number": "918031321315", "voice_enabled": True},   # dup → dropped
                {"number": "917000000000", "voice_enabled": False},  # SMS-only → skipped
            ]
        }
    _mock_request(monkeypatch, handler)
    nums = _run(PlivoService.list_account_numbers(("SUBacct", "tok")))
    assert nums == ["912264232977", "918031321315"]


def test_list_account_numbers_empty_on_error(monkeypatch):
    """Any Plivo error → [] so the dialer falls back to its single configured DID."""
    def handler(method, url, body):
        raise PlivoError("boom")
    _mock_request(monkeypatch, handler)
    assert _run(PlivoService.list_account_numbers(("SUBacct", "tok"))) == []
