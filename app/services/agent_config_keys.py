"""Agent configuration keys (vertical-prompt plumbing).

These ``provider_status`` keys + the policy-version helper are *agent config*,
not Knowledge Base — they drive the single-prompt / vertical-prompt system and
the runtime bundle cache key. They were historically defined in
``agent_knowledge_service``; they live here so the agent config survives the
retirement of the Knowledge Base (document RAG / Qdrant / embeddings).
"""
from __future__ import annotations

from app.models.tenant_resources import TenantResources

# provider_status keys
AGENT_POLICY_CARDS_KEY = "agent_policy_cards"
AGENT_POLICY_VERSION_KEY = "agent_policy_version"
AGENT_SINGLE_PROMPT_CONFIG_KEY = "single_prompt_voice_agent"


def policy_version(tenant_res: TenantResources) -> str:
    """The tenant's agent policy version (bundle cache-busting marker)."""
    provider_status = dict(getattr(tenant_res, "provider_status", None) or {})
    return str(provider_status.get(AGENT_POLICY_VERSION_KEY) or "pv_default")
