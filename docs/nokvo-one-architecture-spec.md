# NOKVO One Architecture Spec

Last updated: 2026-05-21

This document describes the current NOKVO One design as it exists in the codebase now: what the product is, how requests flow, which services own which responsibilities, where latency is spent, and what still needs hardening before broad production rollout.

## 1. Product Intent

NOKVO One is a multi-tenant AI voice operations platform for SMBs. The core use cases are:

- inbound call answering
- appointment booking
- lead qualification and capture
- consented outbound calling
- follow-up and callback management
- outcome tracking

The strongest wedge is appointment-driven businesses, especially clinics and local service businesses where a missed call has direct revenue impact.

The platform is not meant to be a generic chatbot. It is an operational agent with structured workflows, state, auditability, retries, identity checks, and outcome feedback.

## 2. Architectural Principles

The current architecture follows five rules:

1. One canonical agent contract should define behavior across chat, inbound voice, and outbound voice.
2. Runtime state should be shared, explicit, and auditable.
3. Deterministic flows should resolve as much as possible before invoking a heavy LLM turn.
4. Consent and identity must gate any side-effecting action.
5. Failures should be recoverable through retry queues, fallback responses, and operator visibility.

## 3. Main Building Blocks

### 3.1 Canonical agent spec

File: `app/services/agent_spec.py`

This is the behavioral contract for the agent. It defines:

- confirmation policy
- retry policy
- identity policy
- outcome states
- capability declarations

It is the source of truth for what the agent is allowed to do and how the runtime should behave. The spec is intentionally not tenant-specific.

### 3.2 Unified runtime context

File: `app/services/agent_runtime_context.py`

This object combines the pieces every surface needs:

- organization
- tenant resources
- surface type
- business template overrides
- custom tabs
- campaign context
- caller identity
- session id

The goal is to keep voice, chat, and outbound on one context shape instead of each surface inventing its own.

### 3.3 Session helpers

Directory: `app/services/session/`

These helpers split the per-turn mechanics into separate concerns:

- `confirmation.py`: confirmation state and confirmation checks
- `audit.py`: audit trail writes
- `outcome.py`: outcome persistence and summarization
- `retry.py`: retry-state helpers

The compatibility layer in `app/services/flow_session.py` allows older call sites to keep working while the runtime migrates.

### 3.4 Voice pipeline

File: `app/services/nokvo_one_voice_pipeline.py`

This is the core inbound reasoning engine. It handles:

- intent routing
- business flow routing
- sensitive intent handling
- retrieval
- semantic caching
- LLM response generation
- stream-friendly sentence splitting
- tool execution support
- retry enqueueing on failure

### 3.5 Voice stream service

File: `app/services/nokvo_one_voice_stream_service.py`

This is the websocket-facing orchestration layer. It handles:

- receiving audio and transcripts
- language switching
- turn lifecycle events
- emitting agent sentences
- invoking Sarvam TTS
- writing session history
- surfacing runtime metadata to the frontend

### 3.6 Outbound campaign service

File: `app/services/outbound_campaign_service.py`

This service handles:

- campaign creation
- consented lead selection
- lead validation
- script indexing
- Exotel outbound call initiation
- call status handling
- post-call outcome closure

### 3.7 Lead ingestion and OAuth

The outgoing lead pipeline supports consented ingestion from:

- Meta Ads
- Google Ads
- Google Forms
- admin-generated Nokvo forms

The intent is to keep outbound calling restricted to approved sources only.

### 3.8 Retry and outcome subsystems

Files:

- `app/services/tool_retry_service.py`
- `app/services/retry_scheduler.py`
- `app/services/outcome_tracker.py`
- `app/api/nokvo_one_outcomes.py`

These components turn transient failures and call dispositions into persistent operational state.

## 4. Runtime Surfaces

### 4.1 Inbound voice

Inbound calls enter through the websocket stack, are transcribed through Sarvam STT, routed through the voice pipeline, grounded with retrieval and session state, and then spoken back with Sarvam TTS.

### 4.2 Outbound voice

