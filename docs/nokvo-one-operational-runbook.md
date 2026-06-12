# Nokvo One — Operational Runbook

Practical, on-call guide for the voice agent in production. Sits beside
[`nokvo-one-failure-matrix.md`](./nokvo-one-failure-matrix.md) (which catalogs
*designed* degradations); this doc is **what to do when the pager goes off**.

Each section: **Symptoms → Detect → Immediate mitigation → Root cause →
Escalation**. Strings in `code` are real log/grep anchors, not paraphrases.

---

## 0. First 60 seconds — orient

| Probe | Command | Healthy |
|-------|---------|---------|
| Liveness (is the process up?) | `curl -s :8000/health/live` | `{"status":"ok"}` |
| Readiness (are deps up?) | `curl -s :8000/health \| jq` | `status: "ok"`, every check `ok:true` |

`/health` pings **Redis + Postgres + Qdrant** in parallel (each time-boxed ~2s):
- **503 / `status:"unhealthy"`** → Redis or Postgres is down (request-path critical). Jump to §3 (Redis) or check Postgres.
- **200 / `status:"degraded"`** → only Qdrant is down. The retired-RAG path; **not** call-affecting. Note it, don't page.
- `/health/live` stays 200 even when a dependency is down — it's pure liveness, so the load balancer doesn't cycle pods over a transient blip.

**Key env toggles** (`.env` / secret store):
`AZURE_OPENAI_POOL_JSON`, `AZURE_OPENAI_NANO_POOL_JSON` (LLM capacity) ·
`REDIS_URL` · `LANGSMITH_API_KEY` + `LANGSMITH_PROJECT` (prompt traces) ·
`OTEL_ENABLED` / `OTEL_EXPORTER` (trace-id correlation) ·
`ALLOW_AZURE_CLIENT_SECRET_FALLBACK` (must be `false` in prod).

---

## 1. LLM pool exhaustion (all boxes at TPM)

The shared "cupcake-box" pool (`app/services/llm_pool.py`) gives every tenant a
per-key tokens-per-minute budget in Redis. When every box is at its cap, new
reservations are refused and turns can't get an LLM.

**Symptoms**
- Agent goes quiet mid-call or replies with the soft fallback; callers report "it stopped talking".
- Latency spike as turns wait then fail.

**Detect** (grep app logs — every call line carries `[trace=<id>]`, see §5):
- `LLM pool saturated — all keys at TPM cap` (`llm_pool.py:294`) — the hard signal.
- `NOKVO-LLM: pool saturated — soft fallback to <key>` (`nokvo_one_voice_pipeline.py:376`) — pressure, degrading but surviving.
- `LLM pool reserve failed` (`llm_pool.py:209`) — Redis eval error (overlaps with §3).
- `No LLM pool members configured` — misconfiguration, not load (see Root cause).

**Immediate mitigation**
1. Add capacity: append a box (a real Azure deployment) to `AZURE_OPENAI_POOL_JSON`, or raise an existing box's `tpm`. Restart picks it up (pool members are cached; a redeploy/`LLMPool.reset_cache()` re-reads).
2. Check for **cooldowns**: a provider `429` zeroes a box for the rest of its 60s window (`LLMPool.cooldown`). If one deployment is 429-ing, the rest absorb the load — add a box rather than waiting.
3. Sizing reference: the contention test (`tests/nokvo_one/test_llm_pool_concurrency.py`) proves the reserve Lua is atomic — at `tpm=1000`, exactly `1000/est` concurrent turns succeed, no oversubscription. Size `tpm` to *peak concurrent turns × tokens-per-turn × safety factor*.

**Root cause**
- Real traffic > provisioned TPM → add boxes / raise TPM.
- A deleted/renamed Azure resource → `No LLM pool members` or DNS `[Errno 8]`; fix `AZURE_OPENAI_POOL_JSON`.

**Escalation** — if adding capacity doesn't clear it, page the Azure-account owner (quota increase) and consider temporarily lowering per-turn `max_tokens`.

---

## 2. Sarvam STT down / degraded

