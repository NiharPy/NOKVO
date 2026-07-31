# NOKVO — Voice AI Calling Platform

> A multi-tenant, low-latency **voice AI agent** platform for Indian businesses (real-estate first).
> It answers inbound calls, runs scheduled outbound campaigns, qualifies leads, and closes the
> loop into a CRM — in English, Hindi, and Telugu — with an **`eos → first-audio` budget under one
> second** across all supported languages.

NOKVO is a FastAPI backend + Vue 3 frontend, deployed on Azure Container Apps. Its most interesting
engineering lives in two places: a **shared, Redis-budgeted LLM pool** that lets hundreds of
concurrent calls draw from a small set of Azure OpenAI deployments without oversubscribing any of
them, and a **fully automated telephony number lifecycle** (compliance → DID rental → rotation)
that needs no human in the loop.

---

## Table of contents

- [Architecture at a glance](#architecture-at-a-glance)
- [Tech stack](#tech-stack)
- [Repository layout](#repository-layout)
- [Running locally (native)](#running-locally-native)
- [Running with Docker Compose](#running-with-docker-compose)
- [Configuration](#configuration)
- [Deep dive: the LLM layer](#deep-dive-the-llm-layer)
- [Deep dive: data structures & algorithms behind the LLM pool](#deep-dive-data-structures--algorithms-behind-the-llm-pool)
- [Deep dive: number rotation & the telephony lifecycle](#deep-dive-number-rotation--the-telephony-lifecycle)
- [Deep dive: the voice pipeline & the sub-1s latency budget](#deep-dive-the-voice-pipeline--the-sub-1s-latency-budget)
- [Concurrency, scheduling & background loops](#concurrency-scheduling--background-loops)
- [Design choices & why](#design-choices--why)
- [Testing](#testing)
- [Deployment (Azure)](#deployment-azure)
- [Observability & health](#observability--health)
- [Security posture](#security-posture)

---

## Architecture at a glance

```
                         ┌────────────────────────────────────────────────────┐
   PSTN caller ──▶ Plivo │  Azure Container Apps  ·  FastAPI (1 worker/replica)│
   (in / outbound)  │    │                                                     │
        audio (WS)  └───▶│  ┌───────────────┐   ┌──────────────────────────┐   │
                         │  │ Voice pipeline│──▶│  Shared LLM pool         │───┼─▶ Azure OpenAI
   Browser ──▶ Static    │  │ STT→LLM→TTS   │   │  (Redis token budgeter)  │   │   gpt-5-mini · nano
   Web App (Vue)  ──────▶│  └──────┬────────┘   └──────────────────────────┘   │
   REST/JWT              │         │                                            │──▶ Sarvam AI (STT/TTS)
                         │  ┌──────▼────────┐   ┌──────────────────────────┐   │
                         │  │ Domain services│  │  Background loops         │   │──▶ Razorpay (billing)
                         │  │ leads/campaigns│  │  dialer · number poller   │   │──▶ Meta / Google (leads)
                         │  │ billing/moderat│  │  CRM drainer · pool refresh│  │
                         │  └──────┬────────┘   └──────────────┬───────────┘   │
                         │         │                           │               │
                         └─────────┼───────────────────────────┼───────────────┘
                                   ▼                           ▼
                          PostgreSQL (durable)          Redis (hot path:
                          tenants · leads · calls        token budgets · sessions ·
                          campaigns · billing            locks · caches · fanout)
```

**Three product surfaces, one backend:**

| Surface | What it is | Auth |
|---|---|---|
| **Nokvo One** | The core tenant portal — inbound agents, leads, transcripts, billing | Org JWT (tiered) |
| **NOKVO APEX** | Deterministic outbound calling (campaigns, credits wallet, members, the *Nova* in-product assistant) | APEX JWT |
| **SuperAdmin** | Provisioning, plan management, LLM-pool key management, COGS | SuperAdmin JWT |

All three are Vue apps served from Azure Static Web Apps and talk to the same FastAPI service.

---

## Tech stack

| Layer | Choice |
|---|---|
| **Language / runtime** | Python 3.13, `asyncio` throughout |
| **Web framework** | FastAPI 0.136 + Uvicorn (`--proxy-headers`, 1 worker/replica) |
| **Data** | PostgreSQL (async via `asyncpg` + SQLAlchemy 2.x), Alembic migrations |
| **Hot-path state** | Redis (`redis.asyncio`) — token budgets, sessions, locks, caches, pub/sub |
| **LLM** | Azure OpenAI — **gpt-5-mini** (main agent pool) + **nano** (summaries/condensers); OpenAI embeddings fallback |
| **Speech** | Sarvam AI (STT + TTS, tuned for Indian languages) |
| **Telephony** | Plivo (primary, incl. India compliance/DID lifecycle); Exotel (legacy follow-up path) |
| **Payments** | Razorpay (subscriptions + orders, webhook-verified) |
| **Lead sources** | Meta Lead Ads, Google, generic CRM webhooks |
| **Frontend** | Vue 3 + Vite, Vue Router |
| **Infra** | Azure Container Apps (API), Static Web Apps (UI), Key Vault, Blob, Bicep IaC, GitHub Actions (OIDC) |
| **Observability** | OpenTelemetry + App Insights, LangSmith (prompt tracing) |

---

## Repository layout

```
app/
  main.py                 FastAPI app: routers, health, startup wiring of background loops
  api/                    HTTP/WS route handlers (one module per surface/concern)
  services/               Domain + infra logic (the bulk of the codebase, ~100 modules)
    llm_pool.py             ★ Redis-budgeted shared LLM pool ("cupcake-box budgeter")
    azure_grounded_llm.py   ★ the LLM client every non-voice service calls (complete / nano / stream)
    plivo_number_poller.py  ★ compliance-gated DID rental & rotation
    outbound_campaign_ticker.py  windowed-campaign dialer resume loop
    voice_stream/, pipeline/     the STT→LLM→TTS voice pipeline (modularized)
    ...
  models/                 SQLAlchemy models (tenants, leads, campaigns, billing, llm_pool_keys, …)
  core/                   config (pydantic-settings), rate limiting, crypto, auth
  db/                     async engine + session factory
migrations/               Alembic revisions (55+)
frontend/                 Vue 3 + Vite SPA (Nokvo One + APEX + SuperAdmin + Affiliate)
infra/                    Bicep, deploy scripts, env templates, load harness
docs/                     Architecture spec, failure matrix, operational runbook
tests/                    pytest suite (unit + FSM eval + integration)
Dockerfile                multi-stage prod image (also runs the migration job)
```

---

## Running locally (native)

### Prerequisites

- **Python 3.13**
- **PostgreSQL 14+** and **Redis 6+** reachable locally
- **Node 18+** (for the frontend)

> **⚠️ Postgres TLS gotcha.** The DB DSN is built with `?ssl=require` (see
> `app/core/config.py::SQLALCHEMY_DATABASE_URI`) because the managed Azure Postgres in
> prod/staging mandates TLS. A vanilla local Postgres does **not** speak SSL out of the box, so
> `asyncpg` will refuse to connect. Either point `POSTGRES_SERVER` at a TLS-capable Postgres
> (any managed instance — Neon/Supabase/Azure), or use the [Docker Compose](#running-with-docker-compose)
> path below, which starts Postgres with a self-signed cert and TLS enabled.

### 1. Backend

```bash
# from the repo root
python3.13 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Create a `.env` in the repo root. Only **five** settings are required to boot — everything else has
a sensible default and features degrade gracefully when their keys are absent (no LLM keys → agent
calls fail loudly but the API serves; no Qdrant → health reports `degraded`, not down):

```dotenv
# ── Required ──
POSTGRES_SERVER=localhost
POSTGRES_USER=nokvo
POSTGRES_PASSWORD=nokvo
POSTGRES_DB=nokvo
SECRET_KEY=dev-only-change-me-to-32+-random-chars

# ── Sensible dev defaults shown for clarity ──
ENVIRONMENT=development
REDIS_URL=redis://localhost:6379/0
EXPECTED_ORIGIN=http://localhost:5173     # frontend origin (CORS)

# ── Optional: fill in to exercise the real features ──
# AZURE_OPENAI_POOL_JSON=[{"key_id":"m0","endpoint":"https://…","api_key":"…","deployment":"gpt-5-mini","tpm":200000}]
# SARVAM_API_KEY=…            # STT/TTS
# PLIVO_AUTH_ID=…  PLIVO_AUTH_TOKEN=…      # telephony
# RAZORPAY_KEY_ID=…  RAZORPAY_KEY_SECRET=…  RAZORPAY_WEBHOOK_SECRET=…
```

Apply migrations and run the API:

```bash
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

- API: <http://localhost:8000>
- Interactive docs: <http://localhost:8000/docs>
- Liveness: `GET /health/live` · Deep readiness: `GET /health`

### 2. Frontend

```bash
cd frontend
npm install
cp .env.example .env        # VITE_API_BASE_URL defaults to http://localhost:8000
npm run dev                 # Vite dev server on http://localhost:5173
```

The dev API automatically whitelists `localhost:5173/5174` for CORS when `ENVIRONMENT` is not
`production`.

---

## Running with Docker Compose

The production `Dockerfile` builds the API image (and doubles as the Alembic migration job). For a
**one-command local stack** — API + Postgres (TLS on) + Redis, with migrations auto-applied — a
`docker-compose.yml` is included:

```bash
# create a .env as above (POSTGRES_SERVER/REDIS_URL are overridden by compose)
docker compose up --build
```

What compose does:

1. Starts **Redis**.
2. Starts **Postgres** with a generated self-signed cert and `ssl=on`, so the app's `ssl=require`
   DSN connects cleanly.
3. Builds the API image, waits for both to be healthy, runs `alembic upgrade head`, then launches
   Uvicorn on **:8000**.

To build the production image by itself (Container Apps runs `linux/amd64`; Apple Silicon is arm64):

```bash
docker buildx build --platform linux/amd64 -t nokvo-api:local .
# run migrations with the SAME image:
docker run --rm --env-file .env nokvo-api:local alembic upgrade head
```

> The image is intentionally lean: multi-stage build, non-root `app` user, no `ffmpeg` (nothing
> shells out to it at runtime), config injected from the environment — secrets are **never** baked
> into the image (`.dockerignore` excludes every `.env`).

---

## Configuration

Configuration is a single `pydantic-settings` model (`app/core/config.py`) that reads environment
variables first, then a `.env` file (`extra="ignore"`, `case_sensitive=True`). Highlights:

| Group | Keys | Notes |
|---|---|---|
| **Core (required)** | `POSTGRES_*`, `SECRET_KEY` | app refuses to start without these |
| **JWT** | `SUPERADMIN_/ORGANIZATION_/…_JWT_SECRET_KEY` | per-tier HMAC-derived secrets; `JWT_LEGACY_SECRET_FALLBACK` off by default |
| **LLM pool** | `AZURE_OPENAI_POOL_JSON`, `AZURE_OPENAI_NANO_POOL_JSON`, `LLM_POOL_DEFAULT_TPM` (200k), `LLM_POOL_WINDOW_SECONDS` (60), `AZURE_OPENAI_POOL_API_VERSION`, `AZURE_OPENAI_REASONING_EFFORT` (`minimal`) | see the deep dive below |
| **Speech** | `SARVAM_API_KEY`, `SARVAM_STT_REST_URL`, `SARVAM_TTS_REST_URL` | Indian-language STT/TTS |
| **Telephony** | `PLIVO_AUTH_ID/TOKEN`, `PLIVO_NUMBER_COUNTRY` (IN), `PLIVO_VALIDATE_SIGNATURES` | signatures **enforced** in prod |
| **Payments** | `RAZORPAY_KEY_ID/SECRET/WEBHOOK_SECRET` | webhook signature verified fail-closed |
| **Feature flags** | `NOKVO_CONNECT_ENABLED`, `ENABLE_APEX_PLANS`, `NOKVO_ONE_OUTBOUND_ENABLED`, … | dormant surfaces register no routes when off |

A startup `validate_security_config()` check loudly logs any fail-open knob left unset in
production (missing webhook secret, weak `SECRET_KEY`, permissive CORS, legacy JWT fallback on).

---

## Deep dive: the LLM layer

**Emphasis: this is where NOKVO does the most interesting work.** Every LLM call in the system —
the live voice agent, the *Nova* assistant, intent classifiers, call summarizers, moderation —
routes through one shared abstraction instead of per-tenant model deployments.

### Why a shared pool at all?

Per-tenant Azure OpenAI provisioning doesn't scale: you'd hold idle TPM quota per tenant, pay for
capacity nobody uses, and still hit per-deployment rate limits under bursty call traffic. NOKVO
instead runs a **small set of global Azure OpenAI deployments shared by all tenants**, with a
*centralized* token budget so no single deployment is ever oversubscribed — no matter how many
workers or tenants draw from it simultaneously.

### The mental model: a box of cupcakes

Each pool member (an Azure OpenAI deployment + key) is a **box of cupcakes**. The cupcakes are its
tokens-per-minute (TPM) budget. Taking cupcakes decrements the box's counter. When a box hits zero,
everyone skips it and moves to the next box — until the 60-second window rolls over and the box is
restacked to full TPM.

### Two pools

- **`mini`** — `gpt-5-mini`, the main agent brain (conversation, tools, Nova).
- **`nano`** — a cheaper model for summaries, condensers, and classification.

Members come from three sources, merged and de-duped by endpoint:

1. `AZURE_OPENAI_POOL_JSON` / `AZURE_OPENAI_NANO_POOL_JSON` (env).
2. The standalone global account, appended as a box (env *augments*, never replaces).
3. **DB-managed keys** (`llm_pool_keys` table, Fernet-encrypted) that a SuperAdmin can add at
   runtime — a background refresher merges them every ~30s, so **new Azure capacity starts taking
   traffic without a redeploy**.

### What a single call looks like

```
reserve(estimate)  ──▶  atomic Lua: pick a box with capacity, decrement it
       │                 (sticky: start the search at THIS call's home box)
       ▼
POST the deployment's chat/completions
       │
   ┌───┴────────────────────────────────────────────┐
   │ 429?  → cooldown the box (zero for the window)  │→ retry the next box
   │ 400 (bad param)? → learn the deployment's real  │→ rebuild body, retry same box
   │      profile (reasoning vs classic), cache it   │
   │ 200?  → reconcile: INCRBY the difference between │
   │      estimate and real usage.total_tokens        │
   └─────────────────────────────────────────────────┘
```

Three details worth calling out:

- **Sticky routing for prompt-cache hits.** A call's id is stashed in a `ContextVar` at the turn
  boundary; `reserve()` hashes it (blake2b, so it's stable *across processes*, unlike Python's
  salted `hash()`) into a "home box." Every turn of the same call prefers the same deployment, so
  Azure's per-deployment **prompt cache keeps hitting** — a large latency and cost win on multi-turn
  calls. Only when the home box is at its cap does the search walk to another box.

- **Estimate-then-reconcile.** Reservation uses a cheap estimate (`~chars/4 + max_tokens`) so the
  atomic decrement is fast; after the response we settle against the *real* `usage.total_tokens`,
  refunding or charging the difference. Optimistic accounting — never blocks on knowing the exact
  cost up front.

- **Self-correcting request profiles.** The gpt-5/o-reasoning family and classic gpt-4.x family
  accept *different* parameters (reasoning models reject `max_tokens` and non-default `temperature`,
  and silently return empty content without `reasoning_effort`). NOKVO seeds each member's profile
  from its deployment name and then **corrects it from Azure's own 400 responses**, caching the
  learned profile — so an arbitrarily-named deployment converges after one failed call.

---

## Deep dive: data structures & algorithms behind the LLM pool

This is the part the pool README-worthy: getting correct, fair, race-free budgeting across many
concurrent async workers and (potentially) a clustered Redis.

### 1. Redis fixed-window counters as a distributed token bucket

Each box's budget is a Redis key scoped to a **60-second tumbling window**:

```
{llm_pool:budget}:<pool>:<key_id>:<minute_epoch>
```

- The value is *remaining* tokens; first touch seeds it to the box's TPM.
- The window is `floor(now / 60)`, so keys naturally expire and "refill" with no cron — a classic
  **fixed-window rate limiter**, but with the counter *shared* across every replica.
- **Redis Cluster hash tag `{…}`**: only the constant prefix is inside the braces, so *every*
  member's budget key hashes to the **same slot**. This is load-bearing — the reservation is a
  multi-key `EVAL`, and without co-location the moment the pool grew past one member every reserve
  died with `CROSSSLOT` (which was swallowed, silently disabling all budgeting and 429 failover).

### 2. Atomic select-and-decrement (single Lua script)

Selection and decrement must be **one atomic step**, or two workers both read "box has room" and
both decrement past zero. The `_RESERVE_LUA` script:

```
for off = 0 .. n-1:
    i = ((start + off) mod n) + 1          -- rotating scan from `start`
    remaining = GET(box_i)  or  cap_i       -- seed to TPM on first touch
    if remaining >= est:
        SET(box_i, remaining - est); EXPIRE(box_i, window)
        return i                            -- first-fit
return -1                                    -- all boxes full
```

- **First-fit-from-rotating-start** over the members. The `start` index is the sticky home (or
  random when there's no call context), giving **affinity + failover + load spread** in one loop.
- Runs server-side in Redis → atomic against all concurrent callers, one network round trip.
- `O(n)` in pool size (n is tiny — a handful of deployments).

### 3. Sticky hashing (affinity without a coordinator)

```
home = int(blake2b(call_id).hexdigest(), 16) mod n
```

Modulo hashing over a stable digest gives each call a deterministic home box **with no shared
routing table** — any replica computes the same home for the same call. `blake2b` (not builtin
`hash()`) because Python salts `hash()` per process, which would scatter a call's turns across
replicas and blow the prompt cache.

### 4. Reservation ↔ reconciliation (two-phase accounting)

`reserve()` debits an *estimate*; `reconcile()` issues a single `INCRBY` of
`estimate − actual` (positive → refund, negative → charge more). This decouples the fast path
(atomic reserve) from exact cost (known only after the response), keeping the hot path cheap while
still converging the budget to true usage.

### 5. Per-member circuit breaker (429 cooldown)

A 429 from a box means "provider says this deployment is out of headroom." Rather than hammer it,
`cooldown()` **zeros the box for the rest of the window** — an ephemeral circuit-open state that
expires automatically when the window rolls. The retry loop then walks to the next box.

### 6. Bounded retry with a deadline

The client retries across boxes under a wall-clock deadline (`_MAX_RETRY_WAIT`) with short sleeps
when *every* box is momentarily empty, and gives up with a clear `LLMPoolError("pool saturated")`
rather than hanging a live call. Param-negotiation retries are separately bounded (≤3) so a
genuinely broken request can't loop.

### 7. Adaptive feature detection cache

`_PARAM_PROFILES` is a per-`key_id` dict memoizing each deployment's learned capabilities
(`use_max_completion_tokens`, `supports_temperature`, `reasoning_effort`). It's populated lazily
from deployment-name heuristics and **corrected from 400 responses** — a tiny online-learning cache
that removes the need to hardcode which model wants which params.

> **Net effect:** a handful of Azure deployments safely absorb bursty, many-tenant call traffic;
> hot calls stick to a cache-warm deployment; a rate-limited or misconfigured deployment is routed
> around within milliseconds; and operators can add capacity live from a console.

---

## Deep dive: number rotation & the telephony lifecycle

Indian phone numbers (DIDs) can't be bought at signup — Plivo requires an **approved compliance
application** (business KYC) first, and that approval is asynchronous (minutes to days, on Plivo's
side). NOKVO makes this fully hands-off:

```
onboarding files the compliance application  ──▶  status: pending
                                                       │
        plivo_number_poller (every 10 min, jittered)   │  polls each pending tenant
                                                       ▼
                        Plivo flips status to "approved"
                                                       │
                 acquire per-tenant provision lock (single-flight)
                                                       │
                 refresh the tenant row (snapshot may be minutes stale)
                                                       │
                 rent + assign a DID, link the compliance app, persist
                                                       ▼
                            number appears on the tenant — no manual step
```

Key correctness properties:

- **Single-flight across replicas.** Every replica runs the poller. Two replicas both seeing "no
  number yet" would each rent a *paid* DID. A **per-tenant provisioning lock** (Redis) serializes
  it; the winner re-checks on a **freshly refreshed row** before renting; losers (or a Redis blip)
  just defer to the next tick.
- **Fail-open, jittered.** A Plivo/Redis hiccup never crashes the loop — it defers the tenant. The
  start is jittered (0–30s) so replicas don't stampede.
- **Fuzzy status matching.** Plivo returns `approved` / `Approved` / `in-review` / … inconsistently;
  the poller treats anything containing `approv` (and not `reject`) as approved.
- **APEX bulk pool auto-provision.** For APEX one-click bulk calling, the same poller **tops the
  DID pool up to 5 numbers** on a dedicated sub-account, reusing the account's onboarding
  compliance — cost-safe and deferred, replacing what used to be a manual paste.

Related: the **outbound campaign ticker** resumes windowed campaigns. A campaign that's paused
(outside its 09:00–19:00 IST band, on an off day, or past its daily cap) fires no call-end webhook,
so nothing would otherwise re-trigger the dialer. A 10-minute tick wakes every RUNNING campaign,
runs a **gated, idempotent** dial check (the time/day/cap/balance gates decide whether to actually
place a call), and a **stale-row reaper** releases contact rows whose terminal webhook never
arrived — so one lost webhook can't pin a dial slot forever. Exactly **one active campaign per
tenant** is enforced with an advisory lock (contention → `409`).

---

## Deep dive: the voice pipeline & the sub-1s latency budget

The live call path is **STT → LLM → TTS**, and the design goal is `end-of-speech → first-audio` in
**under one second** for all supported languages, inbound and outbound. The defenses that get it
there:

- **Streamed LLM + sentence-level TTS** — start speaking the first sentence while the rest is still
  generating.
- **Sticky, cache-warm LLM routing** (see the pool deep dive) — multi-turn calls reuse a warm
  prompt cache.
- **TTS byte-cache in Redis** — identical synthesis requests (keyed by a sha256 of the request
  body) skip the provider entirely; questionnaires are pre-translated to en/hi/te at creation and
  pre-warmed.
- **Early-intent exits & a fast intent router** — deterministic/cheap paths short-circuit the LLM
  where possible.
- **Retry off the live turn path** — anything that can fail-and-retry does so out of band via the
  in-process retry scheduler.

The modularized pipeline lives under `app/services/voice_stream/` and `app/services/pipeline/`;
`docs/nokvo-one-architecture-spec.md` has the full turn-flow and latency model.

---

## Concurrency, scheduling & background loops

The service is `asyncio`-first and runs several **single-process background loops**, all wired in
`app/main.py`'s startup hook. Each is idempotent, fail-open, and jittered:

| Loop | Cadence | Job |
|---|---|---|
| `llm_pool` refresher | ~30s | merge DB-managed pool keys into the live pool |
| `plivo_number_poller` | 10 min | rent DIDs once compliance is approved; top up APEX bulk pool |
| `outbound_campaign_ticker` | 10 min | resume windowed campaigns; reap stale contact rows |
| `crm_webhook_drainer` | ~30s | deliver outbound CRM webhooks (HMAC-signed, at-least-once) |
| `retry_scheduler` | 2 min | drain `pending_tool_retries` off the live path |
| `lead_sync_scheduler` | 30 min | pull fresh leads from Meta/Google |
| `followup_scheduler` | 60s | place scheduled follow-up calls (currently globally disabled) |
| `platform_settings` refresher | periodic | pick up SuperAdmin FX/config across replicas |

Cross-replica correctness relies on **Redis locks / advisory locks** (provisioning single-flight,
one-active-campaign-per-tenant, contact-row `FOR UPDATE`) rather than assuming a single instance.

---

## Design choices & why

- **One Uvicorn worker per replica; scale by replicas, not workers.** Keeps the DB connection-pool
  math and the Container Apps concurrency scaler clean — each replica caps at `pool_size +
  max_overflow = 20` connections, so N replicas stay well under the Postgres SKU's `max_connections`.
- **Redis on the hot path, Postgres for durable truth.** The per-turn path touches Redis (budgets,
  sessions, caches); Postgres holds tenants, leads, calls, campaigns, billing. The DB pool is
  deliberately small.
- **Shared LLM pool over per-tenant deployments.** Higher utilization, centralized rate-limit
  safety, live capacity management — detailed above.
- **Same image for web + migrations.** The migration job runs the exact deployed image with an
  `alembic upgrade head` command override, so schema always matches code.
- **Managed identity in prod; no secrets in the image.** Prod secrets live in Key Vault and are
  pulled at deploy time; `AZURE_CLIENT_SECRET` is intentionally dropped in prod.
- **Fail-open background work, fail-closed security.** Background loops degrade quietly; security
  gates (Plivo signature enforcement, Razorpay webhook verification) fail *closed* in production.
- **Feature flags for dormant surfaces.** Off-by-default features (`NOKVO_CONNECT_ENABLED`,
  `ENABLE_APEX_PLANS`, …) register **no routes** at all when disabled — the surface is inert, not
  just hidden, so stale credentials can't hit a dead endpoint.

---

## Testing

```bash
# unit tests + FSM eval framework against the in-memory FakeDB
./run_tests.sh

# a single area (fast, avoids the suite's known order-dependent flakiness)
venv/bin/python -m pytest tests/nokvo_one/test_llm_pool.py -v

# optional: integration tests against a REAL Postgres (catches session/greenlet bugs)
export TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/nokvo_test
./run_tests.sh
```

> The full suite has known **order-dependent** flakiness (~pre-existing). Prefer running targeted
> files when validating a change. The LLM pool specifically has focused suites:
> `test_llm_pool.py`, `test_llm_pool_concurrency.py`, `test_llm_pool_params.py`.

---

## Deployment (Azure)

- **API** → Azure Container Apps (build with `az acr build`, roll a new revision). CI deploys on
  push to `main` via GitHub Actions using OIDC (no stored cloud creds).
- **Migrations** → a Container Apps **job** running the same image with `alembic upgrade head`,
  executed before the revision roll.
- **Frontend** → Azure Static Web Apps (`npm run build`, per-mode `.env`).
- **Secrets** → Key Vault, referenced by the app via managed identity.
- **IaC** → `infra/` holds the Bicep (`platform.bicep`, `app.bicep`), `deploy.sh`, and env
  templates. `infra/.env.prod.example` documents every prod setting and which ones are `[AUTO]`
  (filled from Bicep outputs).

The manual fallback (ACR build → migrate job → revision roll → SWA deploy) is documented in
`docs/nokvo-one-operational-runbook.md`.

---

## Observability & health

- **`GET /health/live`** — pure liveness (no dependency I/O), so an LB probe never flaps on a
  transient backend blip.
- **`GET /health`** — deep readiness: pings Redis, Postgres, and Qdrant **in parallel** with
  per-dependency timeouts. Redis/Postgres down → **503**; Qdrant down → **`degraded`** (it backs a
  retired RAG path) but the service stays up.
- **OpenTelemetry → App Insights** with per-call trace-id stamped onto every log line.
- **LangSmith** prompt tracing (no-ops cleanly when `LANGSMITH_API_KEY` is unset).
- Per-call **COGS** (STT/LLM/TTS/telephony) is metered live via a `ContextVar` sink and recorded to
  cost columns for the SuperAdmin console.

---

## Security posture

- No secrets in the repo or image; `.env*` git-ignored and docker-ignored; API keys stored
  Fernet-encrypted at rest (`llm_pool_keys`, Plivo credentials).
- Plivo webhook **signatures enforced** and Razorpay webhooks **verified fail-closed** in prod.
- Per-tier HMAC-derived JWT secrets; legacy raw-secret fallback off by default.
- Startup `validate_security_config()` surfaces any fail-open knob left unset in production.
- Rate limiting (SlowAPI) is wired fail-fast — a misconfigured limiter stops startup rather than
  silently serving unlimited traffic.

---

<sub>Built with FastAPI · Azure OpenAI · Sarvam · Plivo · Redis · PostgreSQL · Vue.</sub>
