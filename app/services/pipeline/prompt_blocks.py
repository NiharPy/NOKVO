"""Prompt-block builders: policy cards, single-prompt guidance, project /
services inventory blocks, and the field-questions prompt.

Extracted from nokvo_one_voice_pipeline.py (turn_router helpers pattern:
functions taking ``helpers`` receive the ``NokvoOneVoicePipeline`` class and
call sibling statics through it, so class-attribute monkeypatches keep
working). The pipeline class keeps delegating wrappers - no API change.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization
from app.models.tenant_resources import TenantResources
from app.services.agent_config_keys import (
    AGENT_POLICY_CARDS_KEY,
    AGENT_SINGLE_PROMPT_CONFIG_KEY,
    policy_version as _agent_policy_version,
)
from app.services.agent_runtime_bundle import RuntimeBundle, get_bundle as get_runtime_bundle
from app.services.tool_flow_questions import build_tool_flow_questions, format_field_questions_prompt

logger = logging.getLogger(__name__)


def _policy_card_chunks(tenant_res: TenantResources, policy_version: str) -> list[dict[str, Any]]:
    """Synthesize retrieval chunks from active policy cards.

    These aren't real Qdrant results — they're the policy's own
    ``source_text``, formatted to look like a chunk so the existing
    ``_messages`` builder treats them as grounding context. Used as a
    last-resort when Qdrant retrieval came up empty on a sensitive
    cancellation/refund intent.
    """
    provider_status = dict(tenant_res.provider_status or {})
    cards = provider_status.get(AGENT_POLICY_CARDS_KEY) or []
    out: list[dict[str, Any]] = []
    for card in cards:
        if card.get("approval_status") not in (None, "approved"):
            continue
        if card.get("status") not in (None, "active", "ok"):
            continue
        if policy_version and card.get("policy_version") and card.get("policy_version") != policy_version:
            continue
        text = (card.get("source_text") or "").strip()
        if not text:
            # Build text from the structured conditions when source_text
            # isn't preserved.
            conds = card.get("conditions") or []
            lines = [str(cond.get("customer_message") or "").strip() for cond in conds]
            text = "\n".join(line for line in lines if line)
        if not text:
            continue
        out.append(
            {
                "document_id": str(card.get("document_id") or ""),
                "document_name": str(card.get("source_section_title") or "Policy"),
                "chunk_id": str(card.get("id") or ""),
                "text": text[:4000],
                "score": 1.0,
                "metadata": {
                    "source_type": "agent_policy_card",
                    "topic": card.get("topic"),
                    "policy_version": card.get("policy_version"),
                    "sensitivity": "sensitive",
                    "source_title": card.get("source_section_title") or "Policy",
                },
            }
        )
    return out


def _single_prompt_guidance(tenant_res: TenantResources) -> str:
    # Explicit-admin-override probe only. This gates whether to SUPPRESS
    # the built-in FSMs (clinic appointments, etc.) — NOT whether the agent
    # has a persona. The curated per-vertical persona is always present and
    # is composed separately on the async bundle path
    # (``agent_runtime_bundle._single_prompt_guidance``). Returning "" when
    # no legacy override is configured (the normal case now) lets the
    # built-in FSMs run.
    provider_status = dict(tenant_res.provider_status or {})
    config = provider_status.get(AGENT_SINGLE_PROMPT_CONFIG_KEY) or {}
    if not isinstance(config, dict) or not config.get("enabled"):
        return ""
    prompt = str(config.get("prompt") or "").strip()
    return prompt[:8000]


def _single_prompt_enabled(helpers: Any, tenant_res: TenantResources) -> bool:
    return bool(helpers._single_prompt_guidance(tenant_res))


async def _projects_block_for_bundle(
    db: AsyncSession | None,
    bundle: "RuntimeBundle",
) -> tuple[str, list]:
    """Return ``(inventory_block, active_projects)`` for a real-estate org,
    or ``("", [])`` otherwise.

    The block is injected as its own top-level system section by the
    voice prompt builder so the live agent treats it as the source of
    truth for inventory questions (overriding any project names the
    admin may have hardcoded into their single-prompt text). The project
    list is handed back so callers can reuse it (project-name hints,
    objection focus) without a second round-trip — the underlying
    ``load_active_projects`` is uncached."""
    if (bundle.organization_industry or "").lower() != "real_estate":
        return "", []
    organization_id = getattr(bundle.organization, "id", None)
    if organization_id is None:
        return "", []
    try:
        from app.services.real_estate_project_service import (
            load_active_projects,
            projects_prompt_section,
        )

        projects = await load_active_projects(db, organization_id)
    except Exception:
        return "", []
    block = projects_prompt_section(projects)
    # Append the org-wide site-visit working window so the live agent can
    # refuse out-of-hours requests conversationally (the booking step also
    # enforces it deterministically via the out-of-hours guard).
    try:
        from app.services.nokvo_one_assignment_service import working_hours_prompt_line

        # NOTE: NokvoOneAssignmentService is intentionally NOT imported (verbatim
        # from the pre-extraction module, where the name was also unbound in this
        # scope): the NameError lands in the except below, so the hours line has
        # never been appended here. Import it to actually enable the feature.
        org_defaults = await NokvoOneAssignmentService.resolve_org_working_window(db, organization_id)
        hours_line = working_hours_prompt_line(org_defaults)
        if hours_line:
            block = f"{block}\n\n{hours_line}" if block else hours_line
    except Exception:
        pass
    return block, projects


async def _services_block_for_bundle(
    db: AsyncSession | None,
    bundle: "RuntimeBundle",
) -> str:
    """Authoritative clinic SERVICES catalog block (services + which doctors
    + price/duration) for a clinic org, else "". Injected as its own system
    section so the agent quotes real services/doctors and routes booking
    service-first. Loaded per-call (uncached) so edits reflect immediately."""
    if (bundle.organization_industry or "").lower() != "clinics":
        return ""
    organization_id = getattr(bundle.organization, "id", None)
    if organization_id is None:
        return ""
    try:
        from app.services.clinic_service_service import (
            load_services_with_providers,
            services_prompt_section,
        )

        services = await load_services_with_providers(db, organization_id)
    except Exception:
        return ""
    return services_prompt_section(services)


def _focus_project_summary(
    projects: list,
    conversational_memory: Any,
) -> str | None:
    """One-line summary of the project the caller named (matched from
    FACT_PROPERTY), for the strategy layer's price/competitor objection
    focus. ``None`` when no property is known or no confident match exists."""
    if conversational_memory is None or not projects:
        return None
    try:
        from app.services.conversational_memory import FACT_PROPERTY
        from app.services.real_estate_project_service import (
            find_project_match,
            project_summary_lines,
        )

        spoken = conversational_memory.get(FACT_PROPERTY)
        if not spoken:
            return None
        project = find_project_match(projects, project_name=str(spoken))
        if project is None:
            return None
        lines = project_summary_lines([project])
        return lines[0] if lines else None
    except Exception:
        return None


async def _voice_business_context(
    db: AsyncSession | None,
    tenant_res: TenantResources,
) -> tuple[Organization, dict[str, Any], list[dict[str, Any]]] | None:
    """Resolve the ``(organization, overrides, custom_tabs)`` tuple via
    the per-tenant :class:`RuntimeBundle` cache so repeat turns avoid a
    DB round-trip and a custom_tabs rebuild."""
    bundle = await get_runtime_bundle(db, tenant_res)
    return bundle.as_business_context_tuple()


def _field_questions_prompt_for_bundle(
    bundle: "RuntimeBundle",
    *,
    language: str,
    project_names: list[str] | None = None,
) -> str:
    """Build the "use these exact phrasings" prompt block from the
    per-tenant runtime bundle. Empty string when no record-creation
    fields are configured — keeps the prompt lean for inbound calls
    that aren't collecting structured records.

    ``project_names`` (real-estate only) is the live DB list and is
    substituted into the Project slot's question so the LLM can't fall
    back to a project list baked into the admin's single prompt.
    """
    try:
        catalog = build_tool_flow_questions(
            bundle.organization_industry,
            bundle.overrides,
            bundle.custom_tabs,
        )
    except Exception:
        return ""
    return format_field_questions_prompt(
        catalog, language=language, project_names=project_names
    )
