from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    PROJECT_NAME: str = "SuperAdmin Privileged Access Management"

    # Deployment environment: "development" | "staging" | "production".
    # Gates dev-only conveniences (e.g. localhost CORS origins) off in prod.
    ENVIRONMENT: str = "development"

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.strip().lower() in {"production", "prod"}

    # Database
    POSTGRES_SERVER: str
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_DB: str
    
    @property
    def SQLALCHEMY_DATABASE_URI(self) -> str:
        return f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_SERVER}/{self.POSTGRES_DB}?ssl=require"

    # JWT
    SECRET_KEY: str
    SUPERADMIN_JWT_SECRET_KEY: str = ""
    ORGANIZATION_JWT_SECRET_KEY: str = ""
    NOKVO_ONE_SETUP_JWT_SECRET_KEY: str = ""
    OAUTH_STATE_SECRET_KEY: str = ""
    # Accept raw-SECRET_KEY-signed tokens on any tier (pre-tier-migration legacy).
    # Default OFF: per-tier secrets are HMAC-derived + isolated, so the only thing
    # this enabled was decoding old tokens that have since expired. Set True via env
    # only as a temporary rollback if legacy tokens are somehow still in flight.
    JWT_LEGACY_SECRET_FALLBACK: bool = False
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    # APEX + SuperAdmin console sessions live 30 minutes. Neither frontend silently
    # refreshes (APEX stores no refresh token; the console stores one but logs out
    # on any 401), so the access-token lifetime IS the login duration there.
    APEX_ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    SUPERADMIN_ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_HOURS: int = 4

    # Nokvo One TOTP encryption (Fernet key; if unset, derived from SECRET_KEY).
    NOKVO_TOTP_ENCRYPTION_KEY: str = ""

    # Nokvo One signup/invite
    NOKVO_ONE_PUBLIC_BASE_URL: str = "http://localhost:5173"
    NOKVO_ONE_EMAIL_TOKEN_TTL_HOURS: int = 24
    NOKVO_ONE_INVITE_TOKEN_TTL_HOURS: int = 72

    # ── Affiliate program (APEX referrals) ──
    # 5% of the first invoice (platform fee + minutes addon), 2% of every later
    # monthly subscription charge. Settlement is operator-marked manual payout;
    # a commission becomes "due" once older than the T+2 window. Affiliate
    # sessions are a plain access token (no refresh — re-login is number+TOTP).
    AFFILIATE_FIRST_MONTH_RATE: float = 0.05
    AFFILIATE_RECURRING_RATE: float = 0.02
    AFFILIATE_SETTLEMENT_DUE_HOURS: int = 48
    AFFILIATE_ACCESS_TOKEN_EXPIRE_HOURS: int = 12

    # Recurring usage-invoice mailer. The internal endpoint that runs the
    # billing cycle is guarded by this shared secret (sent as the
    # ``X-Cron-Secret`` header by the scheduler). Empty in dev = endpoint
    # rejects all calls unless a secret is configured.
    BILLING_CRON_SECRET: str = ""
    USAGE_INVOICE_ENABLED: bool = True
    # Don't reprocess more than this many overdue cycles for one org in a single
    # run — a safety cap against a runaway catch-up loop.
    USAGE_INVOICE_MAX_CYCLES_PER_RUN: int = 6

    # Nokvo One onboarding v2 (outcome wizard, deferred MFA, simplified nav).
    # When false, the legacy onboarding flow is preserved exactly. The v2 surface
    # piggybacks on the same auth states + endpoints; it only changes how the
    # frontend routes and which actions require MFA up-front.
    NOKVO_ONBOARDING_V2: bool = False
    NOKVO_ONE_NATIVE_TOOL_CALLING: bool = True
    NOKVO_ONE_TOOL_LOOP_MAX_ITERATIONS: int = 4

    # Call-center ambience mixing for the agent's voice output. When enabled,
    # the frontend mic tester (and, when wired, the Exotel media path) layers
    # a low-volume office hum under the agent so calls feel realistic.
    # Files live in app/assets/audio/call_center_ambience/ (download via
    # `python -m app.scripts.download_ambience_audio`).
    NOKVO_CALL_CENTER_AMBIENCE_ENABLED: bool = True
    NOKVO_CALL_CENTER_AMBIENCE_VOLUME: float = 0.28  # 0.0 (silent) to 1.0 (full)
    # Server-side room tone on REAL telephony calls: mixes a low-gain office bed
    # under every agent speech chunk inside the Plivo playback adapter (the web
    # test call keeps its client-side ambience above — separate paths, no
    # doubling). GAIN is a linear PCM multiplier and is deliberately NOT the
    # browser VOLUME knob: 0.28 under 8k speech would be far too hot.
    NOKVO_AMBIENCE_TELEPHONY_ENABLED: bool = False
    NOKVO_AMBIENCE_TELEPHONY_GAIN: float = 0.10

    # Nokvo Connect — public API key infrastructure that lets customers embed
    # the voice agent into their own apps. Default OFF: the surface (admin
    # key-management routes, public session/voice routes, and the frontend
    # nav button + landing pages) is hidden until an operator explicitly
    # turns it on. Flip to ``true`` in .env when the feature is ready for a
    # given deployment.
    NOKVO_CONNECT_ENABLED: bool = False

    # Knowledge-base document uploads. The Upload Document card on the
    # Knowledge Base page is admin-only, but in most deployments we don't
    # want admins to be able to add raw documents (the answer surface is
    # better served by the onboarding-time sample upload + sources we
    # manage centrally). Default OFF: the card is hidden until an operator
    # flips this flag in .env. The backend upload endpoint stays available
    # regardless — gating is presentational only.
    NOKVO_KB_DOCUMENT_UPLOAD_ENABLED: bool = False

    # SMTP (optional — when unset, emails are logged only)
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = "no-reply@nokvo.ai"
    SMTP_FROM_NAME: str = "Nokvo"
    SMTP_USE_TLS: bool = True

    # Concierge WhatsApp onboarding — where the "client requested WhatsApp setup"
    # alert is emailed (ops inbox). When unset, no alert is sent (the request is
    # still visible in the superadmin console's WhatsApp requests queue).
    WHATSAPP_ONBOARDING_ALERT_EMAIL: str = ""

    # Google OAuth
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    GOOGLE_LEADS_OAUTH_CLIENT_ID: str = ""
    GOOGLE_LEADS_OAUTH_CLIENT_SECRET: str = ""
    GOOGLE_LEADS_OAUTH_REDIRECT_URI: str = ""
    GOOGLE_ADS_DEVELOPER_TOKEN: str = ""
    GOOGLE_ADS_LOGIN_CUSTOMER_ID: str = ""
    GOOGLE_ADS_API_VERSION: str = "v23"
    META_ADS_APP_ID: str = ""
    META_ADS_APP_SECRET: str = ""
    META_ADS_REDIRECT_URI: str = ""
    META_GRAPH_VERSION: str = "v23.0"
    META_LEADGEN_WEBHOOK_VERIFY_TOKEN: str = ""
    ZOHO_CLIENT_ID: str = ""
    ZOHO_CLIENT_SECRET: str = ""
    ZOHO_REDIRECT_URI: str = ""
    ZOHO_ACCOUNTS_URL: str = "https://accounts.zoho.com"
    
    # WebAuthn
    RP_ID: str = "localhost"
    RP_NAME: str = "NOKVO SuperAdmin"
    EXPECTED_ORIGIN: str = "http://localhost:5173"
    AGENT_PUBLIC_BASE_URL: str = "http://localhost:8000"

    # Redis (Rate Limiting & Tenant Cache)
    REDIS_URL: str = "redis://localhost:6379"

    # Per-tenant concurrency is split into TWO independent pools (see
    # app/services/call_concurrency.py):
    #   * outbound (campaign) calls           → NOKVO_MAX_CONCURRENT_OUTBOUND_PER_TENANT
    #   * inbound + follow-up calls (shared)  → NOKVO_MAX_CONCURRENT_INBOUND_FOLLOWUP_PER_TENANT
    # Follow-ups share the inbound pool, so disabling follow-up for a campaign
    # frees that whole pool for inbound. The legacy single-budget knob below is
    # kept for back-compat but no longer gates calls directly.
    NOKVO_MAX_CONCURRENT_CALLS_PER_TENANT: int = 10
    NOKVO_MAX_CONCURRENT_OUTBOUND_PER_TENANT: int = 5
    NOKVO_MAX_CONCURRENT_INBOUND_FOLLOWUP_PER_TENANT: int = 5

    # Session-state v2 dual-write rollout. When True (the default for one
    # deploy cycle), every unified-store write also updates the legacy
    # ``:state`` and ``:history`` keys so a code rollback during the rollout
    # window can still serve in-flight calls. Flip to False once one full
    # deploy cycle has elapsed and the legacy reader is no longer needed.
    SESSION_STATE_V2_DUAL_WRITE: bool = True

    # Azure Provisioning
    AZURE_SUBSCRIPTION_ID: str = ""
    AZURE_DEFAULT_REGION: str = "centralindia"
    AZURE_OPENAI_REGION: str = "swedencentral"
    AZURE_OPENAI_GLOBAL_ENDPOINT: str = ""
    AZURE_OPENAI_GLOBAL_API_KEY: str = ""
    AZURE_OPENAI_GLOBAL_DEPLOYMENT: str = "gpt-5.4-mini"
    AZURE_OPENAI_GLOBAL_API_VERSION: str = "2024-10-21"

    # ── LLM public pool ───────────────────────────────────────────────────────
    # A shared pool of GPT-5-mini Azure OpenAI deployments used by ALL tenants
    # (replaces per-tenant Azure OpenAI). A centralized per-key TPM budget in
    # Redis picks a key with remaining capacity; an exhausted key is skipped
    # until its 60s window refills. JSON list of
    # {"key_id","endpoint","api_key","deployment","tpm"}.
    AZURE_OPENAI_POOL_JSON: str = ""
    AZURE_OPENAI_POOL_API_VERSION: str = "2024-10-21"
    AZURE_OPENAI_POOL_MODEL: str = "gpt-5-mini"
    LLM_POOL_WINDOW_SECONDS: int = 60
    LLM_POOL_DEFAULT_TPM: int = 200000
    # How often each instance re-reads the DB-managed pool keys (llm_pool_keys)
    # so SuperAdmin changes propagate without a redeploy.
    LLM_POOL_DB_REFRESH_SECONDS: int = 30
    # gpt-5 family is a REASONING model: with the default effort its reasoning
    # tokens eat the whole max_output_tokens budget → empty visible reply. For a
    # latency-sensitive voice agent we want minimal reasoning so the budget goes
    # to the actual answer. (Responses API: reasoning.effort = minimal|low|medium|high.)
    AZURE_OPENAI_REASONING_EFFORT: str = "minimal"

    # ── In-call rolling summary (conversational awareness) ─────────────────────
    # A dedicated gpt-4.1-nano deployment maintains a compact running summary of
    # the call so the agent stays aware of the whole conversation, not just the
    # last few turns. Runs async (off the latency path); if unset, the fold falls
    # back to the gpt-5-mini pool. CONDENSE_WINDOW = turns kept verbatim before
    # folding into the summary; SEND_WINDOW (> CONDENSE) = turns actually sent to
    # the agent, a 2-turn buffer so an evicted turn stays verbatim until the
    # async fold absorbs it (lossless under rapid-fire).
    AZURE_OPENAI_NANO_ENDPOINT: str = ""
    AZURE_OPENAI_NANO_API_KEY: str = ""
    AZURE_OPENAI_NANO_DEPLOYMENT: str = "gpt-4-1-nano"
    AZURE_OPENAI_NANO_API_VERSION: str = "2024-10-21"
    # Optional pool of nano deployments (like AZURE_OPENAI_POOL_JSON) to spread the
    # summary load across keys. The single AZURE_OPENAI_NANO_* above is appended as
    # a box. JSON list of {"key_id","endpoint","api_key","deployment","tpm"}.
    AZURE_OPENAI_NANO_POOL_JSON: str = ""
    IN_CALL_SUMMARY_ENABLED: bool = True
    IN_CALL_SUMMARY_CONDENSE_WINDOW: int = 6
    IN_CALL_SUMMARY_SEND_WINDOW: int = 8
    IN_CALL_SUMMARY_MAX_TOKENS: int = 120

    # Standardized on gpt-5-mini (the shared LLM pool) — gpt-4.1-mini is retired.
    # These are fallback names only; live LLM calls route through the gpt-5-mini
    # pool (AZURE_OPENAI_POOL_JSON) via AzureGroundedLLM.complete.
    AZURE_OPENAI_AGENT_DEPLOYMENT: str = "gpt-5-mini"
    AZURE_OPENAI_AGENT_MODEL: str = "gpt-5-mini"
    AZURE_OPENAI_AGENT_API_VERSION: str = "2024-10-21"

    # Nokvo One Azure OpenAI realtime-mini deployment.
    # Per Azure Foundry docs (May 2026):
    #   - gpt-realtime-mini valid versions: 2025-10-06, 2025-12-15
    #   - Supported regions: canadacentral, centralus, eastus, eastus2, northcentralus,
    #     francecentral, norwayeast, swedencentral, switzerlandnorth, southindia
    #   - SKU: GlobalStandard everywhere; DataZoneStandard only in southindia.
    # 2025-08-28 belongs to the full gpt-realtime (not the mini) — pairing it with
    # gpt-realtime-mini returned DeploymentModelNotSupported. Use 2025-12-15 (newest mini).
    AZURE_OPENAI_REALTIME_MODEL: str = "gpt-realtime-mini"
    AZURE_OPENAI_REALTIME_MODEL_VERSION: str = "2025-12-15"
    AZURE_OPENAI_REALTIME_DEPLOYMENT: str = "gpt-realtime-mini"
    AZURE_OPENAI_REALTIME_SKU: str = "GlobalStandard"
    AZURE_OPENAI_REALTIME_REGION: str = "swedencentral"

    # Per-tenant chat fallback name — retired in favour of the shared gpt-5-mini
    # pool (all chat now routes through it). Kept only so legacy code paths that
    # read these have a sane default; no live call provisions gpt-4.1-mini.
    AZURE_OPENAI_CHAT_MODEL: str = "gpt-5-mini"
    AZURE_OPENAI_CHAT_MODEL_VERSION: str = "2025-04-14"
    AZURE_OPENAI_CHAT_DEPLOYMENT: str = "gpt-5-mini"
    AZURE_OPENAI_CHAT_SKU: str = "GlobalStandard"
    # Capacity is in K-TPM (1 unit = 1,000 tokens/min, 6 requests/min). The Azure
    # floor of 1 (1K TPM / 6 RPM) is far too small for conversational voice — a
    # single turn burns ~2-5K tokens and interactive testing trips the quota
    # every couple of utterances. Bumping to 500K TPM / 3,000 RPM gives any new
    # tenant headroom for sustained calls without falling back to the global pool.
    AZURE_OPENAI_CHAT_CAPACITY: int = 500
    AZURE_OPENAI_CHAT_REGION: str = "southindia"

    # Nokvo One per-tenant text embedding deployment (text-embedding-3-small in
    # South India). Deployed onto the same Azure OpenAI account as the chat model
    # during signup so the tenant's knowledge base embeds against its own
    # endpoint rather than the shared platform OpenAI key.
    # SKU note: southindia only offers GlobalStandard for text-embedding-3-small.
    AZURE_OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    AZURE_OPENAI_EMBEDDING_MODEL_VERSION: str = "1"
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT: str = "text-embedding-3-small"
    AZURE_OPENAI_EMBEDDING_SKU: str = "GlobalStandard"
    AZURE_OPENAI_EMBEDDING_CAPACITY: int = 1
    AZURE_OPENAI_EMBEDDING_REGION: str = "southindia"
    AZURE_OPENAI_EMBEDDING_API_VERSION: str = "2024-02-15-preview"
    AZURE_MANAGED_IDENTITY_CLIENT_ID: str = ""
    AZURE_TENANT_ID: str = ""
    AZURE_CLIENT_ID: str = ""
    AZURE_CLIENT_SECRET: str = ""
    # Secure-by-default: never silently fall back to a long-lived client secret when
    # managed identity is unavailable. Set True explicitly only in a local/dev env
    # that genuinely needs the secret fallback.
    ALLOW_AZURE_CLIENT_SECRET_FALLBACK: bool = False
    AZURE_PREFER_MANAGED_IDENTITY: bool = False
    AZURE_SHARED_STORAGE_ACCOUNT: str = ""
    AZURE_SHARED_STORAGE_CONTAINER: str = "nokvo-tenants"
    AZURE_SHARED_KEY_VAULT_NAME: str = ""
    AZURE_LOG_ANALYTICS_WORKSPACE_ID: str = ""
    ENFORCE_KEY_VAULT_AUDIT_LOGS: bool = False
    KEY_VAULT_SECRET_ROTATION_DAYS: int = 90
    
    # Provisioning Flags
    CREATE_STORAGE_PER_TENANT: bool = False
    CREATE_KEYVAULT_PER_TENANT: bool = False
    TWILIO_AUTO_PROVISION: bool = False
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_BASE_URL: str = "https://api.twilio.com/2010-04-01"
    SONIOX_API_KEY: str = ""
    SONIOX_STT_WEBSOCKET_URL: str = "wss://stt-rt.soniox.com/transcribe-websocket"
    SONIOX_STT_MODEL: str = "stt-rt-v4"
    SONIOX_STT_AUDIO_FORMAT: str = "auto"
    SONIOX_TTS_MODEL: str = "tts-rt-v1-preview"
    SONIOX_TTS_REST_URL: str = "https://tts-rt.soniox.com/tts"
    SONIOX_TTS_STREAM_URL: str = "wss://tts-rt.soniox.com/tts-websocket"
    SONIOX_TTS_VOICE: str = "Adrian"
    SONIOX_TTS_SAMPLE_RATE: int = 24000
    SONIOX_TTS_AUDIO_FORMAT: str = "wav"
    AGENT_VOICE_BACKEND: str = "sarvam_pipeline"
    SARVAM_API_KEY: str = ""
    SARVAM_STT_REST_URL: str = "https://api.sarvam.ai/speech-to-text"
    SARVAM_STT_WEBSOCKET_URL: str = "wss://api.sarvam.ai/speech-to-text/ws"
    SARVAM_STT_MODEL: str = "saaras:v3"
    SARVAM_STT_MODE: str = "transcribe"
    SARVAM_STT_SAMPLE_RATE: int = 16000
    SARVAM_STT_AUDIO_ENCODING: str = "audio/wav"
    # When True, the streaming STT connects in auto-detect mode (language-code
    # "unknown") so Sarvam reports the actual spoken language each segment and
    # the agent can follow a caller who simply starts speaking another language
    # mid-call. When False, the STT is pinned to the call's initial language —
    # marginally better single-language accuracy, but it CANNOT detect a switch
    # (a Telugu speaker on an English-seeded call gets transcribed as garbled
    # English forever). The blob/VAD path already auto-detects unconditionally.
    SARVAM_STT_AUTO_DETECT_LANGUAGE: bool = True
    SARVAM_TTS_REST_URL: str = "https://api.sarvam.ai/text-to-speech"
    SARVAM_TTS_STREAM_URL: str = "https://api.sarvam.ai/text-to-speech/stream"
    SARVAM_TTS_WEBSOCKET_URL: str = "wss://api.sarvam.ai/text-to-speech/ws"
    # HTTP streaming TTS (/text-to-speech/stream) is OFF by default: a direct
    # probe showed it returns ZERO audio chunks for this account/model while
    # still taking ~2s, so it was pure wasted latency on every sentence (each
    # then fell through to REST anyway). With it off we go straight to REST.
    # The working streaming path is the WebSocket API below
    # (SARVAM_TTS_WS_ENABLED); this HTTP-stream flag is kept only as a fallback
    # rung and stays off.
    SARVAM_TTS_STREAMING_ENABLED: bool = False
    # WebSocket streaming TTS (SARVAM_TTS_WEBSOCKET_URL) — the REAL streaming path.
    # When ON, stream_sentence_tts opens a per-sentence WS, streams audio as it's
    # synthesized (~360-390ms first-audio incl. ~150ms TLS connect, vs REST's full-
    # synthesis wait of ~1-2s), and falls back to REST on any error / first-audio-
    # deadline miss. Takes precedence over the dead HTTP stream. Protocol + output
    # rate (24kHz PCM16) + clean idle-gap termination verified against live Sarvam
    # (scripts/probe_sarvam_tts_ws.py). Rollback = set False (instant, no data change).
    SARVAM_TTS_WS_ENABLED: bool = True
    # Raw PCM16 @ SARVAM_TTS_SAMPLE_RATE so headerless streamed frames decode via
    # _parse_tts_audio's raw-PCM fallback with no bridge change (must match the
    # rate Sarvam actually emits — verified by the probe).
    SARVAM_TTS_WS_OUTPUT_CODEC: str = "linear16"
    # Bound on the WS connect/handshake so a hung connect trips the first-audio
    # watchdog and REST takes over instead of stalling the turn.
    SARVAM_TTS_WS_CONNECT_TIMEOUT_MS: int = 1500
    # End-of-utterance idle gap. The live probe (scripts/probe_sarvam_tts_ws.py)
    # showed Sarvam streams audio frames with a max inter-frame gap of ~54ms and
    # then keeps the session OPEN with NO terminal marker and NO close. So an idle
    # gap this long after the last frame IS the end-of-utterance signal (~5x the
    # observed max gap → won't cut synthesis short; adds only this much tail
    # latency before the next batch/socket-close).
    SARVAM_TTS_WS_IDLE_EOU_MS: int = 250
    SARVAM_TTS_MODEL: str = "bulbul:v3"
    SARVAM_TTS_SPEAKER: str = "ritu"
    # Per-language native speaker overrides (bulbul:v3). Empty = use
    # SARVAM_TTS_SPEAKER for every language (current behaviour). Populate with a
    # voice from Sarvam's bulbul:v3 catalog that sounds native for the language —
    # e.g. a Telugu-native speaker for `te` so Telugu calls don't speak in the
    # Hindi-leaning default voice. A bad/unknown id falls back to the default
    # speaker at synthesis time, so these are safe to A/B by ear.
    SARVAM_TTS_SPEAKER_TE: str = ""
    SARVAM_TTS_SPEAKER_HI: str = ""
    SARVAM_TTS_SPEAKER_EN: str = ""
    # Per-language pace multiplier on top of the per-tone prosody pace. The
    # bulbul:v3 Telugu voice renders noticeably slower than English at pace 1.0,
    # so we speed Telugu up by default. Applied even when a turn carries no
    # explicit pace, so every Telugu chunk gets the bump. 1.0 = no change.
    SARVAM_TTS_PACE_TE: float = 1.1
    SARVAM_TTS_PACE_HI: float = 1.0
    SARVAM_TTS_PACE_EN: float = 1.0
    SARVAM_TTS_SAMPLE_RATE: int = 24000
    SARVAM_TTS_AUDIO_CODEC: str = "wav"
    SARVAM_TTS_ENABLE_CACHED_RESPONSES: bool = False
    # Local Redis byte-cache for synthesized TTS audio (our own store, distinct
    # from Sarvam's server-side ENABLE_CACHED_RESPONSES). Opt-in per call: only the
    # deterministic scripted lines (APEX opener/outro) pass cache=True, so the same
    # audio is synthesized once and served from Redis on every later call — near-zero
    # first-audio latency + ~zero TTS COGS on those lines. Keyed by the normalized
    # text + every voice param that changes the bytes, so a hit is byte-identical.
    TTS_CACHE_ENABLED: bool = True
    TTS_CACHE_TTL_SECONDS: int = 604800  # 7 days
    # APEX Phase 3: when ON, the deterministic questionnaire SPEAKS each question
    # verbatim from its pre-translated per-language string (text_i18n) instead of
    # letting the LLM rephrase — so the exact string recurs across calls and hits
    # the TTS cache (huge COGS/latency win) while staying native hi/te. Behavior
    # change: question DELIVERY is scripted, but the LLM still handles re-asks,
    # non-answers, clarifying questions, and off-script replies (those turns fall
    # through to it). The dealbreaker gate is enforced deterministically
    # (agent_outbound_context.gate_failed) so verbatim delivery can't skip it. ON
    # by default; rollback = set False (instant, no data change). Campaigns whose
    # questions lack text_i18n degrade gracefully to the LLM-rephrase flow, so run
    # the translation backfill (scripts/backfill_apex_questionnaire_i18n.py) first.
    APEX_VERBATIM_QUESTIONS_ENABLED: bool = True
    # ── APEX humanization (all defaults = current behavior; each knob is an
    # independent env flip, rollback included) ─────────────────────────────────
    # Gap-target response timing on the DETERMINISTIC (verbatim) path only. The
    # cached reply otherwise lands with near-constant, unnaturally fast latency
    # every turn. Instead of a blind delay (which would stack on the EOU silence
    # wait and feel slow), we top up the total perceived gap — EOU tier wait +
    # delay + fetch — toward TARGET±JITTER: after a fast-tier EOU we add a
    # couple hundred ms; after the long continuation wait we add ZERO. 0 = off.
    APEX_TURN_GAP_TARGET_MS: int = 0  # rollout value ~800
    APEX_TURN_GAP_JITTER_MS: int = 120
    APEX_VERBATIM_DELAY_MAX_MS: int = 450
    # When a micro-ack will be spoken, the ACK is the gap-filler — clamp the
    # pre-speech sleep hard so the ack voice lands almost immediately.
    APEX_VERBATIM_DELAY_ACK_MAX_MS: int = 100
    # Deterministic micro-acknowledgments before verbatim questions ("సరే.",
    # "जी.", "Got it.") — seeded per call+question, never repeating the last
    # one, native-script pools only (en/hi/te). Fixes the interrogation cadence
    # without reintroducing the LLM stock-ack repetition this product banned.
    APEX_ACK_ENABLED: bool = False
    APEX_ACK_PROBABILITY: float = 0.45
    APEX_ACK_GAP_MS: int = 250  # silence between ack and question (Plivo only)
    APEX_ACK_GAP_JITTER_MS: int = 80
    # Rendition variants for cached scripted lines: N>1 salts the TTS cache key
    # (x_variant, stripped before the request reaches Sarvam) so each line has N
    # distinct natural takes; a call picks one via crc32(call_id) and keeps it
    # for the whole call. 1 = exactly today's single-rendition behavior.
    APEX_TTS_VARIANTS: int = 1
    # Optional audible spread across variants: variant k multiplies pace by
    # 1 + spread*(k-(N+1)/2). 0.0 = rely on synthesis temperature alone.
    APEX_TTS_VARIANT_PACE_SPREAD: float = 0.0
    # Deterministic opener template variants (per-call crc32 rotation). 1 = the
    # single current template.
    APEX_OPENER_VARIANTS: int = 1
    # Questionnaire translation register: when ON, the campaign-creation
    # translation prompt carries the spoken code-switch register rules
    # (language_style.py) so verbatim questions match the LLM turns' register
    # instead of sounding textbook-formal.
    APEX_I18N_SPOKEN_REGISTER: bool = True
    # Conversation-style rewrite (questionnaire_style): the wizard's
    # style-rewrite preview endpoint. OFF = the endpoint returns the
    # questionnaire unchanged (soft degrade, same shape as the tier gate);
    # campaigns already styled keep speaking their styled text either way.
    APEX_STYLE_REWRITE_ENABLED: bool = True
    # NOKVO APEX plan-driven pricing (Core/Growth/Pinnacle/Enterprise/Free Trial) +
    # request-gated onboarding (no self-serve signup; SuperAdmin creates accounts). When
    # OFF the new create/activate/plan-pricing surfaces are gated and the legacy slab path
    # is unaffected. Rollout completed 2026-08-07: `apex_plans_v1` applied to prod, then
    # flipped ON here (prod also carries an explicit ENABLE_APEX_PLANS=true env var).
    # NOTE: the self-serve signup + unknown-Google-login gates are now UNCONDITIONAL and no
    # longer keyed to this flag — turning it OFF re-opens plan pricing only, not signup.
    ENABLE_APEX_PLANS: bool = True
    # Optional CAPTCHA/verification gate on the public APEX request-access form. OFF by
    # default (rate-limit + per-email dedupe still apply); wire a provider before enabling.
    APEX_REQUEST_CAPTCHA: bool = False
    # Plivo telephony (the sole provider). The MASTER account creds; each tenant
    # gets its own Plivo subaccount + DID + Application created via the API.
    PLIVO_AUTH_ID: str = ""
    PLIVO_AUTH_TOKEN: str = ""
    PLIVO_API_BASE: str = "https://api.plivo.com/v1"

    # ── Razorpay (payment-gated onboarding: monthly subscription) ──────────────
    # KEY_SECRET stays backend-only (signs orders + verifies checkout signature);
    # the public KEY_ID is returned by the create-subscription endpoint so the
    # frontend never needs a build-time env var. WEBHOOK_SECRET is the dashboard
    # webhook secret used to verify the X-Razorpay-Signature on inbound webhooks.
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""
    RAZORPAY_WEBHOOK_SECRET: str = ""
    # Payment gateway master switch. When False (e.g. local dev), signup skips the
    # Razorpay payment step entirely: the org is activated + provisioned straight
    # away (the same convergence point a successful payment hits) and lands in the
    # onboarding wizard. Keep True in production.
    PAYMENTS_ENABLED: bool = True
    RAZORPAY_API_BASE: str = "https://api.razorpay.com/v1"
    # Optional: pin the Razorpay Plan IDs (created once in the dashboard). When
    # empty, the service lazily creates a monthly plan on first use and caches
    # the id per-process. Pin these in production so restarts don't make plans.
    RAZORPAY_PLAN_INBOUND_ONLY: str = ""
    RAZORPAY_PLAN_INBOUND_OUTBOUND: str = ""
    RAZORPAY_PLAN_APEX: str = ""
    # Public base used to build Application answer_url / media WS (defaults to the
    # request host when empty). e.g. https://api.nokvo.example
    PLIVO_WEBHOOK_BASE_URL: str = ""
    PLIVO_NUMBER_COUNTRY: str = "IN"
    # Outbound campaign dialer: how many calls stay live at once. The launch
    # places this many, then refills one-for-one as each call ends (driven by the
    # Plivo status webhook), so we never fire hundreds of simultaneous calls.
    OUTBOUND_DIAL_CONCURRENCY: int = 5
    # How long an outbound call RINGS before we cancel it (Plivo ring_timeout,
    # allowed 5-600, Plivo default 120). Kept SHORT so an unanswered call is cut
    # BEFORE carrier voicemail engages (~30-45s in India): the contact lands in
    # no_answer instead of "connecting" to a mailbox — which would burn Plivo
    # minutes, deduct the customer's Call Credits, and run the whole voice
    # pipeline against a greeting. In-call voicemail DETECTION stays as the
    # safety net for mailboxes that answer faster than this.
    OUTBOUND_RING_TIMEOUT_S: int = 25
    # Scalable per-row contact storage (outbound_campaign_contacts) instead of the
    # O(n) JSONB blob on outbound_campaigns.contacts. When ON, bulk campaigns ingest
    # via async COPY, dial via an advisory-locked indexed claim, and update contacts
    # with O(1) single-row writes — the path that scales to 1M contacts.
    # DEPLOY PREREQ: run `alembic upgrade head` AND
    # `scripts/backfill_campaign_contacts.py` before this takes effect, else the V2
    # queries hit missing columns / legacy blob campaigns read empty. Set False to
    # roll back to the legacy blob path instantly (no data change).
    CAMPAIGN_CONTACTS_V2: bool = True
    # Rows COPY-inserted per chunk during async ingestion (bounded memory).
    CAMPAIGN_INGEST_CHUNK: int = 10000
    # Content guard on campaign create/edit (the three text-entry endpoints).
    # off | warn | enforce. Default "enforce": an LLM verdict blocks campaigns
    # that disparage a third party, impersonate a company that isn't the
    # registered tenant, or harass/deceive recipients — the "company A degrades
    # company B" abuse. "warn" runs the verdict + operator alert but allows
    # (rollback if false positives bite); "off" skips the LLM call entirely.
    # Fail-open on LLM outage in every mode — creation never breaks on the pool.
    CAMPAIGN_CONTENT_MODERATION: str = "enforce"
    # X-Plivo-Signature-V2 validation on the Plivo webhook endpoints.
    # off | warn | enforce. Default "enforce": reject forged/unsigned call
    # webhooks. Auto-off when PLIVO_AUTH_TOKEN is unset (so local dev without a
    # token still works). A mismatch under enforce logs the COMPUTED signing URL
    # so a public_base_url/PLIVO_WEBHOOK_BASE_URL contract error is one-env-var
    # fixable; set to "warn" locally for tunneled dev if the signed URL differs.
    PLIVO_VALIDATE_SIGNATURES: str = "enforce"
    # Startup auto-repair of stale Application answer_urls. Default off —
    # the superadmin resync endpoint is the deliberate repair path; silent
    # mutation on boot is risky with rotating tunnels / multiple instances.
    PLIVO_WEBHOOK_AUTOSYNC: bool = False
    # A follow-up row stuck in_flight (status webhook never arrived) is
    # failed by the reconciliation sweep after this many minutes.
    FOLLOWUP_INFLIGHT_TIMEOUT_MINUTES: int = 30
    # Master kill switch for the Follow-up agent (auto-callbacks after a call
    # ends). Default OFF: we do NOT auto-call leads/customers back — not after a
    # cut call, a no-answer, or a promised callback, for inbound OR outbound.
    # When False: every enqueue path is a no-op, the dispatch loop never dials
    # (so even already-pending rows stay put), and the UI hides all follow-up
    # surfaces. Flip to True to bring the feature back.
    FOLLOWUP_AGENT_ENABLED: bool = False
    # Master kill switch for NOKVO ONE outbound calling. Outbound has moved to the
    # dedicated NOKVO APEX product, so Nokvo One is inbound-only. Default OFF:
    # Nokvo One orgs (product_tier="nokvo_one") cannot create/launch lead campaigns
    # or bulk-calling campaigns and the dialer never places a call for them; the UI
    # hides every outbound surface. APEX orgs (product_tier="nokvo_apex") share the
    # bulk-calling backend and are NOT affected — they keep dialing. Flip to True to
    # restore Nokvo One outbound.
    NOKVO_ONE_OUTBOUND_ENABLED: bool = False
    # Request 16 kHz from Plivo's <Stream> (its highest L16 rate): preserves HD/VoLTE
    # audio when present, and matches Sarvam STT's native 16 kHz input so there's no
    # lossy 8k→16k upsample. Better recognition, especially for spoken digits.
    PLIVO_DEFAULT_SAMPLE_RATE: int = 16000
    # WhatsApp (Plivo WhatsApp Business API). Off by default → every send is a
    # no-op until an operator enables it. The PRODUCTION sender number is per
    # tenant (``provider_status.plivo.whatsapp_number``, bound to that tenant's
    # subaccount/WABA); PLIVO_WHATSAPP_FROM is ONLY a fallback for local/master
    # testing and must never be relied on as a shared multi-tenant sender.
    PLIVO_WHATSAPP_ENABLED: bool = False
    PLIVO_WHATSAPP_FROM: str = ""

    # SMS delivery (Plivo Messaging) — the active brochure+location channel while
    # WhatsApp is off. Per-tenant sender is provider_status.plivo.sms_number;
    # PLIVO_SMS_FROM is a local/master test fallback only. India A2P needs DLT
    # (registered sender header + content templates) for delivery to +91 numbers.
    PLIVO_SMS_ENABLED: bool = True
    PLIVO_SMS_FROM: str = ""

    # DND (NCPR) scrubbing for bulk CSV calling. Before a bulk list is dialed,
    # numbers are checked against the National Customer Preference Register via a
    # cheap third-party API, and opted-out numbers are dropped. Provider = nixinfo
    # (https://nixinfo.in/dnd-checking-api — free tier; GET with query params,
    # JSON {number,status,preference}). Fail-OPEN by default: an API error never
    # blocks a campaign.
    #
    # The request param NAMES are configurable because each vendor differs and
    # nixinfo only hands you the exact endpoint + param names after you register —
    # fill DND_SCRUB_API_URL / *_PARAM from your nixinfo dashboard, no code change.
    # nixinfo's free DND lookup is single-number, so DND_SCRUB_BATCH_SIZE defaults
    # to 1 (one GET per number); raise it only for a vendor whose endpoint accepts
    # a comma-separated batch (e.g. manthanitsolutions/easygosms — set to ~200).
    #
    # NOTE: this is a convenience filter, not a substitute for RTM/DLT telemarketer
    # registration. Disabled unless creds are set.
    DND_SCRUB_ENABLED: bool = False
    DND_SCRUB_API_URL: str = "http://msg.msgclub.net/rest/services/sendSMS/getNdncNonNdncNumbers"
    DND_SCRUB_API_KEY: str = ""             # msgclub "AUTH_KEY"
    DND_SCRUB_API_PASS: str = ""            # unused for msgclub; blank → omitted
    DND_SCRUB_KEY_PARAM: str = "AUTH_KEY"
    DND_SCRUB_PASS_PARAM: str = ""          # msgclub needs no third param
    DND_SCRUB_MOBILE_PARAM: str = "mobileNumbers"
    DND_SCRUB_BATCH_SIZE: int = 200         # msgclub accepts comma-separated batches
    DND_SCRUB_TIMEOUT_S: float = 20.0
    TELNYX_API_KEY: str = ""
    TELNYX_BASE_URL: str = "https://api.telnyx.com/v2"
    TELNYX_APP_ID: str = ""
    TELNYX_MAX_PHONE_LINKS: int = 5
    AGENT_LLM_TIMEOUT_MS: int = 350
    AGENT_RETRIEVAL_TOP_K: int = 3
    AGENT_MAX_CONTEXT_CHARS: int = 3000
    AGENT_MIN_RELEVANCE_SCORE: float = 0.35
    AGENT_MIN_RELEVANCE_SCORE_SENSITIVE: float = 0.45
    AGENT_RETRIEVAL_TOP_K_SENSITIVE: int = 8
    AGENT_RAG_DEBUG: bool = False
    AGENT_RAG_MIN_QUERY_WORDS: int = 3
    # Cross-lingual translate-STT for retrieval. DISABLED by default: its only
    # consumer was Qdrant/KB retrieval, which is now retired (always returns
    # empty — see KB_RETIREMENT_REMAINING.md), so the up-to-800ms translate call
    # before the LLM on every non-English inbound turn was pure dead latency.
    # Left as a flag (not deleted) so retrieval can be re-armed if it ever
    # returns; the `retrieval_text` plumbing downstream stays intact + harmless.
    AGENT_TRANSLATE_FOR_RETRIEVAL_ENABLED: bool = False
    # Hard cap on the cross-lingual translate-STT call that blocks a non-English
    # turn before the LLM starts (only used when the flag above is re-enabled).
    # Sarvam translate usually returns in 0.5-0.8s; 800ms caps the stall so
    # Telugu/Hindi first-audio latency stays low (we fall back to the native
    # transcript for retrieval when it times out).
    AGENT_TRANSLATE_TIMEOUT_MS: int = 800
    # Per-call conversation memory. Set to 10 min so a 7-min call always has
    # full history available even with no agent activity for a stretch
    # (caller on hold, on-screen confirmation, etc.). Refreshed on every
    # append, so an active call effectively never times out.
    AGENT_SESSION_HISTORY_TTL_SECONDS: int = 600
    AGENT_SESSION_HISTORY_MAX_TURNS: int = 30
    AGENT_INTENT_CLASSIFIER_TIMEOUT_MS: int = 500
    # Voice agent latency tuning
    FILLER_TRIGGER_MS: int = 400          # Play filler only if the real answer is not ready quickly
    TTS_SEGMENT_IDLE_DONE_MS: int = 750   # Treat a TTS segment as done after audio goes idle
    TTS_SEGMENT_FIRST_AUDIO_TIMEOUT_MS: int = 2500
    # Per-sentence Sarvam STREAMING first-audio deadline (NOKVO One voice path).
    # Sarvam streaming first-audio is normally ~80-180ms; if no audio frame has
    # arrived within this deadline the stream is degraded — abandon it and fall
    # back to REST immediately rather than letting one slow sentence stall the
    # whole turn for seconds behind the serial TTS pump. Only bounds FIRST audio;
    # once audio is flowing the rest of the sentence streams uninterrupted.
    VOICE_TTS_STREAM_FIRST_AUDIO_DEADLINE_MS: int = 1200
    AGENT_LLM_STREAM_TOTAL_MS: int = 6000 # Max total LLM stream wait
    AGENT_TOPIC_CONTINUITY_OVERLAP: float = 0.35  # Word overlap to reuse last chunks
    AGENT_MAX_FIRST_SENTENCE_CHARS: int = 110     # Force TTS dispatch after this many chars
    # Inbound automatic gain control before STT. Forwarded telephony audio is
    # quiet/variable-level; AGC boosts it toward a target so Sarvam STT resolves
    # speech cleanly. Disable instantly via env if it ever regresses latency.
    VOICE_STT_AGC_ENABLED: bool = True
    VOICE_STT_AGC_TARGET_DBFS: float = -20.0
    # RNNoise speech denoise on the caller→STT path (pyrnnoise). Best-effort:
    # when the library is missing or fails to import, the feature disables
    # itself with one warning and audio passes through untouched.
    VOICE_STT_DENOISE_ENABLED: bool = True
    VOICE_EOU_DEBOUNCE_MS: int = 1200     # Trailing-off speech: silence before firing (continuation tier; + bonus below)
    VOICE_EOU_CONTINUATION_BONUS_MS: int = 1100  # Extra wait when speech likely continues
    # Adaptive endpointing (see _eou_completeness_tier). Most turns answer the
    # agent (question / time / yes-no) → fire fast; ambiguous declaratives wait a
    # moderate amount; only trailing-off speech keeps the long DEBOUNCE+BONUS.
    # Start conservative; tighten COMPLETE only as the cut-off guardrail allows.
    VOICE_EOU_COMPLETE_MS: int = 400      # High-confidence-complete utterance → fire fast
    VOICE_EOU_NEUTRAL_MS: int = 650       # Ambiguous declarative → moderate wait (650ms FLOOR: below
    # this, Indian-telephony jitter risks clipping callers mid-utterance; the dynamic latency guard
    # below absorbs the difference — the filler just fires ~250ms later and we still land sub-1s.)
    # ── Sub-1s latency guard ──────────────────────────────────────────────────
    # End-of-caller-speech → first agent audio is held strictly under
    # VOICE_LATENCY_BUDGET_MS on fast/neutral turns. If the real LLM answer has
    # not yielded its first sentence in time, a short LOCALIZED filler/bridge is
    # spoken so SOME audio lands within budget (an audible hold counts). The
    # guard's wait is computed dynamically from how much of the budget the EOU
    # silence-wait already consumed (see _run_text_turn), so it fires early
    # enough to keep eos→audio < budget. VOICE_FIRST_SENTENCE_TIMEOUT_MS is the
    # CEILING used only when no end-of-speech anchor is available (e.g. manual /
    # proactive turns).
    VOICE_LATENCY_BUDGET_MS: int = 1000           # The hard eos→first-audio budget
    VOICE_LATENCY_GUARD_FLOOR_MS: int = 120       # Min wait before firing the filler (give the real LLM a chance)
    VOICE_LATENCY_GUARD_TTS_MARGIN_MS: int = 120  # Reserve for TTS dispatch→audible-audio
    # Grace window peeked AFTER the budget elapses, BEFORE committing a filler.
    # The single ordered TTS pump plays a filler fully before the real content,
    # so a filler fired a beat before the real answer just delays that answer by
    # its own ~1s of playback. If a real sentence lands within this window we
    # skip the filler and speak the content directly.
    VOICE_LATENCY_GUARD_CONTENT_GRACE_MS: int = 150
    VOICE_FIRST_SENTENCE_TIMEOUT_MS: int = 750    # Ceiling for the filler wait when no eos anchor exists
    VOICE_LLM_STREAM_RETRY_ATTEMPTS: int = 2
    VOICE_LLM_STREAM_MAX_RETRY_WAIT_MS: int = 350
    # ── Outbound humanization (barge-in immunity + pace) ──────────────────────
    # OUTBOUND only (gated on an active campaign). On a phone call the agent must
    # not cut itself off for a cough or a background "uh-huh": a barge-in is
    # honoured only when the caller's speech is SUSTAINED past this window. Below
    # it, a speech_start→speech_end blip is treated as noise and the agent keeps
    # talking. Inbound keeps the immediate cut (these don't apply).
    VOICE_BARGE_IN_MIN_MS: int = 300       # sustained-speech gate before a real barge-in cancels the agent
    # Slightly slower outbound delivery reads as more deliberate/human. Applied
    # as a multiplier on the per-tone pace, composed with the per-language
    # SARVAM_TTS_PACE_* factor. 1.0 = unchanged. Inbound is unaffected.
    VOICE_OUTBOUND_PACE_FACTOR: float = 0.95
    # OUTBOUND end-of-utterance tiers (default to the global values → no change
    # unless overridden). Lets ops tune outbound turn-taking without touching
    # inbound or the sub-1s latency budget. See _eou_completeness_tier.
    VOICE_EOU_COMPLETE_MS_OUTBOUND: int = 400
    VOICE_EOU_NEUTRAL_MS_OUTBOUND: int = 650
    VOICE_EOU_DEBOUNCE_MS_OUTBOUND: int = 1200
    VOICE_EOU_CONTINUATION_BONUS_MS_OUTBOUND: int = 1100

    # Billing and Usage Tracking
    ORGANIZATION_BASE_MONTHLY_COST_USD: float = 5.00
    QDRANT_COLLECTION_MONTHLY_COST_USD: float = 8.00
    SHARED_REDIS_NAMESPACE_MONTHLY_COST_USD: float = 2.00
    BLOB_PREFIX_MONTHLY_COST_USD: float = 1.50
    KEY_VAULT_REFERENCE_MONTHLY_COST_USD: float = 1.00
    AZURE_OPENAI_MONTHLY_COST_USD: float = 12.00
    SONIOX_STT_MONTHLY_COST_USD: float = 4.00
    SONIOX_TTS_MONTHLY_COST_USD: float = 4.00
    TWILIO_SUBACCOUNT_MONTHLY_COST_USD: float = 3.00
    COST_PER_VOICE_MINUTE_USD: float = 0.0200
    COST_PER_STT_MINUTE_USD: float = 0.0120
    COST_PER_TWILIO_MINUTE_USD: float = 0.0130
    COST_PER_TTS_1K_CHARS_USD: float = 0.0180
    # Plivo telephony COGS: a FLAT fee per CONNECTED call (duration > 0), in
    # rupees — INR-NATIVE, never multiplied by USD_TO_INR. This replaces the
    # old per-minute telephony pricing in per-call COGS (compute_cogs_inr);
    # COST_PER_TWILIO_MINUTE_USD above remains only for the legacy monthly
    # estimator in tenant_billing_service.
    COST_PLIVO_PER_CONNECTED_CALL_INR: float = 0.6
    # GPT-5-mini list price (the shared pool model): $0.25/1M input, $2.00/1M
    # output, cached input ~$0.025/1M. The previous $3.00 / $12.00 per-1M
    # placeholders overstated LLM cost by ~8-12x. Confirm against the actual
    # Azure invoice for the deployed model and adjust if a different tier.
    COST_PER_LLM_INPUT_1K_TOKENS_USD: float = 0.00025
    COST_PER_LLM_OUTPUT_1K_TOKENS_USD: float = 0.0020
    COST_PER_LLM_CACHED_INPUT_1K_TOKENS_USD: float = 0.000025
    # Per-call COGS components (STT/LLM/TTS) are persisted in INR on CallCost.
    # The per-unit rates above are vendor list prices in USD, so we convert
    # with this FX rate. NOTE: COST_PER_TWILIO_MINUTE_USD is the legacy Plivo
    # per-minute rate — NO LONGER used for per-call COGS (telephony is now the
    # flat INR connect fee above); it survives only for tenant_billing_service's
    # monthly estimate. The STT/TTS rates price Sarvam (saaras:v3 / bulbul:v3).
    # The FX rate is hardcoded for this MVP: a static rate means the INR margin
    # figures shown in the SuperAdmin console slowly decouple from the actual
    # USD Azure/Sarvam/Plivo invoices as the rate drifts. Revisit with a live
    # FX feed or INR-native vendor rates if call volume scales up.
    USD_TO_INR: float = 96.0

    # Blended COGS per BILLED minute (INR), used by the SuperAdmin console to
    # compute margin as ``revenue − (COGS_PER_MINUTE_INR × billed_minutes)``.
    # A flat per-minute rate gives a complete, consistent COGS for every call
    # (the per-component ``cost_*_inr`` columns are NULL on uninstrumented/older
    # calls, which would undercount COGS and overstate margin). Derived from the
    # fixed STT+telephony floor (~₹2.15/min) plus typical LLM+TTS (~₹1/min);
    # tune as vendor rates / call density change.
    COGS_PER_MINUTE_INR: float = 3.2

    # ── NOKVO ONE SDK — P2 generic Offering catalog (staged Option B rollout) ──
    # Both default OFF → zero behaviour change. Operational sequence:
    #   enable DUAL_WRITE → run offering_backfill_v1 → verify → enable READ
    #   (gated by the byte-parity tests in test_offering_adapters.py) → later,
    #   stop writing the legacy real_estate_projects / clinic_services tables.
    OFFERINGS_DUAL_WRITE: bool = False  # CRUD also mirrors projects/services into `offerings`
    OFFERINGS_READ: bool = False        # loaders read the catalog from `offerings` via adapters

    # ── NOKVO ONE SDK — P3 staged sector enablement ──
    # Comma-separated business types to OFFER at onboarding in addition to
    # real_estate (always on). Empty = real_estate only (unchanged). Flip one
    # sector at a time, e.g. "clinics" then "clinics,ecommerce".
    ENABLED_BUSINESS_TYPES: str = ""


    # Provider APIs
    QDRANT_URL: str = ":memory:"
    QDRANT_API_KEY: str = ""
    QDRANT_COLLECTION_PREFIX: str = "tenant"
    QDRANT_VECTOR_SIZE: int = 1536
    QDRANT_MAX_POINTS_PER_TENANT: int = 100000
    QDRANT_MAX_UPSERT_POINTS: int = 2000

    # MCP / generated database tools
    MCP_SQL_MAX_ROWS: int = 100
    MCP_SQL_STATEMENT_TIMEOUT_MS: int = 10000

    # Privacy audit logging for voice/transcript data. Kept fail-open by default
    # so older databases without the audit table do not break live calls.
    VOICE_DATA_AUDIT_ENFORCED: bool = False
    
    OPENAI_API_KEY: str = ""
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    OPENAI_EMBEDDING_BATCH_SIZE: int = 96
    OPENAI_EMBEDDING_TIMEOUT_SECONDS: float = 4.0
    # The OpenAI SDK defaults to 2 retries with exponential backoff on 429,
    # which can push a single embed call past 20s during rate-limit bursts.
    # Cap retries so voice latency stays bounded.
    OPENAI_EMBEDDING_MAX_RETRIES: int = 1
    OPENAI_EMBEDDING_ALLOW_ZERO_FALLBACK: bool = False

    # Voice agent answer cache (Redis)
    AGENT_ANSWER_CACHE_ENABLED: bool = True
    AGENT_ANSWER_CACHE_TTL_SECONDS: int = 300

    # End-of-booking SMS confirmation offer. Disabled by default because SMS
    # dispatch isn't wired into the platform yet — offering it then silently
    # not sending is a worse caller experience than not offering at all. Each
    # tenant can opt in via provider_status["agent_offer_sms_confirmation"] =
    # True once their SMS gateway is connected.
    NOKVO_AGENT_OFFER_SMS_CONFIRMATION: bool = False

    # Tier-2 LLM intent classifier budget. Measured round-trip latency for
    # the gpt-4.1-mini classifier call is ~1.5–2.4s; the previous 800ms cap
    # timed out ~100% of the time, so every Tier-2 turn paid the wait and
    # got a "classifier timeout" fallback. 2500ms captures the full
    # distribution. Only Tier-2 (regex-miss) turns are affected.
    NOKVO_INTENT_CLASSIFIER_TIMEOUT_MS: int = 2500

    # LangSmith prompt observability. When LANGSMITH_API_KEY is unset the
    # tracer module (``app/services/langsmith_tracer.py``) is a silent
    # no-op everywhere — zero latency, no SDK calls on the hot path. When
    # set, every LLM call (voice turns, intent classifier, outcome
    # classifier, condenser) emits a trace into the named project. The
    # *_V2 flag also gets exported as an env var by ``init_tracer()`` so
    # the SDK's implicit hooks pick up the same toggle.
    LANGSMITH_API_KEY: str = ""
    LANGSMITH_PROJECT: str = "nokvo-one"
    # NOVA (the APEX chat assistant) traces into its OWN project so text-chat
    # turns never mix with voice_call traces — different shape, different
    # debugging workflow (chat sessions vs call transcripts).
    LANGSMITH_NOVA_PROJECT: str = "nokvo-nova"
    LANGSMITH_TRACING_V2: bool = False
    # Blank → SDK defaults to LangSmith cloud (api.smith.langchain.com).
    # Override for self-hosted deployments.
    LANGSMITH_ENDPOINT: str = ""
    # Workspace (tenant) id. Required when the API key is *org-scoped*: the
    # SDK sends it as the X-Tenant-Id header on ingest, without which
    # /runs/* writes 403 even though reads succeed. Blank is fine for a
    # workspace-scoped key (the tenant is baked into the key).
    LANGSMITH_WORKSPACE_ID: str = ""

    # ── OpenTelemetry trace-id correlation (voice hot path) ────────────────
    # Off by default. When enabled, a real per-call trace id is generated and
    # stamped on every log line + persisted on call_costs + cross-linked into
    # the LangSmith run. The exporter is separate: "none" generates ids without
    # shipping spans (no collector needed), "console" prints them, "otlp" ships
    # to OTEL_EXPORTER_OTLP_ENDPOINT.
    OTEL_ENABLED: bool = False
    OTEL_EXPORTER: str = "none"  # none | console | otlp
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""
    OTEL_SERVICE_NAME: str = "nokvo-one"

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

