# Knowledge Base retirement — remaining work (PR-by-PR)

Status as of this checkpoint. The **provisioning overhaul** (shared GPT-5-mini LLM
pool + slimmed onboarding) is done and shipped; this doc tracks only the **complete
KB / document-RAG retirement** tail, which was deliberately stopped here because the
remaining cuts touch the live voice hot-path, the generic `agent_*` product, the
2,290-line auth surface, the frontend, and DB migrations — none validatable from a
headless backend. Do these **with the app + frontend running**, leaf-first, one PR each.

## SCOPE (confirmed): retire KB **everywhere**, incl. the live Agent Studio product
The KB is shared by two products: **nokvo_one** (real-estate/clinic agent) and a
separate, live **Agent Studio** runtime (`agent_voice_stream_service` /
`agent_realtime_voice_service` / `agent_runtime_service`, gated by
`AGENT_VOICE_BACKEND`, exposed via `connect_public` + the telephony bridges). Both
are to be cut off the KB so `agent_knowledge_service` / `qdrant_service` /
`text_embedding_service` / `azure_blob_service` can be fully deleted.

## ✅ nokvo_one side — DONE & verified (591 tests, backend boots)
- Campaign RAG no-op'd; KB HTTP API deleted; KB answer-cards removed from turn_router.
- Vertical-prompt config extracted to `agent_config_keys.py` (decoupled from KB).
- **`nokvo_one_voice_pipeline` is now FULLY off the KB** — `retrieve()` returns empty,
  the Qdrant/embedding calls are gone, and the `qdrant_service` / `text_embedding_service`
  / `agent_knowledge_service` imports are removed. (The unreachable retrieval body is
  left behind a clear early-return marker; excise it during PR4.)
  nokvo_one answers purely from its vertical system prompt.

## ⏳ Cross-product remainder (large; needs the app + frontend running to validate)

### Already done & verified (591 tests pass, backend boots)
- **LLM pool (A/B)** — `app/services/llm_pool.py`; `AzureGroundedLLM.complete/stream/complete_global` resolve via the pool.
- **Slim provisioner (D)** — signup no longer provisions Azure OpenAI / Resource Group / Key Vault. (`nokvo_one_provisioning_service`).
- **KB partial (C):**
  - Campaign RAG retired — `outbound_campaign_service._index_campaign_script` is a no-op; qdrant/embedding/KB imports removed.
  - KB HTTP API deleted — `app/api/nokvo_one_knowledge_base.py` removed + unregistered in `app/main.py`.
  - KB answer-card fast-path removed from `app/services/pipeline/turn_router.py`.
  - **Config decoupled (keystone)** — new `app/services/agent_config_keys.py` holds the vertical-prompt plumbing (`AGENT_SINGLE_PROMPT_CONFIG_KEY`, `AGENT_POLICY_CARDS_KEY`, `AGENT_POLICY_VERSION_KEY`, `policy_version`). `agent_runtime_bundle` + `nokvo_one_voice_pipeline` now import config from there, not from the KB module.

## Key findings that shape the remaining work
- `qdrant_service`, `text_embedding_service`, `azure_blob_service` are **shared infra**, not KB-only (also used by the dormant integration/toolkit layer). They can only be deleted after every consumer is gone.
- The KB module's **config write-side helpers** (`configure_single_prompt_voice_agent`, `disable_single_prompt_voice_agent`, `get_single_prompt_setup`, `single_prompt_config`, `_policy_cards`, `_build_policy_cards`) have **no remaining external callers** (their caller was the deleted KB API) → they drop with the module. Re-verify before deleting.
- The actual vertical prompts come from `vertical_prompts.py`, not the legacy `single_prompt_voice_agent` provider_status config — confirm the bundle's `single_prompt_*` reads are still meaningful or can be simplified.

---

