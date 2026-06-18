"""Razorpay payments service — monthly subscriptions for payment-gated onboarding.

REST is called directly via httpx (no SDK dependency, easily mockable), HTTP
basic auth with (KEY_ID, KEY_SECRET) — same shape as ``plivo_service``. Two
plans: Inbound Only (₹4499/mo) and Inbound + Outbound (₹6499/mo). The plan
choice drives ``Organization.calling_enabled``.

Checkout flow (Razorpay Standard Checkout, subscription mode): we create a
Subscription server-side, the frontend opens checkout.js with the
``subscription_id``; on success Razorpay returns ``razorpay_payment_id`` +
``razorpay_subscription_id`` + ``razorpay_signature`` where the signature is
``HMAC_SHA256(payment_id + "|" + subscription_id, KEY_SECRET)`` (note: the
operand order differs from the one-time-order flow).
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx

from app.core.config import settings


class RazorpayError(RuntimeError):
    """Raised on a Razorpay API failure or misconfiguration."""


# Plan catalog. amount_paise is the monthly price; ``outbound`` gates calling.
PLAN_CATALOG: dict[str, dict[str, Any]] = {
    "inbound_only": {"amount_paise": 449900, "label": "Inbound Only", "outbound": False},
    "inbound_outbound": {"amount_paise": 649900, "label": "Inbound + Outbound", "outbound": True},
}

# Per-process cache of lazily-created Razorpay plan ids, keyed by plan key.
_PLAN_CACHE: dict[str, str] = {}


class RazorpayService:
    # ── low-level REST ────────────────────────────────────────────────────────
    @staticmethod
    def _auth() -> tuple[str, str]:
        if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
            raise RazorpayError("RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are required.")
        return settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET

    @staticmethod
    async def _request(method: str, path: str, *, json_body: dict | None = None) -> dict[str, Any]:
        url = f"{settings.RAZORPAY_API_BASE.rstrip('/')}/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=30.0, auth=RazorpayService._auth()) as client:
            resp = await client.request(method, url, json=json_body)
        if resp.status_code >= 400:
            raise RazorpayError(f"Razorpay {method} {path} failed ({resp.status_code}): {resp.text[:300]}")
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}

    # ── plans + subscriptions ─────────────────────────────────────────────────
    @staticmethod
    async def ensure_plan(plan_key: str) -> str:
        """Return a Razorpay plan_id for ``plan_key``. Prefers a pinned id from
        settings; else lazily creates a monthly plan and caches it per-process."""
        spec = PLAN_CATALOG.get(plan_key)
        if spec is None:
            raise RazorpayError(f"Unknown plan: {plan_key}")
        pinned = (
            settings.RAZORPAY_PLAN_INBOUND_ONLY if plan_key == "inbound_only"
            else settings.RAZORPAY_PLAN_INBOUND_OUTBOUND
        )
        if pinned:
            return pinned
        if plan_key in _PLAN_CACHE:
            return _PLAN_CACHE[plan_key]
        created = await RazorpayService._request(
            "POST",
            "plans",
            json_body={
                "period": "monthly",
                "interval": 1,
                "item": {
                    "name": f"Nokvo One — {spec['label']}",
                    "amount": spec["amount_paise"],
                    "currency": "INR",
                },
            },
        )
        plan_id = str(created.get("id") or "")
        if not plan_id:
            raise RazorpayError(f"Razorpay plan creation returned no id: {created}")
        _PLAN_CACHE[plan_key] = plan_id
        return plan_id

    @staticmethod
    async def create_subscription(plan_id: str, *, total_count: int = 120, notes: dict | None = None) -> dict[str, Any]:
        """Create a subscription. ``total_count`` = max billing cycles (months)."""
        return await RazorpayService._request(
            "POST",
            "subscriptions",
            json_body={
                "plan_id": plan_id,
                "total_count": total_count,
                "customer_notify": 1,
                "notes": notes or {},
            },
        )

    @staticmethod
    async def fetch_subscription(subscription_id: str) -> dict[str, Any]:
        return await RazorpayService._request("GET", f"subscriptions/{subscription_id}")

    # ── signature verification ────────────────────────────────────────────────
    @staticmethod
    def verify_checkout_signature(payment_id: str, subscription_id: str, signature: str) -> bool:
        """Razorpay subscription checkout signature: HMAC_SHA256(payment_id |
        subscription_id, KEY_SECRET). Constant-time compare."""
        if not (payment_id and subscription_id and signature and settings.RAZORPAY_KEY_SECRET):
            return False
        expected = hmac.new(
            settings.RAZORPAY_KEY_SECRET.encode("utf-8"),
            f"{payment_id}|{subscription_id}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    @staticmethod
    def verify_webhook_signature(raw_body: bytes, signature: str | None) -> bool:
        """Razorpay webhook signature: HMAC_SHA256(raw_body, WEBHOOK_SECRET)."""
        if not signature or not settings.RAZORPAY_WEBHOOK_SECRET:
            return False
        expected = hmac.new(
            settings.RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
