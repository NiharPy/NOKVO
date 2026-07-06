"""Nova — the APEX in-product AI assistant. Mounted at /api/nokvo-one/apex/nova.

Three endpoints drive the chat panel:
  * POST /chat     — one agent turn (Q&A, tool use, follow-up questions).
  * POST /upload   — attach a campaign brief document (pdf/docx/pptx/txt);
                     extracted fields merge into the session's draft (phase 3).
  * POST /confirm  — execute a pending side effect (ticket) the user approved
                     via a card. Actions only ever execute HERE, never inside
                     the LLM loop; the action_id is consumed atomically.

Any active APEX user may chat (members get Q&A/legal/tickets; campaign tools
are admin-only, filtered inside the agent service by role).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.models.organization_user import OrganizationUser
from app.models.tenant_resources import TenantResources
from app.services import nova_session_store as store
from app.services.nova_agent_service import NovaTurnResult, nova_turn

logger = logging.getLogger(__name__)

router = APIRouter()


def _apex_user_dep():
    return deps.RequireApexOrganization(allowed_statuses=["active"])


async def _tenant(db: AsyncSession, user: OrganizationUser) -> TenantResources:
    from sqlalchemy import select

    tr = (
        await db.execute(
            select(TenantResources).where(TenantResources.organization_id == user.organization_id)
        )
    ).scalars().first()
    if tr is None:
        raise HTTPException(status_code=404, detail="Tenant resources not found")
    return tr


class NovaChatRequest(BaseModel):
    session_id: str | None = Field(default=None, max_length=64)
    message: str = Field(min_length=1, max_length=4000)


@router.post("/apex/nova/chat")
async def nova_chat(
    payload: NovaChatRequest,
    user: OrganizationUser = Depends(_apex_user_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant(db, user)
    try:
        result: NovaTurnResult = await nova_turn(
            db, tr, user, payload.session_id, payload.message
        )
    except RuntimeError as exc:  # session lock contention
        raise HTTPException(status_code=503, detail="Nova is busy with your last message — retry in a second.") from exc
    return {
        "session_id": result.session_id,
        "reply": result.reply,
        "cards": result.cards,
        "draft": result.draft,
        "tool_calls": result.tool_calls,
    }


_BRIEF_EXTENSIONS = {"pdf", "docx", "pptx", "txt"}
_MAX_BRIEF_BYTES = 20 * 1024 * 1024  # 20 MB, mirrors the brochure analyzer


def _apex_admin_dep():
    return deps.RequireApexOrganization(allowed_statuses=["active"], allowed_roles=["admin"])


@router.post("/apex/nova/upload")
async def nova_upload(
    session_id: str = Form(""),
    file: UploadFile = File(...),
    user: OrganizationUser = Depends(_apex_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Attach a campaign brief (pitch deck / brochure / offer doc). Extracts a
    draft, merges it into the chat session's campaign draft, and reports what's
    still missing. Two-stage 422s (unreadable vs no fields) mirror the brochure
    analyzer. Admin-only — drafting is a campaign action."""
    tr = await _tenant(db, user)
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in _BRIEF_EXTENSIONS:
        raise HTTPException(status_code=422, detail="Upload a PDF, DOCX, PPTX, or TXT file.")
    content = await file.read()
    if len(content) > _MAX_BRIEF_BYTES:
        raise HTTPException(status_code=422, detail="That file is over the 20 MB limit.")

    from app.services.nova_brief_service import analyze_campaign_brief
    from app.services.document_text import extract_document_text

    text = extract_document_text(file.filename or "doc", content)
    if len((text or "").strip()) < 25:
        raise HTTPException(
            status_code=422,
            detail="I couldn't read any text in that file — scanned/image-only documents don't work yet.",
        )
    from app.services.langsmith_tracer import trace_nova

    async with trace_nova(
        name="nova_brief_extraction",
        organization_id=user.organization_id,
        tenant_id=tr.tenant_id,
        session_id=session_id,
        filename=file.filename,
        chars=len(text),
    ) as _span:
        analysis = await analyze_campaign_brief(text, tr)
        if _span is not None:
            try:
                _span.add_outputs({
                    "extracted_fields": sorted(analysis["extracted"].keys()),
                    "truncated": analysis["truncated"],
                })
            except Exception:
                pass
    extracted = analysis["extracted"]
    if not extracted:
        raise HTTPException(
            status_code=422,
            detail="I read the file but couldn't pull campaign details from it. Try describing the offer in chat instead.",
        )

    from app.services.nova_agent_service import draft_missing, merge_draft_fields

    sid = session_id or store.new_session_id()
    ns = store.AgentSessionStore.namespace(tr)
    merged: dict = {}

    def _merge(state: dict) -> None:
        draft = state.get("draft") or {}
        if draft.get("handed_off"):
            draft = {}
        merge_draft_fields(draft, extracted)
        state["draft"] = draft
        state["pending_action"] = None
        store.append_history(state, "user", f"[uploaded document: {file.filename}]")
        merged.update(draft)

    reply_bits = []
    if analysis["truncated"]:
        pct = int(analysis["read_fraction"] * 100)
        reply_bits.append(
            f"That document was long — I read the first ~{pct}% of it, so double-check nothing important was missed."
        )
    got = [k for k in ("name", "company_name", "content", "questionnaire") if extracted.get(k)]
    reply_bits.append(f"Got it — I pulled {', '.join(got) if got else 'a few details'} from {file.filename}.")

    def _after_missing() -> str:
        missing = draft_missing(merged)
        if missing:
            return f"Next: {missing[0]}?"
        return "The draft looks complete — want me to show you the preview?"

    def _persist_reply(state: dict) -> None:
        store.append_history(state, "assistant", " ".join(reply_bits) + " " + _after_missing())

    await store.mutate(ns, sid, _merge)
    reply = " ".join(reply_bits) + " " + _after_missing()
    await store.mutate(ns, sid, _persist_reply)

    return {
        "session_id": sid,
        "extracted": extracted,
        "missing": draft_missing(merged),
        "truncated": analysis["truncated"],
        "reply": reply,
    }