settings = Settings()


def validate_security_config() -> list[str]:
    """Deploy-time security warnings (PRODUCTION only). Returned, not raised, so
    startup logs loudly without refusing to boot — mirrors
    ``app.services.public_url.validate_public_base_url``. Surfaces the fail-open
    knobs that must be set/closed in prod."""
    warnings: list[str] = []
    if not settings.is_production:
        return warnings
    if not (settings.RAZORPAY_WEBHOOK_SECRET or "").strip():
        warnings.append(
            "RAZORPAY_WEBHOOK_SECRET is not set — the payment webhook is fail-closed "
            "(503) in prod, so payments can't confirm via webhook. Set the secret."
        )
    if (settings.PLIVO_VALIDATE_SIGNATURES or "").strip().lower() != "enforce":
        warnings.append(
            f"PLIVO_VALIDATE_SIGNATURES is '{settings.PLIVO_VALIDATE_SIGNATURES}', not "
            "'enforce' — forged Plivo call webhooks would be accepted. Set it to 'enforce'."
        )
    if (settings.CAMPAIGN_CONTENT_MODERATION or "").strip().lower() != "enforce":
        warnings.append(
            f"CAMPAIGN_CONTENT_MODERATION is '{settings.CAMPAIGN_CONTENT_MODERATION}', not "
            "'enforce' — campaigns that disparage/attack another company or scam "
            "recipients would be accepted. Set it to 'enforce'."
        )
    origin = settings.EXPECTED_ORIGIN or ""
    if "localhost" in origin or "127.0.0.1" in origin or not origin.strip():
        warnings.append(
            f"EXPECTED_ORIGIN '{origin}' is localhost/empty — CORS will reject the real "
            "frontend. Set it to the production origin."
        )
    if settings.JWT_LEGACY_SECRET_FALLBACK:
        warnings.append(
            "JWT_LEGACY_SECRET_FALLBACK is on — raw-SECRET_KEY tokens are accepted on any "
            "tier. Turn it off once legacy (pre-tier-migration) tokens have expired."
        )
    if (settings.SECRET_KEY or "").strip().lower() in {"", "changeme", "secret", "dev", "development", "test"}:
        warnings.append("SECRET_KEY is empty or a known dev value — set a strong, unique secret in prod.")
    return warnings
