"""The main turn orchestrators: answer_text (route - retrieve - compose -
complete) and stream_answer_sentences (streaming voice variant).

Extracted from nokvo_one_voice_pipeline.py (turn_router helpers pattern:
functions taking ``helpers`` receive ``NokvoOneVoicePipeline`` and call
sibling statics through it, so class-attribute monkeypatches keep
working). The class keeps delegating wrappers - no API change.
"""
from __future__ import annotations

import logging
from app.core.config import settings
from app.models.tenant_resources import TenantResources
from app.services.agent_outbound_context import (
    OutboundCampaignContext,
    compose_outbound_system_section,
    update_outbound_memory,
)
from app.services.agent_runtime_bundle import RuntimeBundle, get_bundle as get_runtime_bundle
from app.services.agent_session_store import AgentSessionStore
from app.services.fast_intent_router import IntentResult
from app.services.tool_flow_questions import build_tool_flow_questions
from app.services.azure_grounded_llm import (
    AzureGroundedLLM,
    NokvoOneAgentRateLimited,
    NokvoOneAgentRuntimeError,
)
from app.services.pipeline.text_norm import _normalize
from app.services.sarvam_voice_service import SarvamVoiceService
from sqlalchemy.ext.asyncio import AsyncSession
from time import perf_counter
from typing import Any, AsyncIterator
import asyncio

logger = logging.getLogger(__name__)


