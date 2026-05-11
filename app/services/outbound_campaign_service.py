"""Outbound campaign service.

Flow:
  1. Admin uploads Excel (phone + name columns) + reference document
  2. Service creates OutboundCampaign row (status=draft)
  3. On launch: fires parallel Telnyx outbound calls, one per contact
  4. Each call gets a unique call_link_id → voice webhook returns TeXML with
     the campaign WebSocket URL, which injects campaign doc chunks into the agent
  5. Per-call status updated via Telnyx status callbacks
"""
from __future__ import annotations

import asyncio
import io
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outbound_campaign import CampaignStatus, OutboundCampaign
from app.models.tenant_resources import TenantResources
from app.services.telnyx_service import TelnyxService


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

        # Resolve caller ID: provided → first linked Telnyx number → error
        caller_id = from_number
        if not caller_id:
            links = TelnyxService.list_linked_numbers(tenant_res)
            if links:
                caller_id = links[0]["phone_number"]
        if not caller_id:
            raise ValueError("No phone number available as caller ID. "
                             "Link at least one phone number to this tenant first.")

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

        campaign = OutboundCampaign(
            id=uuid.uuid4(),
            tenant_id=tenant_res.tenant_id,
            name=name,
            status=CampaignStatus.draft,
            contacts=contacts,
            doc_blob_path=doc_blob_path,
            doc_text=doc_text,
            from_number=caller_id,
            total_count=len(contacts),
        )
        db.add(campaign)
        await db.commit()
        await db.refresh(campaign)
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
    ) -> OutboundCampaign:
        if campaign.status != CampaignStatus.draft:
            raise ValueError(f"Campaign is already '{campaign.status}' — only draft campaigns can be launched.")

        base = public_base_url.rstrip("/")
        contacts = list(campaign.contacts or [])

        campaign.status = CampaignStatus.running
        campaign.started_at = datetime.now(timezone.utc)
        db.add(campaign)
        await db.commit()
        await db.refresh(campaign)

        # Fire all calls in parallel — don't await individually
        async def _call_one(contact: dict) -> None:
            link_id = contact["call_link_id"]
            webhook = f"{base}/api/org-auth/agent/telnyx/outbound-voice/{link_id}"
            try:
                result = await TelnyxService.initiate_call(
                    from_number=campaign.from_number,
                    to_number=contact["phone"],
                    webhook_url=webhook,
                    client_state=f"{campaign.id}:{link_id}",
                )
                contact["call_id"] = result.get("call_control_id") or result.get("id")
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

        elif event_type in ("call.hangup", "call.failed", "call.machine.detection.ended"):
            hangup_cause = payload.get("hangup_cause", "")
            if target["status"] not in ("answered",):
                target["status"] = "no_answer" if "no_answer" in hangup_cause.lower() else "failed"
                campaign.failed_count = (campaign.failed_count or 0) + 1
            duration = payload.get("duration_seconds") or 0
            target["duration_s"] = int(duration)

        campaign.contacts = contacts

        # Check if all calls are terminal
        terminal = {"answered", "no_answer", "failed"}
        if all(c.get("status") in terminal for c in contacts):
            campaign.status = CampaignStatus.completed
            campaign.completed_at = datetime.now(timezone.utc)

        db.add(campaign)
        await db.commit()

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
