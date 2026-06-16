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
from app.services.plivo_service import PlivoService
from app.services.agent_outbound_context import build_agent_config, invalidate as invalidate_outbound_context
from app.services.outgoing_lead_service import OutgoingLeadService, lead_is_callable


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
        # Campaign reference-text RAG is retired: the outbound agent runs on the
        # campaign objectives/context, not semantic retrieval of an uploaded
        # script. No-op — kept so callers and the stored ``script_indexed_points``
        # metadata stay stable.
        return 0

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
        plivo_cfg = dict((tenant_res.provider_status or {}).get("plivo") or {})
        caller_id = (
            from_number
            or plivo_cfg.get("number")
            or tenant_res.twilio_phone_number
        )
        if not caller_id:
            raise ValueError("No Plivo caller ID is configured. The tenant's Plivo number is still provisioning.")

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
        doc_file: UploadFile | None = None,
        from_number: str | None = None,
        agent_config: dict[str, Any] | None = None,
    ) -> OutboundCampaign:
        """Create a campaign with pre-attached leads.

        ``doc_file`` is kept as an optional input for backwards compat —
        the agent's knowledge now lives in ``agent_config.agent_prompt``,
        so most callers will not pass one. When provided, the doc is
        indexed to Qdrant as before; when absent, indexing is skipped.
        """
        cfg = build_agent_config(**dict(agent_config or {}))
        if not str(cfg.get("agent_prompt") or "").strip():
            raise ValueError(
                "Campaigns need an agent prompt — it's what the agent reads "
                "during the call. Add one and try again."
            )

        leads = await OutgoingLeadService.validate_callable_leads(tenant_res, db, lead_ids)

        doc_text: str | None = None
        doc_blob_path: str | None = None
        indexed_points = 0
        if doc_file is not None:
            doc_bytes = await doc_file.read()
            doc_text = _parse_document(doc_file.filename or "doc.txt", doc_bytes)
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

        plivo_cfg = dict((tenant_res.provider_status or {}).get("plivo") or {})
        caller_id = (
            from_number
            or plivo_cfg.get("number")
            or tenant_res.twilio_phone_number
        )
        if not caller_id:
            raise ValueError("No Plivo caller ID is configured. The tenant's Plivo number is still provisioning.")

        campaign_id = uuid.uuid4()
        if doc_text:
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
            agent_config=cfg,
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
    async def create_campaign_prompt_only(
        tenant_res: TenantResources,
        db: AsyncSession,
        *,
        name: str,
        from_number: str | None = None,
        agent_config: dict[str, Any] | None = None,
    ) -> OutboundCampaign:
        """Create a campaign with just a name + agent_prompt.

        The agent_prompt IS the knowledge — there's no separate reference
        document. ``from_number`` is resolved against tenant defaults but
        may end up ``None`` for a draft (caller ID check is deferred to
        launch time). Leads are attached later via :meth:`attach_leads`.
        """
        cfg = build_agent_config(**dict(agent_config or {}))
        if not str(cfg.get("agent_prompt") or "").strip():
            raise ValueError(
                "Campaigns need an agent prompt — it's what the agent reads "
                "during the call. Add one and try again."
            )

        plivo_cfg = dict((tenant_res.provider_status or {}).get("plivo") or {})
        caller_id = (
            from_number
            or plivo_cfg.get("number")
            or tenant_res.twilio_phone_number
            or None
        )

        campaign = OutboundCampaign(
            id=uuid.uuid4(),
            tenant_id=tenant_res.tenant_id,
            name=name,
            status=CampaignStatus.draft,
            contacts=[],
            doc_blob_path=None,
            doc_text=None,
            agent_config=cfg,
            from_number=caller_id,
            total_count=0,
        )
        db.add(campaign)
        await db.commit()
        await db.refresh(campaign)
        invalidate_outbound_context(campaign.id)
        return campaign

    @staticmethod
    async def attach_leads(
        tenant_res: TenantResources,
        db: AsyncSession,
        *,
        campaign: OutboundCampaign,
        lead_ids: list[uuid.UUID],
    ) -> OutboundCampaign:
        """Attach consented leads to an existing campaign.

        Validates each lead is callable + same tenant, appends to the
        inline ``contacts`` snapshot, creates ``OutboundCampaignContact``
        rows for the launch path, and marks each lead ``queued``.
        Idempotent — already-attached leads are skipped silently.
        """
        if not lead_ids:
            raise ValueError("Provide at least one lead to attach.")
        if campaign.status not in (CampaignStatus.draft,):
            raise ValueError("Leads can only be attached to draft campaigns.")

        leads = await OutgoingLeadService.validate_callable_leads(tenant_res, db, lead_ids)

        contacts = list(campaign.contacts or [])
        already_attached_ids = {str(c.get("lead_id")) for c in contacts if c.get("lead_id")}
        existing_rows_res = await db.execute(
            select(OutboundCampaignContact).where(
                OutboundCampaignContact.campaign_id == campaign.id
            )
        )
        existing_rows = {row.outgoing_lead_id for row in existing_rows_res.scalars().all()}

        added = 0
        for lead in leads:
            if str(lead.id) in already_attached_ids and lead.id in existing_rows:
                continue
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
            contacts.append(
                {
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
                }
            )
            if lead.id not in existing_rows:
                db.add(
                    OutboundCampaignContact(
                        campaign_id=campaign.id,
                        outgoing_lead_id=lead.id,
                        status="pending",
                        call_link_id=link_id,
                        snapshot=snapshot,
                    )
                )
            lead.call_status = LeadCallStatus.queued
            db.add(lead)
            added += 1

        campaign.contacts = contacts
        campaign.total_count = len(contacts)
        db.add(campaign)
        await db.commit()
        await db.refresh(campaign)
        invalidate_outbound_context(campaign.id)
        return campaign

    @staticmethod
    async def detach_lead(
        tenant_res: TenantResources,
        db: AsyncSession,
        *,
        campaign: OutboundCampaign,
        lead_id: uuid.UUID,
    ) -> OutboundCampaign:
        """Remove a lead from a draft campaign (and reset its call_status)."""
        if campaign.status != CampaignStatus.draft:
            raise ValueError("Leads can only be detached from draft campaigns.")

        lead_id_str = str(lead_id)
        contacts = [c for c in (campaign.contacts or []) if str(c.get("lead_id")) != lead_id_str]
        if len(contacts) == len(campaign.contacts or []):
            raise ValueError("Lead is not attached to this campaign.")

        row_res = await db.execute(
            select(OutboundCampaignContact).where(
                OutboundCampaignContact.campaign_id == campaign.id,
                OutboundCampaignContact.outgoing_lead_id == lead_id,
            )
        )
        for row in row_res.scalars().all():
            await db.delete(row)

        lead_res = await db.execute(
            select(OutgoingLead).where(
                OutgoingLead.id == lead_id,
                OutgoingLead.tenant_id == tenant_res.tenant_id,
            )
        )
        lead = lead_res.scalars().first()
        if lead is not None and lead.call_status == LeadCallStatus.queued:
            lead.call_status = LeadCallStatus.new
            db.add(lead)

        campaign.contacts = contacts
        campaign.total_count = len(contacts)
        db.add(campaign)
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

    @staticmethod
    async def delete_campaign(
        campaign: OutboundCampaign, db: AsyncSession
    ) -> None:
        """Hard-delete a campaign and its contact join rows.

        Running campaigns must be cancelled first — refusing to delete one in
        flight is the only safe behaviour; we don't want to orphan in-flight
        Exotel calls or partially-processed leads.

        Contacts (``outbound_campaign_contacts``) have a FK to the campaign
        without ON DELETE CASCADE, so they must be removed first. The actual
        ``OutgoingLead`` rows are NOT touched — leads outlive campaigns.

        Cost-ledger rows reference ``campaign_id`` but as a nullable index
        (no FK), so they remain intact as a billing audit trail.
        """
        if campaign.status == CampaignStatus.running:
            raise ValueError(
                "Cancel the campaign before deleting it. Running campaigns are protected."
            )

        # Drop the join rows first so the FK constraint stays happy.
        contacts = await db.execute(
            select(OutboundCampaignContact).where(
                OutboundCampaignContact.campaign_id == campaign.id
            )
        )
        for row in contacts.scalars().all():
            await db.delete(row)

        await db.delete(campaign)
        await db.commit()

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
            # Plivo: pass an HTTP answer_url (returns <Stream> XML) — not a WS url.
            answer_url = f"{base}{prefix}/plivo/outbound-answer/{link_id}"
            status_callback = f"{base}{prefix}/plivo/outbound-status/{link_id}"
            try:
                result = await PlivoService.initiate_outbound_call(
                    tenant_res,
                    to_number=contact["phone"],
                    answer_url=answer_url,
                    status_callback=status_callback,
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
        campaign: OutboundCampaign | None,
        call_link_id: str,
        event_type: str,
        payload: dict,
        db: AsyncSession,
        followup_contact: dict | None = None,
    ) -> None:
        """Webhook entry for call.answered / call.hangup / call.failed /
        call.machine.detection.ended.

        ``followup_contact`` carries the synthetic contact dict produced by
        :meth:`get_by_call_link_id` when the call_link_id resolved against
        ``lead_followup_schedules`` instead of a regular campaign contact.
        In that case ``campaign`` may be None (manual follow-up not tied to
        a campaign) and the contacts list lives in-memory only.
        """
        is_followup_synthetic = followup_contact is not None
        if is_followup_synthetic:
            contacts: list[dict] = [followup_contact]
            target = followup_contact
        else:
            if campaign is None:
                return
            contacts = list(campaign.contacts or [])
            target = next((c for c in contacts if c.get("call_link_id") == call_link_id), None)
            if not target:
                return

        if event_type == "call.answered":
            target["status"] = "answered"
            target["answered_at"] = datetime.now(timezone.utc).isoformat()
            if campaign is not None:
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
                if campaign is not None:
                    campaign.failed_count = (campaign.failed_count or 0) + 1
            duration = payload.get("duration_seconds") or 0
            target["duration_s"] = int(duration)

            # Close the outcome loop: any record the agent created during
            # this call gets its outcome derived from the call disposition,
            # and a follow-up callback is auto-scheduled for no_show /
            # failed_followup states.
            converted_outcome = False
            tenant_for_lookups = (
                campaign.tenant_id if campaign is not None else target.get("tenant_id")
            )
            try:
                from app.services.outcome_tracker import OutcomeTracker
                from app.models.tenant_resources import TenantResources
                from app.services.outcome_tracker import OUTCOME_STATES
                from app.services import flow_session

                if tenant_for_lookups is None:
                    raise RuntimeError("no tenant context for outcome closure")
                tr_res = await db.execute(
                    select(TenantResources).where(TenantResources.tenant_id == tenant_for_lookups)
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
                        # Note: we no longer call auto_followup_if_needed
                        # here — the follow-up agent below owns the
                        # scheduling decision (promise > rule > clamp >
                        # caps), and double-firing would create two pending
                        # rows for the same lead.

                # Conversion kill switch: if any created record reached the
                # 'completed' outcome (i.e. a successful booking/lead row),
                # we treat the call as converted and follow-up enqueue will
                # cancel pending follow-ups instead of scheduling more.
                from app.models.nokvo_one_tool_record import NokvoOneToolRecord

                if org_id and created_record_ids:
                    for rec_id in created_record_ids:
                        try:
                            rec_uuid = uuid.UUID(str(rec_id))
                        except (TypeError, ValueError):
                            continue
                        rec = await db.execute(
                            select(NokvoOneToolRecord)
                            .where(NokvoOneToolRecord.id == rec_uuid)
                            .where(NokvoOneToolRecord.organization_id == org_id)
                        )
                        record = rec.scalars().first()
                        if record is None:
                            continue
                        outcome = flow_session.outcome_summary(record.data or {})
                        if (
                            outcome
                            and outcome.get("status") == OUTCOME_STATES.completed
                        ):
                            converted_outcome = True
                            break
            except Exception:
                logger.exception("NOKVO-CAMPAIGN: outcome closure failed")

            # ── Follow-up agent enqueue ──────────────────────────────────
            # Read the prior call's session memory to surface a callback
            # promise or opt-out cue. If neither is present, the follow-up
            # service falls back to the campaign's admin-set disposition
            # rules. Either way, four kill switches gate the actual insert.
            try:
                from app.services.followup_scheduler_service import (
                    FollowupCue,
                    FollowupSchedulerService,
                )
                from app.services.conversational_memory import (
                    FACT_OPTED_OUT,
                    FACT_PROMISED_CALLBACK_AT,
                )
                from app.services.agent_session_store import AgentSessionStore
                from app.services.outgoing_lead_service import (
                    OutgoingLeadService,
                )
                from app.models.tenant_resources import TenantResources
                from datetime import datetime

                if target.get("lead_id"):
                    lead_id = uuid.UUID(str(target["lead_id"]))
                    lead_res = await db.execute(
                        select(OutgoingLead).where(OutgoingLead.id == lead_id)
                    )
                    lead = lead_res.scalars().first()
                else:
                    lead = None

                # Inspect session memory for opt-out / promise cues. The
                # session may already have been promoted + GC'd by another
                # post-call hook; we gracefully degrade to disposition-only.
                cue_opted_out = False
                cue_promised: datetime | None = None
                call_id = target.get("call_id") or call_link_id
                tenant_for_cue = (
                    campaign.tenant_id if campaign is not None else target.get("tenant_id")
                )
                tr_res2 = (
                    await db.execute(
                        select(TenantResources).where(
                            TenantResources.tenant_id == tenant_for_cue
                        )
                    )
                    if tenant_for_cue
                    else None
                )
                tr2 = tr_res2.scalars().first() if tr_res2 is not None else None
                if tr2 and call_id:
                    try:
                        state = await AgentSessionStore.get_state(tr2, call_id)
                        facts = ((state or {}).get("memory") or {}).get("facts") or {}
                        opt_fact = facts.get(FACT_OPTED_OUT) or {}
                        if opt_fact.get("value") is True:
                            cue_opted_out = True
                        promised_fact = facts.get(FACT_PROMISED_CALLBACK_AT) or {}
                        iso = promised_fact.get("value")
                        if isinstance(iso, str) and iso:
                            try:
                                cue_promised = datetime.fromisoformat(iso)
                            except ValueError:
                                cue_promised = None
                    except Exception:
                        logger.debug(
                            "NOKVO-CAMPAIGN: session inspect for cues failed",
                            exc_info=True,
                        )

                # Opt-out is the legal kill switch — flip consent + cancel
                # pending follow-ups, then stop. Don't enqueue anything.
                if lead and cue_opted_out:
                    await OutgoingLeadService.revoke_consent_and_cancel_followups(
                        lead, db=db, reason="opted_out"
                    )
                elif lead:
                    cue = FollowupCue(
                        promised_callback_at=cue_promised,
                        opted_out=False,
                        converted=converted_outcome,
                    )
                    # Clinic tenants are gated inside enqueue_after_call
                    # (kill switch #0): clinics never auto-schedule.
                    await FollowupSchedulerService.enqueue_after_call(
                        lead=lead,
                        campaign=campaign,
                        source_call_id=call_id,
                        disposition=target["status"],
                        outcome=None,
                        cue=cue,
                        db=db,
                    )
            except Exception:
                logger.exception("NOKVO-CAMPAIGN: follow-up enqueue failed")

        # Follow-up synthetic path: just update the follow-up row state.
        # No campaign contact row to update, no batch terminal check.
        if is_followup_synthetic:
            terminal = {"answered", "no_answer", "failed"}
            if target.get("status") in terminal:
                from app.models.lead_followup_schedule import (
                    FollowupStatus,
                    LeadFollowupSchedule,
                )

                followup_id = target.get("_followup_id")
                if followup_id:
                    try:
                        fid = uuid.UUID(str(followup_id))
                        row = await db.get(LeadFollowupSchedule, fid)
                        if row is not None:
                            row.status = FollowupStatus.completed
                            db.add(row)
                            await db.commit()
                    except Exception:
                        logger.exception(
                            "NOKVO-CAMPAIGN: failed to mark follow-up row complete"
                        )
                # Customer-targeted follow-up: bump the customer's call
                # counters (inbound calls do this at WS teardown; outbound
                # customer calls land here via the status webhook).
                if target.get("customer_id"):
                    try:
                        from datetime import datetime as _dt, timezone as _tz

                        from app.models.customer_base import CustomerBase

                        cust = await db.get(
                            CustomerBase, uuid.UUID(str(target["customer_id"]))
                        )
                        if cust is not None:
                            cust.last_call_at = _dt.now(_tz.utc)
                            cust.call_count = int(cust.call_count or 0) + 1
                            cust.last_call_id = str(
                                target.get("call_id") or call_link_id
                            )
                            db.add(cust)
                            await db.commit()
                    except Exception:
                        logger.exception(
                            "NOKVO-CAMPAIGN: failed to bump customer counters"
                        )
            else:
                await db.commit()
            return

        # Regular campaign path.
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
        """Return (campaign, contact) matching the call_link_id.

        Resolution order:
          1. Campaign contact (regular launch). The contact dict lives
             inline in ``campaign.contacts`` JSONB.
          2. Follow-up schedule row (placed_call_id). Returns a synthetic
             contact dict so the rest of the webhook pipeline behaves
             identically. The synthetic dict carries ``_followup_id`` and
             ``is_followup=True`` so downstream code can detect follow-up
             state without an extra DB hit.
        """
        result = await db.execute(
            select(OutboundCampaign).where(
                OutboundCampaign.status.in_([CampaignStatus.running, CampaignStatus.completed])
            )
        )
        for campaign in result.scalars().all():
            for contact in campaign.contacts or []:
                if contact.get("call_link_id") == call_link_id:
                    return campaign, contact

        # Fall through: follow-up table.
        from app.models.lead_followup_schedule import (
            FollowupStatus,
            LeadFollowupSchedule,
        )

        fr_res = await db.execute(
            select(LeadFollowupSchedule)
            .where(LeadFollowupSchedule.placed_call_id == call_link_id)
            .where(
                LeadFollowupSchedule.status.in_(
                    [FollowupStatus.in_flight, FollowupStatus.completed]
                )
            )
            .limit(1)
        )
        followup = fr_res.scalars().first()
        if followup is None:
            return None, None

        campaign = None
        if followup.campaign_id is not None:
            cr_res = await db.execute(
                select(OutboundCampaign).where(
                    OutboundCampaign.id == followup.campaign_id
                )
            )
            campaign = cr_res.scalars().first()

        # Customer-targeted follow-up (clinic manual path): synthesize the
        # contact from the CustomerBase row. No lead_id — downstream code
        # keys lead-only behavior (consent, auto-followup enqueue) on it.
        if followup.customer_id is not None and followup.lead_id is None:
            from app.models.customer_base import CustomerBase

            customer = await db.get(CustomerBase, followup.customer_id)
            synthetic_contact = {
                "call_link_id": call_link_id,
                "customer_id": str(followup.customer_id),
                "tenant_id": followup.tenant_id,
                "phone": customer.phone_e164 if customer else None,
                "name": (customer.name if customer else None) or "",
                "status": "calling",
                "is_followup": True,
                "_followup_id": str(followup.id),
                "_source_call_id": followup.source_call_id,
                "_attempt_n": int(followup.attempts or 0),
                "_admin_note": followup.note or "",
            }
            return campaign, synthetic_contact

        lead_res = await db.execute(
            select(OutgoingLead).where(OutgoingLead.id == followup.lead_id)
        )
        lead = lead_res.scalars().first()

        synthetic_contact = {
            "call_link_id": call_link_id,
            "lead_id": str(followup.lead_id),
            "phone": (lead.phone_e164 or lead.phone_raw) if lead else None,
            "name": (lead.name if lead else None) or "",
            "status": "calling",
            "is_followup": True,
            "_followup_id": str(followup.id),
            "_source_call_id": followup.source_call_id,
            "_attempt_n": int(followup.attempts or 0),
            # The captured callback reason (e.g. "call back Tue 4pm re: 3BHK
            # pricing") set by LeadFollowupNoteScheduler. Surfaces as
            # ``admin_note`` in the follow-up call's prompt alongside the lead's
            # handoff_note (the customer branch already carries this).
            "_admin_note": followup.note or "",
        }
        return campaign, synthetic_contact

    # ------------------------------------------------------------------
    # Chunks for agent injection
    # ------------------------------------------------------------------

    @staticmethod
    def get_chunks(campaign: OutboundCampaign) -> list[dict[str, Any]]:
        """Return document chunks in Qdrant-compatible shape for agent injection."""
        if not campaign.doc_text:
            return []
        return _doc_to_chunks(campaign.doc_text)
