"""Canonical Nokvo One agent specification.

A single source of truth for *what the agent is* across every surface that
talks to a caller — inbound voice (WebSocket → Sarvam STT/TTS), outbound
voice campaigns (Exotel/Twilio bridges), and text chat (the ``text_query``
event on the same WebSocket). All three surfaces import from this module
instead of hard-coding policy decisions in their own files.

What lives here:

* :data:`AGENT_VERSION` — the spec version. Bump when a behavioural change
  ships so downstream caches / clients can invalidate.
* :class:`ConfirmationPolicy` — which slot kinds need a read-back handshake
  and when to skip (confidence shortcuts).
* :class:`RetryPolicy` — inline retry count + backoff schedule for the
  pending-tool-retries queue.
* :class:`IdentityPolicy` — which intents require a verified caller before
  any side-effecting action runs.
* :class:`OutcomeStates` — the closed set of terminal record outcomes.
* :class:`AgentCapabilities` — what intents the agent claims it can handle.
  Used by the chat surface to advertise capabilities and by tests to make
  sure no surface silently regresses behaviour the others rely on.

What does NOT live here:

* Per-tenant configuration (business templates, custom tabs, scheduler
  windows). Those stay in :mod:`nokvo_one_business_templates` / member
  settings — the spec is the *contract*, the tenant config is the *data*.
* Prompts / TTS / STT settings — those are infrastructure, separate concern.
* Tool implementations — those live in :mod:`predefined_tools_service`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final


AGENT_VERSION: Final[str] = "2026.05.21"


# ── Confirmation policy ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class ConfirmationPolicy:
    """Which slot kinds require a "yes, that's right" handshake before we
    persist them to a record, and when we're allowed to skip."""

    # Kinds always confirmed (name, phone, email, IDs — STT-prone or
    # high-stakes for downstream systems).
    always_confirm_kinds: frozenset[str] = frozenset({"name", "email", "reference_number", "order_id"})

    # Phones are confirmed UNLESS they match a canonical pattern with no
    # obvious STT artefacts (see :func:`voice_turn_policy._is_high_confidence_phone`).
    confirm_phone_unless_high_confidence: bool = True

    # Slots the agent OFFERS (proposed scheduling slots) always need an
    # explicit "yes" before they snap into the record.
    require_explicit_yes_for_proposed_slots: bool = True


CONFIRMATION_POLICY: Final[ConfirmationPolicy] = ConfirmationPolicy()


def needs_confirmation(kind: str) -> bool:
    """True when a slot of ``kind`` requires a read-back handshake."""
    if kind in CONFIRMATION_POLICY.always_confirm_kinds:
        return True
    if kind == "phone" and CONFIRMATION_POLICY.confirm_phone_unless_high_confidence:
        # The high-confidence skip is decided per-value at the call site —
        # the policy itself says "always ask unless the value waives it".
        return True
    return False


# ── Retry policy ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RetryPolicy:
    """Tool-execution retry rules. The voice pipeline does one inline retry
    before falling back to "team will call you back"; persistent failures
    land in pending_tool_retries and the worker reads ``max_attempts`` +
    ``backoff_minutes`` from here."""

    inline_retries: int = 1
    inline_delay_seconds: float = 0.4
    max_attempts: int = 5
    backoff_minutes: tuple[int, ...] = (2, 5, 15, 60, 240)


RETRY_POLICY: Final[RetryPolicy] = RetryPolicy()


# ── Identity policy ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class IdentityPolicy:
    """Which intents require the caller to be identity-verified (phone match
    against an existing record) before the agent executes anything."""

    sensitive_intents: frozenset[str] = frozenset({"cancellation_request", "refund_eligibility"})

    # If the caller's phone is captured by Caller-ID (telephony provider's
    # ``From`` header), we treat them as already-verified for read-only
    # context — they still need to *speak* the booking number for writes.
    trust_caller_id_for_context: bool = True


