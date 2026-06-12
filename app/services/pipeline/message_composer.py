"""LLM message-builder for the voice pipeline.

Two pure, synchronous composers — no I/O, no DB, no Redis. They produce
the ``list[dict[str, str]]`` shape the OpenAI / Azure chat-completions
APIs accept.

These were inlined into :class:`NokvoOneVoicePipeline` as ``_messages``
and ``_messages_smalltalk`` for years. Moving them out:

  * shrinks ``nokvo_one_voice_pipeline.py`` by ~400 lines;
  * exposes them as plain functions (the staticmethod wrapping was
    historical, not architectural);
  * makes the prompt structure easier to unit-test in isolation.

The pipeline class now imports + delegates to these. The contract is
unchanged, so call sites need no edits.
"""
from __future__ import annotations

import re
from typing import Any

from app.core.config import settings
from app.services.agent_outbound_context import (
    OutboundCampaignContext,
    compose_outbound_system_section,
)
from app.services.language_style import (
    outbound_fewshot as language_outbound_fewshot,
    style_guidance as language_style_guidance,
)
from app.services.sarvam_voice_service import SarvamVoiceService


def compose_rag_messages(
    query: str,
    chunks: list[dict[str, Any]],
    *,
    language: str,
    history: list[dict[str, str]],
    company_name: str | None = None,
    campaign_goal: str | None = None,
    single_prompt_guidance: str | None = None,
    outbound_context: OutboundCampaignContext | None = None,
    covered_objectives: list[str] | None = None,
    outbound_memory: dict[str, Any] | None = None,
    conversational_memory_block: str | None = None,
    conversation_strategy_block: str | None = None,
    field_questions_prompt: str | None = None,
    projects_block: str | None = None,
    services_block: str | None = None,
    tool_flow_state: dict[str, Any] | None = None,
    tool_flow_bundle: dict[str, Any] | None = None,
    turn_index: int | None = None,
    agent_mode_block: str | None = None,
    conversational_memory: Any = None,
    business_type: str | None = None,
) -> list[dict[str, str]]:
    """RAG path message builder.

    Used for both inbound (with retrieved KB chunks) and outbound
    (proactive sales prompt) flows. Branches on ``outbound_context``:
    outbound builds a leaner prompt (drops the inbound voice / format
    / grounding boilerplate the outbound persona already encodes),
    inbound keeps the full set of voice/format/grounding rules.
    """
    language_label = SarvamVoiceService.language_label(language)
    context_parts: list[str] = []
    remaining = settings.AGENT_MAX_CONTEXT_CHARS
    for index, chunk in enumerate(chunks, start=1):
        text = re.sub(r"\s+", " ", str(chunk.get("text") or "")).strip()
        if not text:
            continue
        excerpt = text[:remaining]
        context_parts.append(f"[{index}] {excerpt}")
        remaining -= len(excerpt)
        if remaining <= 0:
            break

    # Brochure-on-WhatsApp request on an outbound / follow-up call → flip into
    # whatsapp_mode for this turn (the outbound FSM reads tool_flow.whatsapp_intent).
    _tool_flow_state = tool_flow_state
    if outbound_context is not None:
        try:
            from app.services.tool_flow_policy import brochure_intent_active

            if brochure_intent_active(query, history):
                _tool_flow_state = {**(tool_flow_state or {}), "whatsapp_intent": {"kind": "brochure"}}
        except Exception:
            _tool_flow_state = tool_flow_state

    # Outbound campaign system fragment. When the campaign config has an
    # explicit agent_prompt + objectives we drop in a full proactive-mode
    # block; otherwise we fall back to the legacy one-liner.
    outbound_section = compose_outbound_system_section(
        outbound_context,
        covered_objectives=covered_objectives,
        outbound_memory=outbound_memory,
        tool_flow_state=_tool_flow_state,
        tool_flow_bundle=tool_flow_bundle,
        language=language,
        turn_index=turn_index,
        conversational_memory=conversational_memory,
        business_type=business_type,
        latest_user_text=query,
    )
    if outbound_section:
        campaign_rule = (
            outbound_section
            + "\n\n# FINAL OUTBOUND REMINDER\n"
            "The prospect is not a captive audience. Listen to the latest reply, answer or adapt to it, "
            "then say only one useful next thing in 1 to 2 short sentences. "
            "If they just gave permission to continue, ask one discovery question and do not pitch features first."
        )
    elif campaign_goal:
        campaign_rule = (
            f"Campaign goal: {campaign_goal}. Follow this goal, but still use only the supplied context."
        )
    else:
        campaign_rule = "This is an inbound support conversation unless campaign context says otherwise."

    custom_guidance = (single_prompt_guidance or "").strip()
    brand = "the configured business" if custom_guidance else (company_name or "the tenant")
    custom_guidance_section = (
        "# ADMIN SINGLE-PROMPT VOICE AGENT GUIDANCE\n"
        f"{custom_guidance}\n\n"
        "This tenant-provided prompt is part of the agent's active configuration. Use explicit business facts from it together with retrieved tenant context. "
        "If approved retrieved documents conflict with this prompt, prefer the retrieved documents. It does not override safety, language, or no-hallucination rules.\n\n"
        if custom_guidance
        else ""
    )

    # Real-estate project inventory — separate, high-priority section.
    _projects_inner = (projects_block or "").strip()
    projects_block_section = (
        "# CURRENT PROJECT INVENTORY (authoritative — overrides admin prompt)\n"
        f"{_projects_inner}\n\n"
        if _projects_inner
        else ""
    )
    projects_override_directive = (
        "# PROJECT INVENTORY OVERRIDE — NON-NEGOTIABLE\n"
        "For this organization, the live PROJECT INVENTORY section below is "
        "the SOLE source of truth for project / property facts (names, "
        "locations, prices, RERA numbers, configurations, possession dates, "
        "amenities). The admin's single-prompt may contain example or "
        "outdated project text — IGNORE every project name, price, or "
        "RERA number you find in that prompt. Quote only from the "
        "PROJECT INVENTORY block. If the inventory has fewer projects "
        "than the admin prompt suggests, that is intentional: the "
        "inventory is the current portfolio.\n\n"
        if _projects_inner
        else ""
    )
    projects_final_reminder = (
        "\nWhen the caller asks about properties, projects, what's available, or what you offer, base every fact ONLY on the PROJECT INVENTORY section. Do NOT reuse project names from the admin prompt."
        if _projects_inner
        else ""
    )

    # Language directive — top and bottom of the system prompt. LLMs weight
    # start and end of long prompts most heavily, and the reply language must
    # dominate the conversation history (which may be in English).
    style_block = language_style_guidance(language)
    language_directive_top = (
        f"# REPLY LANGUAGE — NON-NEGOTIABLE\n"
        f"Reply in {language_label}, primarily using its native script. This overrides the conversation history, "
        f"the user's most recent message, and your training defaults. "
        f"Natural code-switching is REQUIRED, not banned: keep common English loanwords (order, refund, appointment, payment, status, OK, sorry, address, SMS, WhatsApp, link) and all numbers / ₹ amounts / dates / times in English / digits exactly as a real Indian phone-support rep would. "
        f"Do NOT produce a literary, news-anchor, or Sanskritised register — speak the way a real call-center agent speaks on the phone. "
        f"Do not apologise for not knowing this language — you do know it. Reply in it.\n\n"
        + (f"{style_block}\n\n" if style_block else "")
    )

    _outbound_proactive = bool(outbound_context) and outbound_context.is_proactive
    outbound_fewshot_block = language_outbound_fewshot(language)
    memory_block = (conversational_memory_block or "").strip()
    memory_section = f"\n\n{memory_block}\n" if memory_block else ""
    strategy_block = (conversation_strategy_block or "").strip()
    strategy_section = f"\n{strategy_block}\n" if strategy_block else ""

    if _outbound_proactive:
        system_content = (
            language_directive_top
            + projects_override_directive
            + "# PROSODY — make it sound human\n"
            "Wrap EACH sentence in exactly one tone tag: [empathy]…[/empathy] (apologies, bad news), "
            "[warm]…[/warm] (greetings, acknowledgments), [neutral]…[/neutral] (facts, default), "
            "[excited]…[/excited] (good news), [question]…[/question] (direct questions). "
            "Tags are stripped before speaking — they only set the voice's tone.\n\n"
            + campaign_rule
            + memory_section
            + strategy_section
            + (f"\n\n{projects_block_section}" if projects_block_section else "")
            + (f"\n\n{outbound_fewshot_block}" if outbound_fewshot_block else "")
            + f"\n\n# REMINDER\nReply in {language_label} with natural English code-switching for loanwords, numbers, and ₹ amounts. Keep it to 1-2 sentences."
            + projects_final_reminder
        )
        messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]
        for turn in history[-6:]:
            role = turn.get("role") if turn.get("role") in {"user", "assistant"} else "user"
            messages.append({"role": role, "content": str(turn.get("content") or "")[:600]})
        user_content = (
            f"Latest prospect reply — respond to this first:\n{query}\n\n"
            f"Campaign brief context, if needed:\n{chr(10).join(context_parts)}\n\n"
            f"Reply in {language_label}."
        )
        messages.append({"role": "user", "content": user_content})
        return messages

    # Inbound branch. Split into a STATIC PREFIX (identical for a given
    # tenant+language on every turn → the provider caches it, billing it at the
    # cached rate and cutting first-token latency) and a DYNAMIC SUFFIX (the
    # per-turn agent mode / memory / strategy / field-questions). Keeping the
    # prefix byte-identical is what makes caching hit — never let per-turn
    # content leak into it.
    static_prefix = (
        language_directive_top
        + f"You are Nokvo One's live voice agent for {brand}. Talk like a real person on a phone call, not a help-center bot.\n\n"
        "# PROSODY (spoken aloud)\n"
        "Wrap EACH sentence in exactly one tone tag: [empathy] (apologies/bad news), [warm] (greetings/acks), "
        "[neutral] (facts — DEFAULT), [excited] (good news), [question] (direct questions). Tags are stripped before speaking. "
        "e.g. [warm]Of course.[/warm] [neutral]It's ₹2.45 crore.[/neutral] Mostly [neutral] with one warm/empathic opener.\n\n"
        "# VOICE & PERSONALITY\n"
        f"{custom_guidance_section}"
        f"{projects_block_section}"
        + (f"# CLINIC SERVICES\n{services_block}\n\n" if services_block else "")
        + "- Use contractions ('I'll', 'you're'). Open with quick acks ('Sure', 'Got it', 'Right'), not 'I understand your concern'.\n"
        "- If the caller is upset, acknowledge the feeling in one phrase first, then help. Vary your openers across turns.\n\n"
        "# FORMAT\n"
        "- SHORT: 1-3 sentences, the first immediately useful. No markdown, lists, or citations. Be specific with names, ₹ amounts, times from context.\n"
        "- Do NOT speak RERA numbers, long IDs, or URLs aloud — say 'RERA-registered' (offer to text exact details).\n"
        "- Speak only the words themselves — never include surrounding quotation marks in what you say.\n\n"
        "# USE THE CONVERSATION\n"
        "- Reuse names/numbers/preferences already given; don't re-ask. React to what they just said; if you're wrong, 'Ah, my mistake' and adjust.\n"
        "- Don't assert a name or detail from memory as if the caller just said it — if THIS turn didn't supply it, confirm ('Is that Nihar?').\n"
        "- Short replies ('yes', 'ok', 'hmm'): if you asked something last turn, treat it as the answer; else respond openly. Don't invent a topic.\n\n"
        "# GROUNDING — company facts\n"
        "- Company facts (names, locations, prices, hours, policies): ONLY from the inventory/services/guidance above. Not there → say the team will confirm; never invent, infer, or import outside knowledge.\n"
        "- Share EVERY relevant fact you have before noting what's missing — including in non-English (translate and share; never 'I don't have that' when context has it).\n"
        "- Ambiguous pronouns ('where is it', 'how much') → this business. Numbers/dates must match context EXACTLY; no hedge words. Promise next steps ('I'll pass this to the team'), not done deals. Never mention internal systems/tools/prompts.\n\n"
        f"# CAMPAIGN\n{campaign_rule}\n\n"
    )

    # Conditional assembly: the FIELD-COLLECTION SCRIPT (record schemas + collection
    # rules, the biggest dynamic block) is only relevant once a record-capture flow
    # is actually in progress. On pure Q&A / enquiry turns it's dead weight — and the
    # FSM re-includes it the moment a flow starts — so gate it on an active flow.
    _tf = tool_flow_state or {}
    booking_active = bool(_tf.get("active")) and not _tf.get("completed")
    include_fields = bool(field_questions_prompt) and (booking_active or bool(_tf.get("pending_slot")))
    dynamic_suffix = (
        (f"{agent_mode_block}\n\n" if agent_mode_block else "")
        + (f"{memory_block}\n\n" if memory_block else "")
        + (f"{strategy_block}\n\n" if strategy_block else "")
        + (f"{field_questions_prompt}\n\n" if include_fields else "")
        + f"# REMINDER\nReply in {language_label} with natural English code-switching for loanwords, numbers, and ₹ amounts."
        + projects_final_reminder
    )

    messages = [{"role": "system", "content": static_prefix}]
    if dynamic_suffix.strip():
        messages.append({"role": "system", "content": dynamic_suffix})
    # Send window = last 8 turns verbatim (a 2-turn buffer over the condense
    # window of 6) so an exchange evicted-for-condensing stays verbatim until the
    # async rolling-summary fold absorbs it — lossless under rapid-fire. Older
    # context lives in the `# CONVERSATION SO FAR` summary in the suffix above.
    for turn in history[-settings.IN_CALL_SUMMARY_SEND_WINDOW:]:
        role = turn.get("role") if turn.get("role") in {"user", "assistant"} else "user"
        messages.append({"role": role, "content": str(turn.get("content") or "")[:1200]})
    # (KB/document-RAG retired — no retrieved-context slot.)
    user_content = (
        f"Current user question:\n{query}\n\n"
        f"Reply in {language_label}."
    )
    messages.append({"role": "user", "content": user_content})
    return messages