Speech-to-text runs against Sarvam's streaming WS (`saaras:v3`, `app/services/sarvam_voice_service.py`).

**Symptoms**
- Agent repeatedly says "Sorry, I couldn't catch that"; transcripts empty; callers feel unheard.

**Detect**
- `NOKVO-VOICE: STT rate-limited after retries` (`nokvo_one_voice_stream_service.py:1412`) — Sarvam 429s.
- `Sarvam STT failed (<code>)` / `Sarvam translate STT failed (<code>)` (`sarvam_voice_service.py:194,239`).
- Frontend frames: `stt_error`, `stt_empty`, `translate_stt_error`.
- TTS counterpart (caller hears nothing back): `NOKVO-TTS: Sarvam TTS failed (<code>)` (`sarvam_voice_service.py:422`).

**Immediate mitigation**
1. Check Sarvam status / your account rate limits. `429` → you're over quota; back off non-essential traffic.
2. Transient `5xx` → the pipeline already retries with backoff; if persistent, it's upstream.
3. Audio-quality (not outage) misfires — garbled digits/names on forwarded calls — are handled by the ANI phone pre-fill and the name-confirm cap; confirm `PLIVO_DEFAULT_SAMPLE_RATE=16000` and `VOICE_EOU_DEBOUNCE_MS` are intact before assuming an outage. Also check the per-call `NOKVO-AUDIO: stream rate=… enc=…` log line — a rate/encoding mismatch from the carrier is the classic garble source, and `stream rate mismatch` is logged at ERROR when it happens.
4. The caller→STT path runs RNNoise speech denoise (`VOICE_STT_DENOISE_ENABLED`, dependency `pyrnnoise` — **note: the repo has no requirements.txt; install `pip install pyrnnoise` wherever the app is deployed**). It degrades gracefully: if the wheel is missing you'll see one `VOICE-DENOISE: RNNoise unavailable` warning and audio passes through undenoised (worse on noisy lines, never broken).

---

## 2b. Inbound calls don't connect (webhook layer)

The Plivo Application's `answer_url` is registered once at provisioning from `PLIVO_WEBHOOK_BASE_URL`. If that URL changes (domain move, tunnel rotation), every existing tenant's Application points at a dead URL.

**Detect**
- `GET /api/nokvo-one/agents/phone-link` → `webhook_health` block: `in_sync` false means the stored `answer_url` doesn't match the current base; `last_inbound_webhook` empty means Plivo never reached us; `last_stream_error` populated means the voice webhook worked but the media WebSocket failed (usually a `ws://` URL from a misconfigured base — must be `wss://`).
- Startup log `PUBLIC-URL:` errors (unset/localhost/http base) and `PLIVO-WEBHOOKS: N tenant(s) have a STALE … answer_url`.

**Fix**
1. Set `PLIVO_WEBHOOK_BASE_URL` to the public **https** URL and restart.
2. `POST /superadmin/tenants/plivo/resync-webhooks?dry_run=true` to preview, then without `dry_run` to re-point every stale Application. (Or set `PLIVO_WEBHOOK_AUTOSYNC=true` for boot-time auto-repair — not recommended with rotating tunnels.)
3. Place a test call; confirm `webhook_health.last_inbound_webhook` updates and two-way audio flows.

Webhook security: `PLIVO_VALIDATE_SIGNATURES` is `warn` by default (logs X-Plivo-Signature-V2 mismatches without rejecting). After one real call confirms signatures validate (look for no `PLIVO-SIG warn` lines), flip to `enforce`. Outbound status webhooks that never arrive are reconciled by the follow-up scheduler after `FOLLOWUP_INFLIGHT_TIMEOUT_MINUTES` (default 30) — stuck rows fail with reason `webhook_timeout` instead of hanging in_flight forever.

**Root cause** — Sarvam outage, quota exhaustion, or a regional network path. STT/TTS are external; we can retry and communicate, not fix their backend.

**Escalation** — open a Sarvam support ticket with the failing `<code>` + timestamps; post status to the ops channel; if extended, pause outbound campaigns to stop burning failed calls.

---

## 3. Redis failover / outage