IDENTITY_POLICY: Final[IdentityPolicy] = IdentityPolicy()


def requires_identity_verification(intent: str) -> bool:
    return intent in IDENTITY_POLICY.sensitive_intents


# ── Outcome states ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class OutcomeStates:
    """The closed set of terminal record outcomes the outcome-tracker
    accepts. Anything outside this set is rejected at the API boundary."""

    pending: str = "pending"
    confirmed: str = "confirmed"
    no_show: str = "no_show"
    cancelled: str = "cancelled"
    failed_followup: str = "failed_followup"
    completed: str = "completed"

    def all_values(self) -> frozenset[str]:
        return frozenset(
            {self.pending, self.confirmed, self.no_show, self.cancelled, self.failed_followup, self.completed}
        )


OUTCOME_STATES: Final[OutcomeStates] = OutcomeStates()


# ── Capabilities ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class AgentCapabilities:
    """What the agent claims it can do, surface-agnostically. Chat clients
    use this to render UI affordances; tests use it to catch silent
    regressions where one surface drops a capability another relies on."""

    handled_intents: frozenset[str] = frozenset(
        {
            "appointment_flow",
            "tool_flow",
            "availability_check",
            "language_switch",
            "urgent_symptom",
            "cancellation_request",
            "refund_eligibility",
            "kb_lookup",
        }
    )

    supported_languages: tuple[str, ...] = ("en", "hi", "te", "ta", "bn", "kn", "ml", "mr", "gu", "pa")

    surfaces: tuple[str, ...] = ("voice_inbound", "voice_outbound", "chat")

    deterministic_flows: tuple[str, ...] = (
        "clinic_appointment",
        "real_estate_site_visit",
        "leads_create",
    )

    cross_call_memory: bool = True
    org_wide_blocked_slots: bool = True
    auto_retry_queue: bool = True
    outcome_feedback_loop: bool = True


CAPABILITIES: Final[AgentCapabilities] = AgentCapabilities()


# ── Spec snapshot ───────────────────────────────────────────────────────────
def spec_snapshot() -> dict:
    """Serialisable summary of the spec. Used by the ``/agent/spec``
    health endpoint and by clients that want to verify they're talking to
    the version of the agent they were built against."""
    return {
        "agent_version": AGENT_VERSION,
        "confirmation": {
            "always_confirm_kinds": sorted(CONFIRMATION_POLICY.always_confirm_kinds),
            "confirm_phone_unless_high_confidence": CONFIRMATION_POLICY.confirm_phone_unless_high_confidence,
            "require_explicit_yes_for_proposed_slots": CONFIRMATION_POLICY.require_explicit_yes_for_proposed_slots,
        },
        "retry": {
            "inline_retries": RETRY_POLICY.inline_retries,
            "inline_delay_seconds": RETRY_POLICY.inline_delay_seconds,
            "max_attempts": RETRY_POLICY.max_attempts,
            "backoff_minutes": list(RETRY_POLICY.backoff_minutes),
        },
        "identity": {
            "sensitive_intents": sorted(IDENTITY_POLICY.sensitive_intents),
            "trust_caller_id_for_context": IDENTITY_POLICY.trust_caller_id_for_context,
        },
        "outcomes": sorted(OUTCOME_STATES.all_values()),
        "capabilities": {
            "handled_intents": sorted(CAPABILITIES.handled_intents),
            "supported_languages": list(CAPABILITIES.supported_languages),
            "surfaces": list(CAPABILITIES.surfaces),
            "deterministic_flows": list(CAPABILITIES.deterministic_flows),
            "cross_call_memory": CAPABILITIES.cross_call_memory,
            "org_wide_blocked_slots": CAPABILITIES.org_wide_blocked_slots,
            "auto_retry_queue": CAPABILITIES.auto_retry_queue,
            "outcome_feedback_loop": CAPABILITIES.outcome_feedback_loop,
        },
    }
