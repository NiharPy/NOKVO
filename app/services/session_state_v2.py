"""Unified per-call session state — single source of truth.

Replaces the two parallel Redis silos:

  * ``AgentSessionStore`` writes to ``{ns}:agent:call:{call_id}:state``
    (top-level dict with ``tool_flow``, ``appointment``,
    ``outbound_memory``, plus a co-located ``state["memory"]`` sub-dict
    that holds the :class:`ConversationalMemory` blob) AND
    ``{ns}:agent:call:{call_id}:history`` (separate key for the
    conversation log).
  * :class:`ConversationalMemory` owns ``facts``, ``objections``,
    ``commitments``, ``preferences``, ``salient_notes``,
    ``bootstrap_keys``, ``prior_stage``, ``asked_questions``.

This module collapses both into a single typed :class:`SessionState`
stored under one key ``{ns}:agent:call:{call_id}:v2``. The old classes
keep their public API as thin facades that delegate to this module.

## Atomic mutation

Lost-update race avoidance is the central reason this exists. All
read-modify-write sequences go through :func:`mutate_state`, which
acquires a per-call :class:`asyncio.Lock` held in a module-level
:class:`weakref.WeakValueDictionary`. Distinct call_ids don't
serialise (different lock instances). Single-worker uvicorn pins a
WS to one process, so an in-process lock is sufficient. For
multi-worker deploys the lock graduates to a Redis distributed lock
via WATCH/MULTI-EXEC — the boundary is :func:`mutate_state` and the
change is contained.

## Backwards compatibility during rollout

Reader prefers ``:v2``; on miss, reconstructs from legacy
``(state, history)`` keys via :func:`from_legacy_blobs`, writes
``:v2`` back, and (during dual-write window) ALSO keeps writing the
legacy keys so a rollback can pick up where it left off. The toggle
is :data:`settings.SESSION_STATE_V2_DUAL_WRITE` (default True for one
deploy cycle, then off).

## Field naming

The historic two-store split produced two conventions for "which turn
did this signal land on":

  * :class:`MemoryFact.source_turn` (integer, ``-1`` = bootstrap)
  * bucket entries: ``turn`` (integer)

Both are preserved on disk. :class:`MemoryFact` gains a ``turn``
property that aliases ``source_turn`` for read consistency. The
rename to ``turn`` everywhere is a mechanical follow-up.

Similarly, prior-call provenance was two ways:

  * facts: marked by membership in ``ConversationalMemory.bootstrap_keys``
  * bucket entries: ``from_prior_call: bool`` flag

:class:`MemoryFact` gains a ``from_prior_call: bool`` field.
``bootstrap_keys`` becomes a computed property derived from the facts'
flags. The set is no longer serialised; legacy blobs are migrated by
restamping the matching facts on load.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)


# ─── Lock registry ────────────────────────────────────────────────────────────


# Per-call asyncio.Lock instances. WeakValueDictionary lets a finished call's
# lock get garbage-collected once nothing holds it — no manual cleanup needed.
_CALL_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
_LOCK_REGISTRY_LOCK = asyncio.Lock()


async def _lock_for(call_id: str) -> asyncio.Lock:
    """Return the (per-call) lock, creating it if needed.

    Two-step lookup so creating a fresh lock for an unseen call_id is
    itself serialised — otherwise concurrent first-mutations on the
    same call could each get a different lock instance.
    """
    existing = _CALL_LOCKS.get(call_id)
    if existing is not None:
        return existing
    async with _LOCK_REGISTRY_LOCK:
        existing = _CALL_LOCKS.get(call_id)
        if existing is not None:
            return existing
        lock = asyncio.Lock()
        _CALL_LOCKS[call_id] = lock
        return lock


# ─── Typed slices ─────────────────────────────────────────────────────────────


@dataclass
class BucketEntry:
    """One entry in the objections / commitments bucket.

    Mirrors the dict shape the extractor has used historically — every
    field except ``code`` defaults to a "missing / not set" value so
    construction from legacy JSON dicts is straightforward.
    """

    code: str
    text: str = ""
    turn: int = -1
    from_prior_call: bool = False
    resolved_at_turn: int | None = None
    ts: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "turn": self.turn}
        if self.text:
            out["text"] = self.text
        if self.from_prior_call:
            out["from_prior_call"] = True
        if self.resolved_at_turn is not None:
            out["resolved_at_turn"] = self.resolved_at_turn
        if self.ts is not None:
            out["ts"] = self.ts
        for k, v in self.extra.items():
            out.setdefault(k, v)
        return out

    @classmethod
    def from_any(cls, payload: Any) -> "BucketEntry | None":
        if not isinstance(payload, dict):
            return None
        known = {"code", "text", "turn", "from_prior_call", "resolved_at_turn", "ts"}
        extra = {k: v for k, v in payload.items() if k not in known}
        return cls(
            code=str(payload.get("code") or ""),
            text=str(payload.get("text") or ""),
            turn=int(payload.get("turn")) if isinstance(payload.get("turn"), int) else -1,
            from_prior_call=bool(payload.get("from_prior_call")),
            resolved_at_turn=(
                int(payload["resolved_at_turn"])
                if isinstance(payload.get("resolved_at_turn"), int)
                else None
            ),
            ts=(float(payload["ts"]) if isinstance(payload.get("ts"), (int, float)) else None),
            extra=extra,
        )


@dataclass
class HistoryTurn:
    """One conversation turn.

    ``turn_index`` is the monotonic counter that replaces the racey
    ``len(get_history)`` derivation — see :attr:`SessionState.next_turn_index`.
    """

    role: str
    content: str
    turn_index: int

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "content": self.content, "turn_index": self.turn_index}

    @classmethod
    def from_any(cls, payload: Any, fallback_index: int = -1) -> "HistoryTurn | None":
        if not isinstance(payload, dict):
            return None
        role = str(payload.get("role") or "").lower()
        if role not in {"user", "assistant"}:
            return None
        content = str(payload.get("content") or "")
        idx = payload.get("turn_index")
        if not isinstance(idx, int):
            idx = fallback_index
        return cls(role=role, content=content, turn_index=idx)


@dataclass
class ToolFlowState:
    """Slot-fill FSM state.

    Mirrors the dict-of-loose-fields shape that lives in
    ``state["tool_flow"]`` today. Every field is optional so a
    freshly-constructed instance round-trips to ``{"active": false}``.
    """

    active: bool = False
    flow_key: str | None = None
    tool_key: str | None = None
    collected: dict[str, Any] = field(default_factory=dict)
    pending_slot: str | None = None
    completed: bool = False
    deferred_for_kb: bool = False
    awaiting_id_confirmation: bool = False
    id_confirmation_slot: str | None = None
    id_confirmation_kind: str | None = None
    awaiting_name_confirmation: bool = False
    name_confirmation_slot: str | None = None
    awaiting_slot_confirm: bool = False
    awaiting_past_time_shift: bool = False
    proposed_slot_utc: str | None = None
    proposed_slot_label: str | None = None
    proposed_date: str | None = None
    original_time: str | None = None
    past_shift_count: int = 0
    offered_disambiguation: bool = False
    created_record_id: str | None = None
    assignment_status: str | None = None
    assigned_member_name: str | None = None
    confirmations: dict[str, Any] = field(default_factory=dict)
    audit_trail: list[dict[str, Any]] = field(default_factory=list)
    needs_callback: bool = False
    tool_error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        # Round-trip via the explicit field list so unknown extras are
        # preserved without polluting the typed surface.
        out: dict[str, Any] = {
            "active": self.active,
            "flow_key": self.flow_key,
            "tool_key": self.tool_key,
            "collected": dict(self.collected),
            "pending_slot": self.pending_slot,
            "completed": self.completed,
            "deferred_for_kb": self.deferred_for_kb,
            "awaiting_id_confirmation": self.awaiting_id_confirmation,
            "id_confirmation_slot": self.id_confirmation_slot,
            "id_confirmation_kind": self.id_confirmation_kind,
            "awaiting_name_confirmation": self.awaiting_name_confirmation,
            "name_confirmation_slot": self.name_confirmation_slot,
            "awaiting_slot_confirm": self.awaiting_slot_confirm,
            "awaiting_past_time_shift": self.awaiting_past_time_shift,
            "proposed_slot_utc": self.proposed_slot_utc,
            "proposed_slot_label": self.proposed_slot_label,
            "proposed_date": self.proposed_date,
            "original_time": self.original_time,
            "past_shift_count": self.past_shift_count,
            "offered_disambiguation": self.offered_disambiguation,
            "created_record_id": self.created_record_id,
            "assignment_status": self.assignment_status,
            "assigned_member_name": self.assigned_member_name,
            "confirmations": dict(self.confirmations),
            "audit_trail": list(self.audit_trail),
            "needs_callback": self.needs_callback,
            "tool_error": self.tool_error,
        }
        # Drop None / empty defaults so the JSON stays compact.
        out = {k: v for k, v in out.items() if v not in (None, False, 0, "", [], {})}
        out.setdefault("active", False)
        for k, v in (self.extra or {}).items():
            out.setdefault(k, v)
        return out

    @classmethod
    def from_dict(cls, payload: Any) -> "ToolFlowState":
        if not isinstance(payload, dict):
            return cls()
        known = {f.name for f in cls.__dataclass_fields__.values()} - {"extra"}
        extra = {k: v for k, v in payload.items() if k not in known}
        return cls(
            active=bool(payload.get("active")),
            flow_key=payload.get("flow_key"),
            tool_key=payload.get("tool_key"),
            collected=dict(payload.get("collected") or {}),
            pending_slot=payload.get("pending_slot"),
            completed=bool(payload.get("completed")),
            deferred_for_kb=bool(payload.get("deferred_for_kb")),
            awaiting_id_confirmation=bool(payload.get("awaiting_id_confirmation")),
            id_confirmation_slot=payload.get("id_confirmation_slot"),
            id_confirmation_kind=payload.get("id_confirmation_kind"),
            awaiting_name_confirmation=bool(payload.get("awaiting_name_confirmation")),
            name_confirmation_slot=payload.get("name_confirmation_slot"),
            awaiting_slot_confirm=bool(payload.get("awaiting_slot_confirm")),
            awaiting_past_time_shift=bool(payload.get("awaiting_past_time_shift")),
            proposed_slot_utc=payload.get("proposed_slot_utc"),
            proposed_slot_label=payload.get("proposed_slot_label"),
            proposed_date=payload.get("proposed_date"),
            original_time=payload.get("original_time"),
            past_shift_count=int(payload.get("past_shift_count") or 0),
            offered_disambiguation=bool(payload.get("offered_disambiguation")),
            created_record_id=payload.get("created_record_id"),
            assignment_status=payload.get("assignment_status"),
            assigned_member_name=payload.get("assigned_member_name"),
            confirmations=dict(payload.get("confirmations") or {}),
            audit_trail=list(payload.get("audit_trail") or []),
            needs_callback=bool(payload.get("needs_callback")),
            tool_error=payload.get("tool_error"),
            extra=extra,
        )


# ─── The unified state ───────────────────────────────────────────────────────


SCHEMA_VERSION = 2


@dataclass
class SessionState:
    """One per-call state blob — replaces ``state`` + ``history`` +
    ``state["memory"]`` with a single typed object."""

    schema_version: int = SCHEMA_VERSION

    # identity (loosely set; helpful for telemetry / debug dumps)
    call_id: str = ""

    # session metadata (formerly top-level Redis state keys)
    language: str | None = None
    status: str | None = None
    call_surface: str | None = None
    campaign_id: str | None = None
    agent_mode: str | None = None
    agent_mode_final: str | None = None
    identity_verified: bool = False
    identity_verification_pending: bool = False

    # markers: auto_lead_created, auto_lead_id, auto_site_visit_*, etc. Stored
    # as a free-form dict because the call-end safety net writes a half-dozen
    # mostly-disjoint flags and pinning each on the dataclass would balloon
    # the surface area for almost no read-time benefit.
    markers: dict[str, Any] = field(default_factory=dict)

    # canonical turn counter — replaces ``len(get_history)`` derivations.
    next_turn_index: int = 0

    # facts + buckets (ConversationalMemory's data, now top-level)
    facts: dict[str, Any] = field(default_factory=dict)  # MemoryFact stored as dict; class import is circular
    objections: list[BucketEntry] = field(default_factory=list)
    commitments: list[BucketEntry] = field(default_factory=list)
    preferences: list[dict[str, Any]] = field(default_factory=list)
    salient_notes: list[dict[str, Any]] = field(default_factory=list)
    asked_questions: list[dict[str, Any]] = field(default_factory=list)
    prior_stage: str | None = None
    journey_stage: str | None = None

    # FSM slices
    tool_flow: ToolFlowState = field(default_factory=ToolFlowState)
    appointment: dict[str, Any] = field(default_factory=dict)
    outbound_memory: dict[str, Any] = field(default_factory=dict)
    campaign_objectives_covered: list[str] = field(default_factory=list)
    proactive_silence_nudges: int = 0

    # conversation log (formerly the separate :history key)
    history: list[HistoryTurn] = field(default_factory=list)

    # ── serialisation ────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe canonical dict. Round-trips via :meth:`from_dict`."""
        return {
            "schema_version": self.schema_version,
            "call_id": self.call_id,
            "language": self.language,
            "status": self.status,
            "call_surface": self.call_surface,
            "campaign_id": self.campaign_id,
            "agent_mode": self.agent_mode,
            "agent_mode_final": self.agent_mode_final,
            "identity_verified": self.identity_verified,
            "identity_verification_pending": self.identity_verification_pending,
            "markers": dict(self.markers),
            "next_turn_index": self.next_turn_index,
            "facts": dict(self.facts),
            "objections": [e.to_dict() for e in self.objections],
            "commitments": [e.to_dict() for e in self.commitments],
            "preferences": list(self.preferences),
            "salient_notes": list(self.salient_notes),
            "asked_questions": list(self.asked_questions),
            "prior_stage": self.prior_stage,
            "journey_stage": self.journey_stage,
            "tool_flow": self.tool_flow.to_dict(),
            "appointment": dict(self.appointment),
            "outbound_memory": dict(self.outbound_memory),
            "campaign_objectives_covered": list(self.campaign_objectives_covered),
            "proactive_silence_nudges": self.proactive_silence_nudges,
            "history": [t.to_dict() for t in self.history],
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "SessionState":
        if not isinstance(payload, dict):
            return cls()
        return cls(
            schema_version=int(payload.get("schema_version") or SCHEMA_VERSION),
            call_id=str(payload.get("call_id") or ""),
            language=payload.get("language"),
            status=payload.get("status"),
            call_surface=payload.get("call_surface"),
            campaign_id=payload.get("campaign_id"),
            agent_mode=payload.get("agent_mode"),
            agent_mode_final=payload.get("agent_mode_final"),
            identity_verified=bool(payload.get("identity_verified")),
            identity_verification_pending=bool(payload.get("identity_verification_pending")),
            markers=dict(payload.get("markers") or {}),
            next_turn_index=int(payload.get("next_turn_index") or 0),
            facts=dict(payload.get("facts") or {}),
            objections=_bucket_from_any(payload.get("objections")),
            commitments=_bucket_from_any(payload.get("commitments")),
            preferences=list(payload.get("preferences") or []),
            salient_notes=list(payload.get("salient_notes") or []),
            asked_questions=list(payload.get("asked_questions") or []),
            prior_stage=payload.get("prior_stage"),
            journey_stage=payload.get("journey_stage"),
            tool_flow=ToolFlowState.from_dict(payload.get("tool_flow")),
            appointment=dict(payload.get("appointment") or {}),
            outbound_memory=dict(payload.get("outbound_memory") or {}),
            campaign_objectives_covered=list(payload.get("campaign_objectives_covered") or []),
            proactive_silence_nudges=int(payload.get("proactive_silence_nudges") or 0),
            history=_history_from_any(payload.get("history")),
        )

    # ── legacy interop ───────────────────────────────────────────────

    def to_legacy_state_blob(self) -> dict[str, Any]:
        """Build the dict shape that ``AgentSessionStore.get_state`` has
        always returned. Keeps existing call sites compiling unchanged
        while the unified store is the source of truth underneath.
        """
        blob: dict[str, Any] = {}
        # Top-level scalar / list fields the pipeline reads directly.
        for key in (
            "language",
            "status",
            "call_surface",
            "campaign_id",
            "agent_mode",
            "agent_mode_final",
        ):
            value = getattr(self, key)
            if value is not None:
                blob[key] = value
        if self.identity_verified:
            blob["identity_verified"] = True
        if self.identity_verification_pending:
            blob["identity_verification_pending"] = True
        if self.proactive_silence_nudges:
            blob["proactive_silence_nudges"] = self.proactive_silence_nudges
        if self.campaign_objectives_covered:
            blob["campaign_objectives_covered"] = list(self.campaign_objectives_covered)
        if self.appointment:
            blob["appointment"] = dict(self.appointment)
        if self.outbound_memory:
            blob["outbound_memory"] = dict(self.outbound_memory)
        # The pipeline reads tool_flow as a dict at state["tool_flow"].
        tool_flow_dict = self.tool_flow.to_dict()
        if tool_flow_dict and tool_flow_dict != {"active": False}:
            blob["tool_flow"] = tool_flow_dict
        # Markers (auto_lead_*, etc.) live at the top level historically.
        for key, value in (self.markers or {}).items():
            blob.setdefault(key, value)
        # Memory blob — historically at state["memory"].
        memory_blob = self.to_legacy_memory_blob()
        if memory_blob:
            blob["memory"] = memory_blob
        return blob

    def to_legacy_memory_blob(self) -> dict[str, Any]:
        """The shape :meth:`ConversationalMemory.from_state_blob` expects."""
        bootstrap_keys: list[str] = [
            key
            for key, payload in self.facts.items()
            if isinstance(payload, dict) and payload.get("from_prior_call")
        ]
        return {
            "facts": {k: dict(v) for k, v in self.facts.items() if isinstance(v, dict)},
            "objections": [e.to_dict() for e in self.objections],
            "commitments": [e.to_dict() for e in self.commitments],
            "preferences": list(self.preferences),
            "salient_notes": list(self.salient_notes),
            "asked_questions": list(self.asked_questions),
            "bootstrap_keys": sorted(bootstrap_keys),
            "prior_stage": self.prior_stage,
        }

    def to_legacy_history(self) -> list[dict[str, str]]:
        """The shape :meth:`AgentSessionStore.get_history` has always returned."""
        out: list[dict[str, str]] = []
        for turn in self.history:
            out.append({"role": turn.role, "content": turn.content})
        return out


def _bucket_from_any(payload: Any) -> list[BucketEntry]:
    if not isinstance(payload, list):
        return []
    out: list[BucketEntry] = []
    for entry in payload:
        parsed = BucketEntry.from_any(entry)
        if parsed is not None:
            out.append(parsed)
    return out


def _history_from_any(payload: Any) -> list[HistoryTurn]:
    if not isinstance(payload, list):
        return []
    out: list[HistoryTurn] = []
    for idx, entry in enumerate(payload):
        parsed = HistoryTurn.from_any(entry, fallback_index=idx)
        if parsed is not None:
            out.append(parsed)
    return out


# ─── Legacy-blob migration ────────────────────────────────────────────────────


def from_legacy_blobs(
    state_blob: dict[str, Any] | None,
    history_list: Iterable[dict[str, Any]] | None,
) -> SessionState:
    """Reconstruct a :class:`SessionState` from the two legacy Redis shapes.

    Used by the loader when ``:v2`` is missing — typically when the new
    code first reads a call that started before the deploy. After this
    runs once, the loader writes ``:v2`` back so subsequent reads are
    fast.

    Parameters
    ----------
    state_blob:
        Whatever ``AgentSessionStore.get_state`` returned (the dict
        from the ``:state`` Redis key), or ``None`` if the key was
        missing.
    history_list:
        Whatever ``AgentSessionStore.get_history`` returned (the list
        from the ``:history`` Redis key), or ``None`` if missing.

    The reader is tolerant: missing fields default, unknown fields are
    dropped except ``markers`` which sweeps up any unrecognised
    top-level state keys so we don't silently lose data.
    """
    state = SessionState()
    blob = state_blob if isinstance(state_blob, dict) else {}

    # Scalar / typed top-level keys.
    state.language = blob.get("language")
    state.status = blob.get("status")
    state.call_surface = blob.get("call_surface")
    state.campaign_id = blob.get("campaign_id")
    state.agent_mode = blob.get("agent_mode")
    state.agent_mode_final = blob.get("agent_mode_final")
    state.identity_verified = bool(blob.get("identity_verified"))
    state.identity_verification_pending = bool(blob.get("identity_verification_pending"))
    if isinstance(blob.get("proactive_silence_nudges"), int):
        state.proactive_silence_nudges = blob["proactive_silence_nudges"]
    if isinstance(blob.get("campaign_objectives_covered"), list):
        state.campaign_objectives_covered = list(blob["campaign_objectives_covered"])
    if isinstance(blob.get("appointment"), dict):
        state.appointment = dict(blob["appointment"])
    if isinstance(blob.get("outbound_memory"), dict):
        state.outbound_memory = dict(blob["outbound_memory"])
    if isinstance(blob.get("tool_flow"), dict):
        state.tool_flow = ToolFlowState.from_dict(blob["tool_flow"])

    # Memory sub-dict.
    memory = blob.get("memory") if isinstance(blob.get("memory"), dict) else {}
    facts_raw = memory.get("facts") or {}
    bootstrap_keys = set(memory.get("bootstrap_keys") or [])
    parsed_facts: dict[str, Any] = {}
    if isinstance(facts_raw, dict):
        for key, payload in facts_raw.items():
            if not isinstance(payload, dict):
                continue
            fact_dict = dict(payload)
            # Migrate legacy bootstrap_keys → from_prior_call on each fact.
            if key in bootstrap_keys and "from_prior_call" not in fact_dict:
                fact_dict["from_prior_call"] = True
            parsed_facts[str(key)] = fact_dict
    state.facts = parsed_facts
    state.objections = _bucket_from_any(memory.get("objections"))
    state.commitments = _bucket_from_any(memory.get("commitments"))
    state.preferences = list(memory.get("preferences") or [])
    state.salient_notes = list(memory.get("salient_notes") or [])
    state.asked_questions = list(memory.get("asked_questions") or [])
    state.prior_stage = memory.get("prior_stage")

    # Markers sweep — every top-level state key we didn't claim above
    # gets preserved here so call-end auto_lead_* flags survive.
    claimed = {
        "language", "status", "call_surface", "campaign_id", "agent_mode",
        "agent_mode_final", "identity_verified", "identity_verification_pending",
        "proactive_silence_nudges", "campaign_objectives_covered",
        "appointment", "outbound_memory", "tool_flow", "memory",
    }
    markers: dict[str, Any] = {}
    for key, value in blob.items():
        if key in claimed:
            continue
        markers[key] = value
    state.markers = markers

    # History: derive next_turn_index from the legacy list's length so
    # the new monotonic counter starts where the old "len(history) // 2"
    # call would have produced.
    parsed_history: list[HistoryTurn] = []
    if history_list:
        for idx, raw in enumerate(history_list):
            if not isinstance(raw, dict):
                continue
            role = str(raw.get("role") or "").lower()
            if role not in {"user", "assistant"}:
                continue
            parsed_history.append(
                HistoryTurn(
                    role=role,
                    content=str(raw.get("content") or ""),
                    turn_index=idx,
                )
            )
    state.history = parsed_history
    # next_turn_index is the next-USER-turn index. Each turn is two history
    # entries (user + assistant), so the next user turn is len(history) // 2.
    state.next_turn_index = max(0, len(parsed_history) // 2)

    return state


# ─── Patch application ───────────────────────────────────────────────────────


def apply_legacy_patch(state: SessionState, patch: dict[str, Any]) -> None:
    """Apply the merge-state patch shape (the dict passed to
    ``AgentSessionStore.merge_state``) onto a SessionState.

    Mirrors the historic ``merge_state`` semantics: dict-typed slots
    are shallow-merged, every other value replaces. The patch dict
    can carry any of the legacy top-level keys; we route them onto
    typed fields where possible and into ``markers`` otherwise.
    """
    if not isinstance(patch, dict):
        return

    typed_scalar = {
        "language", "status", "call_surface", "campaign_id",
        "agent_mode", "agent_mode_final",
    }
    typed_bool = {"identity_verified", "identity_verification_pending"}

    for key, value in patch.items():
        if key == "memory":
            # The legacy patch could carry a memory blob. Re-import via
            # the same migration helper so all conventions normalise.
            mem = value if isinstance(value, dict) else {}
            facts_raw = mem.get("facts") or {}
            if isinstance(facts_raw, dict):
                for fkey, fpayload in facts_raw.items():
                    if isinstance(fpayload, dict):
                        state.facts[str(fkey)] = dict(fpayload)
            if "objections" in mem:
                state.objections = _bucket_from_any(mem.get("objections"))
            if "commitments" in mem:
                state.commitments = _bucket_from_any(mem.get("commitments"))
            if "preferences" in mem:
                state.preferences = list(mem.get("preferences") or [])
            if "salient_notes" in mem:
                state.salient_notes = list(mem.get("salient_notes") or [])
            if "asked_questions" in mem:
                state.asked_questions = list(mem.get("asked_questions") or [])
            if "prior_stage" in mem:
                state.prior_stage = mem.get("prior_stage")
            if "bootstrap_keys" in mem:
                # Restamp bootstrap_keys onto facts.
                for fk in mem.get("bootstrap_keys") or []:
                    fk_str = str(fk)
                    if fk_str in state.facts and isinstance(state.facts[fk_str], dict):
                        state.facts[fk_str]["from_prior_call"] = True
            continue
        if key == "tool_flow":
            if isinstance(value, dict):
                # Shallow merge: collected slots accumulate, other fields replace.
                current = state.tool_flow.to_dict()
                current.update(value)
                # Re-merge collected sub-dict if both sides provided it.
                if isinstance(state.tool_flow.collected, dict) and isinstance(value.get("collected"), dict):
                    merged_collected = dict(state.tool_flow.collected)
                    merged_collected.update(value["collected"])
                    current["collected"] = merged_collected
                state.tool_flow = ToolFlowState.from_dict(current)
            continue
        if key == "appointment":
            if isinstance(value, dict):
                merged = dict(state.appointment)
                merged.update(value)
                state.appointment = merged
            continue
        if key == "outbound_memory":
            if isinstance(value, dict):
                merged = dict(state.outbound_memory)
                merged.update(value)
                state.outbound_memory = merged
            continue
        if key == "campaign_objectives_covered":
            if isinstance(value, list):
                state.campaign_objectives_covered = list(value)
            continue
        if key == "proactive_silence_nudges":
            if isinstance(value, int):
                state.proactive_silence_nudges = value
            continue
        if key in typed_scalar:
            setattr(state, key, value)
            continue
        if key in typed_bool:
            setattr(state, key, bool(value))
            continue
        # Unrecognised top-level keys land in markers — preserves
        # auto_lead_*, auto_site_visit_*, etc. without a separate map.
        state.markers[key] = value


# ─── Async API ────────────────────────────────────────────────────────────────


def _v2_key(namespace: str, call_id: str) -> str:
    return f"{namespace}:agent:call:{call_id}:v2"


def _legacy_state_key(namespace: str, call_id: str) -> str:
    return f"{namespace}:agent:call:{call_id}:state"


def _legacy_history_key(namespace: str, call_id: str) -> str:
    return f"{namespace}:agent:call:{call_id}:history"


_DEFAULT_TTL_SECONDS = 900


async def load_state(
    redis_client: Any,
    namespace: str,
    call_id: str,
) -> SessionState:
    """Read the unified state. Prefers ``:v2``; on miss reconstructs from
    legacy keys and stores ``:v2`` back so subsequent reads are fast.
    Never raises — Redis hiccups return an empty SessionState."""
    if not call_id:
        return SessionState()
    try:
        raw = await redis_client.get(_v2_key(namespace, call_id))
        if raw:
            try:
                blob = json.loads(raw)
            except Exception:
                blob = None
            if isinstance(blob, dict):
                return SessionState.from_dict(blob)
    except Exception:
        logger.debug("session_state_v2: v2 read failed", exc_info=True)

    # Legacy fallback.
    try:
        state_raw = await redis_client.get(_legacy_state_key(namespace, call_id))
        history_raw = await redis_client.get(_legacy_history_key(namespace, call_id))
    except Exception:
        return SessionState()
    state_blob = None
    if state_raw:
        try:
            state_blob = json.loads(state_raw)
        except Exception:
            state_blob = None
    history_list = None
    if history_raw:
        try:
            history_list = json.loads(history_raw)
        except Exception:
            history_list = None
    state = from_legacy_blobs(state_blob, history_list)
    state.call_id = call_id
    # Best-effort write-back so subsequent reads skip the legacy path.
    try:
        await save_state(redis_client, namespace, call_id, state, dual_write=False)
    except Exception:
        logger.debug("session_state_v2: write-back after migration failed", exc_info=True)
    return state


async def save_state(
    redis_client: Any,
    namespace: str,
    call_id: str,
    state: SessionState,
    *,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    dual_write: bool = True,
) -> None:
    """Write the unified blob. When ``dual_write`` is true, also writes
    the legacy ``:state`` and ``:history`` keys so a code rollback
    during the rollout window can still read."""
    if not call_id:
        return
    blob = json.dumps(state.to_dict(), ensure_ascii=False)
    try:
        await redis_client.setex(_v2_key(namespace, call_id), int(ttl_seconds), blob)
    except Exception:
        logger.debug("session_state_v2: v2 write failed", exc_info=True)
        return
    if not dual_write:
        return
    try:
        legacy_blob = json.dumps(state.to_legacy_state_blob(), ensure_ascii=False)
        await redis_client.setex(_legacy_state_key(namespace, call_id), int(ttl_seconds), legacy_blob)
    except Exception:
        logger.debug("session_state_v2: legacy state write failed", exc_info=True)
    try:
        legacy_history = json.dumps(state.to_legacy_history(), ensure_ascii=False)
        # Use the same TTL so the legacy keys don't outlive the unified blob.
        await redis_client.setex(_legacy_history_key(namespace, call_id), int(ttl_seconds), legacy_history)
    except Exception:
        logger.debug("session_state_v2: legacy history write failed", exc_info=True)


async def replace_state(
    redis_client: Any,
    namespace: str,
    call_id: str,
    state: SessionState,
    *,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    dual_write: bool = True,
) -> None:
    """Full-replace write — same as :func:`save_state` but acquires the
    per-call lock so the replace can't race a concurrent
    :func:`mutate_state`. The three historic ``set_state`` callers at
    session start go through this."""
    if not call_id:
        return
    lock = await _lock_for(call_id)
    async with lock:
        await save_state(
            redis_client,
            namespace,
            call_id,
            state,
            ttl_seconds=ttl_seconds,
            dual_write=dual_write,
        )


async def mutate_state(
    redis_client: Any,
    namespace: str,
    call_id: str,
    fn: Callable[[SessionState], None],
    *,
    ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    dual_write: bool = True,
) -> SessionState:
    """Lock → load → mutate via ``fn`` → save. The atomic primitive every
    other writer goes through.

    ``fn(state)`` receives a fresh SessionState. It MUST mutate in place
    and return ``None``. Returning a new instance is silently ignored
    so callers can't accidentally substitute a different object that
    skips the load-time legacy migration.
    """
    if not call_id:
        # Without a call_id we have no lock target. Fall back to
        # constructing an empty state, applying the mutation, and
        # discarding — matches the no-op behaviour the old facade had
        # for empty call_ids.
        state = SessionState()
        try:
            fn(state)
        except Exception:
            logger.exception("session_state_v2: mutate_state fn raised on empty call_id")
        return state
    lock = await _lock_for(call_id)
    async with lock:
        state = await load_state(redis_client, namespace, call_id)
        if not state.call_id:
            state.call_id = call_id
        try:
            fn(state)
        except Exception:
            logger.exception("session_state_v2: mutate_state fn raised; not persisting")
            return state
        await save_state(
            redis_client,
            namespace,
            call_id,
            state,
            ttl_seconds=ttl_seconds,
            dual_write=dual_write,
        )
        return state


__all__ = (
    "SCHEMA_VERSION",
    "BucketEntry",
    "HistoryTurn",
    "SessionState",
    "ToolFlowState",
    "apply_legacy_patch",
    "from_legacy_blobs",
    "load_state",
    "mutate_state",
    "replace_state",
    "save_state",
)