Outbound campaigns select consented leads, launch Exotel calls, and connect answered calls into the same voice runtime. This is important: outbound is not a separate agent brain.

### 4.3 Chat / Studio

The chat/admin surface is used for configuration, testing, and control. It should read the same agent contract as voice even if the UX is different.

## 5. Request Flow

### 5.1 Inbound turn flow

1. Audio arrives over websocket.
2. Sarvam STT produces a transcript.
3. The voice stream service emits turn metadata and language state.
4. The voice pipeline performs fast routing:
   - greeting
   - language switch
   - smalltalk
   - template flow
   - policy flow
   - identity-gated flow
5. The pipeline checks session memory and semantic cache.
6. If needed, it runs retrieval over tenant knowledge in Qdrant.
7. Azure OpenAI generates a grounded answer.
8. Sarvam TTS streams sentence-level audio.
9. Session history, audit, and state are updated.

### 5.2 Outbound call flow

1. Admin selects consented leads.
2. Campaign is created with script and knowledge indexing.
3. Exotel initiates parallel outbound calls.
4. Answered calls are bridged into the same voice runtime.
5. The conversation runs with campaign context and caller identity.
6. Call status is mapped to a downstream outcome.
7. Follow-up callbacks or other record effects are written back.

## 6. State Model

### 6.1 Redis-backed state

`app/services/agent_session_store.py` stores:

- call history
- semantic cache
- per-call state
- session TTL state

This is the hot conversational memory.

### 6.2 Database-backed state

The database stores:

- organizations and tenant metadata
- leads
- campaigns
- tool records
- pending retries
- outcomes
- audit-linked record data

This is the durable operational memory.

### 6.3 Session helper state

The session helper layer is intended to make these concepts explicit:

- what value was confirmed
- what tool action happened
- what failed and should be retried
- what outcome closed the loop

## 7. Safety Model

The platform is intentionally conservative in three places:

### 7.1 Confirmation

Sensitive values should be read back before they are committed:

- name
- phone
- email
- identifiers
- proposed schedule slots

### 7.2 Identity verification

Cancellation and refund-like actions require identity verification before the agent executes the request.

### 7.3 Consent gating

Outbound calling is restricted to approved, consented lead sources. The system should not call arbitrary imported lists.

## 8. Latency Model

The dominant latency contributors are:

- STT round-trip
- Redis and DB reads
- retrieval embedding + Qdrant search
- LLM generation
- TTS generation

The current architecture already has some latency defenses:

- semantic caching
- early intent exits
- retrieval prefetch in some branches
- streamed LLM responses
- sentence-level TTS
- retry off the live turn path

The remaining latency problem is mostly about removing serial waits and repeated I/O, not about removing capabilities.

## 9. Current Bottlenecks

The biggest remaining bottlenecks are:

1. Some runtime policy still lives in local branches instead of being fully centralized in `agent_spec`.
2. Some paths still fetch state sequentially instead of in parallel.
3. STT and TTS still require external network calls per turn.
4. Sentence-level TTS increases provider round trips.
5. The operator UI for retries, outcomes, and source health is still thin.
6. Outbound disposition mapping is correct but still coarse.

## 10. Production Hardening Checklist

Before broad production rollout, the following should be verified:

- all surfaces consume the same canonical runtime contract
- retry queue draining is monitored
- outcome feedback is visible in the UI
- Meta OAuth refresh works live
- Google OAuth refresh works live
- Exotel failure handling is observable
- provider outages produce graceful fallbacks
- concurrency under multiple simultaneous calls is load-tested
- hot-path Redis/DB work is minimized
- call outcome classification is richer than answered/no-answer/failed

## 11. Current Assessment

The platform is already strong enough to be useful. It is not a toy agent. It has real workflow handling, state, retries, and outcome closure.

The main remaining work is integration tightening:

- remove duplicated local policy decisions
- make the shared runtime context the default everywhere
- improve operator visibility
- validate real-world provider behavior at scale

That is the difference between a functioning product and a production-grade operating system for calls.
