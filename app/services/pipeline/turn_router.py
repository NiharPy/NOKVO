"""Intent-first turn router.

Formerly :meth:`NokvoOneVoicePipeline._route_turn` — 960 lines inlined into
the orchestrator class. Extracted here as a module-level async function so
the orchestrator's surface shrinks and the routing logic is testable /
diff-readable in isolation.

Behaviour is byte-identical to the original method. The function takes a
``helpers`` namespace (typed loosely as ``Any`` to avoid the import cycle
that would arise if we typed it as ``type[NokvoOneVoicePipeline]``). At
runtime ``helpers`` IS that class; every ``helpers.<method>`` call resolves
to a static / classmethod on it. Twenty-one inline calls into the
orchestrator's static helpers (e.g. ``_turn_state``, ``_turn_history``,
``_template_reply``, ``_apply_route_state``) work unchanged.

The router returns the same decision-dict shape the original method did:

  * ``route == 'template'``      — local canned reply (greeting / nudge / FSM completion)
  * ``route == 'answer_card'``   — matched a tenant Q/A answer card
  * ``route == 'policy_card'``   — deterministic sensitive-policy answer
  * ``route == 'smalltalk_llm'`` — chitchat that needs the LLM but no retrieval
  * ``route == 'rag'``           — full retrieval + LLM path
  * ``route == 'identity_verification'`` — caller must verify before sensitive
                                  intents are answered

The legacy method on :class:`NokvoOneVoicePipeline` is now a thin wrapper
that forwards to :func:`route_turn` and passes ``cls`` as ``helpers``.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from app.core.config import settings
from app.services.agent_knowledge_service import AgentKnowledgeService
from app.services.agent_outbound_context import OutboundCampaignContext
from app.services.agent_session_store import AgentSessionStore
from app.services.fast_intent_router import (
    INTENT_CANCELLATION_REQUEST,
    INTENT_GREETING,
    INTENT_REFUND_ELIGIBILITY,
    INTENT_SMALLTALK,
    INTENT_UNKNOWN_GENERAL,
    FastIntentRouter,
    IntentResult,
)
from app.services.llm_intent_classifier import (
    INTENT_CANCELLATION_REQUEST as LLM_INTENT_CANCEL,
    INTENT_ESCALATION as LLM_INTENT_ESCALATION,
    INTENT_OUT_OF_SCOPE as LLM_INTENT_OUT_OF_SCOPE,
    INTENT_REFUND_ELIGIBILITY as LLM_INTENT_REFUND,
    INTENT_SMALLTALK as LLM_INTENT_SMALLTALK,
    LLMIntentClassifier,
)
from app.services.policy_decision_engine import (
    DEC_EXACT_MATCH,
    DEC_MATRIX_RESPONSE,
    DEC_NO_MATCH,
    PolicyDecisionEngine,
    extract_live_context_from_history,
    fetch_live_order_context,
)
from app.models.tenant_resources import TenantResources
from app.services.tool_flow_policy import evaluate_tool_flow_policy
from app.services.tool_flow_questions import build_tool_flow_questions
from app.services.voice_turn_policy import evaluate_voice_turn_policy

try:  # SQLAlchemy is a heavy import; gracefully degrade for unit tests that don't need it
    from sqlalchemy.ext.asyncio import AsyncSession
except Exception:  # pragma: no cover
    AsyncSession = Any  # type: ignore[assignment, misc]


logger = logging.getLogger(__name__)


async def route_turn(
    helpers: Any,
    tenant_res: TenantResources,
    user_text: str,
    *,
    language: str,
    company_name: str | None,
    call_id: str | None,
    english_text: str | None = None,
    db: "AsyncSession | None" = None,
    top_k: int | None = None,
    campaign_id: str | None = None,
    turn_cache: dict[str, Any] | None = None,
    code_switching: bool = False,
    outbound_context: OutboundCampaignContext | None = None,
) -> dict[str, Any]:
    """See module docstring."""
    intent_result = FastIntentRouter.classify(user_text, language=language)
    turn_cache = turn_cache if turn_cache is not None else {}

    _outbound_active = bool(outbound_context) and outbound_context.is_proactive
    single_prompt_active_hint = bool(helpers._single_prompt_guidance(tenant_res))

    # 0) FSM precedence: if the appointment / tool_flow is *expecting* a
    # yes-or-no answer this turn (slot offered, name to confirm, phone to
    # confirm, etc.), the SMALLTALK fast-path must NOT short-circuit with
    # "Sure, go ahead." A bare "Yes" must reach the FSM so it can lock
    # the booking. We probe state once and skip the template branch when
    # any awaiting_* flag is set.
    state_pre_check = await helpers._turn_state(tenant_res, call_id, turn_cache)
    suppress_template = False
    if isinstance(state_pre_check, dict):
        appt_pre = state_pre_check.get("appointment") or {}
        tool_pre = state_pre_check.get("tool_flow") or {}
        awaiting_flags = (
            "awaiting_slot_confirm",
            "awaiting_name_confirmation",
            "awaiting_phone_confirmation",
            "awaiting_id_confirmation",
            "awaiting_past_time_shift",
        )
        appointment_awaiting = False if (_outbound_active or single_prompt_active_hint) else any(
            bool(appt_pre.get(flag)) for flag in awaiting_flags
        )
        tool_awaiting = any(bool(tool_pre.get(flag)) for flag in awaiting_flags)
        suppress_template = appointment_awaiting or tool_awaiting

    # 1) Greeting / thanks / goodbye / smalltalk — no LLM, no cache, no embeddings.
    templated = helpers._template_reply(intent_result.intent, language, company_name)
    if templated and not suppress_template and not _outbound_active:
        if intent_result.intent == INTENT_GREETING and single_prompt_active_hint:
            return {
                "route": "smalltalk_llm",
                "answer": None,
                "intent_result": intent_result,
                "safe_to_cache": False,
                "sensitive": False,
                "classified": {
                    "intent": "smalltalk",
                    "needs_kb": False,
                    "sentiment": "neutral",
                    "reason": "single prompt greeting override",
                },
            }
        if (
            single_prompt_active_hint
            and intent_result.intent == INTENT_SMALLTALK
            and helpers._is_short_permission_reply(user_text)
        ):
            history_for_template = await helpers._turn_history(tenant_res, call_id, turn_cache)
            last_assistant = helpers._last_assistant_text(history_for_template)
            if helpers._assistant_asked_for_user_decision(last_assistant):
                return {
                    "route": "smalltalk_llm",
                    "answer": None,
                    "intent_result": intent_result,
                    "safe_to_cache": False,
                    "sensitive": False,
                    "classified": {
                        "intent": "smalltalk",
                        "needs_kb": False,
                        "sentiment": "neutral",
                        "reason": "single prompt contextual permission reply",
                    },
                }
        return {
            "route": "template",
            "answer": templated,
            "intent_result": intent_result,
            "safe_to_cache": False,
            "sensitive": False,
        }

    # 2) Answer-card cache (existing Q/A card lookup).
    card = None if _outbound_active else AgentKnowledgeService.find_answer_card(tenant_res, user_text, language)
    if card and card.get("answer"):
        return {
            "route": "answer_card",
            "answer": str(card["answer"]),
            "intent_result": intent_result,
            "safe_to_cache": bool(card.get("cacheable", True)) and not intent_result.sensitive,
            "sensitive": intent_result.sensitive,
            "card_id": card.get("id"),
        }

    history_for_turn = await helpers._turn_history(tenant_res, call_id, turn_cache)
    state_for_turn = await helpers._turn_state(tenant_res, call_id, turn_cache)
    prior_appointment = dict((state_for_turn or {}).get("appointment") or {})
    prior_in_booking_flow = bool(prior_appointment.get("active")) and not (
        prior_appointment.get("completed") and not prior_appointment.get("pending_slot")
    )
    prior_pending_slot = prior_appointment.get("pending_slot")

    # Clinic FSM gate. The appointment slot-fill ("patient name", "eye
    # concern", "urgent symptoms", "follow-up?") is hard-wired
    # ophthalmology language — it leaks into real-estate, hospitality,
    # ecommerce, and any single-prompt tenant the moment we let it run.
    #
    # Rules:
    #   1. Outbound calls NEVER run it (the outbound LLM owns dialogue).
    #   2. Single-prompt tenants NEVER run it ("I drive the agent
    #      myself" — adding deterministic clinic prompts on top of the
    #      operator's persona is a bug).
    #   3. Industry must be explicitly ``clinics``. An empty/unknown
    #      industry MUST default to OFF — the previous code defaulted
    #      to ON, which is how a real-estate tenant ended up being
    #      asked about "eye concerns".
    bundle = await helpers._turn_bundle(db, tenant_res, turn_cache)
    industry = ""
    bundle_single_prompt_enabled = False
    if bundle is not None:
        industry = str(bundle.organization_industry or "").strip()
        bundle_single_prompt_enabled = bool(getattr(bundle, "single_prompt_enabled", False))
    if not industry:
        try:
            context_for_industry = await helpers._voice_business_context(db, tenant_res)
        except Exception:
            context_for_industry = None
        if context_for_industry is not None:
            org_obj, _overrides, _tabs = context_for_industry
            if org_obj is not None:
                industry = str(getattr(org_obj, "industry", "") or "").strip()
    single_prompt_active = bundle_single_prompt_enabled or single_prompt_active_hint
    is_clinic_org = (
        False
        if (_outbound_active or single_prompt_active)
        else (industry.lower() == "clinics")
    )
    if prior_in_booking_flow and not is_clinic_org:
        prior_appointment = {
            **prior_appointment,
            "active": False,
            "completed": True,
            "pending_slot": None,
            "disabled_reason": "appointment_flow_not_enabled_for_account",
        }
        if call_id:
            await AgentSessionStore.merge_state(
                tenant_res,
                call_id,
                {"appointment": prior_appointment},
            )
        prior_in_booking_flow = False
        prior_pending_slot = None

    turn_policy = (
        evaluate_voice_turn_policy(
            user_text,
            history=history_for_turn,
            state=state_for_turn,
            language=language,
        )
        if is_clinic_org
        else None
    )

    # If the regex side-question detector inside evaluate_voice_turn_policy
    # yielded mid-booking, persist a `deferred_for_kb` marker so the next
    # FSM turn can acknowledge the digression with a "Coming back..."
    # prefix. The function itself is sync and can't touch Redis, so we do
    # the merge here.
    if turn_policy is None and prior_in_booking_flow:
        await helpers._mark_appointment_deferred(tenant_res, call_id, prior_appointment)

    # LLM digression fallback: if the FSM is about to re-ask the SAME slot
    # (i.e., the caller's input didn't advance the flow), the regex detector
    # missed something. Ask the small LLM classifier with a tight timeout —
    # if it says the caller pivoted (kb_question, complaint, escalation,
    # cancel/refund), bypass the FSM and let the route fall through to RAG
    # or the sensitive-policy handler.
    if (
        turn_policy
        and turn_policy.get("answer")
        and turn_policy.get("intent") == "appointment_flow"
        and prior_in_booking_flow
        and prior_pending_slot
        and turn_policy.get("state_slot") == prior_pending_slot
    ):
        digression = await helpers._llm_check_booking_digression(
            tenant_res, user_text, history_for_turn
        )
        if digression is not None:
            await helpers._mark_appointment_deferred(tenant_res, call_id, prior_appointment)
            if digression.intent == LLM_INTENT_CANCEL:
                intent_result = FastIntentRouter._build(
                    INTENT_CANCELLATION_REQUEST, confidence=0.9, reason="llm digression"
                )
            elif digression.intent == LLM_INTENT_REFUND:
                intent_result = FastIntentRouter._build(
                    INTENT_REFUND_ELIGIBILITY, confidence=0.9, reason="llm digression"
                )
            turn_policy = None

    # Availability check is the one policy intent that doesn't carry its
    # own answer — the pipeline must consult the scheduler to fill it in.
    needs_availability_lookup = (
        turn_policy is not None
        and turn_policy.get("intent") == "availability_check"
    )
    if turn_policy and (turn_policy.get("answer") or needs_availability_lookup):
        action = await helpers._maybe_execute_turn_policy_action(
            tenant_res,
            call_id,
            db,
            turn_policy,
        )
        if action:
            turn_policy["answer"] = action.get("answer") or turn_policy.get("answer") or ""
            turn_policy["state_patch"] = action.get("state_patch") or turn_policy.get("state_patch") or {}
            turn_policy["state_slot"] = action.get("state_slot") or turn_policy.get("state_slot")
            turn_policy["reason"] = action.get("route_reason") or turn_policy.get("reason")
        elif needs_availability_lookup and not turn_policy.get("answer"):
            # No business context (e.g., not a clinic) — fall through to
            # the normal RAG/template path by clearing the intent.
            turn_policy = None
        metadata = {
            **(intent_result.metadata or {}),
            "turn_policy_intent": turn_policy.get("intent"),
            "turn_policy_reason": turn_policy.get("reason"),
            "entities": turn_policy.get("entities") or {},
            "state_slot": turn_policy.get("state_slot"),
            "tool_calls": (action or {}).get("tool_calls") or [],
        }
        return {
            "route": "template",
            "answer": str(turn_policy["answer"]),
            "intent_result": IntentResult(
                intent=intent_result.intent,
                topic=intent_result.topic,
                confidence=max(intent_result.confidence, 0.88),
                sensitive=intent_result.sensitive,
                requires_live_status=intent_result.requires_live_status,
                reason=turn_policy.get("reason") or intent_result.reason,
                metadata=metadata,
            ),
            "safe_to_cache": False,
            "sensitive": intent_result.sensitive,
            "state_patch": turn_policy.get("state_patch") or {},
            "detected_entities": turn_policy.get("entities") or {},
            "state_slot": turn_policy.get("state_slot"),
            "route_reason": turn_policy.get("reason"),
            "tool_calls": (action or {}).get("tool_calls") or [],
        }

    # Reuse the bundle's tuple when available; fall back to the
    # ``_voice_business_context`` helper so test-stub paths (which
    # monkeypatch only that helper) still hit the tool_flow branch.
    business_context: tuple[Any, dict[str, Any], list[dict[str, Any]]] | None = None
    if bundle is not None:
        business_context = bundle.as_business_context_tuple()
    if business_context is None:
        try:
            business_context = await helpers._voice_business_context(db, tenant_res)
        except Exception:
            business_context = None
    if business_context is not None:
        organization, overrides, custom_tabs = business_context
        prior_tool_flow = dict((state_for_turn or {}).get("tool_flow") or {})
        prior_in_tool_flow = bool(prior_tool_flow.get("active")) and not bool(prior_tool_flow.get("completed"))
        prior_tool_flow_slot = prior_tool_flow.get("pending_slot")
        # Outbound campaign objective gate. When the operator picks only
        # "Book a site visit" for this campaign, the leads_create flow must
        # NOT auto-start (and vice versa). ``allowed_flow_keys=None`` for
        # inbound = all flows allowed.
        allowed_flow_keys_for_call: list[str] | None = None
        if (
            _outbound_active
            and outbound_context is not None
            and str(organization.industry or "").lower() == "real_estate"
        ):
            try:
                from app.services.real_estate_outbound_agent_fsm import (
                    OBJECTIVE_LEAD,
                    OBJECTIVE_SITE_VISIT,
                    normalize_objectives,
                )
                _objs = normalize_objectives(getattr(outbound_context, "objectives", None))
                allowed_flow_keys_for_call = []
                if OBJECTIVE_SITE_VISIT in _objs:
                    allowed_flow_keys_for_call.append("real_estate_site_visit")
                if OBJECTIVE_LEAD in _objs:
                    allowed_flow_keys_for_call.append("leads_create")
                # If the campaign carries no structured objectives (legacy
                # free-text), don't gate anything — keep the old behaviour.
                if not allowed_flow_keys_for_call:
                    allowed_flow_keys_for_call = None
            except Exception:
                allowed_flow_keys_for_call = None
        tool_flow = evaluate_tool_flow_policy(
            user_text,
            business_type=organization.industry,
            schema_overrides=overrides,
            custom_tabs=custom_tabs,
            provider_status=dict(tenant_res.provider_status or {}),
            history=history_for_turn,
            state=state_for_turn,
            language=language,
            allowed_flow_keys=allowed_flow_keys_for_call,
        )

        # Regex side-question detector inside evaluate_tool_flow_policy
        # returns None when the caller pivots mid-flow. Persist the
        # deferred-for-kb marker so the next turn resumes with a
        # "Coming back to your booking — " prefix on the slot question.
        if tool_flow is None and prior_in_tool_flow:
            await helpers._mark_tool_flow_deferred(tenant_res, call_id, prior_tool_flow)

        # LLM digression fallback: if the FSM is about to re-ask the SAME
        # tool_flow slot (regex extractor failed to advance), check with
        # the small LLM classifier. When it says "kb_question / complaint
        # / escalation / cancel / refund", bypass the FSM so the route
        # falls through to RAG or the sensitive-policy handler.
        if (
            tool_flow
            and tool_flow.get("answer")
            and tool_flow.get("intent") == "tool_flow"
            and prior_in_tool_flow
            and prior_tool_flow_slot
            and tool_flow.get("state_slot") == prior_tool_flow_slot
        ):
            digression = await helpers._llm_check_booking_digression(
                tenant_res, user_text, history_for_turn
            )
            if digression is not None:
                await helpers._mark_tool_flow_deferred(tenant_res, call_id, prior_tool_flow)
                if digression.intent == LLM_INTENT_CANCEL:
                    intent_result = FastIntentRouter._build(
                        INTENT_CANCELLATION_REQUEST, confidence=0.9, reason="llm digression"
                    )
                elif digression.intent == LLM_INTENT_REFUND:
                    intent_result = FastIntentRouter._build(
                        INTENT_REFUND_ELIGIBILITY, confidence=0.9, reason="llm digression"
                    )
                tool_flow = None

        # Mirror the clinic-flow handling: the tool_flow's availability
        # intent comes back with answer=None — the scheduler fills it in.
        # The previous code's `if tool_flow.get("answer")` guard dropped
        # the response on the floor, never dispatched the scheduler, AND
        # silently discarded the state_patch (offered_disambiguation,
        # pending_slot), so the next turn looped right back into the same
        # availability question. Use a needs_lookup flag instead.
        tool_flow_needs_lookup = (
            tool_flow is not None
            and tool_flow.get("intent") == "availability_check"
        )
        if tool_flow and (tool_flow.get("answer") or tool_flow_needs_lookup):
            if tool_flow_needs_lookup:
                action = await helpers._handle_availability_check(tenant_res, db, tool_flow)
            else:
                action = await helpers._maybe_execute_tool_flow_action(
                    tenant_res,
                    call_id,
                    db,
                    tool_flow,
                    business_context=business_context,
                    language=language,
                )
            if action:
                tool_flow["answer"] = action.get("answer") or tool_flow.get("answer") or ""
                tool_flow["state_patch"] = action.get("state_patch") or tool_flow.get("state_patch") or {}
                tool_flow["state_slot"] = action.get("state_slot") or tool_flow.get("state_slot")
                tool_flow["reason"] = action.get("route_reason") or tool_flow.get("reason")
            elif tool_flow_needs_lookup and not tool_flow.get("answer"):
                # Scheduler couldn't satisfy the lookup (no assignable
                # member for this request_type). Fall back to asking the
                # original missing slot directly — DO persist the
                # state_patch so offered_disambiguation stays True and
                # we don't loop.
                flow_state = dict((tool_flow.get("state_patch") or {}).get("tool_flow") or {})
                pending = flow_state.get("pending_slot") or "visit_time"
                business_type_local = (business_context[0].industry if business_context else None)
                bundle_local = build_tool_flow_questions(
                    business_type_local,
                    (business_context[1] if business_context else None),
                    (business_context[2] if business_context else None),
                )
                slot_question = None
                for slot_def in ((bundle_local.get("flows") or {}).get(tool_flow.get("flow_key") or "") or {}).get("slots") or []:
                    if slot_def.get("key") == pending:
                        questions = slot_def.get("questions") or {}
                        slot_question = questions.get(language) or questions.get("en")
                        break
                tool_flow["answer"] = slot_question or "What time would you prefer?"
                tool_flow["state_patch"] = {"tool_flow": flow_state}
                tool_flow["state_slot"] = pending
            metadata = {
                **(intent_result.metadata or {}),
                "turn_policy_intent": tool_flow.get("intent"),
                "turn_policy_reason": tool_flow.get("reason"),
                "flow_key": tool_flow.get("flow_key"),
                "state_slot": tool_flow.get("state_slot"),
                "tool_calls": (action or {}).get("tool_calls") or [],
            }
            # Outbound mode: the tool_flow's regex slot scraper is
            # useful (it still captures slots into state_patch and
            # executes the tool on completion), but its inbound-
            # style canned questions ("May I have your name?",
            # "What date would you prefer?") must NOT become the
            # caller-facing reply — the outbound LLM speaks for the
            # agent. Suppress the template short-circuit unless this
            # turn is the completion (state_slot == "complete") OR
            # a tool was actually executed this turn, in which case
            # the deterministic confirmation ("I've created the
            # site visit request…") is the right user-facing reply.
            _is_completion = (
                tool_flow.get("state_slot") == "complete"
                or bool((action or {}).get("tool_calls"))
            )
            # Same rule as the clinic FSM gate above: a single-prompt
            # tenant has explicitly said "I drive the agent myself",
            # so deterministic slot-question text from the tool_flow
            # (e.g., "What's your name?", "What date would you prefer?")
            # must NOT replace the LLM's persona-voiced reply. Slots
            # are still scraped into Redis below and the completion
            # path still fires the tool — only the mid-flow canned
            # question is suppressed.
            # Confirmation prompts ("Just to confirm — the name is Nihar.
            # Is that right?") are verbatim read-back challenges the FSM
            # needs back as a yes/no. If we let the LLM paraphrase them
            # in single-prompt mode, the slot extractor can't reliably
            # tell whether the next user turn confirmed or corrected.
            # So: confirmation prompts ALWAYS play deterministically.
            _is_confirmation_prompt = str(tool_flow.get("state_slot") or "").endswith("_confirm")
            _suppress_tool_flow_template = (
                (_outbound_active or single_prompt_active)
                and not _is_completion
                and not _is_confirmation_prompt
            )
            if _suppress_tool_flow_template:
                # Persist the scraped slots into the state-patch
                # path used by the rag branch so the LLM's next
                # turn sees up-to-date slot data, then route
                # straight to RAG/LLM so the LLM can ask the next
                # slot question in the admin's persona.
                state_patch_holder: dict[str, Any] = tool_flow.get("state_patch") or {}
                if state_patch_holder:
                    await helpers._apply_route_state(
                        tenant_res,
                        call_id,
                        {
                            "state_patch": state_patch_holder,
                            "state_slot": tool_flow.get("state_slot"),
                        },
                    )
                # CRITICAL: short slot-answer like "9704628375" or
                # "Nihar" would otherwise hit the downstream
                # "word-count gate" (line ~3840) and get nudged with
                # "Mm-hm, go on". Force the RAG/LLM route now so the
                # LLM uses the freshly-scraped state to compose the
                # next reply (acknowledge + ask next slot or
                # complete) instead of treating the utterance as a
                # vague filler.
                return {
                    "route": "rag",
                    "answer": None,
                    "intent_result": IntentResult(
                        intent=intent_result.intent,
                        topic=intent_result.topic,
                        confidence=max(intent_result.confidence, 0.9),
                        sensitive=intent_result.sensitive,
                        requires_live_status=intent_result.requires_live_status,
                        reason="slot scraped in single-prompt mode; defer reply to LLM",
                        metadata={
                            **(intent_result.metadata or {}),
                            "tool_flow_slot_scraped": True,
                            "tool_flow_state_slot": tool_flow.get("state_slot"),
                            "flow_key": tool_flow.get("flow_key"),
                        },
                    ),
                    "safe_to_cache": False,
                    "sensitive": intent_result.sensitive,
                    "state_patch": state_patch_holder,
                    "state_slot": tool_flow.get("state_slot"),
                    "route_reason": tool_flow.get("reason") or "tool_flow_slot_captured",
                    "tool_calls": (action or {}).get("tool_calls") or [],
                    "prefetched_retrieval": None,
                }
            else:
                return {
                    "route": "template",
                    "answer": str(tool_flow["answer"]),
                    "intent_result": IntentResult(
                        intent=intent_result.intent,
                        topic=intent_result.topic,
                        confidence=max(intent_result.confidence, 0.9),
                        sensitive=intent_result.sensitive,
                        requires_live_status=intent_result.requires_live_status,
                        reason=tool_flow.get("reason") or intent_result.reason,
                        metadata=metadata,
                    ),
                    "safe_to_cache": False,
                    "sensitive": intent_result.sensitive,
                    "state_patch": tool_flow.get("state_patch") or {},
                    "detected_entities": {},
                    "state_slot": tool_flow.get("state_slot"),
                    "route_reason": tool_flow.get("reason"),
                    "tool_calls": (action or {}).get("tool_calls") or [],
                }

    # 3) Sensitive policy intents (cancellation/refund) → deterministic engine.
    # The set of "sensitive" intents lives in :mod:`agent_spec` so chat /
    # voice / outbound all read the same list.
    from app.services.agent_spec import IDENTITY_POLICY

    if intent_result.intent in IDENTITY_POLICY.sensitive_intents or intent_result.intent in (
        INTENT_CANCELLATION_REQUEST,
        INTENT_REFUND_ELIGIBILITY,
    ):
        # Caller identity gate: before answering anything actionable
        # about a cancellation or refund, require a phone number that
        # matches an existing record. Without this anyone can call in
        # and "cancel my appointment" — a hard policy hole.
        verified = await helpers._caller_is_verified(tenant_res, db, call_id, user_text)
        if not verified["verified"]:
            if not verified.get("challenged"):
                challenge = verified.get("challenge") or (
                    "Before I can change a booking, I need to verify you — "
                    "could you share the phone number the booking is under?"
                )
                await AgentSessionStore.set_state(
                    tenant_res, call_id, {"identity_verification_pending": True}
                )
                return {
                    "route": "identity_verification",
                    "answer": challenge,
                    "intent_result": intent_result,
                    "safe_to_cache": False,
                    "sensitive": True,
                    "state_patch": {"identity_verification_pending": True},
                    "state_slot": "identity_verification",
                    "route_reason": "identity verification required",
                    "tool_calls": [],
                }
        policy_cards = helpers._active_policy_cards(tenant_res)
        # Prefer authoritative live context (CRM/order service) when
        # available, otherwise mine the conversation history for what
        # the caller already told us in prior turns. This is what makes
        # multi-turn cancellation work: agent asks "how long ago did you
        # place it?", caller says "3 minutes", caller asks "can I cancel?"
        # → engine fires with order_age_minutes=3 instead of re-asking.
        #
        # We feed the English-translated transcript to the extractor when
        # it's available. The extractor's patterns are English-only —
        # Telugu "5 మినిట్స్కే క్యాన్సిల్" never matches, but the
        # translated form "cancelled at 5 minutes" does.
        extractor_text = english_text or user_text
        live_context = await fetch_live_order_context(
            tenant_res.tenant_id,
            call_id,
            user_text,
        )
        if not live_context:
            history = await helpers._turn_history(tenant_res, call_id, turn_cache)
            live_context = extract_live_context_from_history(
                history,
                current_user_text=extractor_text,
            )
        provider_status = dict(tenant_res.provider_status or {})
        decision = PolicyDecisionEngine.evaluate(
            intent_result.intent,
            intent_result.topic,
            user_text,
            policy_cards,
            live_context,
            current_policy_version=str(provider_status.get("agent_policy_version") or "") or None,
        )
        # Only terminate the route when we have CONFIDENT signal:
        #   - DEC_EXACT_MATCH    — single condition matched live context
        #                          (age + status pinned a specific rule).
        #   - DEC_MATRIX_RESPONSE — pure general policy question, no
        #                          context given. Returning the full
        #                          matrix is correct.
        # DEC_NO_MATCH (user gave partial context but no clean match) and
        # DEC_LIVE_STATUS_NEEDED both fall through to the RAG path. The
        # LLM reads the policy source text and can reason conditionally
        # ("depending on whether the restaurant accepted, here's what
        # happens"), which is much more human-like than the canned matrix
        # dump. The strict grounding prompt + policy_card_chunks
        # injection keeps it from hallucinating.
        confident_codes = {DEC_EXACT_MATCH, DEC_MATRIX_RESPONSE}
        if decision.answered and decision.answer and decision.decision_code in confident_codes:
            return {
                "route": "policy_card",
                "answer": decision.answer,
                "intent_result": intent_result,
                "safe_to_cache": decision.safe_to_cache,
                "sensitive": True,
                "policy_card_id": decision.matched_card_id,
                "decision_code": decision.decision_code,
                "matched_condition": decision.matched_condition,
            }
        # Partial signal — return RAG route DIRECTLY. retrieve() injects
        # policy_card source_text as synthetic chunks when Qdrant comes up
        # empty, so the LLM always has the policy matrix to reason from.
        # We deliberately do NOT fall through to the Tier-2 classifier
        # because Tier 1 already correctly identified the intent — the
        # classifier would just duplicate the engine call.
        return {
            "route": "rag",
            "answer": None,
            "intent_result": intent_result,
            "safe_to_cache": False,
            "sensitive": True,
        }

    # Outbound short-circuit: by now the tool_flow slot scraping has
    # run (so any slots in this turn are persisted) and any genuine
    # completion already returned with route="template" above. From
    # here down the route would otherwise burn ~500-800ms on inbound
    # Tier-2 LLM intent classification + Qdrant prefetch, neither of
    # which apply to a sales call. Hand control to the outbound LLM.
    if _outbound_active:
        return {
            "route": "rag",
            "answer": None,
            "intent_result": intent_result,
            "safe_to_cache": False,
            "sensitive": intent_result.sensitive,
            "prefetched_retrieval": None,
        }

    location_retrieval_query = helpers._business_location_retrieval_rewrite(user_text)
    if location_retrieval_query:
        prefetch_task = asyncio.create_task(
            helpers.retrieve(
                tenant_res,
                helpers.retrieval_query_for(user_text, english_text),
                db=db,
                top_k=top_k,
                campaign_id=campaign_id,
                intent_result=intent_result,
                english_text=english_text or location_retrieval_query,
                dual_retrieval=code_switching,
            )
        )
        return {
            "route": "rag",
            "answer": None,
            "intent_result": intent_result,
            "safe_to_cache": True,
            "sensitive": False,
            "classified": {
                "intent": "kb_question",
                "needs_kb": True,
                "sensitive": False,
                "reason": "deterministic business location query",
            },
            "prefetched_retrieval": prefetch_task,
        }

    # Word-count gate: a short non-greeting utterance ("yeah", "uh ok",
    # "but I mean") has no informational intent. But: many Indian
    # languages express a complete question in 2 words ("rifand vastada?"
    # = "will refund come?"), so we ALSO require: no clear question
    # punctuation (?, ।, ؟) and (if we have an English translation)
    # nothing question-shaped in English either.
    clear_question = bool(
        "?" in user_text
        or "؟" in user_text
        or "।" in user_text
        or (english_text and "?" in english_text)
    )
    # Pure-digit answers (phone numbers, OTPs, order ids, amounts) carry
    # real information even though they only count as one "word". Spaces
    # between groups of digits are common in dictation ("970 462 8375")
    # so we strip whitespace before checking.
    _digit_only = re.sub(r"\s+", "", user_text).isdigit() and len(re.sub(r"\D+", "", user_text)) >= 4
    # ``prior_in_tool_flow`` is only defined inside the business_context
    # branch above. Default to False here so the gate behaves as it did
    # when no real-estate / clinic tool_flow context exists.
    _prior_in_tool_flow_for_gate = False
    if business_context is not None:
        prior_tool_flow = dict((state_for_turn or {}).get("tool_flow") or {})
        _prior_in_tool_flow_for_gate = bool(prior_tool_flow.get("active")) and not bool(prior_tool_flow.get("completed"))
    if (
        intent_result.intent == INTENT_UNKNOWN_GENERAL
        and len(user_text.split()) < settings.AGENT_RAG_MIN_QUERY_WORDS
        and not clear_question
        and not _digit_only
        # During an active booking flow, a single-word reply IS a slot
        # answer attempt ("Tukkuguda" for the project slot) — never a
        # vague filler. The previous code ate these with "Mm-hm, go on"
        # and the agent looped on the same question. Defer to the LLM
        # so it can either capture the value or ask for clarification.
        and not _prior_in_tool_flow_for_gate
    ):
        nudge = {
            "hi": "हाँ, बताइए।",
            "ta": "சரி, சொல்லுங்கள்.",
            "te": "సరే, చెప్పండి.",
            "bn": "হ্যাঁ, বলুন।",
        }.get(language, "Mm-hm, go on.")
        return {
            "route": "template",
            "answer": nudge,
            "intent_result": intent_result,
            "safe_to_cache": False,
            "sensitive": False,
        }

    # ── Tier 2: LLM classifier ──
    # Tier 1 regex didn't recognize this utterance. Ask a small LLM to
    # classify what the caller is actually trying to do — handles
    # paraphrasing, code-switching, STT errors, and idioms the regex
    # can't possibly enumerate. Capped at 800ms with a safe default,
    # so a slow/down classifier never blocks the turn.
    #
    # We send the classifier BOTH the native + English-translated forms
    # (when available). Small LLMs handle English best; the native form
    # is the source of truth and is still presented to preserve nuance.
    prefetch_task = asyncio.create_task(
        helpers.retrieve(
            tenant_res,
            helpers.retrieval_query_for(user_text, english_text),
            db=db,
            top_k=top_k,
            campaign_id=campaign_id,
            intent_result=intent_result,
            english_text=english_text,
            dual_retrieval=code_switching,
        )
    )
    history = await helpers._turn_history(tenant_res, call_id, turn_cache)
    classifier_text = (
        f"{user_text}\n(English translation: {english_text})"
        if english_text and english_text.strip() and english_text.strip() != user_text.strip()
        else user_text
    )
    classified = await LLMIntentClassifier.classify(
        classifier_text,
        tenant_res=tenant_res,
        history=history,
    )

    # Promote LLM-detected sensitive intents into Tier-1-style routing so
    # downstream code paths see consistent intent constants. For each
    # case, we also REWRITE the IntentResult so logging + retrieve()
    # filters use the correct topic.
    if classified.intent == LLM_INTENT_CANCEL:
        intent_result = FastIntentRouter._build(
            INTENT_CANCELLATION_REQUEST, confidence=0.9, reason="llm classifier"
        )
    elif classified.intent == LLM_INTENT_REFUND:
        intent_result = FastIntentRouter._build(
            INTENT_REFUND_ELIGIBILITY, confidence=0.9, reason="llm classifier"
        )

    # Sensitive policy intents — re-run the policy_card path. This catches
    # cancellation/refund questions that Tier 1's regex missed (paraphrasing,
    # other languages, STT typos).
    if intent_result.intent in (INTENT_CANCELLATION_REQUEST, INTENT_REFUND_ELIGIBILITY):
        helpers._cancel_retrieval_task(prefetch_task)
        policy_cards = helpers._active_policy_cards(tenant_res)
        live_context = await fetch_live_order_context(
            tenant_res.tenant_id,
            call_id,
            user_text,
        )
        if not live_context:
            # Use the English translation for extraction; native Telugu /
            # Hindi won't match the English-only patterns.
            live_context = extract_live_context_from_history(
                history,
                current_user_text=english_text or user_text,
            )
        provider_status = dict(tenant_res.provider_status or {})
        decision = PolicyDecisionEngine.evaluate(
            intent_result.intent,
            intent_result.topic,
            user_text,
            policy_cards,
            live_context,
            current_policy_version=str(provider_status.get("agent_policy_version") or "") or None,
        )
        confident_codes = {DEC_EXACT_MATCH, DEC_MATRIX_RESPONSE, DEC_NO_MATCH}
        if decision.answered and decision.answer and decision.decision_code in confident_codes:
            return {
                "route": "policy_card",
                "answer": decision.answer,
                "intent_result": intent_result,
                "safe_to_cache": decision.safe_to_cache,
                "sensitive": True,
                "policy_card_id": decision.matched_card_id,
                "decision_code": decision.decision_code,
                "matched_condition": decision.matched_condition,
                "classified": classified.to_dict(),
            }
        # Fall through to RAG (with policy_card_chunks injected by retrieve()).

    # ── Out-of-scope: verify by retrieval before deflecting ──
    # The classifier sometimes mis-labels operational questions ("can I
    # get an appointment today?", "which clinic is this?") as
    # out_of_scope. Before sending the canned deflection, run retrieval.
    # If the KB has a relevant chunk, the classifier was wrong — let RAG
    # answer it. Only deflect when the KB genuinely has nothing.
    if classified.intent == LLM_INTENT_OUT_OF_SCOPE:
        probe = await helpers._await_prefetched_retrieval(
            {"prefetched_retrieval": prefetch_task}
        )
        if probe is None:
            probe = await helpers.retrieve(
                tenant_res,
                helpers.retrieval_query_for(user_text, english_text),
                db=db,
                top_k=top_k,
                campaign_id=campaign_id,
                intent_result=intent_result,
                english_text=english_text,
                dual_retrieval=code_switching,
            )
        probe_chunks = probe.get("chunks") or []
        if probe_chunks:
            # KB has relevant content → fall through to the normal RAG
            # path. Stash the probe so retrieve() doesn't re-run; we'll
            # surface it via the `prefetched_retrieval` route hint.
            logger.info(
                "NOKVO-VOICE: classifier said out_of_scope but retrieval "
                "found %s chunks — overriding to RAG", len(probe_chunks),
            )
            return {
                "route": "rag",
                "answer": None,
                "intent_result": intent_result,
                "safe_to_cache": False,
                "sensitive": False,
                "classified": classified.to_dict(),
                "prefetched_retrieval": probe,
            }
        if helpers._single_prompt_guidance(tenant_res):
            return {
                "route": "rag",
                "answer": None,
                "intent_result": intent_result,
                "safe_to_cache": False,
                "sensitive": False,
                "classified": classified.to_dict(),
                "prefetched_retrieval": probe,
            }
        # KB really has nothing → friendly redirect template.
        brand = company_name or "us"
        msg = {
            "hi": f"मैं केवल {brand} से जुड़े सवालों में मदद कर सकता हूँ। और कुछ बताइए?",
            "ta": f"நான் {brand} தொடர்பான கேள்விகளில் மட்டுமே உதவ முடியும். வேறு என்ன உதவி வேண்டும்?",
            "te": f"నేను {brand}కి సంబంధించిన ప్రశ్నలకు మాత్రమే సహాయం చేయగలను. మరేమైనా కావాలా?",
            "bn": f"আমি শুধু {brand} সম্পর্কিত প্রশ্নে সাহায্য করতে পারি। আর কিছু?",
        }.get(language, f"I don't have that information — I'm here to help with {brand}. What else can I do for you?")
        return {
            "route": "template",
            "answer": msg,
            "intent_result": intent_result,
            "safe_to_cache": False,
            "sensitive": False,
            "classified": classified.to_dict(),
        }

    # ── Pure smalltalk → LLM in conversational mode, no RAG ──
    # The classifier said this is chitchat. Let the LLM respond like a
    # human (using its conversational ability), but the smalltalk prompt
    # forbids it from inventing world or company facts.
    if classified.intent == LLM_INTENT_SMALLTALK and not classified.needs_kb:
        helpers._cancel_retrieval_task(prefetch_task)
        return {
            "route": "smalltalk_llm",
            "answer": None,
            "intent_result": intent_result,
            "safe_to_cache": False,
            "sensitive": False,
            "classified": classified.to_dict(),
        }

    # ── Escalation request ──
    if classified.intent == LLM_INTENT_ESCALATION:
        helpers._cancel_retrieval_task(prefetch_task)
        msg = {
            "hi": "ज़रूर, मैं इसे सपोर्ट टीम को आगे भेज देता हूँ।",
            "ta": "சரி, இதை ஆதரவு குழுவிற்கு அனுப்புகிறேன்.",
            "te": "సరే, దీన్ని సపోర్ట్ టీమ్‌కు పంపుతున్నాను.",
        }.get(language, "Sure, I'll transfer this to support. One moment.")
        return {
            "route": "template",
            "answer": msg,
            "intent_result": intent_result,
            "safe_to_cache": False,
            "sensitive": False,
            "classified": classified.to_dict(),
        }

    # ── Everything else → RAG path ──
    # kb_question, complaint, order_status, unclear — all need retrieval.
    # The downstream prompt is strict about not inventing company facts.
    # classified.sensitive carries through so retrieve() applies tighter
    # thresholds + topic filters when appropriate.
    merged_sensitive = bool(intent_result.sensitive or classified.sensitive)
    if merged_sensitive:
        helpers._cancel_retrieval_task(prefetch_task)
    return {
        "route": "rag",
        "answer": None,
        "intent_result": IntentResult(
            intent=intent_result.intent,
            topic=intent_result.topic,
            confidence=intent_result.confidence,
            sensitive=merged_sensitive,
            requires_live_status=intent_result.requires_live_status,
            reason=intent_result.reason,
            metadata={**(intent_result.metadata or {}), "llm_classifier": classified.to_dict()},
        ),
        "safe_to_cache": not merged_sensitive,
        "sensitive": merged_sensitive,
        "classified": classified.to_dict(),
        "prefetched_retrieval": None if merged_sensitive else prefetch_task,
    }


__all__ = ("route_turn",)
