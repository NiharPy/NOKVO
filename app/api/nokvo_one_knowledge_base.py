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
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OrganizationAgentDocumentResponse(**document)


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