## PR 1 — Strip document-RAG from `nokvo_one_voice_pipeline.py`
- Remove the retrieval machinery: `_retrieve` / `_retrieve_dual` / `_search` / `_chunks_from` / `_map_point` (≈ lines 1100–1300) and the grounded/RAG branch in the turn flow that calls them.
- Remove `from app.services.agent_knowledge_service import AGENT_KNOWLEDGE_SOURCE_TYPE` and any `TextEmbeddingService` / `QdrantService` imports + usages in this file.
- Result: the agent answers from its vertical system prompt only (no chunk grounding).
- **Validate (live):** an inbound call still answers; backend boots; `pytest tests/nokvo_one -q`.

## PR 2 — Generic `agent_*` product (decide its fate first)
This product (`agent_voice_stream_service`, `agent_realtime_voice_service`, `agent_runtime_service`, `agent_runtime_health`) is RAG-centric. If retiring:
- Remove RAG from `agent_runtime_service` (`generate_grounded_answer` / `stream_grounded_answer` + the grounded prompt branch).
- Remove `find_answer_card` / `test_retrieval` / grounded streaming from `agent_voice_stream_service` + `agent_realtime_voice_service`.
- `agent_runtime_health`: drop `AgentKnowledgeService._documents(...)`; repoint `policy_version` → `agent_config_keys.policy_version`.
- Repoint any remaining `AgentKnowledgeService.policy_version` calls (`agent_voice_stream_service:1401`) → `agent_config_keys`.
- **Validate (live):** Agent Studio voice still works (or confirm the product is dead and remove it wholesale).

## PR 3 — Integration / toolkit layer (dormant)
- Delete `database_integration_service`, `crm_integration_service`, `erp_integration_service`, `shipping_integration_service`, `zoho_desk_service`, `toolkit_generator_service`, `app/services/mcp_toolkit/`.
- `app/api/organization_auth.py` (2,290 lines): remove the integration endpoints (`/database|/crm|/erp|/shipping/...` providers/status/connect, the toolkit generate/review/registry endpoints) + their imports + the `OrganizationToolkit*` / `Organization{Database,CRM,ERP,Shipping}*` schemas.
- `policy_decision_engine.py`: remove CRM/ERP integration usage.
- **Validate (live):** **login + org-auth endpoints still work** (this is the auth surface — highest risk).

## PR 4 — Delete the shared modules
- After PRs 1–3 remove all consumers: delete `agent_knowledge_service.py`, `qdrant_service.py`, `text_embedding_service.py`, `azure_blob_service.py`.
- Remove their imports from `app/scripts/delete_nokvo_one_tenants.py` and `azure_tenant_provisioning_service.py`.
- Re-confirm the config write-side helpers (see findings) have no callers.

## PR 5 — Drop Qdrant + Blob provisioning + cost/columns
- `nokvo_one_provisioning_service`: remove Step 1 (Blob) + Step 2 (Qdrant) → provisioner = **Redis namespace + Exotel placeholder only**. Drop `qdrant_*` / blob fields from the provider_status seed + return.
- Update `tests/nokvo_one/test_provisioning_rollback.py` for the 2-step flow.
- Remove `QDRANT_COLLECTION_MONTHLY_COST_USD` + `BLOB_PREFIX_MONTHLY_COST_USD` from `tenant_billing_service` + `config.py`.
- Alembic migration: drop the now-unused `TenantResources` columns (`qdrant_*`, `storage_*`, `blob_prefix`, `key_vault_*`, `secret_refs`).

## PR 6 — Frontend + DB
- Remove the Knowledge Base tab/views + API-client calls in `frontend/`.
- Alembic migration to drop any `agent_knowledge_*` tables.

---

## Then: Phase E — deprovision existing tenants
Idempotent, dry-run-first script reusing the existing rollback helpers
(`AzureOpenAIChatService.delete_chat_account`, `AzureResourceGroupService.delete_resource_group`,
`_delete_qdrant_collection`, `_delete_blob_prefix`) to tear down legacy per-tenant
Azure OpenAI accounts (+ RG), Key Vault, Qdrant collections, and blob prefixes for
tenants provisioned before the overhaul.
