from __future__ import annotations

import base64

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.models.organization_user import OrganizationUser
from app.models.tenant_resources import TenantResources
from app.schemas.organization_auth import (
    OrganizationAgentDocumentResponse,
    OrganizationAgentDocumentReviewRequest,
    OrganizationAgentDocumentUploadRequest,
    OrganizationAgentDocumentsResponse,
    OrganizationAgentSinglePromptSetupRequest,
    OrganizationAgentSinglePromptSetupResponse,
    OrganizationAgentTestAnswerResponse,
    OrganizationAgentTestQueryRequest,
    OrganizationAgentTestRetrievalResponse,
)
from app.services.agent_knowledge_service import AgentKnowledgeService

router = APIRouter()


_ALLOWED_STATUSES = ["pending_approval", "active", "suspended"]


def _viewer_dep():
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=_ALLOWED_STATUSES,
        allowed_roles=["admin", "manager"],
    )


def _admin_dep():
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=_ALLOWED_STATUSES,
        allowed_roles=["admin"],
    )


async def _tenant_resources(db: AsyncSession, user: OrganizationUser) -> TenantResources:
    res = await db.execute(
        select(TenantResources).where(TenantResources.organization_id == user.organization_id)
    )
    tenant_res = res.scalars().first()
    if tenant_res is None:
        raise HTTPException(status_code=404, detail="Tenant resources not found for organization")
    return tenant_res