async def answer_text(
    helpers: Any,
    tenant_res: TenantResources,
    query: str,
    *,
    db: AsyncSession | None = None,
    top_k: int | None = None,
    latency_budget_ms: int | None = None,
    response_language: str | None = None,
    conversation_history: list[dict[str, str]] | None = None,
    call_id: str | None = None,
    retrieval_text: str | None = None,
    campaign_id: str | None = None,
    campaign_goal: str | None = None,
    company_name: str | None = None,
    outbound_context: OutboundCampaignContext | None = None,
) -> dict[str, Any]:
    started = perf_counter()
    user_text = _normalize(query)
    language = SarvamVoiceService.normalize_language(response_language)

    # Parallel turn startup: history fetch, state fetch, and the
    # per-tenant runtime bundle all run concurrently. Without this
    # primer the pipeline would fetch each value separately as it was
    # needed, paying a full Redis round trip every time.
    turn_cache = await helpers._prime_turn_cache(db, tenant_res, call_id)
    history = (conversation_history or []) + list(turn_cache.get("history") or [])

    # English-translated transcript (when caller spoke a non-English
    # language). retrieval_text holds the translate-STT output from the
    # voice-stream service — use it for extractor + classifier where
    # English patterns are required, while user_text stays the source
    # of truth for prompts.
    english_text = retrieval_text if retrieval_text and _normalize(retrieval_text) != user_text else None
    retrieval_query = helpers.retrieval_query_for(user_text, english_text)

    # Intent-first route: greeting/thanks/goodbye/policy-card paths
    # terminate the turn before any cache/Qdrant/LLM work.
    route = await helpers._route_turn(
        tenant_res,
        user_text,
        language=language,
        company_name=company_name,
        call_id=call_id,
        english_text=english_text,
        db=db,
        top_k=top_k,
        campaign_id=campaign_id,
        turn_cache=turn_cache,
        outbound_context=outbound_context,
    )
    intent_result: IntentResult = route["intent_result"]
    bundle: RuntimeBundle = turn_cache["bundle"]
    single_prompt_guidance = bundle.single_prompt_guidance
    projects_block, active_projects = await helpers._projects_block_for_bundle(db, bundle)
    services_block = await helpers._services_block_for_bundle(db, bundle)
    if route["route"] in {"template", "answer_card", "policy_card"}:
        answer = route["answer"]
        await helpers._apply_route_state(tenant_res, call_id, route)
        await AgentSessionStore.append_turn(tenant_res, call_id, user_text, answer)
        total_ms = int((perf_counter() - started) * 1000)
        helpers._log_route(
            {
                "tenant_id": tenant_res.tenant_id,
                "call_id": call_id,
                "text": user_text[:120],
                "intent": intent_result.intent,
                "topic": intent_result.topic,
                "route": route["route"],
                "sensitive": route.get("sensitive"),
                "cache_hit": False,
                "qdrant_called": False,
                "llm_called": False,
                "policy_card_id": route.get("policy_card_id"),
                "decision_code": route.get("decision_code"),
                "single_prompt_enabled": bool(single_prompt_guidance),
                "detected_entities": route.get("detected_entities"),
                "state_slot": route.get("state_slot"),
                "route_reason": route.get("route_reason"),
                "total_ms": total_ms,
            }
        )
        return {
            "query": query,
            "answer": answer,
            "refused": False,
            "citations": [],
            "chunks": [],
            "runtime": {
                "graph": "nokvo_rag_pipeline",
                "mode": route["route"],
                "model": None,
                "response_language": language,
                "latency_ms": total_ms,
            },
            "retrieval": {"used": False, "cache_hit": False, "relevant_count": 0},
            "intent": {"type": intent_result.intent, "topic": intent_result.topic, "should_retrieve": False},
            "tool_calls": route.get("tool_calls") or [],
        }

    # RAG fallback path — only cache non-sensitive queries.
    cached = None
    if not intent_result.sensitive:
        cached = await AgentSessionStore.get_cached_answer(
            tenant_res,
            retrieval_query,
            language,
            campaign_id=campaign_id,
            call_context=call_id,
        )
    if cached and cached.get("answer"):
        answer = str(cached["answer"])
        await AgentSessionStore.append_turn(tenant_res, call_id, user_text, answer)
        total_ms = int((perf_counter() - started) * 1000)
        helpers._log_route(
            {
                "tenant_id": tenant_res.tenant_id,
                "call_id": call_id,
                "intent": intent_result.intent,
                "topic": intent_result.topic,
                "route": "cache",
                "sensitive": False,
                "cache_hit": True,
                "qdrant_called": False,
                "llm_called": False,
                "single_prompt_enabled": bool(single_prompt_guidance),
                "total_ms": total_ms,
            }
        )
        return {
            "query": query,
            "answer": answer,
            "refused": False,
            "citations": cached.get("citations") or [],
            "chunks": cached.get("chunks") or [],
            "runtime": {
                "graph": "nokvo_rag_pipeline",
                "mode": "semantic_cache",
                "model": settings.AZURE_OPENAI_AGENT_MODEL,
                "response_language": language,
                "latency_ms": total_ms,
            },
            "retrieval": {"used": False, "cache_hit": True, "relevant_count": len(cached.get("chunks") or [])},
            "intent": {"type": intent_result.intent, "topic": intent_result.topic, "should_retrieve": False},
        }

    # Reuse the probe retrieval done by _route_turn when it overrode
    # an out_of_scope decision — avoids a duplicate embed+Qdrant call
    # on the hot path. (answer_text path — chat/non-voice surface, so
    # code_switching defaults to False.)
    retrieval = await helpers._await_prefetched_retrieval(route)
    if not retrieval:
        retrieval = await helpers.retrieve(
            tenant_res,
            retrieval_query,
            db=db,
            top_k=top_k,
            campaign_id=campaign_id,
            intent_result=intent_result,
            english_text=english_text,
        )
    chunks = retrieval.get("chunks") or []
    citations = [
        {
            "document_id": chunk.get("document_id"),
            "document_name": chunk.get("document_name"),
            "chunk_id": chunk.get("chunk_id"),
            "score": chunk.get("score"),
        }
        for chunk in chunks
    ]
    if not chunks and not single_prompt_guidance:
        answer, refused = helpers._no_context_answer(
            user_text,
            intent=intent_result.intent,
            language=language,
            company_name=company_name,
        )
        await AgentSessionStore.append_turn(tenant_res, call_id, user_text, answer)
        return {
            "query": query,
            "answer": answer,
            "refused": refused,
            "citations": [],
            "chunks": [],
            "runtime": {
                "graph": "nokvo_rag_pipeline",
                "mode": "no_context_refusal" if refused else "conversation",
                "model": settings.AZURE_OPENAI_AGENT_MODEL,
                "response_language": language,
                "latency_ms": int((perf_counter() - started) * 1000),
            },
            "retrieval": {
                "used": True,
                "cache_hit": False,
                "retrieved_count": 0,
                "relevant_count": 0,
                "top_score": 0.0,
                "relevance_threshold": settings.AGENT_MIN_RELEVANCE_SCORE,
                "skipped_reason": retrieval.get("refusal"),
            },
            "intent": {"type": "RAG_ALWAYS_ON", "should_retrieve": True, "reason": "pre-indexed tenant retrieval"},
        }

    project_names_for_prompt = [p.name for p in active_projects if p.name]
    field_questions_prompt = helpers._field_questions_prompt_for_bundle(
        bundle, language=language, project_names=project_names_for_prompt
    )
    memory_block_v2 = ""
    strategy_block_v2 = ""
    if conversational_memory is not None:
        try:
            memory_block_v2 = conversational_memory.compose_prompt_block(
                language=language,
                business_type=bundle.organization_industry,
            )
        except Exception:
            memory_block_v2 = ""
        try:
            from app.services.conversation_strategy import compose_strategy_block

            # A record-capture flow (site visit / lead) owns field collection
            # via the FIELD-COLLECTION SCRIPT (operator-configured fields).
            # Tell the strategy layer to suppress its hardcoded "ask exactly
            # X" Next-Best-Action directive so it doesn't compete with — and
            # override — the configured schema.
            _tf_for_strategy = (turn_cache.get("state") or {}).get("tool_flow") or {}
            _capture_flow_active = bool(
                _tf_for_strategy.get("active")
                and not _tf_for_strategy.get("completed")
                and not _tf_for_strategy.get("deferred_for_kb")
                and str(_tf_for_strategy.get("flow_key") or "")
                in ("real_estate_site_visit", "leads_create")
            )
            strategy_block_v2 = compose_strategy_block(
                conversational_memory,
                business_type=bundle.organization_industry,
                is_outbound=outbound_context is not None,
                language=language,
                focus_project=helpers._focus_project_summary(
                    active_projects, conversational_memory
                ),
                company_name=company_name,
                capture_flow_active=_capture_flow_active,
                # Cold-open greeting+discovery agenda only on the genuine first
                # turn (no prior agent reply) — else it re-greets every turn.
                is_first_turn=not any((t or {}).get("role") == "assistant" for t in (history or [])),
            )
        except Exception:
            strategy_block_v2 = ""
    # Live tool_flow snapshot — when an inbound-style booking flow is
    # active during an outbound call, surface its slot state so the LLM
    # is forced to drive the next slot instead of free-form chatting.
    # For real-estate INBOUND, we also surface it (FSM site_visit mode).
    from app.services.real_estate_agent_fsm import (
        current_mode as _fsm_current_mode,
        enabled_for_business_type as _fsm_enabled,
        mode_block_for_prompt as _fsm_mode_block,
    )
    from app.services.agent_outbound_context import render_booking_flow_state

    tf_state = dict((turn_cache.get("state") or {}).get("tool_flow") or {})
    _fsm_active_inbound = (
        outbound_context is None
        and bundle is not None
        and _fsm_enabled(bundle.organization_industry)
    )
    tf_bundle: dict[str, Any] | None = None
    if bundle is not None and tf_state.get("active") and (
        outbound_context is not None or _fsm_active_inbound
    ):
        try:
            tf_bundle = build_tool_flow_questions(
                bundle.organization_industry,
                bundle.overrides,
                bundle.custom_tabs,
            )
        except Exception:
            tf_bundle = None

    # Compose the inbound FSM mode block. Empty when org isn't real-estate
    # so other industries keep their existing behaviour unchanged.
    agent_mode_block_inbound: str | None = None
    if _fsm_active_inbound:
        session_state = turn_cache.get("state") or {}
        # Brochure-on-WhatsApp request → stay in whatsapp_mode across the short
        # exchange (sticky over recent turns) so a follow-up "yeah" / number
        # readout doesn't drop back into lead-collection.
        from app.services.tool_flow_policy import brochure_intent_active as _brochure_active
        if _brochure_active(user_text, turn_cache.get("history")):
            _tf_wa = dict(session_state.get("tool_flow") or {})
            _tf_wa["whatsapp_intent"] = {"kind": "brochure"}
            session_state = {**session_state, "tool_flow": _tf_wa}
        current = _fsm_current_mode(session_state, memory=conversational_memory)
        pending_label: str | None = None
        pending_question: str | None = None
        # Both active capture modes drive a flow's slots; pick the flow that
        # matches the mode so the prompt names the next pending field.
        _mode_flow_key = {
            "site_visit": "real_estate_site_visit",
            "lead_capture": "leads_create",
        }.get(current)
        if _mode_flow_key and tf_bundle is not None:
            flow_def = ((tf_bundle.get("flows") or {}).get(_mode_flow_key) or {})
            pending_slot_key = str(tf_state.get("pending_slot") or "")
            for slot in (flow_def.get("slots") or []):
                if not isinstance(slot, dict):
                    continue
                skey = str(slot.get("key") or "")
                if pending_slot_key and skey != pending_slot_key:
                    continue
                if not pending_slot_key and (tf_state.get("collected") or {}).get(skey):
                    continue
                pending_label = str(slot.get("label") or skey)
                questions = slot.get("questions") or {}
                pending_question = str(
                    questions.get(language) or questions.get("en") or ""
                )
                break
        blocks: list[str] = [
            _fsm_mode_block(
                current,
                pending_slot_label=pending_label,
                pending_slot_question=pending_question,
                memory=conversational_memory,
            )
        ]
        if _mode_flow_key:
            booking_block = render_booking_flow_state(
                tf_state, tf_bundle, language=language
            )
            if booking_block:
                blocks.append(booking_block)
        # In whatsapp_mode, surface the caller's own number (ANI) so the agent
        # passes it straight to the brochure tool instead of asking for it.
        if current == "whatsapp":
            _cp = str((session_state.get("caller_phone") or "")).strip()
            if _cp:
                blocks.append(
                    "# CALLER'S WHATSAPP NUMBER (already known — do not ask)\n"
                    f"Send the brochure to {_cp} — the number they're calling from. "
                    "Pass this exact number to the brochure tool."
                )
        agent_mode_block_inbound = "\n\n".join(b for b in blocks if b)

    # Clinic mode block. Clinics use the voice_turn_policy appointment FSM
    # (not tool_flow), so derive the clinic mode from appointment state +
    # the latest utterance (triage detection). Persona/guardrail only —
    # complementary to the slot engine.
    if agent_mode_block_inbound is None and outbound_context is None and bundle is not None:
        try:
            from app.services.clinic_agent_fsm import (
                enabled_for_business_type as _clinic_enabled,
                current_mode as _clinic_mode,
                mode_block_for_prompt as _clinic_block,
            )

            if _clinic_enabled(bundle.organization_industry):
                _c_state = turn_cache.get("state") or {}
                _c_appt = _c_state.get("appointment") or {}
                _c_pending = str(_c_appt.get("pending_slot") or "").replace("_", " ").strip() or None
                agent_mode_block_inbound = _clinic_block(
                    _clinic_mode(_c_state, latest_user_text=user_text),
                    pending_slot_label=_c_pending,
                )
        except Exception:
            pass

    messages = helpers._messages(
        user_text,
        chunks,
        language=language,
        history=history,
        company_name=company_name,
        campaign_goal=campaign_goal,
        single_prompt_guidance=single_prompt_guidance,
        outbound_context=outbound_context,
        outbound_memory=update_outbound_memory(
            dict((turn_cache.get("state") or {}).get("outbound_memory") or {}),
            caller_text=user_text,
        ) if outbound_context is not None else None,
        conversational_memory_block=memory_block_v2,
        conversation_strategy_block=strategy_block_v2,
        field_questions_prompt=field_questions_prompt,
        projects_block=projects_block,
        services_block=services_block,
        tool_flow_state=tf_state if outbound_context is not None else None,
        tool_flow_bundle=tf_bundle,
        turn_index=(len(history) // 2) + 1 if outbound_context is not None else None,
        agent_mode_block=agent_mode_block_inbound,
        conversational_memory=conversational_memory,
        business_type=bundle.organization_industry if bundle is not None else None,
    )
    timeout = max(0.8, (latency_budget_ms or settings.AGENT_LLM_TIMEOUT_MS) / 1000)
    llm_error = None
    try:
        answer = await asyncio.wait_for(AzureGroundedLLM.complete(tenant_res, messages), timeout=timeout)
        answer = helpers._sanitize_answer(answer) or helpers._refusal(language)
        refused = helpers._is_refusal(answer, language)
    except Exception as exc:
        llm_error = str(exc)[:240]
        answer = helpers._refusal(language)
        refused = True
    await AgentSessionStore.append_turn(tenant_res, call_id, user_text, answer)
    # Sensitive intents (cancellation/refund/payment/account/food_quality)
    # bypass caching even when the chunk metadata looks fine — the answer
    # may be tied to a transient policy version or user-specific phrasing.
    cache_eligible = not intent_result.sensitive and not llm_error and helpers._cacheable(retrieval_query, answer, chunks)
    if cache_eligible:
        await AgentSessionStore.set_cached_answer(
            tenant_res,
            retrieval_query,
            language,
            {"answer": answer, "citations": citations, "chunks": chunks[:2]},
            campaign_id=campaign_id,
            call_context=call_id,
        )
    total_ms = int((perf_counter() - started) * 1000)
    helpers._log_route(
        {
            "tenant_id": tenant_res.tenant_id,
            "call_id": call_id,
            "intent": intent_result.intent,
            "topic": intent_result.topic,
            "route": ("single_prompt_rag" if not chunks else "qdrant_rag") if not refused else "refusal",
            "sensitive": intent_result.sensitive,
            "cache_hit": False,
            # Drive from actual retrieval result instead of hardcoded True —
            # the single_prompt_rag fallback doesn't always hit qdrant.
            "qdrant_called": bool(chunks),
            "llm_called": True,
            "single_prompt_enabled": bool(single_prompt_guidance),
            "total_ms": total_ms,
            "top_score": max((float(c.get("score") or 0.0) for c in chunks), default=0.0),
            "chunk_count": len(chunks),
        }
    )
    return {
        "query": query,
        "answer": answer,
        "refused": refused,
        "citations": citations,
        "chunks": chunks,
        "runtime": {
            "graph": "nokvo_rag_pipeline",
            "mode": "single_prompt_grounded" if not chunks else "grounded_rag",
            "model": settings.AZURE_OPENAI_AGENT_MODEL,
            "response_language": language,
            "latency_ms": total_ms,
            "llm_error": llm_error,
        },
        "retrieval": {
            "used": True,
            "cache_hit": False,
            "retrieved_count": len(chunks),
            "relevant_count": len(chunks),
            "top_score": max((float(chunk.get("score") or 0.0) for chunk in chunks), default=0.0),
            "relevance_threshold": retrieval.get("min_score") or settings.AGENT_MIN_RELEVANCE_SCORE,
        },
        "intent": {"type": intent_result.intent, "topic": intent_result.topic, "should_retrieve": True, "sensitive": intent_result.sensitive},
    }


async def stream_answer_sentences(
    helpers: Any,
    tenant_res: TenantResources,
    query: str,
    *,
    db: AsyncSession | None = None,
    top_k: int | None = None,
    response_language: str | None = None,
    call_id: str | None = None,
    retrieval_text: str | None = None,
    campaign_id: str | None = None,
    campaign_goal: str | None = None,
    company_name: str | None = None,
    code_switching: bool = False,
    outbound_context: OutboundCampaignContext | None = None,
    covered_objectives: list[str] | None = None,
    outbound_memory: dict[str, Any] | None = None,
    conversational_memory: Any = None,
) -> AsyncIterator[dict[str, Any]]:
    started = perf_counter()
    user_text = _normalize(query)
    language = SarvamVoiceService.normalize_language(response_language)

    turn_cache = await helpers._prime_turn_cache(db, tenant_res, call_id)
    history = list(turn_cache.get("history") or [])

    english_text = retrieval_text if retrieval_text and _normalize(retrieval_text) != user_text else None
    retrieval_query = helpers.retrieval_query_for(user_text, english_text)

    route = await helpers._route_turn(
        tenant_res,
        user_text,
        language=language,
        company_name=company_name,
        call_id=call_id,
        english_text=english_text,
        db=db,
        top_k=top_k,
        campaign_id=campaign_id,
        turn_cache=turn_cache,
        code_switching=code_switching,
        outbound_context=outbound_context,
    )
    intent_result: IntentResult = route["intent_result"]
    bundle: RuntimeBundle = turn_cache["bundle"]
    single_prompt_guidance = bundle.single_prompt_guidance
    projects_block, active_projects = await helpers._projects_block_for_bundle(db, bundle)
    services_block = await helpers._services_block_for_bundle(db, bundle)
    # Outbound is a different agent — *no* inbound short-circuits apply.
    # Template smalltalk ("Sure, go ahead." for a "Yes") is the worst
    # offender: it derails the outbound flow because the agent should be
    # advancing the pitch on every turn, not handing the floor back.
    # answer_card and policy_card are inbound tenant data and equally
    # wrong here. Run every utterance through the LLM with the outbound
    # system fragment + campaign brief so it can drive the call.
    _outbound_active = bool(outbound_context) and outbound_context.is_proactive
    _deterministic_routes = (
        {"template"}  # outbound templates only come from deterministic tool flows here
        if _outbound_active
        else {"template", "answer_card", "policy_card"}
    )
    prompt_outbound_memory = outbound_memory
    if _outbound_active and prompt_outbound_memory is None:
        state_for_memory = dict(turn_cache.get("state") or {})
        prompt_outbound_memory = update_outbound_memory(
            dict(state_for_memory.get("outbound_memory") or {}),
            caller_text=user_text,
        )
    if route["route"] in _deterministic_routes:
        answer = route["answer"]
        yield {"type": "sentence", "text": answer, "language": language, "cache_hit": False}
        await helpers._apply_route_state(tenant_res, call_id, route)
        await AgentSessionStore.append_turn(tenant_res, call_id, user_text, answer)
        # A deterministic route delivered an answer — reset the
        # clarification escalation counter if it had been bumped.
        await helpers._apply_clarification(
            tenant_res,
            call_id,
            turn_cache=turn_cache,
            user_text=user_text,
            route=route["route"],
            intent=intent_result.intent,
            refused=False,
            chunks=[],
            state_slot=route.get("state_slot"),
            language=language,
            original_answer=answer,
        )
        total_ms = int((perf_counter() - started) * 1000)
        helpers._log_route(
            {
                "tenant_id": tenant_res.tenant_id,
                "call_id": call_id,
                "text": user_text[:120],
                "intent": intent_result.intent,
                "topic": intent_result.topic,
                "route": route["route"],
                "sensitive": route.get("sensitive"),
                "cache_hit": False,
                "qdrant_called": False,
                "llm_called": False,
                "policy_card_id": route.get("policy_card_id"),
                "decision_code": route.get("decision_code"),
                "single_prompt_enabled": bool(single_prompt_guidance),
                "detected_entities": route.get("detected_entities"),
                "state_slot": route.get("state_slot"),
                "route_reason": route.get("route_reason"),
                "total_ms": total_ms,
            }
        )
        yield {
            "type": "final",
            "answer": answer,
            "refused": False,
            "chunks": [],
            "citations": [],
            "runtime": {
                "graph": "nokvo_rag_pipeline",
                "mode": route["route"],
                "latency_ms": total_ms,
            },
            "intent": {"type": intent_result.intent, "topic": intent_result.topic, "should_retrieve": False},
            "tool_calls": route.get("tool_calls") or [],
        }
        return

    # Smalltalk LLM mode: chat naturally, no RAG, no chunks, no grounding.
    # The smalltalk system prompt explicitly forbids inventing world or
    # company facts — so the LLM can say "yeah that's frustrating" but
    # not "the weather is sunny" or "our policy is X".
    if route["route"] == "smalltalk_llm":
        classified = route.get("classified") or {}
        sentiment = str(classified.get("sentiment") or "neutral")
        history = await helpers._turn_history(tenant_res, call_id, turn_cache)
        messages = helpers._messages_smalltalk(
            user_text,
            language=language,
            history=history,
            company_name=company_name,
            sentiment=sentiment,
            single_prompt_guidance=single_prompt_guidance,
        )
        answer_parts: list[str] = []
        try:
            async for chunk in AzureGroundedLLM.stream_prosody(
                tenant_res,
                messages,
                max_tokens=120,
                retry_attempts=settings.VOICE_LLM_STREAM_RETRY_ATTEMPTS,
                max_retry_wait_s=settings.VOICE_LLM_STREAM_MAX_RETRY_WAIT_MS / 1000,
            ):
                sentence = helpers._sanitize_answer(chunk.text)
                if not sentence:
                    continue
                answer_parts.append(sentence)
                yield {"type": "sentence", "text": sentence, "language": language, "tone": chunk.tone}
        except NokvoOneAgentRateLimited as exc:
            logger.warning(f"NOKVO-LLM: smalltalk rate-limited: {exc}")
            fallback = helpers._rate_limited_reply(language)
            answer_parts = [fallback]
            yield {"type": "sentence", "text": fallback, "language": language, "tone": "warm"}
        except Exception as exc:
            # Smalltalk LLM failed — fall back to a friendly template so
            # the caller never gets dead air.
            fallback = {
                "hi": "ठीक है, बताइए मैं कैसे मदद कर सकता हूँ?",
                "ta": "சரி, எப்படி உதவ முடியும்?",
                "te": "సరే, ఎలా సహాయం చేయగలను?",
            }.get(language, "Mm-hm. What can I help with?")
            answer_parts = [fallback]
            yield {"type": "sentence", "text": fallback, "language": language, "tone": "warm"}
        answer = " ".join(answer_parts).strip() or helpers._refusal(language)
        await AgentSessionStore.append_turn(tenant_res, call_id, user_text, answer)
        total_ms = int((perf_counter() - started) * 1000)
        helpers._log_route(
            {
                "tenant_id": tenant_res.tenant_id,
                "call_id": call_id,
                "text": user_text[:120],
                "intent": intent_result.intent,
                "topic": intent_result.topic,
                "route": "smalltalk_llm",
                "sensitive": False,
                "cache_hit": False,
                "qdrant_called": False,
                "llm_called": True,
                "single_prompt_enabled": bool(single_prompt_guidance),
                "total_ms": total_ms,
                "classified": classified,
            }
        )
        yield {
            "type": "final",
            "answer": answer,
            "refused": False,
            "chunks": [],
            "citations": [],
            "runtime": {"graph": "nokvo_rag_pipeline", "mode": "smalltalk_llm", "latency_ms": total_ms},
            "intent": {"type": intent_result.intent, "topic": intent_result.topic, "should_retrieve": False},
        }
        return

    # Outbound is a *different* agent: it doesn't read the inbound KB,
    # and the tenant's inbound single-prompt guidance does not apply.
    # The campaign's own brief + persona fields are the entire source.
    # We synthesize chunks from the doc text so the existing prompt-
    # assembly path keeps working without a second LLM call site.
    outbound_mode = _outbound_active
    if outbound_mode:
        permission_reply = helpers._outbound_post_opener_permission_reply(
            user_text,
            language=language,
            history=history,
            outbound_context=outbound_context,
            covered_objectives=covered_objectives,
        )
        if permission_reply:
            yield {"type": "sentence", "text": permission_reply, "language": language, "tone": "question"}
            await AgentSessionStore.append_turn(tenant_res, call_id, user_text, permission_reply)
            total_ms = int((perf_counter() - started) * 1000)
            helpers._log_route(
                {
                    "tenant_id": tenant_res.tenant_id,
                    "call_id": call_id,
                    "text": user_text[:120],
                    "intent": intent_result.intent,
                    "topic": intent_result.topic,
                    "route": "outbound_permission_discovery",
                    "sensitive": False,
                    "cache_hit": False,
                    "qdrant_called": False,
                    "llm_called": False,
                    "single_prompt_enabled": False,
                    "total_ms": total_ms,
                }
            )
            yield {
                "type": "final",
                "answer": permission_reply,
                "refused": False,
                "chunks": [],
                "citations": [],
                "runtime": {
                    "graph": "nokvo_rag_pipeline",
                    "mode": "outbound_permission_discovery",
                    "latency_ms": total_ms,
                },
                "intent": {"type": intent_result.intent, "topic": intent_result.topic, "should_retrieve": False},
                "tool_calls": [],
            }
            return
        single_prompt_guidance = ""
        chunks = helpers._chunks_from_outbound_doc(outbound_context)
        citations = [
            {
                "document_id": chunk.get("document_id"),
                "document_name": chunk.get("document_name"),
                "chunk_id": chunk.get("chunk_id"),
                "score": chunk.get("score"),
            }
            for chunk in chunks
        ]
        retrieval = {"chunks": chunks, "refusal": None}
    else:
        cached = None
        if not intent_result.sensitive:
            cached = await AgentSessionStore.get_cached_answer(
                tenant_res,
                retrieval_query,
                language,
                campaign_id=campaign_id,
                call_context=call_id,
            )
        if cached and cached.get("answer"):
            answer = str(cached["answer"])
            yield {"type": "sentence", "text": answer, "language": language, "cache_hit": True}
            await AgentSessionStore.append_turn(tenant_res, call_id, user_text, answer)
            yield {
                "type": "final",
                "answer": answer,
                "refused": False,
                "chunks": cached.get("chunks") or [],
                "citations": cached.get("citations") or [],
                "runtime": {"graph": "nokvo_rag_pipeline", "mode": "semantic_cache", "latency_ms": int((perf_counter() - started) * 1000)},
            }
            return

        # Reuse the probe retrieval done by _route_turn when it overrode
        # an out_of_scope decision — avoids a duplicate embed+Qdrant call
        # on the hot path.
        retrieval = await helpers._await_prefetched_retrieval(route)
        if not retrieval:
            retrieval = await helpers.retrieve(
                tenant_res,
                retrieval_query,
                db=db,
                top_k=top_k,
                campaign_id=campaign_id,
                intent_result=intent_result,
                english_text=english_text,
                dual_retrieval=code_switching,
            )
        chunks = retrieval.get("chunks") or []
        citations = [
            {
                "document_id": chunk.get("document_id"),
                "document_name": chunk.get("document_name"),
                "chunk_id": chunk.get("chunk_id"),
                "score": chunk.get("score"),
            }
            for chunk in chunks
        ]
    if not chunks and not single_prompt_guidance and not outbound_mode:
        answer, refused = helpers._no_context_answer(
            user_text,
            intent=intent_result.intent,
            language=language,
            company_name=company_name,
        )
        # Clarification FSM: escalate once the caller has produced
        # several consecutive low-information turns. After two the
        # agent offers concrete options; after three it hands off to
        # support instead of looping the same "sorry, missed that"
        # reply.
        answer, clarify_action, _ = await helpers._apply_clarification(
            tenant_res,
            call_id,
            turn_cache=turn_cache,
            user_text=user_text,
            route="no_context_refusal",
            intent=intent_result.intent,
            refused=refused,
            chunks=[],
            state_slot=None,
            language=language,
            original_answer=answer,
        )
        yield {"type": "sentence", "text": answer, "language": language}
        await AgentSessionStore.append_turn(tenant_res, call_id, user_text, answer)
        yield {
            "type": "final",
            "answer": answer,
            "refused": refused,
            "chunks": [],
            "citations": [],
            "runtime": {
                "graph": "nokvo_rag_pipeline",
                "mode": "no_context_refusal",
                "clarification": clarify_action,
                "latency_ms": int((perf_counter() - started) * 1000),
            },
        }
        return

    project_names_for_prompt = [p.name for p in active_projects if p.name]
    field_questions_prompt = helpers._field_questions_prompt_for_bundle(
        bundle, language=language, project_names=project_names_for_prompt
    )
    memory_block = ""
    strategy_block = ""
    if conversational_memory is not None:
        try:
            memory_block = conversational_memory.compose_prompt_block(
                language=language,
                business_type=bundle.organization_industry,
            )
        except Exception:
            memory_block = ""
        try:
            from app.services.conversation_strategy import compose_strategy_block

            _tf_for_strategy = (turn_cache.get("state") or {}).get("tool_flow") or {}
            _capture_flow_active = bool(
                _tf_for_strategy.get("active")
                and not _tf_for_strategy.get("completed")
                and not _tf_for_strategy.get("deferred_for_kb")
                and str(_tf_for_strategy.get("flow_key") or "")
                in ("real_estate_site_visit", "leads_create")
            )
            strategy_block = compose_strategy_block(
                conversational_memory,
                business_type=bundle.organization_industry,
                is_outbound=outbound_context is not None,
                language=language,
                focus_project=helpers._focus_project_summary(
                    active_projects, conversational_memory
                ),
                company_name=company_name,
                capture_flow_active=_capture_flow_active,
                is_first_turn=not any((t or {}).get("role") == "assistant" for t in (history or [])),
            )
        except Exception:
            strategy_block = ""
    # Outbound's factual scope is the campaign brief alone. Suppress the
    # inbound real-estate project inventory block here so the campaign
    # doc_text + agent_prompt are the only product/pricing/availability
    # source the LLM sees. Inbound paths keep their inventory pin.
    _projects_block_for_messages = "" if outbound_mode else projects_block
    # Live tool_flow snapshot — surfaced for outbound and for real-estate
    # inbound (FSM site_visit mode).
    from app.services.real_estate_agent_fsm import (
        current_mode as _fsm_current_mode,
        enabled_for_business_type as _fsm_enabled,
        mode_block_for_prompt as _fsm_mode_block,
    )
    from app.services.agent_outbound_context import render_booking_flow_state

    tf_state_for_msg = dict((turn_cache.get("state") or {}).get("tool_flow") or {})
    _fsm_active_inbound_streaming = (
        not outbound_mode
        and bundle is not None
        and _fsm_enabled(bundle.organization_industry)
    )
    tf_bundle_for_msg: dict[str, Any] | None = None
    if bundle is not None and tf_state_for_msg.get("active") and (
        outbound_mode or _fsm_active_inbound_streaming
    ):
        try:
            tf_bundle_for_msg = build_tool_flow_questions(
                bundle.organization_industry,
                bundle.overrides,
                bundle.custom_tabs,
            )
        except Exception:
            tf_bundle_for_msg = None

    agent_mode_block_inbound_streaming: str | None = None
    if _fsm_active_inbound_streaming:
        _ss_stream = turn_cache.get("state") or {}
        from app.services.tool_flow_policy import brochure_intent_active as _brochure_active
        if _brochure_active(user_text, turn_cache.get("history")):
            _tf_wa = dict(_ss_stream.get("tool_flow") or {})
            _tf_wa["whatsapp_intent"] = {"kind": "brochure"}
            _ss_stream = {**_ss_stream, "tool_flow": _tf_wa}
        current = _fsm_current_mode(_ss_stream, memory=conversational_memory)
        pending_label: str | None = None
        pending_question: str | None = None
        _mode_flow_key = {
            "site_visit": "real_estate_site_visit",
            "lead_capture": "leads_create",
        }.get(current)
        if _mode_flow_key and tf_bundle_for_msg is not None:
            flow_def = (
                (tf_bundle_for_msg.get("flows") or {}).get(_mode_flow_key) or {}
            )
            pending_slot_key = str(tf_state_for_msg.get("pending_slot") or "")
            for slot in (flow_def.get("slots") or []):
                if not isinstance(slot, dict):
                    continue
                skey = str(slot.get("key") or "")
                if pending_slot_key and skey != pending_slot_key:
                    continue
                if not pending_slot_key and (tf_state_for_msg.get("collected") or {}).get(skey):
                    continue
                pending_label = str(slot.get("label") or skey)
                questions = slot.get("questions") or {}
                pending_question = str(
                    questions.get(language) or questions.get("en") or ""
                )
                break
        blocks: list[str] = [
            _fsm_mode_block(
                current,
                pending_slot_label=pending_label,
                pending_slot_question=pending_question,
                memory=conversational_memory,
            )
        ]
        if _mode_flow_key:
            booking_block = render_booking_flow_state(
                tf_state_for_msg, tf_bundle_for_msg, language=language
            )
            if booking_block:
                blocks.append(booking_block)
        if current == "whatsapp":
            _cp = str((_ss_stream.get("caller_phone") or "")).strip()
            if _cp:
                blocks.append(
                    "# CALLER'S WHATSAPP NUMBER (already known — do not ask)\n"
                    f"Send the brochure to {_cp} — the number they're calling from. "
                    "Pass this exact number to the brochure tool."
                )
        agent_mode_block_inbound_streaming = "\n\n".join(b for b in blocks if b)

    # Clinic mode block (streaming path) — mirror the non-streaming branch.
    if agent_mode_block_inbound_streaming is None and not outbound_mode and bundle is not None:
        try:
            from app.services.clinic_agent_fsm import (
                enabled_for_business_type as _clinic_enabled,
                current_mode as _clinic_mode,
                mode_block_for_prompt as _clinic_block,
            )

            if _clinic_enabled(bundle.organization_industry):
                _c_state = turn_cache.get("state") or {}
                _c_appt = _c_state.get("appointment") or {}
                _c_pending = str(_c_appt.get("pending_slot") or "").replace("_", " ").strip() or None
                agent_mode_block_inbound_streaming = _clinic_block(
                    _clinic_mode(_c_state, latest_user_text=query),
                    pending_slot_label=_c_pending,
                )
        except Exception:
            pass

    messages = helpers._messages(
        user_text,
        chunks,
        language=language,
        history=history,
        company_name=company_name,
        campaign_goal=campaign_goal,
        single_prompt_guidance=single_prompt_guidance,
        outbound_context=outbound_context,
        covered_objectives=covered_objectives,
        outbound_memory=prompt_outbound_memory,
        conversational_memory_block=memory_block,
        conversation_strategy_block=strategy_block,
        field_questions_prompt=field_questions_prompt,
        projects_block=_projects_block_for_messages,
        services_block=("" if outbound_mode else services_block),
        tool_flow_state=tf_state_for_msg if outbound_mode else None,
        tool_flow_bundle=tf_bundle_for_msg,
        turn_index=(len(history) // 2) + 1 if outbound_mode else None,
        agent_mode_block=agent_mode_block_inbound_streaming,
        conversational_memory=conversational_memory,
        business_type=bundle.organization_industry if bundle is not None else None,
    )
    # Prosody-aware streaming: the LLM is asked to wrap each sentence in a
    # [tone]…[/tone] tag. The parser strips the tags and emits one chunk
    # per sentence-or-tone-boundary so we can synthesize each with
    # matching pace/pitch/loudness.
    answer_parts: list[str] = []
    rate_limited = False
    # Outbound: hard token cap so the model physically cannot generate a
    # paragraph reply. Hindi / Telugu / Tamil tokenise to 2-3× more tokens
    # per equivalent sentence than English, so the 48-token English cap
    # would cut them mid-clause. Lift the cap proportionally for those
    # languages so the 1-2 sentence target still ends cleanly. Inbound
    # keeps the default 180.
    _lang_code = (language or "en").split("-")[0].lower()[:2]
    if outbound_mode:
        _stream_max_tokens = 96 if _lang_code in {"hi", "te", "ta", "bn", "kn", "mr"} else 48
    else:
        _stream_max_tokens = 180
    try:
        async for chunk in AzureGroundedLLM.stream_prosody(
            tenant_res,
            messages,
            max_tokens=_stream_max_tokens,
            retry_attempts=settings.VOICE_LLM_STREAM_RETRY_ATTEMPTS,
            max_retry_wait_s=settings.VOICE_LLM_STREAM_MAX_RETRY_WAIT_MS / 1000,
        ):
            sentence = helpers._sanitize_answer(chunk.text)
            if not sentence:
                continue
            answer_parts.append(sentence)
            yield {"type": "sentence", "text": sentence, "language": language, "tone": chunk.tone}
    except NokvoOneAgentRateLimited as exc:
        # Azure deployment is throttled. Tell the caller specifically —
        # "I'm busy, try again" sounds far better than "I do not have
        # enough information", and it's the actual truth.
        logger.warning(f"NOKVO-LLM: stream rate-limited: {exc}")
        rate_limited = True
        fallback = helpers._rate_limited_reply(language)
        answer_parts = [fallback]
        yield {"type": "sentence", "text": fallback, "language": language, "tone": "warm"}

    if rate_limited:
        answer = answer_parts[0]
        refused = False
    else:
        answer = helpers._sanitize_answer(" ".join(answer_parts))
        # Outbound dead-air guard. Filler turns ("Mm-hm", "I would
        # say", "uh") routinely produce an empty / refusal completion
        # from the LLM. We can't leave the caller in silence — emit a
        # short, in-persona nudge instead so the conversation
        # continues. The inbound path keeps its existing refusal so
        # the clarification FSM can escalate after several vague turns.
        if not answer or helpers._is_refusal(answer, language):
            if outbound_mode:
                fallback = "[warm]No rush — take your time.[/warm]"
                answer = helpers._sanitize_answer(fallback)
                yield {"type": "sentence", "text": answer, "language": language, "tone": "warm"}
                refused = False
            else:
                answer = helpers._refusal(language)
                refused = True
        else:
            refused = helpers._is_refusal(answer, language)
    # Clarification FSM is inbound support behavior. Outbound calls use
    # the campaign prompt + memory to handle filler naturally; applying
    # the inbound vague-turn FSM here can make the agent talk over the
    # prospect with generic repair prompts.
    if outbound_mode:
        clarify_action = None
    else:
        # Clarification FSM after the grounded RAG turn: if the LLM
        # ended up refusing despite retrieval finding no chunks the
        # caller is effectively still vague — bump the counter so a
        # third such turn escalates instead of looping refusals.
        answer, clarify_action, _ = await helpers._apply_clarification(
            tenant_res,
            call_id,
            turn_cache=turn_cache,
            user_text=user_text,
            route=("qdrant_rag" if chunks else "single_prompt_rag"),
            intent=intent_result.intent,
            refused=refused,
            chunks=chunks,
            state_slot=None,
            language=language,
            original_answer=answer,
        )
    await AgentSessionStore.append_turn(tenant_res, call_id, user_text, answer)
    cache_eligible = (
        not outbound_mode
        and not intent_result.sensitive
        and helpers._cacheable(retrieval_query, answer, chunks)
    )
    if cache_eligible:
        await AgentSessionStore.set_cached_answer(
            tenant_res,
            retrieval_query,
            language,
            {"answer": answer, "citations": citations, "chunks": chunks[:2]},
            campaign_id=campaign_id,
            call_context=call_id,
        )
    total_ms = int((perf_counter() - started) * 1000)
    helpers._log_route(
        {
            "tenant_id": tenant_res.tenant_id,
            "call_id": call_id,
            "intent": intent_result.intent,
            "topic": intent_result.topic,
            "route": ("single_prompt_rag" if not chunks else "qdrant_rag") if not refused else "refusal",
            "sensitive": intent_result.sensitive,
            "cache_hit": False,
            # Drive from actual retrieval result instead of hardcoded True —
            # the single_prompt_rag fallback doesn't always hit qdrant.
            "qdrant_called": bool(chunks),
            "llm_called": True,
            "single_prompt_enabled": bool(single_prompt_guidance),
            "total_ms": total_ms,
            "top_score": max((float(c.get("score") or 0.0) for c in chunks), default=0.0),
            "chunk_count": len(chunks),
        }
    )
    yield {
        "type": "final",
        "answer": answer,
        "refused": refused,
        "chunks": chunks,
        "citations": citations,
        "runtime": {
            "graph": "nokvo_rag_pipeline",
            "mode": "single_prompt_grounded_streamed" if not chunks else "grounded_rag_streamed",
            "model": settings.AZURE_OPENAI_AGENT_MODEL,
            "response_language": language,
            "latency_ms": total_ms,
        },
        "retrieval": {
            "used": True,
            "cache_hit": False,
            "relevant_count": len(chunks),
            "top_score": max((float(chunk.get("score") or 0.0) for chunk in chunks), default=0.0),
        },
        "intent": {"type": intent_result.intent, "topic": intent_result.topic, "should_retrieve": True, "sensitive": intent_result.sensitive},
    }
