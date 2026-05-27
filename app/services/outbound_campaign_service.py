"""Outbound campaign service.

Flow:
  1. Admin uploads Excel (phone + name columns) + reference document
  2. Service creates OutboundCampaign row (status=draft)
  3. Campaign reference text is chunked, embedded, and indexed in Qdrant
  4. On launch: fires parallel Exotel outbound calls, one per contact
  5. Each answered call connects to the Nokvo One Sarvam/RAG voice pipeline
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.outgoing_lead import LeadCallStatus, OutboundCampaignContact, OutgoingLead
from app.models.outbound_campaign import CampaignStatus, OutboundCampaign
from app.models.tenant_resources import TenantResources
from app.services.agent_knowledge_service import AGENT_KNOWLEDGE_SOURCE_TYPE, AgentKnowledgeService
from app.services.exotel_service import ExotelService
from app.services.agent_outbound_context import build_agent_config, invalidate as invalidate_outbound_context
from app.services.outgoing_lead_service import OutgoingLeadService, lead_is_callable
from app.services.qdrant_service import QdrantService
from app.services.text_embedding_service import TextEmbeddingService


# ---------------------------------------------------------------------------
# Excel parsing
# ---------------------------------------------------------------------------

def _parse_excel(content: bytes) -> list[dict[str, str]]:
    """Return list of {phone, name} from the first two populated columns."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.active
    contacts: list[dict[str, str]] = []
    header_skipped = False
    for row in ws.iter_rows(values_only=True):
        cells = [str(c).strip() if c is not None else "" for c in row]
        # Skip completely empty rows
        if not any(cells):
            continue
        # Auto-detect header row: first row that has no digit-only cell in col0
        first = cells[0] if cells else ""
        if not header_skipped and not re.search(r"\d{7,}", first):
            header_skipped = True
            continue
        phone = re.sub(r"[^\d+]", "", first)
        name = cells[1] if len(cells) > 1 else ""
        if len(phone) >= 7:
            contacts.append({"phone": phone, "name": name or phone})
    wb.close()
    return contacts


def _parse_document(filename: str, content: bytes) -> str:
    """Extract plain text from PDF, DOCX, or TXT file."""
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext == "txt":
        return content.decode("utf-8", errors="replace")
    if ext == "pdf":
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(content))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except ImportError:
            pass
        try:
            import pdfminer.high_level as pm
            return pm.extract_text(io.BytesIO(content))
        except ImportError:
            return content.decode("utf-8", errors="replace")
    if ext in ("docx", "doc"):
        try:
            import docx
            doc = docx.Document(io.BytesIO(content))
            return "\n".join(p.text for p in doc.paragraphs)
        except ImportError:
            return content.decode("utf-8", errors="replace")
    return content.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Chunk splitting (matches Qdrant chunk shape)
# ---------------------------------------------------------------------------