Redis backs the agent **session state** (`AgentSessionStore`) and the **LLM pool budget** counters. It is request-path critical.

**Symptoms**
- `/health` → **503** with `checks.redis.error` populated.
- In-flight calls drop or lose context; new calls can't reserve an LLM (`LLM pool reserve failed`).

**Detect** — `curl :8000/health | jq .checks.redis` shows `ok:false` + the error; app logs show connection errors against `REDIS_URL`.

**Immediate mitigation**
1. Restart / fail over the Redis instance (or repoint `REDIS_URL` to a standby) and redeploy/restart the app so the client reconnects.
2. **Data-loss assessment:** session state is **ephemeral by design** — calls active during the outage are lost, but there is **no billing loss** (the `call_costs` ledger is Postgres) and no lead loss (leads persist to Postgres at capture). Pool budgets simply reset to full on the fresh window — harmless.
3. Confirm recovery: `/health` back to 200, place a tester call.

**Root cause** — Redis crash, OOM, network partition, or a bad `REDIS_URL`.

**Escalation** — if Redis won't come back, infra/platform owner. The service can technically limp without Redis only if both session state and the pool are stubbed — not a supported prod mode; restore Redis.

---

## 4. "The agent said the wrong thing" — escalation & trace workflow

The highest-value path: a customer reports a specific bad call ("it quoted the
wrong price", "it booked the wrong date"). Reproduce and fix deterministically.

**Step-by-step**
1. **Find the call.** In the dashboard, locate the call by **organization + time**
   (the cost view is indexed on `(organization_id, started_at)`). Read its
   **`trace_id`** off the `call_costs` row.
   - *Caveat:* `call_costs` has no phone column, so "the +9198… call at 3pm" is
     located by **org + time**, not by phone number directly.
2. **Pull the logs.** `grep "[trace=<trace_id>]" <logfile>` — **every** log line
   for that call (STT, pool routing, pipeline, TTS) carries the id, because the
   trace id is stamped app-wide via the logging filter
   (`app/services/otel_tracer.py`). The anchor line is
   `NOKVO-CALL-START call_id=… tenant=… kind=… caller=… trace_id=…`.
3. **Open the exact prompt/response.** With `LANGSMITH_API_KEY` set, open the
   LangSmith run for that call (project `nokvo-one`). Its metadata carries the
   same `otel_trace_id`, so you can pivot logs ↔ LangSmith. Inspect the
   per-turn `voice_call → turn → llm` tree to see the *composed* system prompt
   (campaign brief + memory + FSM mode + strategy block) and the model's reply —
   this is where "why did it say X" is answered.
4. **Fix + lock it in.** Once you find the faulty layer (slot extraction, mode,
   strategy block, prompt), fix it AND **add a golden-path eval fixture** so it
   can never regress: drop a transcript JSON into
   `tests/nokvo_one/eval/transcripts/` asserting the corrected
   `collected.*` / `mode` / `tool_flow_flow_key` (see existing fixtures;
   `test_eval_replay.py` auto-discovers it). This closes the loop:
   incident → trace → fix → regression test.

**If `trace_id` is NULL on the row:** OTel was disabled for that call
(`OTEL_ENABLED=false`) — you can still trace via `call_id` in the logs and the
LangSmith run keyed on `call_id`. Enable `OTEL_ENABLED=true` to get the
correlated trace id on future calls.

---

## Appendix — enabling trace correlation

`OTEL_ENABLED=true` turns on per-call W3C trace ids (stamped on logs + persisted
to `call_costs.trace_id` + cross-linked into LangSmith). The **exporter** is
independent:
- `OTEL_EXPORTER=none` (default) — ids generated in-process, nothing shipped. No collector required. This already delivers the §4 workflow.
- `OTEL_EXPORTER=console` — prints spans to stdout (debugging).
- `OTEL_EXPORTER=otlp` + `OTEL_EXPORTER_OTLP_ENDPOINT=<collector>` — ships spans to Jaeger/Tempo/Grafana for waterfall timing of `call → stt/llm/tts → response`.

Disabled (the default) adds **zero** hot-path latency — same no-op discipline as
the LangSmith seam.
