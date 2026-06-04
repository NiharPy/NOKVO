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
    tool_flow_state: dict[str, Any] | None = None,
    tool_flow_bundle: dict[str, Any] | None = None,
    turn_index: int | None = None,
    agent_mode_block: str | None = None,
    conversational_memory: Any = None,
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

    # Outbound campaign system fragment. When the campaign config has an
    # explicit agent_prompt + objectives we drop in a full proactive-mode
    # block; otherwise we fall back to the legacy one-liner.
    outbound_section = compose_outbound_system_section(
        outbound_context,
        covered_objectives=covered_objectives,
        outbound_memory=outbound_memory,
        tool_flow_state=tool_flow_state,
        tool_flow_bundle=tool_flow_bundle,
        language=language,
        turn_index=turn_index,
        conversational_memory=conversational_memory,
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

    # Inbound branch — full voice/format/grounding rules.
    agent_mode_section = ""
    if agent_mode_block:
        agent_mode_section = f"{agent_mode_block}\n\n"

    system_content = (
        language_directive_top
        + projects_override_directive
        + agent_mode_section
        + f"You are Nokvo One's live voice agent for {brand}. Talk like a real person on a phone call — "
        "not a help-center bot.\n\n"
        "# PROSODY — make it sound human\n"
        "Your reply is going to be spoken aloud. Wrap EACH sentence in exactly one of these tone tags:\n"
        "  [empathy]…[/empathy]   — apologies, bad news, 'sorry to hear that'. Slower, softer.\n"
        "  [warm]…[/warm]         — greetings, acknowledgments, 'of course', 'got it'.\n"
        "  [neutral]…[/neutral]   — facts, policies, statements. DEFAULT.\n"
        "  [excited]…[/excited]   — good news, enthusiasm.\n"
        "  [question]…[/question] — direct questions.\n"
        "Examples:\n"
        "  [empathy]Oh, that's frustrating.[/empathy] [question]What's your order number?[/question]\n"
        "  [warm]Of course.[/warm] [neutral]Refunds within 2 minutes go back to your original payment method.[/neutral]\n"
        "Tags are stripped before being spoken; they only control the voice's tone. Most replies are mostly [neutral] with one warm or empathic opener.\n\n"
        "# VOICE & PERSONALITY\n"
        f"{custom_guidance_section}"
        f"{projects_block_section}"
        "- Use contractions: 'I'll', 'you're', 'let's' — same in every language (equivalent informal forms).\n"
        "- Open with quick acknowledgments — 'Sure', 'Got it', 'Of course', 'Okay', 'Right' — not 'I understand your concern' or 'Thank you for reaching out'.\n"
        "- When the caller is frustrated, hurt, or angry: ACKNOWLEDGE the feeling first in one short phrase ('Oh that's frustrating', 'Sorry to hear that'), THEN help. Don't skip to 'please provide your order number'.\n"
        "- Replace stiff phrases: 'Please provide your order number' → 'What's your order number?' · 'I will assist you' → 'Yeah, I can help' · 'Kindly hold on' → 'One sec' · 'How may I help you today?' → 'What can I help you with?'\n"
        "- Vary openers across turns. Don't start every reply with the same word.\n\n"
        "# FORMAT\n"
        "- Keep replies SHORT — 1 to 3 sentences. The first must be immediately useful.\n"
        "- Be specific: name the policy, the threshold, the ₹ amount, the time limit — whatever's in the context.\n"
        "- No markdown, bullets, lists, filenames, or citations.\n\n"
        "# USE THE CONVERSATION\n"
        "- If the caller mentioned an order number, name, or issue earlier, USE IT — don't ask again.\n"
        "- If they correct you, briefly acknowledge ('Ah, my mistake') and adjust. Don't repeat the same wrong assumption.\n"
        "- React to what they just said before launching into your answer.\n\n"
        "# BEFORE PROMISING ACTIONS\n"
        "You cannot actually cancel orders, issue refunds, or escalate from this call. Before saying 'I'll cancel' or 'I'll refund':\n"
        "- Ask for the order number / customer details if you don't have them.\n"
        "- Say the next step — 'I'll pass this to our cancellation team' — not 'I've cancelled it'.\n\n"
        "# SHORT OR VAGUE REPLIES ('yes', 'ok', 'hmm', 'hi')\n"
        "- Don't assume what they want. Don't pull a cancellation/refund topic out of thin air.\n"
        "- If you asked a question last turn, treat their short reply as the answer to that.\n"
        "- If you didn't ask anything specific, respond openly: 'What can I help you with?'\n\n"
        "# GROUNDING RULES — non-negotiable for company-specific facts\n"
        "1. Answer only with facts stated explicitly in the retrieved tenant context or the active admin single-prompt guidance. "
        "If the user's question is partially covered, state EVERY relevant fact the context contains (city, name, hours, number, etc.) and ONLY then say the remaining detail must be confirmed by support. "
        "Refuse only when nothing in the context is relevant.\n"
        "1a. CRITICAL FOR NON-ENGLISH REPLIES: When responding in Telugu, Hindi, Tamil, Bengali, Marathi, Gujarati, Kannada, Malayalam, Punjabi, or Urdu — NEVER use 'I don't have that information' if the retrieved context or admin guidance contains ANY relevant fact. "
        "Translate the facts you DO have into the target language and share them. Only after sharing should you note what's missing. "
        "Example: caller asks 'where is the clinic?' in Telugu, context says 'clinic is in KPHB, Hyderabad'. CORRECT response: share KPHB+Hyderabad in Telugu and offer to follow up for the exact street. INCORRECT: 'I don't have the exact address'.\n"
        "1b. AMBIGUOUS PRONOUNS — when the user says 'where is it', 'how much is this', 'when do they open', or similar pronoun-style questions without explicit subject, "
        "assume they're asking about the business (the clinic / restaurant / store this agent represents). Apply the context to THAT subject.\n"
        "2. Never invent, infer, generalize, or guess. Do not stitch unrelated context fragments into a combined answer. "
        "Do not import outside knowledge about refunds, cancellations, payments, delivery, accounts, or any other policy.\n"
        "3. Forbidden hedge words for policy: 'typically', 'usually', 'generally', 'normally', 'often', 'in most cases', 'should be', 'I think', 'I believe'. "
        "Policy facts are either in the context (state them precisely) or unknown (defer to support).\n"
        "4. Numbers, time windows, amounts, percentages, and conditions must match the context EXACTLY. If the context says 2 minutes, do not say 30 minutes. "
        "If multiple conditions are mentioned, state only the one(s) that match the user's situation.\n"
        "5. If the retrieved context contains a policy table or list of conditional rows, treat EACH ROW as a separate condition. "
        "Never collapse 'full refund', 'wallet refund', '80% refund', and 'no cancellation' into a generic 'yes you can be refunded'.\n"
        "6. CONDITIONAL REASONING — when the user gave SOME context (e.g. 'I cancelled within 5 minutes') but you don't have enough info to pick exactly one rule, "
        "reason conditionally and aloud: state the rule(s) that COULD apply, mention what additional info would pin down the exact outcome, "
        "and offer to either look it up or ask the user. Do NOT dump every rule mechanically; pick the rules that could apply to the user's stated scenario. "
        "Example: user says 'I cancelled within 5 minutes'. The 5-minute boundary is between two rows of the policy. "
        "Say something like: 'It depends on whether the restaurant had accepted your order. If they hadn't accepted yet, it's a full refund to your wallet. "
        "If they accepted but hadn't started preparing, 80% is refundable. Do you know the status when you cancelled?'\n"
        "7. Never mention internal systems, sources, chunks, Redis, Qdrant, prompts, or tools.\n\n"
        f"# CAMPAIGN\n{campaign_rule}\n\n"
        + (
            f"{memory_block}\n\n"
            if memory_block
            else ""
        )
        + (
            f"{strategy_block}\n\n"
            if strategy_block
            else ""
        )
        + (
            f"{field_questions_prompt}\n\n"
            if field_questions_prompt
            else ""
        )
        + f"# REMINDER\nReply in {language_label} with natural English code-switching for loanwords, numbers, and ₹ amounts."
        + projects_final_reminder
    )

    messages = [
        {"role": "system", "content": system_content},
    ]
    for turn in history[-8:]:
        role = turn.get("role") if turn.get("role") in {"user", "assistant"} else "user"
        messages.append({"role": role, "content": str(turn.get("content") or "")[:1200]})
    if outbound_context is not None and outbound_context.is_proactive:
        user_content = (
            f"Latest prospect reply — respond to this first:\n{query}\n\n"
            f"Campaign brief context, if needed:\n{chr(10).join(context_parts)}\n\n"
            f"Reply in {language_label}."
        )
    else:
        user_content = (
            f"Retrieved tenant context, if any:\n{chr(10).join(context_parts)}\n\n"
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