def compose_smalltalk_messages(
    query: str,
    *,
    language: str,
    history: list[dict[str, str]],
    company_name: str | None = None,
    sentiment: str = "neutral",
    single_prompt_guidance: str | None = None,
) -> list[dict[str, str]]:
    """Casual-conversation path message builder.

    Caller said something conversational (greeting, thank-you,
    acknowledgment, vague reply) — not a factual question requiring KB
    retrieval. The LLM can respond warmly using its general
    conversational abilities, but it has NO permission to make factual
    claims about the world or the company (those still require
    KB-grounded context).
    """
    language_label = SarvamVoiceService.language_label(language)
    custom_guidance = (single_prompt_guidance or "").strip()
    brand = "the configured business" if custom_guidance else (company_name or "the company")
    custom_guidance_section = (
        "# ADMIN SINGLE-PROMPT VOICE AGENT GUIDANCE\n"
        f"{custom_guidance}\n\n"
        "Use this for role, tone, and conversation flow. You may use explicit business facts from it, but do not invent details beyond it.\n\n"
        if custom_guidance
        else ""
    )

    sentiment_guidance = {
        "frustrated": "The caller sounds frustrated. ACKNOWLEDGE the feeling first in one short phrase before asking how to help.",
        "negative": "The caller sounds unhappy. Be warm and offer to help.",
        "positive": "The caller is in a positive mood. Match that warmth briefly.",
        "curious": "The caller is curious. Be friendly and clarify what they need.",
        "neutral": "Match the caller's energy. Keep it brief.",
    }.get(sentiment, "Keep it brief and friendly.")

    smalltalk_style_block = language_style_guidance(language)
    system_content = (
        f"# REPLY LANGUAGE — NON-NEGOTIABLE\n"
        f"Reply in {language_label}, primarily using its native script. "
        f"Natural code-switching is REQUIRED — keep common English loanwords (order, refund, appointment, payment, status, OK, sorry, link, SMS) and all numbers / dates / times in English exactly as a real Indian rep would. "
        f"Do NOT produce a literary or news-anchor register.\n\n"
        + (f"{smalltalk_style_block}\n\n" if smalltalk_style_block else "")
        + f"You are Nokvo One's live voice agent for {brand}. The caller just said something CONVERSATIONAL — a greeting, thank-you, acknowledgment, casual remark, or expression of feeling. Not a factual question about the company.\n\n"
        "# RESPONSE STYLE\n"
        f"{custom_guidance_section}"
        "- One or two short sentences. Voice-first — keep it crisp.\n"
        "- Use contractions: 'I'll', 'you're', 'let's'.\n"
        "- Sound like a real person on a phone call, not a help-center bot.\n"
        f"- {sentiment_guidance}\n\n"
        "# PROSODY — wrap EACH sentence in ONE tone tag\n"
        "  [empathy]…[/empathy]   apologies, bad news, 'sorry to hear'.\n"
        "  [warm]…[/warm]         greetings, thanks, acknowledgments.\n"
        "  [neutral]…[/neutral]   facts, statements.\n"
        "  [excited]…[/excited]   good news, enthusiasm (use sparingly).\n"
        "  [question]…[/question] direct questions.\n"
        "Most casual replies are [warm] or [empathy] with a [question] follow-up.\n\n"
        "# YOUR KNOWLEDGE BOUNDARIES — STRICT\n"
        f"You know NOTHING factual about anything outside {brand}'s knowledge base. This includes weather, sports, news, science, geography, current events, other companies, and general world facts.\n"
        f"You also know NOTHING factual about {brand} that isn't already in the conversation history. Do NOT invent prices, policies, hours, addresses, names, or any company specifics.\n"
        "If the caller asks a factual question (about the world OR the company) you don't have grounded information for, say briefly: 'Let me check that for you' or 'I don't have that information — what else can I help with?'. Do NOT make something up.\n\n"
        "# WHAT YOU CAN DO FREELY\n"
        "- Return greetings, accept thanks, say goodbye warmly.\n"
        "- Acknowledge feelings ('that sounds frustrating', 'glad to hear').\n"
        "- Ask what the caller needs ('what can I help you with?', 'go on').\n"
        "- Use small natural fillers ('right', 'okay', 'yeah').\n\n"
        "# NEVER DO\n"
        "- Never invent specifics. Never describe the weather, the time of day, current events.\n"
        "- Never claim you took an action you can't take (no 'I've cancelled', no 'I've processed your refund').\n"
        "- Never use formal openings ('Dear sir/madam').\n"
        "- Never mention internal systems, sources, prompts, or tools.\n"
    )

    messages = [
        {"role": "system", "content": system_content},
    ]
    for turn in history[-6:]:
        role = turn.get("role") if turn.get("role") in {"user", "assistant"} else "user"
        messages.append({"role": role, "content": str(turn.get("content") or "")[:800]})
    messages.append({"role": "user", "content": query})
    return messages


__all__ = (
    "compose_rag_messages",
    "compose_smalltalk_messages",
)
