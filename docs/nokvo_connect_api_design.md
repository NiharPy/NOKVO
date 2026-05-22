# Nokvo Connect — Public Voice/Text Agent API

## Goal

Let a **Nokvo One** organization admin issue API keys their own webapps/mobile
apps use to embed the voice agent. The pipeline is the same one phone calls
use; the API is a thin, hardened wrapper around it.

Connect is a Nokvo One product. Prime tenants are explicitly refused at both
the admin and public surfaces, so a key minted under Nokvo One cannot
"survive" a tier downgrade and keep streaming.

## Scope (v1)

| Endpoint                                          | Purpose                                        |
| ------------------------------------------------- | ---------------------------------------------- |
| `POST   /api/voice/sessions`                      | Create a session (voice or text).              |
| `GET    /api/voice/sessions/{id}`                 | Status, transcript, usage, timestamps.         |
| `POST   /api/voice/sessions/{id}/message`         | Text-only fallback. Returns assistant reply.   |
| `POST   /api/voice/sessions/{id}/end`             | Best-effort explicit end + final usage write.  |
| `WS     /api/voice/sessions/{id}/stream`          | Bidirectional audio + text streaming.          |

Admin-side (`/api/nokvo-one/connect/...`, Nokvo One JWT, admin role):

| Endpoint                                          | Purpose                                        |
| ------------------------------------------------- | ---------------------------------------------- |
| `GET    /connect/api-keys`                        | List keys (prefix + metadata, never raw).      |
| `POST   /connect/api-keys`                        | Mint a key. Returns raw value ONCE.            |
| `PATCH  /connect/api-keys/{id}`                   | Update label, scopes, origins, rate limit.     |
| `POST   /connect/api-keys/{id}/revoke`            | Immediate revoke (cascades to live sessions).  |
| `GET    /connect/api-keys/{id}/usage`             | Per-key rollup (minutes, tokens, $).           |

## Authentication

* Header: `Authorization: Bearer nk_live_<24-char-secret>` **or** `X-Nokvo-API-Key: <same>`.
* Format: `nk_live_` prefix + 32 random base62 chars. `nk_test_` prefix for sandbox.
* Storage: `argon2id` over the secret portion. Display column = first 8 chars.
* Lookup: index on `key_prefix` for O(1) candidate fetch, then argon2 verify against
  the row's `secret_hash`. Constant-time pick prevents timing oracles.

## CORS / origin allowlist

Each key has an `allowed_origins: list[str]` (e.g., `["https://app.tenant.com"]`).
* REST: middleware checks `Origin`/`Referer` against the list **after** key auth.
* WS: the upgrade handler validates `Origin` before `accept()`. Mismatch → 1008.
* Empty list = same-origin only (Nokvo dashboard testing).

## Rate limiting

* SlowAPI keyed by `key_prefix` (not IP). Per-key limit stored in DB (default 60 RPM
  REST + 5 concurrent streams).
* Concurrency: Redis counter `connect:concurrent:{api_key_id}` incremented on
  WS upgrade / session create, decremented on close. 429 if over the cap.

## Session lifecycle

```
client ──POST /sessions──> { session_id, websocket_url, expires_in }
client ──WS  /stream────►  bidir audio / events
                       ◄──  agent audio / transcript events
client ──WS close OR POST /end──► session → completed, usage row written, webhook fired
```

Server-driven cleanup: a session that gets no traffic for `idle_timeout_seconds`
(default 90s) is auto-ended.

## Streaming protocol

WebSocket framing piggybacks on the existing browser tester format so
`NokvoOneVoiceStreamService.run_session` is reusable with an adapter:

Client → server:
* `text frame`: `{"type":"audio_format", "encoding":"pcm16","sample_rate":16000}` (once)
* `text frame`: `{"type":"text_message", "content":"..."}`
* `text frame`: `{"type":"end_turn"}`
* `binary frame`: raw PCM audio chunks

Server → client:
* `{"type":"voice_session_ready"}`
* `{"type":"transcript_interim","text":"..."}`
* `{"type":"transcript_final","text":"...","role":"user"}`
* `{"type":"assistant_text","text":"...","final":bool}`
* `binary frame`: audio chunks (codec advertised in `voice_session_ready`)
* `{"type":"session_complete","reason":"..."}`

## Webhook callbacks

Per-key `webhook_url` (optional). Lifecycle events POSTed with:
* `X-Nokvo-Event`: `session.started | session.ended | session.message | session.failed`
* `X-Nokvo-Signature`: `t=<unix>,v1=<hex(hmac_sha256(secret, t + "." + body))>` (Stripe-shape)
* Body: `{event, organization_id, api_key_prefix, session_id, data}`
* Retries: 5 attempts at 30s, 5m, 30m, 2h, 12h (in-process scheduler; same one
  `retry_scheduler` already runs).

## Usage metering

On session end:
* Write `TenantUsageEvent(event_type="api_voice_session" | "api_text_session", ...)`
  with `stt_minutes`, `tts_characters`, `llm_input_tokens`, `llm_output_tokens`,
  `cost_usd`, `metadata_={api_key_id, session_id}`.
* Existing billing rollup picks it up automatically.

## Tenant isolation

Every code path that holds an API key must end at a `tenant_res` belonging to the
same `organization_id`. We never accept a tenant id off the wire — only via the
key → organization lookup. The pipeline already enforces tenant scope on Redis,
Qdrant, and KB.

## Implementation order

1. `organization_api_key`, `voice_api_session` models + alembic migration.
2. Key mint / hash helpers in `app/core/api_keys.py`.
3. Dep `get_org_via_api_key` in `app/api/deps.py`.
4. Connect admin routes (`/api/org-auth/connect/...`).
5. Public routes (`/api/voice/...`).
6. WS adapter that bridges to `NokvoOneVoiceStreamService.run_session`.
7. Usage-event emission on session close.
8. Webhook delivery service (signed POST + retry).
9. Nokvo Connect dashboard: list/create/revoke keys, view usage.
10. Developer docs + working SDK snippet (JS + Python).
