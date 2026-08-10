"""P5 — Backend Lookup security core + orchestration."""
from __future__ import annotations

import asyncio

from app.services.backend_lookup import (
    BackendLookupSpec,
    is_safe_lookup_url,
    map_response,
    perform_lookup,
    redact,
    render_request,
    render_template,
    specs_from_provider_status,
)


def _run(coro):
    return asyncio.run(coro)


# ── SSRF guard (IP-literal URLs → no DNS needed) ──

def test_ssrf_blocks_private_loopback_metadata_and_non_https():
    assert is_safe_lookup_url("https://8.8.8.8/x") is True            # public
    assert is_safe_lookup_url("https://10.0.0.1/x") is False          # private
    assert is_safe_lookup_url("https://127.0.0.1/x") is False         # loopback
    assert is_safe_lookup_url("https://169.254.169.254/latest") is False  # link-local metadata
    assert is_safe_lookup_url("http://8.8.8.8/x") is False            # not https
    assert is_safe_lookup_url("https://metadata.google.internal/x") is False
    assert is_safe_lookup_url("not a url") is False


def test_ssrf_allowlist():
    assert is_safe_lookup_url("https://8.8.8.8/x", allowed_hosts=["8.8.8.8"]) is True
    assert is_safe_lookup_url("https://8.8.8.8/x", allowed_hosts=["api.shop.com"]) is False


# ── templating / mapping / redaction ──

def test_render_map_redact():
    slots = {"order_id": "A123"}
    assert render_template("id={{ slots.order_id }}", slots) == "id=A123"
    assert render_request({"name": "{{ slots.order_id }}"}, slots) == {"name": "A123"}
    obj = {"orders": [{"fulfillment_status": "shipped", "eta": "Tue", "email": "a@b.c"}]}
    mapped = map_response(obj, {
        "status": "orders[0].fulfillment_status",
        "eta": "orders[0].eta",
        "email": "orders[0].email",
    })
    assert mapped == {"status": "shipped", "eta": "Tue", "email": "a@b.c"}
    assert redact(mapped, ["email"]) == {"status": "shipped", "eta": "Tue"}
    assert map_response(obj, {"missing": "orders[5].x"}) == {"missing": None}


# ── orchestration ──

def test_identity_gate_blocks_when_unverified():
    spec = BackendLookupSpec(key="order_status", url="https://8.8.8.8/o", identity_gate=["phone_last4"])

    async def fetch(*a, **k):
        raise AssertionError("must not fetch when identity unverified")

    res = _run(perform_lookup(spec, {}, identity_verified=False, fetch=fetch))
    assert res["ok"] is False and res["reason"] == "identity_unverified"


def test_unsafe_url_blocked_before_fetch():
    spec = BackendLookupSpec(key="x", url="https://10.0.0.1/o")

    async def fetch(*a, **k):
        raise AssertionError("must not fetch an unsafe URL")

    res = _run(perform_lookup(spec, {}, identity_verified=True, fetch=fetch))
    assert res["ok"] is False and res["reason"] == "unsafe_url"


def test_successful_lookup_maps_redacts_speaks_and_auths():
    spec = BackendLookupSpec(
        key="order_status", url="https://8.8.8.8/orders",
        request_template={"name": "{{ slots.order_id }}"},
        response_map={
            "status": "orders[0].fulfillment_status",
            "eta": "orders[0].eta",
            "address": "orders[0].address",
        },
        pii_fields=["address"],
        speak_template="Your order is {{ slots.status }}, arriving {{ slots.eta }}.",
        auth_secret_ref="kv://tenant/1/token",
    )
    captured: dict = {}

    async def fetch(url, method, headers, params):
        captured.update(url=url, method=method, headers=headers, params=params)
        return {"orders": [{"fulfillment_status": "shipped", "eta": "Tuesday", "address": "12 Main St"}]}

    async def secret_resolver(ref):
        return "SECRET"

    res = _run(perform_lookup(spec, {"order_id": "A123"}, identity_verified=True,
                              fetch=fetch, secret_resolver=secret_resolver))
    assert res["ok"] is True
    assert res["fields"] == {"status": "shipped", "eta": "Tuesday"}  # address redacted (PII)
    assert res["spoken"] == "Your order is shipped, arriving Tuesday."
    assert captured["params"] == {"name": "A123"}
    assert captured["headers"]["Authorization"] == "Bearer SECRET"


def test_fetch_failure_is_graceful():
    spec = BackendLookupSpec(key="x", url="https://8.8.8.8/o")

    async def fetch(*a, **k):
        raise RuntimeError("boom")

    res = _run(perform_lookup(spec, {}, identity_verified=True, fetch=fetch))
    assert res["ok"] is False and res["reason"] == "fetch_failed"


# ── config loading ──

def test_specs_from_provider_status_parses_and_skips_malformed():
    ps = {"backend_lookups": [
        {"key": "order_status", "url": "https://api.shop.com/orders", "response_map": {"s": "a.b"}},
        {"url": "https://x/y"},   # no key → skipped
        {"key": "nourl"},         # no url → skipped
        "not a dict",             # skipped
        {"key": "acct", "url": "https://api.shop.com/acct", "method": "post", "identity_gate": ["last4"]},
    ]}
    specs = specs_from_provider_status(ps)
    assert [s.key for s in specs] == ["order_status", "acct"]
    assert specs[1].method == "POST"        # normalized to upper
    assert specs[1].identity_gate == ["last4"]
    assert specs[0].cache_ttl_s == 60       # default applied


def test_specs_from_provider_status_empty_or_bad():
    assert specs_from_provider_status(None) == []
    assert specs_from_provider_status({}) == []
    assert specs_from_provider_status({"backend_lookups": "nope"}) == []


# ── tool registration ──

def test_lookup_tools_from_specs_builds_agent_tools():
    from app.services.backend_lookup import lookup_tools_from_specs

    specs = [BackendLookupSpec(
        key="order_status", url="https://api.shop.com/o",
        request_template={"name": "{{ slots.order_id }}"},
        identity_gate=["phone_last4"],
    )]
    tools = lookup_tools_from_specs(specs)
    assert len(tools) == 1
    t = tools[0]
    assert t.key == "lookup_order_status"
    assert t.handler_name == "backend_lookup"
    assert t.requires_confirmation is False
    # inputs = the {{ slots.X }} vars referenced by templates + the identity gate
    assert set(t.input_schema["properties"].keys()) == {"order_id", "phone_last4"}
    assert t.input_schema["required"] == ["phone_last4"]
    assert t.metadata["lookup_key"] == "order_status"


def test_resolve_catalog_registers_lookup_tools_only_when_configured():
    from app.services.dynamic_tool_resolver import resolve_index

    specs = [BackendLookupSpec(key="order_status", url="https://api.shop.com/o", identity_gate=["last4"])]
    assert "lookup_order_status" in resolve_index("ecommerce", None, None, backend_lookup_specs=specs)
    assert "lookup_order_status" not in resolve_index("ecommerce", None, None)
