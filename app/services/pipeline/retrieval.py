"""Retrieval orchestration for the voice pipeline: KB/policy chunk search,
dual-language fallback, outbound-doc chunking, and cacheability rules.

Extracted from nokvo_one_voice_pipeline.py (turn_router helpers pattern:
functions taking ``helpers`` receive the ``NokvoOneVoicePipeline`` class and
call sibling statics through it, so class-attribute monkeypatches keep
working). The pipeline class keeps delegating wrappers - no API change.
"""
from __future__ import annotations

import logging
from time import perf_counter
from typing import Any
import asyncio
import re

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.tenant_resources import TenantResources
from app.services.agent_outbound_context import OutboundCampaignContext
from app.services.fast_intent_router import IntentResult, detect_policy_keyword
from app.services.pipeline.text_norm import _normalize

logger = logging.getLogger(__name__)


_SENSITIVE_OR_DYNAMIC_RE = re.compile(
    r"\b(order|ticket|payment|paid|refund status|account|phone|email|address|otp|password|card|upi|bank|delete|cancel my)\b",
    re.IGNORECASE,
)


def _cacheable(query: str, answer: str, chunks: list[dict[str, Any]]) -> bool:
    if _SENSITIVE_OR_DYNAMIC_RE.search(query or ""):
        return False
    for chunk in chunks:
        metadata = dict(chunk.get("metadata") or {})
        if metadata.get("sensitivity") == "sensitive":
            return False
    return bool(answer.strip())


def _map_point(point: Any) -> dict[str, Any]:
    payload = dict(getattr(point, "payload", {}) or {})
    return {
        "document_id": str(payload.get("document_id") or ""),
        "document_name": str(payload.get("document_name") or payload.get("source_title") or "Document"),
        "chunk_id": str(payload.get("chunk_id") or getattr(point, "id", "")),
        "text": str(payload.get("text") or ""),
        "score": float(getattr(point, "score", 0.0) or 0.0),
        "metadata": {
            "source_type": payload.get("source_type"),
            "source_kind": payload.get("source_kind"),
            "document_type": payload.get("document_type"),
            "status": payload.get("status"),
            "document_status": payload.get("document_status"),
            "language": payload.get("language"),
            "campaign_id": payload.get("campaign_id"),
            "topic": payload.get("topic"),
            "sensitivity": payload.get("sensitivity"),
            "source_title": payload.get("source_title"),
            "section_id": payload.get("section_id"),
            "section_title": payload.get("section_title"),
            "parent_section_text": payload.get("parent_section_text"),
        },
    }


def _chunks_from_outbound_doc(
    outbound_context: OutboundCampaignContext | None,
) -> list[dict[str, Any]]:
    """Materialize the campaign-supplied brief as retrieval chunks.

    Outbound is a different agent from inbound — its only data source
    is whatever the operator pinned into the campaign config (the
    ``doc_text`` field on :class:`OutboundCampaignContext`). We
    return Qdrant-shaped chunks so the existing prompt builder
    composes them the same way it does inbound retrievals. The
    agent_prompt rides through the separate outbound system fragment
    and does not need to be a chunk.
    """
    if outbound_context is None:
        return []
    text = (getattr(outbound_context, "doc_text", "") or "").strip()
    if not text:
        return []
    # Split into ~350-word chunks so a long campaign brief doesn't
    # blow the context window on a single LLM call. The reader sees
    # them as ordered excerpts from "Campaign Brief".
    words_per_chunk = 350
    words = text.split()
    out: list[dict[str, Any]] = []
    for i in range(0, len(words), words_per_chunk):
        slice_text = " ".join(words[i : i + words_per_chunk]).strip()
        if not slice_text:
            continue
        out.append(
            {
                "text": slice_text,
                "score": 1.0,
                "chunk_id": f"outbound_doc_chunk_{i // words_per_chunk}",
                "document_id": "outbound_campaign_brief",
                "document_name": "Campaign Brief",
                "metadata": {"source": "outbound_campaign", "approved": True},
            }
        )
        if len(out) >= 6:
            # Cap at 6 chunks so a very long brief doesn't dominate
            # the prompt; the system fragment already carries the
            # persona + objectives.
            break
    return out