@router.get("/apex/nova/session/{session_id}")
async def nova_session(
    session_id: str,
    user: OrganizationUser = Depends(_apex_user_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Restore a chat session (panel reopen). Tenant-namespaced key — another
    org's session id resolves to an empty session, never to data."""
    tr = await _tenant(db, user)
    ns = store.AgentSessionStore.namespace(tr)
    state = await store.load(ns, session_id[:64])
    return {
        "session_id": session_id[:64],
        "messages": state.get("history") or [],
        "draft": state.get("draft") or None,
    }


class NovaConfirmRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    action_id: str = Field(min_length=1, max_length=64)
    decision: str = Field(pattern="^(confirm|cancel)$")


@router.post("/apex/nova/confirm")
async def nova_confirm(
    payload: NovaConfirmRequest,
    user: OrganizationUser = Depends(_apex_user_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Consume the session's pending_action (atomic under the session lock) and
    execute it on confirm. Executors are registered per action type."""
    tr = await _tenant(db, user)
    ns = store.AgentSessionStore.namespace(tr)

    consumed: dict = {}

    def _consume(state: dict) -> None:
        pending = state.get("pending_action") or {}
        if pending.get("action_id") == payload.action_id:
            consumed.update(pending)
            state["pending_action"] = None

    await store.mutate(ns, payload.session_id, _consume)
    if not consumed:
        raise HTTPException(status_code=404, detail="That action has expired or was already handled.")

    if payload.decision == "cancel":
        return {"result": "cancelled", "reply": "Okay, cancelled — nothing was done."}

    from app.services.langsmith_tracer import trace_nova
    from app.services.nova_agent_service import CONFIRM_EXECUTORS

    executor = CONFIRM_EXECUTORS.get(str(consumed.get("type") or ""))
    if executor is None:
        raise HTTPException(status_code=400, detail="This action type can't be executed.")
    try:
        async with trace_nova(
            name="nova_confirm",
            organization_id=user.organization_id,
            tenant_id=tr.tenant_id,
            session_id=payload.session_id,
            action_type=consumed.get("type"),
        ) as _span:
            result, reply = await executor(db, tr, user, consumed.get("payload") or {})
            if _span is not None:
                try:
                    _span.add_outputs({"result": result})
                except Exception:
                    pass
    except Exception:
        logger.exception("NOVA: confirm execution failed (%s)", consumed.get("type"))
        raise HTTPException(status_code=500, detail="The action failed — nothing was recorded. Please try again.")
    return {"result": result, "reply": reply}
