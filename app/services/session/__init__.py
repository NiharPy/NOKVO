"""Session-scoped state for the voice/chat agent, split by concern.

Four explicit boundaries:

* :mod:`.confirmation` — what the caller has explicitly confirmed
  (name/phone/email/proposed-slot handshakes). Owns the
  ``data["confirmations"]`` and ``data["proposed_slot_accepted"]`` keys.

* :mod:`.audit`        — chronological event log of policy actions during
  the conversation. Owns ``data["audit_trail"]``.

* :mod:`.retry`        — re-export of :mod:`tool_retry_service` so all
  session-scoped state lives behind one namespace.

* :mod:`.outcome`      — terminal outcome of the record (was it confirmed?
  no-show? cancelled?). Owns ``data["outcome"]``.

Each module owns a disjoint set of keys on ``NokvoOneToolRecord.data`` —
no module reads or writes another's keys.

For backward compat, :mod:`app.services.flow_session` re-exports the same
symbols from these modules, so existing call sites keep working.
"""

from app.services.session import audit, confirmation, outcome, retry  # noqa: F401