def _expand_parent_section(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace a chunk's text with its parent section when the chunk came
    from a likely table/list section (cancellation/refund/policy). Sliced
    rows lose the conditional structure; the parent section restores it
    for the LLM."""
    expanded: list[dict[str, Any]] = []
    seen_sections: set[str] = set()
    for chunk in chunks:
        section_id = (chunk.get("metadata") or {}).get("section_id") if isinstance(chunk.get("metadata"), dict) else None
        section_title = ((chunk.get("metadata") or {}).get("section_title") or "") if isinstance(chunk.get("metadata"), dict) else ""
        parent_text = ((chunk.get("metadata") or {}).get("parent_section_text") or "") if isinstance(chunk.get("metadata"), dict) else ""
        policy_section = bool(
            re.search(r"cancel|refund|policy|table", section_title or "", re.IGNORECASE)
        )
        if policy_section and parent_text and section_id and section_id not in seen_sections:
            seen_sections.add(section_id)
            copied = dict(chunk)
            copied["text"] = parent_text
            copied["expanded_from_parent_section"] = True
            expanded.append(copied)
        else:
            expanded.append(chunk)
    return expanded


async def retrieve(
    helpers: Any,
    tenant_res: TenantResources,
    query: str,
    *,
    db: AsyncSession | None = None,
    top_k: int | None = None,
    campaign_id: str | None = None,
    intent_result: IntentResult | None = None,
    english_text: str | None = None,
    dual_retrieval: bool = False,
) -> dict[str, Any]:
    # Knowledge-Base document retrieval is retired: the agent answers from
    # its vertical system prompt, not Qdrant/embedding retrieval. Returns an
    # empty result; callers handle ``chunks == []`` by not grounding.
    return {
        "query": query,
        "chunks": [],
        "refusal": None,
        "sensitive": bool(intent_result and intent_result.sensitive),
        "min_score": 0.0,
        "top_k": top_k or 0,
    }
    # --- retired (unreachable below) ---
    # LangSmith retriever span. Currently this function returns an
    # empty chunks list because Qdrant retrieval is retired from the
    # runtime path (see comment below). The span still posts — the
    # zero-chunk result is itself useful debugging signal ("retrieval
    # is disabled, answers come from prompt + memory only"). When
    # Qdrant is re-enabled the spans populate without further work.
    try:
        from langsmith.run_helpers import traceable, get_current_run_tree
        _parent = get_current_run_tree()
    except Exception:
        _parent = None
    if _parent is not None:
        try:
            _retr_span = _parent.create_child(
                name="retrieval",
                run_type="retriever",
                inputs={
                    "query": query,
                    "top_k": top_k,
                    "campaign_id": campaign_id,
                    "dual": dual_retrieval,
                },
            )
            _retr_span.post()
        except Exception:
            _retr_span = None
    else:
        _retr_span = None
    # Qdrant / KB-document retrieval has been retired from the runtime
    # pipeline. The agent now answers from: (a) the curated per-vertical
    # system prompt + the org's BUSINESS FACTS (see
    # app/services/vertical_prompts.py + agent_runtime_bundle), (b) the
    # live real-estate project inventory, (c) conversational memory, and
    # (d) the slot FSM's deterministic flow. Returning an empty chunks
    # list short-circuits every downstream "no chunks → refuse" gate AND
    # keeps the LLM path active because every call site also checks
    # ``single_prompt_guidance`` (now always present) / ``outbound_mode``
    # before refusing. (Policy-card synthetic chunks are injected by
    # callers separately and are unaffected.)
    if not query.strip():
        _result = {"query": query, "chunks": [], "refusal": "Empty query."}
        if _retr_span is not None:
            try:
                _retr_span.add_outputs({"chunks": [], "chunk_count": 0, "status": "empty_query"})
                _retr_span.end()
                _retr_span.patch()
            except Exception:
                pass
        return _result
    _result = {"query": query, "chunks": [], "refusal": None}
    if _retr_span is not None:
        try:
            _retr_span.add_outputs({
                "chunks": [],
                "chunk_count": 0,
                "status": "qdrant_disabled",
            })
            _retr_span.end()
            _retr_span.patch()
        except Exception:
            pass
    return _result
    # Dual retrieval (code-switching path): when the call is actively
    # code-switching between two languages, embedding only the
    # "best" form of the query misses chunks indexed under the other
    # form. We embed BOTH the primary and the secondary form, search
    # in parallel, and union the chunks by chunk_id. Cost: one extra
    # Qdrant search + one extra embedding (almost always a cache hit).
    if (
        dual_retrieval
        and english_text
        and _normalize(english_text).lower() != _normalize(query).lower()
    ):
        return await helpers._retrieve_dual(
            tenant_res,
            primary=query,
            secondary=english_text,
            db=db,
            top_k=top_k,
            campaign_id=campaign_id,
            intent_result=intent_result,
        )
    provider_status = dict(tenant_res.provider_status or {})
    policy_version = str(provider_status.get("agent_policy_version") or "")

    sensitive = bool(intent_result and intent_result.sensitive)
    effective_top_k = top_k or (
        settings.AGENT_RETRIEVAL_TOP_K_SENSITIVE if sensitive else settings.AGENT_RETRIEVAL_TOP_K
    )
    min_score = (
        settings.AGENT_MIN_RELEVANCE_SCORE_SENSITIVE if sensitive else settings.AGENT_MIN_RELEVANCE_SCORE
    )

    # MINIMAL mandatory filter — match agent_lab's pattern. tenant_id is
    # already enforced by QdrantService._payload_filter; we only need
    # source_type to scope to KB chunks (the same collection holds
    # integration tool data, embedding for other sources, etc.).
    #
    # active / approval_status / policy_version / topic were previously
    # in the must-match list. Any chunk whose payload was missing one of
    # those fields (legacy uploads, reconciled-from-Qdrant entries,
    # custom integrations) silently disappeared. Now they're only
    # consulted as soft signals AFTER retrieval — see _filter_unapproved
    # below.
    filters: dict[str, Any] = {
        "source_type": "agent_knowledge",
    }
    if campaign_id:
        filters["campaign_id"] = campaign_id
    if sensitive and intent_result and intent_result.topic and intent_result.topic != "general":
        filters["topic"] = intent_result.topic

    vector = None  # retired: embeddings/KB retrieval removed
    limit = max(1, min(effective_top_k, 12))

    # Score floor + soft approval check. We DON'T reject a chunk just
    # because it lacks an approval_status payload — older chunks or
    # reconciled ones may not have one and we still want to surface them
    # for the LLM. We only reject when approval_status is explicitly set
    # to a rejecting value.
    def _approved(point) -> bool:
        payload = getattr(point, "payload", {}) or {}
        approval = payload.get("approval_status")
        if approval is None:
            return True  # missing → trust it
        return str(approval).lower() in {"approved", "active", "ok", ""}

    def _chunks_from(points, floor: float, *, approval_check: bool) -> list[dict[str, Any]]:
        return [
            helpers._map_point(point)
            for point in points
            if float(getattr(point, "score", 0.0) or 0.0) >= floor
            and (not approval_check or _approved(point))
        ]

    async def _search(label: str, payload_filters: dict[str, Any]) -> list[Any]:
        started = perf_counter()
        points = []  # retired: Qdrant/KB retrieval removed
        # Debug-level so production stdout/log volume doesn't carry the
        # caller's query text or per-turn retrieval stats by default; ops
        # can flip the logger to DEBUG when actually investigating.
        logger.debug(
            "NOKVO-RETRIEVE: tenant=%s label=%s query=%r filters=%s min_score=%s "
            "top_k=%s raw_results=%s scores=%s qdrant_ms=%s",
            tenant_res.tenant_id, label, query[:60], payload_filters,
            min_score, effective_top_k, len(points),
            [round(float(getattr(p, 'score', 0.0) or 0.0), 3) for p in points[:5]],
            int((perf_counter() - started) * 1000),
        )
        return points

    primary_task = asyncio.create_task(_search("primary", filters))
    relaxed_task: asyncio.Task[list[Any]] | None = None
    minimal_task: asyncio.Task[list[Any]] | None = None
    relaxed_filters: dict[str, Any] | None = None
    minimal_filters = {"source_type": "agent_knowledge"}

    if sensitive and "topic" in filters:
        relaxed_filters = dict(filters)
        relaxed_filters.pop("topic", None)
        relaxed_task = asyncio.create_task(_search("relaxed_topic", relaxed_filters))
    if minimal_filters != filters and minimal_filters != relaxed_filters:
        minimal_task = asyncio.create_task(_search("minimal", minimal_filters))

    try:
        primary_results = await primary_task
        chunks = _chunks_from(primary_results, min_score, approval_check=True)
        if chunks:
            for task in (relaxed_task, minimal_task):
                if task and not task.done():
                    task.cancel()
        elif relaxed_task is not None:
            relaxed_results = await relaxed_task
            chunks = _chunks_from(
                relaxed_results,
                settings.AGENT_MIN_RELEVANCE_SCORE,
                approval_check=False,
            )
            if chunks and minimal_task and not minimal_task.done():
                minimal_task.cancel()
        else:
            chunks = []

        if not chunks:
            if minimal_task is not None:
                minimal_results = await minimal_task
            else:
                minimal_results = primary_results
            chunks = _chunks_from(minimal_results, 0.20, approval_check=False)
    finally:
        for task in (relaxed_task, minimal_task):
            if not task:
                continue
            if task.done():
                try:
                    task.exception()
                except BaseException:
                    pass
            else:
                task.cancel()

    # For sensitive topics, broaden context by pulling the whole parent
    # section when a chunk likely came from a policy table or list row.
    if sensitive and chunks:
        chunks = helpers._expand_parent_section(chunks)

    # Grounding insurance for policy intents.
    #
    # If the utterance is about cancellation/refund — either because the
    # intent_result said so, OR because we detected a multi-script policy
    # keyword in the user's actual words — we ALWAYS prepend the active
    # policy_card source_text as synthetic chunks. Even when Qdrant
    # returned its own chunks: those may be unrelated FAQ content, and
    # the policy text is the authoritative answer.
    #
    # Without this, cross-lingual queries ("నాకు రీఫండ్ దొరుకుతదా?")
    # whose translate-STT timed out get classified as `unclear` →
    # retrieval returns nothing or noise → LLM refuses. With it, the
    # LLM always sees the policy matrix and can answer in the caller's
    # language.
    policy_keyword_hit = (
        detect_policy_keyword(query) is not None
        or (english_text and detect_policy_keyword(english_text) is not None)
        or (intent_result and intent_result.topic in ("cancellation", "refund"))
    )
    if policy_keyword_hit:
        policy_chunks = helpers._policy_card_chunks(tenant_res, policy_version)
        if policy_chunks:
            # Deduplicate: don't prepend a policy chunk whose text is
            # already present in a Qdrant result.
            existing_text = {(c.get("text") or "").strip()[:200] for c in chunks}
            new_policy = [
                pc for pc in policy_chunks
                if (pc.get("text") or "").strip()[:200] not in existing_text
            ]
            # Policy text goes FIRST so the LLM sees it before any
            # marginally-relevant Qdrant chunks.
            chunks = new_policy + chunks

    return {
        "query": query,
        "chunks": chunks,
        "refusal": None if chunks else "No indexed tenant context matched this question.",
        "sensitive": sensitive,
        "min_score": min_score,
        "top_k": effective_top_k,
    }


async def _retrieve_dual(
    helpers: Any,
    tenant_res: TenantResources,
    *,
    primary: str,
    secondary: str,
    db: AsyncSession | None,
    top_k: int | None,
    campaign_id: str | None,
    intent_result: IntentResult | None,
) -> dict[str, Any]:
    """Code-switch retrieval helper.

    Runs the primary and secondary queries against Qdrant in parallel
    and unions the chunks by ``chunk_id``, keeping the higher score
    for any duplicates. Limits the merged set to a reasonable
    ``top_k`` so the LLM prompt stays bounded.
    """
    # We deliberately recurse into ``retrieve`` with dual_retrieval=
    # False so each side does its own single-query search.
    primary_task = asyncio.create_task(
        helpers.retrieve(
            tenant_res,
            primary,
            db=db,
            top_k=top_k,
            campaign_id=campaign_id,
            intent_result=intent_result,
            english_text=None,
            dual_retrieval=False,
        )
    )
    secondary_task = asyncio.create_task(
        helpers.retrieve(
            tenant_res,
            secondary,
            db=db,
            top_k=top_k,
            campaign_id=campaign_id,
            intent_result=intent_result,
            english_text=None,
            dual_retrieval=False,
        )
    )
    primary_raw, secondary_raw = await asyncio.gather(
        primary_task, secondary_task, return_exceptions=True
    )
    # When one side fails (e.g., embedding service blip on the code-switch
    # arm), keep whichever results did come back rather than losing the turn.
    primary_res = primary_raw if not isinstance(primary_raw, BaseException) else {}
    secondary_res = secondary_raw if not isinstance(secondary_raw, BaseException) else {}

    merged: dict[str, dict[str, Any]] = {}
    for source_label, res in (("primary", primary_res), ("secondary", secondary_res)):
        for chunk in res.get("chunks") or []:
            key = str(chunk.get("chunk_id") or chunk.get("document_id") or "")
            if not key:
                continue
            if key not in merged or float(chunk.get("score") or 0.0) > float(
                merged[key].get("score") or 0.0
            ):
                merged[key] = chunk
    chunks = sorted(
        merged.values(),
        key=lambda c: float(c.get("score") or 0.0),
        reverse=True,
    )
    # Bound the merged list to a sensible cap — code-switch retrieval
    # naturally inflates the chunk count and we don't want to pay
    # the prompt-size cost.
    effective_top_k = top_k or settings.AGENT_RETRIEVAL_TOP_K
    chunks = chunks[: max(effective_top_k, 4)]
    sensitive = bool(intent_result and intent_result.sensitive)
    return {
        "query": primary,
        "secondary_query": secondary,
        "chunks": chunks,
        "refusal": None if chunks else "No indexed tenant context matched this question.",
        "sensitive": sensitive,
        "min_score": primary_res.get("min_score") or secondary_res.get("min_score"),
        "top_k": effective_top_k,
        "dual_retrieval": True,
    }