@router.get("/documents", response_model=OrganizationAgentDocumentsResponse)
async def list_documents(
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _tenant_resources(db, user)
    return OrganizationAgentDocumentsResponse(documents=AgentKnowledgeService.list_documents(tenant_res))


@router.post("/documents/upload", response_model=OrganizationAgentDocumentResponse)
async def upload_document(
    payload: OrganizationAgentDocumentUploadRequest,
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    try:
        content = base64.b64decode(payload.content_base64, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Document content must be valid base64") from exc
    if not content:
        raise HTTPException(status_code=422, detail="Document file is empty")
    tenant_res = await _tenant_resources(db, user)
    try:
        document = await AgentKnowledgeService.upload_document(
            tenant_res,
            db,
            user,
            name=payload.name,
            document_type=payload.document_type,
            description=payload.description,
            tags=payload.tags,
            filename=payload.filename,
            content=content,
            content_type=payload.content_type,
        )
    except Exception as exc:
        # Surface the actual error and the rough cause so we don't have to
        # tail logs to debug a "Failed" badge in the UI.
        import traceback
        tb = traceback.format_exc()
        print(f"[NOKVO-KB-API] upload_document raised: {exc!r}\n{tb[:1500]}")
        message = str(exc) or repr(exc)
        # Distinguish rate-limit from genuine bad-request so the frontend
        # can show "Azure quota exhausted — retry in a minute" vs.
        # "your file is invalid".
        if "429" in message or "rate" in message.lower() or "quota" in message.lower():
            raise HTTPException(
                status_code=503,
                detail=f"Embedding provider rate-limited. Try again in a minute. ({message[:200]})",
            ) from exc
        raise HTTPException(status_code=400, detail=message[:500]) from exc
    return OrganizationAgentDocumentResponse(**document)


@router.get("/single-prompt-agent", response_model=OrganizationAgentSinglePromptSetupResponse)
async def get_single_prompt_agent(
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _tenant_resources(db, user)
    return OrganizationAgentSinglePromptSetupResponse(
        **AgentKnowledgeService.get_single_prompt_setup(tenant_res)
    )


@router.post("/single-prompt-agent", response_model=OrganizationAgentSinglePromptSetupResponse)
async def configure_single_prompt_agent(
    payload: OrganizationAgentSinglePromptSetupRequest,
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _tenant_resources(db, user)
    try:
        result = await AgentKnowledgeService.configure_single_prompt_voice_agent(
            tenant_res,
            db,
            user,
            prompt=payload.prompt,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        print(f"[NOKVO-KB-API] single-prompt setup failed: {exc!r}\n{tb[:1500]}")
        raise HTTPException(status_code=400, detail=str(exc)[:500]) from exc
    return OrganizationAgentSinglePromptSetupResponse(**result)


@router.post("/single-prompt-agent/disable", response_model=OrganizationAgentSinglePromptSetupResponse)
async def disable_single_prompt_agent(
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _tenant_resources(db, user)
    try:
        result = await AgentKnowledgeService.disable_single_prompt_voice_agent(tenant_res, db, user)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:500]) from exc
    return OrganizationAgentSinglePromptSetupResponse(**result)


@router.post("/documents/{document_id}/approve", response_model=OrganizationAgentDocumentResponse)
async def approve_document(
    document_id: str,
    payload: OrganizationAgentDocumentReviewRequest,
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _tenant_resources(db, user)
    try:
        document = await AgentKnowledgeService.review_document(
            tenant_res,
            db,
            user,
            document_id,
            approve=True,
            notes=payload.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OrganizationAgentDocumentResponse(**document)


@router.post("/documents/{document_id}/reject", response_model=OrganizationAgentDocumentResponse)
async def reject_document(
    document_id: str,
    payload: OrganizationAgentDocumentReviewRequest,
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _tenant_resources(db, user)
    try:
        document = await AgentKnowledgeService.review_document(
            tenant_res,
            db,
            user,
            document_id,
            approve=False,
            notes=payload.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OrganizationAgentDocumentResponse(**document)


@router.post("/documents/reconcile")
async def reconcile_documents(
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Rebuild the document registry from chunks that exist in Qdrant but
    are missing from ``provider_status``. Use when earlier uploads went
    through embedding+Qdrant but failed to persist their metadata.
    Admin-only because it can mass-create approved documents."""
    tenant_res = await _tenant_resources(db, user)
    try:
        return await AgentKnowledgeService.reconcile_from_qdrant(tenant_res, db)
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        print(f"[NOKVO-KB-API] reconcile failed: {exc!r}\n{tb[:1500]}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/_debug/search")
async def debug_search(
    payload: OrganizationAgentTestQueryRequest,
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Raw Qdrant search — embeds the query and returns the top results
    with NO post-filtering. Lets the admin verify what's actually indexed,
    what the embedding model is producing, and what scores look like for
    a given query. Bypasses the agent's intent router entirely."""
    from app.services.text_embedding_service import TextEmbeddingService
    from app.services.qdrant_service import QdrantService

    tenant_res = await _tenant_resources(db, user)
    query = (payload.query or "").strip()
    if not query:
        return {"query": query, "results": []}
    top_k = max(1, min(int(payload.top_k or 8), 25))

    try:
        vector = await TextEmbeddingService.embed_text_for_tenant(tenant_res, query)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Embedding failed: {exc}") from exc

    try:
        results = await QdrantService.search_points(
            tenant_res,
            vector,
            limit=top_k,
            payload_filters={},  # tenant_id is always added by QdrantService
            db=db,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Qdrant search failed: {exc}") from exc

    items = []
    for point in results:
        p = dict(getattr(point, "payload", {}) or {})
        items.append({
            "id": str(getattr(point, "id", "")),
            "score": float(getattr(point, "score", 0.0) or 0.0),
            "document_id": str(p.get("document_id") or ""),
            "document_name": p.get("document_name") or p.get("source_title"),
            "section_title": p.get("section_title"),
            "source_type": p.get("source_type"),
            "active": p.get("active"),
            "approval_status": p.get("approval_status"),
            "policy_version": p.get("policy_version"),
            "topic": p.get("topic"),
            "text_preview": (str(p.get("text") or "")[:300]),
        })
    return {
        "query": query,
        "embedding_dim": len(vector),
        "collection": tenant_res.qdrant_collection_name,
        "total_results": len(items),
        "results": items,
    }


@router.get("/documents/{document_id}/chunks")
async def get_document_chunks(
    document_id: str,
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Return the embedded chunks for a single document so the admin can
    inspect exactly what the agent is grounded on. Read-only — viewer role
    is enough."""
    tenant_res = await _tenant_resources(db, user)
    try:
        return AgentKnowledgeService.get_document_chunks(tenant_res, document_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/documents/{document_id}")
async def delete_document(
    document_id: str,
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Hard-delete a Knowledge Base document: removes its Qdrant vectors,
    any answer/policy cards it produced, the registry entry, and the source
    blob. Admin-only."""
    tenant_res = await _tenant_resources(db, user)
    try:
        result = await AgentKnowledgeService.delete_document(tenant_res, db, document_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return result


@router.post("/test-retrieval", response_model=OrganizationAgentTestRetrievalResponse)
async def test_retrieval(
    payload: OrganizationAgentTestQueryRequest,
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _tenant_resources(db, user)
    try:
        result = await AgentKnowledgeService.test_retrieval(
            tenant_res, payload.query, top_k=payload.top_k, db=db
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OrganizationAgentTestRetrievalResponse(**result)


@router.post("/test-answer", response_model=OrganizationAgentTestAnswerResponse)
async def test_answer(
    payload: OrganizationAgentTestQueryRequest,
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _tenant_resources(db, user)
    try:
        result = await AgentKnowledgeService.test_answer(
            tenant_res, payload.query, top_k=payload.top_k, db=db
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OrganizationAgentTestAnswerResponse(**result)