def _doc_to_chunks(text: str, words_per_chunk: int = 350) -> list[dict[str, Any]]:
    """Split document text into chunks matching the Qdrant retrieval shape."""
    words = text.split()
    chunks: list[dict[str, Any]] = []
    for i in range(0, len(words), words_per_chunk):
        chunk_text = " ".join(words[i : i + words_per_chunk])
        if chunk_text.strip():
            chunks.append({
                "text": chunk_text,
                "score": 1.0,
                "chunk_id": f"campaign_chunk_{i // words_per_chunk}",
                "document_id": "campaign_doc",
                "document_name": "Campaign Reference",
                "metadata": {},
            })
    return chunks


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class OutboundCampaignService:
    @staticmethod
    async def _index_campaign_script(
        tenant_res: TenantResources,
        campaign_id: uuid.UUID,
        campaign_name: str,
        doc_text: str,
        *,
        db: AsyncSession | None = None,
    ) -> int:
        chunks = AgentKnowledgeService._chunk_text(doc_text)
        if not chunks:
            raise ValueError("No usable text was found in the campaign script document.")
        texts = [chunk["text"] for chunk in chunks]
        vectors = await TextEmbeddingService.embed_texts(texts)
        points: list[dict[str, Any]] = []
        for index, chunk in enumerate(chunks):
            chunk_id = f"campaign:{campaign_id}:chunk:{index}"
            points.append(
                {
                    "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{tenant_res.tenant_id}:{chunk_id}")),
                    "vector": vectors[index],
                    "payload": {
                        "organization_id": str(tenant_res.organization_id),
                        "tenant_id": tenant_res.tenant_id,
                        "source_type": AGENT_KNOWLEDGE_SOURCE_TYPE,
                        "source_kind": "campaign_script_chunk",
                        "resource_type": "campaign_script_chunk",
                        "resource": f"campaigns/{campaign_id}/script/{index}",
                        "document_id": f"campaign:{campaign_id}:script",
                        "document_name": f"{campaign_name} Script",
                        "document_type": "script",
                        "campaign_id": str(campaign_id),
                        "chunk_id": chunk_id,
                        "chunk_index": index,
                        "chunk_count": len(chunks),
                        "text": chunk["text"],
                        "status": "active",
                        "document_status": "ok",
                        "approval_status": "approved",
                        "active": True,
                        "language": "en",
                        "source_title": f"{campaign_name} Script",
                        "sensitivity": "normal",
                    },
                }
            )
        await QdrantService.delete_points_by_filter(
            tenant_res,
            {"source_type": AGENT_KNOWLEDGE_SOURCE_TYPE, "campaign_id": str(campaign_id)},
            db=db,
        )
        await QdrantService.upsert_points(tenant_res, points, db=db)
        return len(points)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    @staticmethod
    async def create_campaign(
        tenant_res: TenantResources,
        db: AsyncSession,
        *,
        name: str,
        excel_file: UploadFile,
        doc_file: UploadFile,
        from_number: str | None = None,
        agent_config: dict[str, Any] | None = None,
    ) -> OutboundCampaign:
        excel_bytes = await excel_file.read()
        doc_bytes = await doc_file.read()

        contacts_raw = _parse_excel(excel_bytes)
        if not contacts_raw:
            raise ValueError("No valid phone numbers found in the Excel file. "
                             "Ensure column A has phone numbers and column B has names.")

        doc_text = _parse_document(doc_file.filename or "doc.txt", doc_bytes)

        # Upload doc to Azure Blob if configured (optional — text stored in DB directly)
        doc_blob_path: str | None = None
        try:
            from app.services.azure_blob_service import AzureBlobService
            blob_name = f"campaigns/{tenant_res.tenant_id}/{uuid.uuid4()}/{doc_file.filename}"
            await AzureBlobService.upload_bytes(
                tenant_res, doc_bytes, blob_name,
                content_type="application/octet-stream",
            )
            doc_blob_path = blob_name
        except Exception:
            pass

        # Resolve caller ID: provided → Exotel config → linked phone → global default.
        exotel_cfg = dict((tenant_res.provider_status or {}).get("exotel") or {})
        caller_id = (
            from_number
            or exotel_cfg.get("from_number")
            or tenant_res.twilio_phone_number
            or settings.EXOTEL_CALLER_ID
        )
        if not caller_id:
            raise ValueError("No Exotel caller ID is configured. Link an Exotel number first.")

        contacts = [
            {
                "phone": c["phone"],
                "name": c["name"],
                "status": "pending",
                "call_id": None,
                "call_link_id": str(uuid.uuid4()),
                "duration_s": None,
                "answered_at": None,
            }
            for c in contacts_raw
        ]

        campaign_id = uuid.uuid4()
        indexed_points = await OutboundCampaignService._index_campaign_script(
            tenant_res,
            campaign_id,
            name,
            doc_text,
            db=db,
        )

        campaign = OutboundCampaign(
            id=campaign_id,
            tenant_id=tenant_res.tenant_id,
            name=name,
            status=CampaignStatus.draft,
            contacts=contacts,
            doc_blob_path=doc_blob_path,
            doc_text=doc_text,
            agent_config=build_agent_config(**dict(agent_config or {})),
            from_number=caller_id,
            total_count=len(contacts),
        )
        db.add(campaign)
        await db.commit()
        await db.refresh(campaign)
        invalidate_outbound_context(campaign.id)
        # Keep a small denormalized marker for the operator console without
        # altering the campaign table.
        for contact in campaign.contacts or []:
            contact.setdefault("script_indexed_points", indexed_points)
        return campaign

    @staticmethod
    async def create_campaign_from_leads(
        tenant_res: TenantResources,
        db: AsyncSession,
        *,
        name: str,
        lead_ids: list[uuid.UUID],
        doc_file: UploadFile,
        from_number: str | None = None,
        agent_config: dict[str, Any] | None = None,
    ) -> OutboundCampaign:
        leads = await OutgoingLeadService.validate_callable_leads(tenant_res, db, lead_ids)
        doc_bytes = await doc_file.read()
        doc_text = _parse_document(doc_file.filename or "doc.txt", doc_bytes)

        doc_blob_path: str | None = None
        try:
            from app.services.azure_blob_service import AzureBlobService
            blob_name = f"campaigns/{tenant_res.tenant_id}/{uuid.uuid4()}/{doc_file.filename}"
            await AzureBlobService.upload_bytes(
                tenant_res, doc_bytes, blob_name,
                content_type="application/octet-stream",
            )
            doc_blob_path = blob_name
        except Exception:
            pass

        exotel_cfg = dict((tenant_res.provider_status or {}).get("exotel") or {})
        caller_id = (
            from_number
            or exotel_cfg.get("from_number")
            or tenant_res.twilio_phone_number
            or settings.EXOTEL_CALLER_ID
        )
        if not caller_id:
            raise ValueError("No Exotel caller ID is configured. Link an Exotel number first.")

        campaign_id = uuid.uuid4()
        indexed_points = await OutboundCampaignService._index_campaign_script(
            tenant_res,
            campaign_id,
            name,
            doc_text,
            db=db,
        )

        contacts: list[dict[str, Any]] = []
        campaign_contact_rows: list[OutboundCampaignContact] = []
        for lead in leads:
            link_id = str(uuid.uuid4())
            snapshot = {
                "lead_id": str(lead.id),
                "phone": lead.phone_e164,
                "name": lead.name or lead.phone_e164,
                "email": lead.email,
                "source_provider": lead.source_provider.value if hasattr(lead.source_provider, "value") else lead.source_provider,
                "capture_form_id": str(lead.capture_form_id) if lead.capture_form_id else None,
                "provider_lead_id": lead.provider_lead_id,
                "consent_status": lead.consent_status.value if hasattr(lead.consent_status, "value") else lead.consent_status,
                "consent_text": lead.consent_text,
                "consented_at": lead.consented_at.isoformat() if lead.consented_at else None,
            }
            contact = {
                "phone": lead.phone_e164,
                "name": lead.name or lead.phone_e164,
                "status": "pending",
                "call_id": None,
                "call_link_id": link_id,
                "duration_s": None,
                "answered_at": None,
                "lead_id": str(lead.id),
                "source_provider": snapshot["source_provider"],
                "consent_status": snapshot["consent_status"],
                "consent_text": snapshot["consent_text"],
                "script_indexed_points": indexed_points,
            }
            contacts.append(contact)
            campaign_contact_rows.append(
                OutboundCampaignContact(
                    campaign_id=campaign_id,
                    outgoing_lead_id=lead.id,
                    status="pending",
                    call_link_id=link_id,
                    snapshot=snapshot,
                )
            )
            lead.call_status = LeadCallStatus.queued
            db.add(lead)

        campaign = OutboundCampaign(
            id=campaign_id,
            tenant_id=tenant_res.tenant_id,
            name=name,
            status=CampaignStatus.draft,
            contacts=contacts,
            doc_blob_path=doc_blob_path,
            doc_text=doc_text,
            agent_config=build_agent_config(**dict(agent_config or {})),
            from_number=caller_id,
            total_count=len(contacts),
        )
        db.add(campaign)
        for row in campaign_contact_rows:
            db.add(row)
        await db.commit()
        await db.refresh(campaign)
        invalidate_outbound_context(campaign.id)
        return campaign

    @staticmethod
    async def list_campaigns(tenant_res: TenantResources, db: AsyncSession) -> list[OutboundCampaign]:
        result = await db.execute(
            select(OutboundCampaign)
            .where(OutboundCampaign.tenant_id == tenant_res.tenant_id)
            .order_by(OutboundCampaign.created_at.desc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_campaign(
        campaign_id: uuid.UUID, tenant_res: TenantResources, db: AsyncSession
    ) -> OutboundCampaign | None:
        result = await db.execute(
            select(OutboundCampaign).where(
                OutboundCampaign.id == campaign_id,
                OutboundCampaign.tenant_id == tenant_res.tenant_id,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def cancel_campaign(
        campaign: OutboundCampaign, db: AsyncSession
    ) -> OutboundCampaign:
        if campaign.status not in (CampaignStatus.draft, CampaignStatus.running):
            raise ValueError(f"Cannot cancel a campaign with status '{campaign.status}'.")
        campaign.status = CampaignStatus.cancelled
        campaign.completed_at = datetime.now(timezone.utc)
        db.add(campaign)
        await db.commit()
        await db.refresh(campaign)
        return campaign

    # ------------------------------------------------------------------
    # Launch
    # ------------------------------------------------------------------

    @staticmethod
    async def launch_campaign(
        campaign: OutboundCampaign,
        db: AsyncSession,
        *,
        public_base_url: str,
        path_prefix: str = "/api/org-auth/agent",
        tenant_res: TenantResources | None = None,
    ) -> OutboundCampaign:
        if campaign.status != CampaignStatus.draft:
            raise ValueError(f"Campaign is already '{campaign.status}' — only draft campaigns can be launched.")

        if tenant_res is None:
            res = await db.execute(
                select(TenantResources).where(TenantResources.tenant_id == campaign.tenant_id)
            )
            tenant_res = res.scalars().first()
            if tenant_res is None:
                raise ValueError("Tenant resources for this campaign could not be loaded.")

        base = public_base_url.rstrip("/")
        prefix = path_prefix.rstrip("/")
        contacts = list(campaign.contacts or [])
        lead_ids: list[uuid.UUID] = []
        for contact in contacts:
            lead_id = contact.get("lead_id")
            if not lead_id:
                raise ValueError(
                    "This campaign contains contacts without consented lead records. "
                    "Create a new campaign from eligible Outgoing Agent leads."
                )
            try:
                lead_ids.append(uuid.UUID(str(lead_id)))
            except ValueError as exc:
                raise ValueError("Campaign contains an invalid lead reference.") from exc
        leads = await OutgoingLeadService.validate_callable_leads(tenant_res, db, lead_ids)
        callable_by_id = {str(lead.id): lead for lead in leads if lead_is_callable(lead)}
        if len(callable_by_id) != len(contacts):
            raise ValueError("Campaign contains leads that are no longer callable.")

        campaign.status = CampaignStatus.running
        campaign.started_at = datetime.now(timezone.utc)
        db.add(campaign)
        await db.commit()
        await db.refresh(campaign)

        # Fire all calls in parallel — don't await individually
        async def _call_one(contact: dict) -> None:
            link_id = contact["call_link_id"]
            ws_base = base.replace("https://", "wss://").replace("http://", "ws://")
            stream_url = f"{ws_base}{prefix}/exotel/outbound-media/{link_id}"
            status_callback = f"{base}{prefix}/exotel/outbound-status/{link_id}"
            try:
                result = await ExotelService.initiate_outbound_call(
                    tenant_res,
                    to_number=contact["phone"],
                    stream_url=stream_url,
                    status_callback=status_callback,
                    custom_field=f"{campaign.id}:{link_id}",
                    from_number=campaign.from_number,
                )
                call = result.get("call") if isinstance(result.get("call"), dict) else result
                contact["call_id"] = call.get("sid") or call.get("id")
                contact["status"] = "calling"
            except Exception as exc:
                contact["status"] = "failed"
                contact["error"] = str(exc)[:200]

        await asyncio.gather(*[_call_one(c) for c in contacts], return_exceptions=True)

        # Persist updated contact statuses
        campaign.contacts = contacts
        db.add(campaign)
        await db.commit()
        await db.refresh(campaign)
        return campaign

    # ------------------------------------------------------------------
    # Call-level status updates (called from status webhook)
    # ------------------------------------------------------------------

    @staticmethod
    async def handle_call_status(
        campaign: OutboundCampaign,
        call_link_id: str,
        event_type: str,
        payload: dict,
        db: AsyncSession,
    ) -> None:
        contacts = list(campaign.contacts or [])
        target = next((c for c in contacts if c.get("call_link_id") == call_link_id), None)
        if not target:
            return

        if event_type == "call.answered":
            target["status"] = "answered"
            target["answered_at"] = datetime.now(timezone.utc).isoformat()
            campaign.answered_count = (campaign.answered_count or 0) + 1
            if target.get("lead_id"):
                lead_res = await db.execute(select(OutgoingLead).where(OutgoingLead.id == uuid.UUID(str(target["lead_id"]))))
                lead = lead_res.scalars().first()
                if lead:
                    lead.call_status = LeadCallStatus.called
                    db.add(lead)

        elif event_type in ("call.hangup", "call.failed", "call.machine.detection.ended"):
            hangup_cause = payload.get("hangup_cause", "")
            if target["status"] not in ("answered",):
                target["status"] = "no_answer" if "no_answer" in hangup_cause.lower() else "failed"
                campaign.failed_count = (campaign.failed_count or 0) + 1
            duration = payload.get("duration_seconds") or 0
            target["duration_s"] = int(duration)

            # Close the outcome loop: any record the agent created during
            # this call gets its outcome derived from the call disposition,
            # and a follow-up callback is auto-scheduled for no_show /
            # failed_followup states.
            try:
                from app.services.outcome_tracker import OutcomeTracker
                from app.models.tenant_resources import TenantResources

                tr_res = await db.execute(
                    select(TenantResources).where(TenantResources.tenant_id == campaign.tenant_id)
                )
                tr = tr_res.scalars().first()
                org_id = tr.organization_id if tr else None
                created_record_ids = list(target.get("created_record_ids") or [])
                if org_id:
                    for rec_id in created_record_ids:
                        try:
                            rec_uuid = uuid.UUID(str(rec_id))
                        except (TypeError, ValueError):
                            continue
                        await OutcomeTracker.record_from_disposition(
                            db,
                            organization_id=org_id,
                            record_id=rec_uuid,
                            disposition=target["status"],
                            notes=f"hangup_cause={hangup_cause}" if hangup_cause else None,
                        )
                        await OutcomeTracker.auto_followup_if_needed(
                            db,
                            organization_id=org_id,
                            record_id=rec_uuid,
                        )
            except Exception:
                pass

        campaign.contacts = contacts
        contact_res = await db.execute(
            select(OutboundCampaignContact).where(
                OutboundCampaignContact.campaign_id == campaign.id,
                OutboundCampaignContact.call_link_id == call_link_id,
            )
        )
        campaign_contact = contact_res.scalars().first()
        if campaign_contact:
            campaign_contact.status = target.get("status") or campaign_contact.status
            campaign_contact.call_id = target.get("call_id") or campaign_contact.call_id
            campaign_contact.snapshot = {
                **dict(campaign_contact.snapshot or {}),
                "duration_s": target.get("duration_s"),
                "answered_at": target.get("answered_at"),
                "last_status_payload": payload,
            }
            db.add(campaign_contact)

        # Check if all calls are terminal
        terminal = {"answered", "no_answer", "failed"}
        just_completed = False
        if all(c.get("status") in terminal for c in contacts):
            if campaign.status != CampaignStatus.completed:
                just_completed = True
            campaign.status = CampaignStatus.completed
            campaign.completed_at = datetime.now(timezone.utc)

        db.add(campaign)
        await db.commit()

        # When the campaign just finished, post a P2 inbox summary so
        # the operator sees the batch result without polling the page.
        # Best-effort — a notification failure must not roll back the
        # campaign status that we just committed.
        if just_completed:
            try:
                await OutboundCampaignService._notify_batch_complete(
                    db, campaign, contacts
                )
            except Exception:
                logger.exception("NOKVO-NOTIF: failed to emit outbound_batch summary")

    @staticmethod
    async def _notify_batch_complete(
        db: AsyncSession,
        campaign: OutboundCampaign,
        contacts: list[dict],
    ) -> None:
        from app.models.notification import (
            NOTIFICATION_OUTBOUND_BATCH,
            SEVERITY_P2,
        )
        from app.models.tenant_resources import TenantResources
        from app.services.notification_service import NotificationService

        tr_res = await db.execute(
            select(TenantResources).where(TenantResources.tenant_id == campaign.tenant_id)
        )
        tr = tr_res.scalars().first()
        if tr is None:
            return
        answered = sum(1 for c in contacts if c.get("status") == "answered")
        no_answer = sum(1 for c in contacts if c.get("status") == "no_answer")
        failed = sum(1 for c in contacts if c.get("status") == "failed")
        total = len(contacts)
        await NotificationService.emit(
            db,
            organization_id=tr.organization_id,
            tenant_id=campaign.tenant_id,
            type=NOTIFICATION_OUTBOUND_BATCH,
            severity=SEVERITY_P2,
            title=f"Campaign '{campaign.name}' finished — {answered}/{total} answered",
            body=f"{answered} answered · {no_answer} no answer · {failed} failed",
            payload={
                "campaign_id": str(campaign.id),
                "answered": answered,
                "no_answer": no_answer,
                "failed": failed,
                "total": total,
            },
            # One summary per campaign completion. Re-fires on a re-launch
            # are fine because the campaign_id changes per run.
            dedup_key=f"outbound_batch:{campaign.id}",
        )

    # ------------------------------------------------------------------
    # Lookup by call_link_id (used in webhook handlers)
    # ------------------------------------------------------------------

    @staticmethod
    async def get_by_call_link_id(
        call_link_id: str, db: AsyncSession
    ) -> tuple[OutboundCampaign | None, dict | None]:
        """Return (campaign, contact) matching the call_link_id."""
        result = await db.execute(
            select(OutboundCampaign).where(
                OutboundCampaign.status.in_([CampaignStatus.running, CampaignStatus.completed])
            )
        )
        for campaign in result.scalars().all():
            for contact in campaign.contacts or []:
                if contact.get("call_link_id") == call_link_id:
                    return campaign, contact
        return None, None

    # ------------------------------------------------------------------
    # Chunks for agent injection
    # ------------------------------------------------------------------

    @staticmethod
    def get_chunks(campaign: OutboundCampaign) -> list[dict[str, Any]]:
        """Return document chunks in Qdrant-compatible shape for agent injection."""
        if not campaign.doc_text:
            return []
        return _doc_to_chunks(campaign.doc_text)
