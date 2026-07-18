"""APEX CRM API — the pure, DB-free units.

The DB-bound paths (ingest ON CONFLICT dedupe, resume-from-completed, outbox
claim/backoff against Postgres) are exercised end-to-end in the smoke flow;
here we lock the pure logic: the ``nkap`` key family (mint/parse/verify + the
family guards both auth deps rely on), the alias-tolerant CRM lead extraction
(+ E.164 canonicalization), the outcome→event vocabulary, webhook payload
shape, and the Stripe-style HMAC signature.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace

from app.core import api_keys as k
from app.services import crm_webhook_service as whs
from app.api.apex_public import _extract_lead, _leads_from_body


# ── nkap key family ──────────────────────────────────────────────────────────
def test_mint_nkap_family_roundtrip():
    m = k.mint("live", family="nkap")
    assert m.raw.startswith("nkap_live_")
    # key_prefix fits the 16-char column: "nkap_live_" + 6.
    assert m.key_prefix == m.raw[:16] and len(m.key_prefix) == 16
    p = k.parse(m.raw)
    assert p is not None and p.family == "nkap"
    assert p.key_prefix == m.key_prefix
    assert k.verify(p, m.secret_hash)


def test_legacy_nk_family_unchanged():
    m = k.mint("live")
    assert m.raw.startswith("nk_live_") and m.family == "nk"
    p = k.parse(m.raw)
    assert p is not None and p.family == "nk" and k.verify(p, m.secret_hash)


def test_parse_rejects_malformed_and_cross_family_shapes():
    assert k.parse(None) is None
    assert k.parse("") is None
    assert k.parse("nkap_live_short") is None
    assert k.parse("nkx_live_" + "a" * 42) is None
    # A family guard exists in BOTH deps: nk keys never authenticate /apex/v1
    # and nkap keys never authenticate Connect. The parse layer's family field
    # is what they branch on.
    nk = k.parse(k.mint("live").raw)
    nkap = k.parse(k.mint("live", family="nkap").raw)
    assert nk.family != nkap.family


def test_wrong_secret_fails_verify():
    m = k.mint("live", family="nkap")
    other = k.parse(k.mint("live", family="nkap").raw)
    assert not k.verify(other, m.secret_hash)


# ── alias-tolerant lead extraction ───────────────────────────────────────────
def test_extract_lead_basic_and_e164():
    lead, err = _extract_lead({"phone": "+91 75696 72503", "name": "Asha", "external_id": "z-1"})
    assert err is None
    assert lead["phone"] == "917569672503"  # canonical bare-digit (CSV-path parity)
    assert lead["name"] == "Asha" and lead["external_id"] == "z-1"


def test_extract_lead_aliases_and_meta():
    lead, err = _extract_lead({
        "Mobile": "7569672503",             # case-insensitive alias, bare 10-digit
        "first_name": "Asha", "last_name": "Rao",
        "lead_id": 4711,                     # non-string external id
        "city": "Hyderabad",                 # unrecognized scalar → meta
        "nested": {"drop": "me"},            # non-scalar → dropped
    })
    assert err is None
    assert lead["phone"] == "917569672503"
    assert lead["name"] == "Asha Rao"
    assert lead["external_id"] == "4711"
    assert lead["meta"] == {"city": "Hyderabad"}


def test_extract_lead_errors():
    lead, err = _extract_lead({"name": "No Phone"})
    assert lead is None and "no phone field" in err
    lead, err = _extract_lead({"phone": "123"})
    assert lead is None and "unusable phone" in err
    lead, err = _extract_lead(["not", "a", "dict"])
    assert lead is None and err


def test_leads_from_body_shapes():
    one = {"phone": "9000000001"}
    assert _leads_from_body(one) == [one]                      # single object
    assert _leads_from_body({"leads": [one, one]}) == [one, one]
    assert _leads_from_body({"DATA": [one]}) == [one]          # case-folded list key
    assert _leads_from_body([one]) == [one]                    # bare array
    assert _leads_from_body("junk") == []
    assert _leads_from_body(None) == []


# ── outcome vocabulary + event mapping ───────────────────────────────────────
def test_outcome_status_mapping():
    assert whs.outcome_status("completed", True, {}) == "qualified"
    assert whs.outcome_status("completed", False, {}) == "not_qualified"
    assert whs.outcome_status("no_answer", False, {}) == "unreachable"
    assert whs.outcome_status("failed", False, {}) == "unreachable"
    assert whs.outcome_status("dnd_dropped", False, {}) == "dnd_blocked"
    assert whs.outcome_status("pending", False, {}) == "queued"
    assert whs.outcome_status("ringing", False, {}) == "calling"


def test_event_for_terminal_only():
    assert whs.event_for("qualified") == "lead.completed"
    assert whs.event_for("not_qualified") == "lead.completed"
    assert whs.event_for("unreachable") == "lead.unreachable"
    assert whs.event_for("dnd_blocked") == "lead.dnd_blocked"
    assert whs.event_for("queued") is None
    assert whs.event_for("calling") is None


# ── payload shape ────────────────────────────────────────────────────────────
def _fake_campaign(**cfg):
    return SimpleNamespace(id="11111111-1111-1111-1111-111111111111",
                           name="Diwali", agent_config=cfg)


def _fake_contact(**over):
    base = dict(
        external_id="z-1", phone="917569672503", name="Asha",
        status="completed", qualified=True, lead_score=3, attempt=2,
        duration_s=93, updated_at=None, answered_at=None,
        result={"max_score": 4, "score_breakdown": [{"question": "Budget?", "answer": "1Cr", "points": 1}],
                "call_note": "Actively looking.", "callback_requested": False},
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_build_payload_qualified():
    # Valid canonical questionnaire (blank-text questions are DROPPED by the
    # normalizer, which would zero the threshold).
    campaign = _fake_campaign(questionnaire={
        "threshold": 2,
        "questions": [
            {"type": "intent", "text": "Budget above 80L?"},
            {"type": "intent", "text": "Looking to move in 3 months?", "points": 3},
        ],
    })
    p = whs.build_payload(campaign, _fake_contact(), "lead.completed")
    assert p["event"] == "lead.completed"
    assert p["lead"] == {"external_id": "z-1", "phone": "917569672503", "name": "Asha"}
    o = p["outcome"]
    assert o["status"] == "qualified" and o["lead_score"] == 3 and o["max_score"] == 4
    assert o["threshold"] == 2
    assert o["answers"][0]["question"] == "Budget?"
    assert o["call_note"] == "Actively looking."
    assert o["attempts"] == 2 and o["duration_s"] == 93.0


def test_build_payload_unreachable_minimal():
    p = whs.build_payload(
        _fake_campaign(),
        _fake_contact(status="no_answer", qualified=False, lead_score=None,
                      result={}, duration_s=None),
        "lead.unreachable",
    )
    o = p["outcome"]
    assert o["status"] == "unreachable"
    assert o["lead_score"] is None and o["answers"] is None and o["duration_s"] is None


def test_build_test_payload_is_marked():
    p = whs.build_test_payload(_fake_campaign())
    assert p["test"] is True and p["event"] == "lead.completed"
    assert p["outcome"]["status"] == "qualified"


# ── HMAC signature ───────────────────────────────────────────────────────────
def test_sign_matches_documented_scheme():
    secret, body, ts = "whsec_abc", b'{"event":"lead.completed"}', 1752800000
    header = whs.sign(secret, body, ts)
    assert header.startswith(f"t={ts},v1=")
    v1 = header.split("v1=")[1]
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    assert v1 == expected
    # A customer's verifier recomputing over f"{t}.{raw_body}" must agree —
    # and any body mutation must break it.
    tampered = hmac.new(secret.encode(), f"{ts}.".encode() + body + b" ", hashlib.sha256).hexdigest()
    assert v1 != tampered


def test_backoff_schedule_then_dead():
    # 5 retries over ~7.6h, then dead — the drainer indexes attempt-1.
    assert whs.BACKOFF_SCHEDULE == [60, 300, 900, 3600, 21600]
