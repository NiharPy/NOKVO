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
import json
import logging
import random
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy import text as _sql_text
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.outgoing_lead import LeadCallStatus, OutboundCampaignContact, OutgoingLead
from app.models.outbound_campaign import CampaignStatus, OutboundCampaign
from app.models.organization import Organization
from app.models.tenant_resources import TenantResources
from app.services.plivo_service import PlivoService
from app.services.agent_outbound_context import build_agent_config, invalidate as invalidate_outbound_context
from app.services.outgoing_lead_service import OutgoingLeadService, lead_is_callable


# Bulk caller-ID pool cache: the DIDs rented on a bulk sub-account, keyed by
# auth_id, so we don't hit Plivo's /Number/ list on every dial refill. Short TTL
# so newly-rented numbers appear within a minute. ``time.monotonic`` stamped.
_CALLER_POOL_TTL_S = 60.0
_CALLER_POOL_CACHE: dict[str, tuple[float, list[str]]] = {}


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


def _parse_csv(content: bytes) -> list[dict[str, str]]:
    """Return list of {phone, name} from a CSV: first two populated columns,
    same contract as ``_parse_excel`` (phone in col A, name in col B, header
    row auto-skipped)."""
    import csv

    text = content.decode("utf-8-sig", errors="ignore")
    contacts: list[dict[str, str]] = []
    header_skipped = False
    for row in csv.reader(io.StringIO(text)):
        cells = [str(c).strip() if c is not None else "" for c in row]
        if not any(cells):
            continue
        first = cells[0] if cells else ""
        # Auto-detect header: first row with no digit-run in col0 is the header.
        if not header_skipped and not re.search(r"\d{7,}", first):
            header_skipped = True
            continue
        phone = re.sub(r"[^\d+]", "", first)
        name = cells[1] if len(cells) > 1 else ""
        if len(phone) >= 7:
            contacts.append({"phone": phone, "name": name or phone})
    return contacts


def _parse_contacts(filename: str | None, content: bytes) -> list[dict[str, str]]:
    """Parse a contacts upload into [{phone, name}]. Branches on extension:
    ``.csv`` → CSV reader; ``.xlsx`` (anything else) → openpyxl."""
    name = (filename or "").lower()
    if name.endswith(".csv"):
        return _parse_csv(content)
    return _parse_excel(content)


def _canonical_phone(raw: str | None) -> str:
    """Canonicalize a phone to bare-digit E.164 for India (this is an India-only
    product — +91, DLT, etc.). Strips all formatting; a 10-digit bare mobile gets
    the 91 country code, a leading-0 trunk form (0XXXXXXXXXX) becomes
    91XXXXXXXXXX. Returns "" when there are no usable digits.

    The point is dedupe + correct dialing: ``7569672503``, ``+91 75696 72503``
    and ``917569672503`` all collapse to ``917569672503`` — so the same person
    isn't entered (and dialed) as several separate contacts, and a bare 10-digit
    number that Plivo would otherwise reject gets its country code."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if len(digits) == 10:
        return "91" + digits
    if len(digits) == 11 and digits.startswith("0"):
        return "91" + digits[1:]
    return digits


def _dedupe_contacts(contacts_raw: list[dict[str, str]]) -> list[dict[str, str]]:
    """Collapse a parsed dial list to one entry per canonical phone (first
    occurrence's name wins), dropping rows whose number can't be canonicalized.
    Returns [{phone, name}] with ``phone`` already in canonical dial form.

    Without this, a CSV that lists the same person twice — commonly once bare and
    once with the country code — dials them twice (and only the +country-code one
    actually connects), which reads to the recipient as "it keeps calling me"."""
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for c in contacts_raw:
        canon = _canonical_phone(c.get("phone"))
        if not canon or canon in seen:
            continue
        seen.add(canon)
        out.append({"phone": canon, "name": c.get("name") or canon})
    return out


async def _scrub_dnd(contacts: list[dict[str, str]]) -> tuple[list[dict[str, str]], list[str]]:
    """Drop DND-registered numbers from a canonicalized contact list before dialing.

    Returns ``(kept, dropped)`` where ``dropped`` is the list of canonical phones
    removed because they're on the NCPR/DND register. Fail-open: if scrubbing is
    disabled or the vendor API errors, nothing is dropped (the un-scrubbed list is
    returned). See :mod:`app.services.dnd_scrub_service`."""
    from app.services.dnd_scrub_service import scrub_numbers

    result = await scrub_numbers([c["phone"] for c in contacts])
    if not result.blocked:
        return contacts, []
    kept = [c for c in contacts if not result.is_blocked(c["phone"])]
    dropped = [c["phone"] for c in contacts if result.is_blocked(c["phone"])]
    if dropped:
        logger.info("NOKVO-DND: removed %d DND number(s) from dial list", len(dropped))
    return kept, dropped


# Retain fire-and-forget ingestion tasks so asyncio doesn't GC them mid-run.
_ingest_tasks: set[asyncio.Task] = set()


def _iter_parsed_contacts(filename: str | None, content: bytes):
    """LAZILY yield canonicalized ``{phone, name}`` from an uploaded CSV/XLSX so a
    1M-row file never fully materializes in RAM (the V2 ingestion path). CSV is
    streamed via ``csv.reader``; XLSX falls back to the existing list parser
    (spreadsheets are rarely millions of rows)."""
    name = (filename or "").lower()
    if not name.endswith(".csv"):
        for c in _parse_contacts(filename, content):
            phone = _canonical_phone(c.get("phone"))
            if phone:
                yield {"phone": phone, "name": c.get("name") or phone}
        return
    import csv as _csv

    text = content.decode("utf-8-sig", errors="ignore")
    header_skipped = False
    for row in _csv.reader(io.StringIO(text)):
        cells = [str(c).strip() if c is not None else "" for c in row]
        if not any(cells):
            continue
        first = cells[0] if cells else ""
        if not header_skipped and not re.search(r"\d{7,}", first):
            header_skipped = True
            continue
        phone = _canonical_phone(first)
        if not phone:
            continue
        yield {"phone": phone, "name": (cells[1] if len(cells) > 1 else "") or phone}


def _parse_document(filename: str, content: bytes) -> str:
    """Extract plain text from a PDF, DOCX, or TXT file.

    Thin alias kept for the existing call sites; the implementation now lives in
    the shared ``document_text`` util (also used by the Brochure Analyzer)."""
    from app.services.document_text import extract_document_text

    return extract_document_text(filename, content)


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
# Scheduled-dialer window (TRAI-compliant outbound pacing)
# ---------------------------------------------------------------------------
# Deterministic / APEX campaigns carry a ``call_window`` on agent_config:
#   {working_days: N, hours_per_day: H, calls_per_day: C,
#    timezone: "Asia/Kolkata", window_local: "09:00-19:00",
#    dialed_today: {date: "YYYY-MM-DD", count: N}}   # runtime counter
# India commercial calling is restricted to ~09:00–19:00 IST; we also let the
# customer pick how many weekdays and how many calls/day. These pure helpers make
# the gate decision (the dialer enforces it; the wake-tick poller resumes paused
# campaigns when the window reopens).

_IST_TZ = "Asia/Kolkata"


def _ist_now() -> datetime:
    """Current time in Asia/Kolkata (tz-aware)."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(_IST_TZ))
    except Exception:  # pragma: no cover — zoneinfo always present on 3.9+
        return datetime.now(timezone.utc)


def _allowed_weekdays(working_days) -> set[int]:
    """Map a days/week COUNT to weekday indexes (Mon=0). 5→Mon–Fri, 6→Mon–Sat,
    7→all; clamped to 1..7; the first N starting Monday."""
    try:
        n = int(working_days)
    except (TypeError, ValueError):
        n = 7
    n = max(1, min(7, n))
    return set(range(n))  # 0..n-1  (Mon, Tue, …)


def _parse_window_hours(window_local) -> tuple[int, int]:
    """Parse "HH:MM-HH:MM" → (start_hour, end_hour); default (9, 19)."""
    try:
        start_s, end_s = str(window_local).split("-", 1)
        return int(start_s.split(":")[0]), int(end_s.split(":")[0])
    except Exception:
        return 9, 19


def call_window_allows(call_window: dict | None, now: datetime) -> tuple[bool, str]:
    """Pure: may a campaign dial at ``now`` (tz-aware, IST)? Returns
    (allowed, reason). No window → always allowed (legacy / Nokvo One). The hour
    band is 09:00–19:00 by default; the day is gated by ``working_days``."""
    if not call_window:
        return True, "no_window"
    start_h, end_h = _parse_window_hours(call_window.get("window_local"))
    if now.hour < start_h or now.hour >= end_h:
        return False, "outside_hours"
    if now.weekday() not in _allowed_weekdays(call_window.get("working_days")):
        return False, "outside_days"
    return True, "open"


def _dialed_today_count(call_window: dict | None, today_str: str) -> int:
    """Today's placement count from the runtime counter, 0 on an IST date
    rollover (the counter resets each day)."""
    d = (call_window or {}).get("dialed_today") or {}
    if d.get("date") == today_str:
        try:
            return int(d.get("count") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class OutboundCampaignService:
    # Bulk CSV campaigns dial up to this many lines simultaneously, refilling
    # one-for-one as calls end (independent of the global dial concurrency).
    BULK_DIAL_CONCURRENCY = 5

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
        # Leads can be added to a draft OR an already-launched campaign (running
        # / completed) so the admin can top up an existing campaign and relaunch
        # to call the new ones. A cancelled campaign is terminal — recreate it.
        if campaign.status == CampaignStatus.cancelled:
            raise ValueError("This campaign was cancelled — create a new one to add leads.")

        leads = await OutgoingLeadService.validate_callable_leads(tenant_res, db, lead_ids)

        # Row-lock + refresh BEFORE the read-modify-write of ``contacts``. attach can
        # run on an already-running campaign (only ``cancelled`` is blocked above), so
        # a concurrent in-flight status webhook may be mutating the same JSON blob.
        # Without the lock this admin commit would write back a snapshot read before
        # the webhook landed — reverting a just-ended/answered contact and re-opening
        # the lost-update that strands slots / re-dials reached leads. Held until the
        # commit below. See ``_lock_campaign``.
        locked = await OutboundCampaignService._lock_campaign(db, campaign.id)
        if locked is not None:
            campaign = locked

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
        # MUST flag: contacts is a plain JSONB column and the read-modify-write
        # above mutates shared dict refs in place, so SQLAlchemy's old==new check
        # would otherwise SKIP the UPDATE — freezing contacts at the INSERT value
        # and making the dialer re-dial "pending" contacts forever.
        flag_modified(campaign, "contacts")
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
        # MUST flag: contacts is a plain JSONB column and the read-modify-write
        # above mutates shared dict refs in place, so SQLAlchemy's old==new check
        # would otherwise SKIP the UPDATE — freezing contacts at the INSERT value
        # and making the dialer re-dial "pending" contacts forever.
        flag_modified(campaign, "contacts")
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
        """Stop a campaign: no further contacts are dialed (in-flight calls
        finish — webhook refills check status), and the tenant's one-campaign
        slot frees immediately. ``ingesting`` is cancellable too — a big upload
        holds the slot, so it needs a way out; the background ingest completion
        respects the cancelled status (guarded UPDATE in _ingest_and_dial_v2)."""
        if campaign.status not in (
            CampaignStatus.draft, CampaignStatus.running, CampaignStatus.ingesting
        ):
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
        ``lead_followup_schedules.campaign_id`` (nullable FK, no CASCADE) is
        NULLed rather than deleted — follow-ups may outlive their campaign;
        leaving it referenced was the FK violation that 500'd every delete of
        a campaign that had follow-up rows.

        Cost-ledger rows reference ``campaign_id`` but as a nullable index
        (no FK), so they remain intact as a billing audit trail.
        """
        if campaign.status == CampaignStatus.running:
            raise ValueError(
                "Cancel the campaign before deleting it. Running campaigns are protected."
            )

        # Drop the join rows first so the FK constraint stays happy — as ONE bulk
        # DELETE executed immediately on the connection. The old ORM
        # load-and-delete loop left statement ordering to the unit of work, and
        # with NO relationship() edge between the campaign and contact mappers
        # the flush had no dependency to honour — prod emitted the campaign
        # DELETE before the contact DELETEs and hit the contacts FK every time.
        # (It also loaded every row into RAM — untenable for a V2 campaign.)
        await db.execute(
            _sql_text("DELETE FROM outbound_campaign_contacts WHERE campaign_id = :c"),
            {"c": str(campaign.id)},
        )

        # Orphan (don't delete) the follow-up schedules that point at this
        # campaign — the column is nullable by design.
        await db.execute(
            _sql_text(
                "UPDATE lead_followup_schedules SET campaign_id = NULL WHERE campaign_id = :c"
            ),
            {"c": str(campaign.id)},
        )

        await db.delete(campaign)
        await db.commit()

    # ------------------------------------------------------------------
    # Launch
    # ------------------------------------------------------------------

    @staticmethod
    async def _assert_no_other_active_campaign(
        db: AsyncSession, tenant_id: str, *, exclude_id: uuid.UUID | None = None
    ) -> None:
        """ONE campaign per tenant at a time. Raises ValueError (→ 4xx at the API)
        when another of the tenant's campaigns is running or ingesting.

        Serialized by a tenant-scoped advisory lock taken in the CALLER's
        transaction: the lock is held until the caller's status-flipping commit,
        so two simultaneous launches can't both pass the check — the second
        blocks here, then sees the first's committed active status. Relaunching /
        resuming / appending to the SAME campaign passes ``exclude_id``."""
        await db.execute(
            _sql_text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
            {"k": f"campaign-slot:{tenant_id}"},
        )
        conditions = [
            OutboundCampaign.tenant_id == tenant_id,
            OutboundCampaign.status.in_((CampaignStatus.running, CampaignStatus.ingesting)),
        ]
        if exclude_id is not None:
            conditions.append(OutboundCampaign.id != exclude_id)
        name = (
            await db.execute(select(OutboundCampaign.name).where(*conditions).limit(1))
        ).scalar_one_or_none()
        if name is not None:
            raise ValueError(
                f"'{name}' is still running. Only one campaign can run at a time — "
                "wait for it to finish or cancel it before starting another."
            )

    @staticmethod
    async def launch_campaign(
        campaign: OutboundCampaign,
        db: AsyncSession,
        *,
        public_base_url: str,
        path_prefix: str = "/api/nokvo-one/agents",
        tenant_res: TenantResources | None = None,
    ) -> OutboundCampaign:
        if campaign.status == CampaignStatus.cancelled:
            raise ValueError("This campaign was cancelled — create a new one to call leads.")
        await OutboundCampaignService._assert_no_other_active_campaign(
            db, campaign.tenant_id, exclude_id=campaign.id
        )
        # Relaunch: a running/completed campaign can be launched again to dial the
        # leads added since (only the undialed contacts get called — see below).
        is_relaunch = campaign.status in (CampaignStatus.running, CampaignStatus.completed)

        if tenant_res is None:
            res = await db.execute(
                select(TenantResources).where(TenantResources.tenant_id == campaign.tenant_id)
            )
            tenant_res = res.scalars().first()
            if tenant_res is None:
                raise ValueError("Tenant resources for this campaign could not be loaded.")

        # Caller ID = the tenant's allotted Plivo DID. Resolve + validate ONCE so we
        # fail fast (instead of every contact failing at dial time) and record it on
        # the campaign so the UI can show "calling from <number>".
        caller_id = PlivoService.outbound_caller_id(tenant_res)
        if not caller_id:
            raise ValueError(
                "No outbound number is provisioned for this account yet. "
                "Finish telephony setup before launching a calling campaign."
            )

        base = public_base_url.rstrip("/")
        prefix = path_prefix.rstrip("/")
        # Row-lock + refresh BEFORE re-arming ``contacts``. On a relaunch the prior
        # batch may still be draining (status webhooks mutating the JSON blob under
        # their own lock); arming from an unlocked snapshot would clobber a just-ended
        # contact back to a live state, leaking its slot and starving fan-out. A fresh
        # draft launch simply locks an uncontended row. Held until the commit below.
        locked = await OutboundCampaignService._lock_campaign(db, campaign.id)
        if locked is not None:
            campaign = locked
        contacts = list(campaign.contacts or [])
        # Dial only contacts that haven't been placed yet: a fresh draft → all of
        # them; a relaunch → only the newly-attached (undialed) ones, identified
        # by the absence of a ``call_id`` / terminal ``ended`` marker. This is
        # what stops a relaunch from re-calling leads already contacted.
        to_dial = [c for c in contacts if not c.get("call_id") and not c.get("ended")]
        if not to_dial:
            raise ValueError(
                "No new leads to call — attach more leads before relaunching."
                if is_relaunch
                else "This campaign has no leads to call. Attach leads first."
            )
        lead_ids: list[uuid.UUID] = []
        for contact in to_dial:
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
        if len(callable_by_id) != len(to_dial):
            raise ValueError("Some leads to call are no longer callable — refresh and try again.")

        # Arm only the to-dial contacts; the throttled dialer places up to the
        # concurrency cap and refills one-for-one as calls end (Plivo status
        # webhook → dial_next_pending). Already-dialed contacts keep their state
        # so a relaunch never re-calls them.
        for contact in to_dial:
            contact["status"] = "pending"
            contact.pop("ended", None)
        campaign.from_number = caller_id
        campaign.status = CampaignStatus.running
        campaign.started_at = datetime.now(timezone.utc)
        campaign.contacts = contacts
        # MUST flag: contacts is a plain JSONB column and the read-modify-write
        # above mutates shared dict refs in place, so SQLAlchemy's old==new check
        # would otherwise SKIP the UPDATE — freezing contacts at the INSERT value
        # and making the dialer re-dial "pending" contacts forever.
        flag_modified(campaign, "contacts")
        db.add(campaign)
        await db.commit()
        await db.refresh(campaign)

        await OutboundCampaignService._dial_pending(
            campaign, db, tenant_res=tenant_res, base=base, prefix=prefix
        )
        return campaign

    @staticmethod
    async def create_and_launch_bulk_campaign(
        tenant_res: TenantResources,
        db: AsyncSession,
        *,
        name: str,
        contacts_file: UploadFile,
        agent_config: dict[str, Any] | None = None,
        public_base_url: str,
        path_prefix: str = "/api/nokvo-one/agents",
    ) -> OutboundCampaign:
        """Bulk CSV Calling: parse an uploaded CSV/XLSX of phone+name rows and
        immediately dial the whole list from the tenant's dedicated bulk number.

        Unlike the lead-based path this does NOT require consented lead records —
        access is gated upstream (Inbound+Outbound plan + a SuperAdmin grant that
        provisions the dedicated Plivo number/auth). Calls are placed on that
        dedicated account, never the tenant's inbound DID.
        """
        if not PlivoService.bulk_calling_enabled(tenant_res):
            raise ValueError(
                "Bulk calling isn't enabled for this account yet. It unlocks once "
                "the team provisions your dedicated calling number."
            )
        await OutboundCampaignService._assert_no_other_active_campaign(db, tenant_res.tenant_id)
        cfg = build_agent_config(**dict(agent_config or {}))
        cfg["bulk_csv"] = True
        # Bulk campaigns never auto-call a lead back: a CSV list is dialed once,
        # and if the lead cuts the call we don't chase them with a follow-up.
        cfg["followup_rules"] = {"enabled": False}
        # Campaign type marker (deterministic = intro + scored questionnaire;
        # non-deterministic = free-form offer pitch). Set post-build like bulk_csv
        # since build_agent_config doesn't echo unknown keys.
        cfg["deterministic"] = bool((agent_config or {}).get("deterministic"))
        # A bulk campaign needs SOMETHING to drive the call: either free-form
        # offer details (non-deterministic) OR a questionnaire (deterministic).
        if not str(cfg.get("agent_prompt") or "").strip() and not cfg.get("questionnaire"):
            raise ValueError(
                "Bulk campaigns need either offer details (what to say) or a "
                "questionnaire. Add one and try again."
            )

        raw = await contacts_file.read()

        # ── V2 scale path: async, per-row, up to 1M contacts ────────────────────
        # Skip the inline parse/scrub/blob (which is O(n) and request-blocking).
        # Create the campaign in ``ingesting`` and stream-ingest in the background;
        # DND scrub moves to dial-time. Returns immediately (no request timeout).
        if settings.CAMPAIGN_CONTACTS_V2:
            caller_id = PlivoService.bulk_calling_caller_id(tenant_res)
            auth_override = PlivoService.bulk_calling_auth(tenant_res)
            if not caller_id or not auth_override:
                raise ValueError(
                    "Your dedicated bulk calling number isn't fully configured. "
                    "Contact support to finish setup."
                )
            return await OutboundCampaignService._create_bulk_campaign_v2(
                tenant_res, db,
                name=name, cfg=cfg, raw=raw, filename=contacts_file.filename,
                caller_id=caller_id, public_base_url=public_base_url, path_prefix=path_prefix,
            )

        # Canonicalize + dedupe so the same person listed twice (e.g. bare
        # ``7569672503`` and ``+917569672503``) becomes ONE contact dialed once,
        # in a form Plivo accepts.
        contacts_raw = _dedupe_contacts(_parse_contacts(contacts_file.filename, raw))
        if not contacts_raw:
            raise ValueError(
                "No valid phone numbers found in the file. Put phone numbers in the "
                "first column and names in the second (CSV or XLSX)."
            )

        # Scrub the DND register before dialing — drop opted-out numbers so we
        # don't call people on India's NCPR list. Fail-open: a vendor hiccup leaves
        # the list untouched rather than killing the campaign.
        contacts_raw, dnd_dropped = await _scrub_dnd(contacts_raw)
        if not contacts_raw:
            raise ValueError(
                "Every number on this list is registered on the DND (Do Not "
                "Disturb) register, so none can be called."
            )

        # Surface DND removals to the UI (notice + list), persisted on the config.
        cfg["dnd_dropped"] = dnd_dropped

        caller_id = PlivoService.bulk_calling_caller_id(tenant_res)
        auth_override = PlivoService.bulk_calling_auth(tenant_res)
        if not caller_id or not auth_override:
            raise ValueError(
                "Your dedicated bulk calling number isn't fully configured. "
                "Contact support to finish setup."
            )

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
            status=CampaignStatus.running,
            contacts=contacts,
            agent_config=cfg,
            from_number=caller_id,
            total_count=len(contacts),
            started_at=datetime.now(timezone.utc),
        )
        db.add(campaign)
        await db.commit()
        await db.refresh(campaign)
        invalidate_outbound_context(campaign.id)

        await OutboundCampaignService._dial_pending(
            campaign,
            db,
            tenant_res=tenant_res,
            base=public_base_url.rstrip("/"),
            prefix=path_prefix.rstrip("/"),
        )
        return campaign

    @staticmethod
    async def rerun_bulk_campaign(
        campaign: OutboundCampaign,
        db: AsyncSession,
        *,
        tenant_res: TenantResources,
        public_base_url: str,
        path_prefix: str = "/api/nokvo-one/agents",
    ) -> OutboundCampaign:
        """Dial a bulk CSV campaign's whole list again — re-runs the same contacts
        from scratch (fresh call_link_ids, counters reset). Useful after fixing the
        dedicated number, or to re-contact the list. Unlike the lead-based relaunch
        (which only dials newly-added consented leads), this re-arms EVERY contact.
        """
        if not OutboundCampaignService._is_bulk(campaign):
            raise ValueError("Re-run is only for bulk CSV campaigns.")
        if not PlivoService.bulk_calling_enabled(tenant_res):
            raise ValueError(
                "Bulk calling isn't enabled for this account. It unlocks once the "
                "team provisions your dedicated calling number."
            )
        await OutboundCampaignService._assert_no_other_active_campaign(
            db, campaign.tenant_id, exclude_id=campaign.id
        )
        caller_id = PlivoService.bulk_calling_caller_id(tenant_res)
        if not caller_id or not PlivoService.bulk_calling_auth(tenant_res):
            raise ValueError(
                "Your dedicated bulk calling number isn't fully configured yet."
            )

        # ── V2 (per-row) campaigns: contacts live in outbound_campaign_contacts,
        # not the blob — re-arm the unreached rows (no_answer/failed → pending,
        # fresh call_link_ids) and resume. This is also the re-run a just-appended
        # CSV takes: the new rows are already pending, so it resumes dialing them
        # alongside any re-armed misses. Answered/completed rows stay untouched.
        # ``not contacts`` (not ``is None``): an EMPTY blob is a V2 campaign too —
        # its rows are the store, the [] is stray legacy residue. ──
        if settings.CAMPAIGN_CONTACTS_V2 and not campaign.contacts:
            if campaign.status == CampaignStatus.cancelled:
                raise ValueError("This campaign was cancelled — create a new one instead.")
            from app.services import campaign_contacts_v2 as v2

            await v2.rearm_unreached(db, campaign.id)
            if await v2.pending_count(db, campaign.id) <= 0:
                raise ValueError(
                    "Everyone on this list was already reached — there's no one to re-run."
                )
            await db.execute(
                _sql_text(
                    "UPDATE outbound_campaigns SET status = 'running', completed_at = NULL "
                    "WHERE id = :c AND status <> 'cancelled'"
                ),
                {"c": str(campaign.id)},
            )
            await db.commit()
            await db.refresh(campaign)
            invalidate_outbound_context(campaign.id)
            await OutboundCampaignService._dial_pending(
                campaign,
                db,
                tenant_res=tenant_res,
                base=public_base_url.rstrip("/"),
                prefix=path_prefix.rstrip("/"),
            )
            return campaign

        # Row-lock + refresh BEFORE reading/rebuilding ``contacts``. Re-run has no
        # status guard, so the prior batch can still be in flight when the operator
        # clicks it. Reading the contacts snapshot unlocked here races each call's
        # single hangup webhook: if a contact's ``answered``/``ended`` commit landed
        # after this method's stale read, the rebuild below would see it as
        # not-reached, re-arm it with a fresh call_link_id and re-dial someone who
        # already picked up — the "I cut the call and it rang me again" report. The
        # lock (held to the commit below) serializes against the webhook writer.
        locked = await OutboundCampaignService._lock_campaign(db, campaign.id)
        if locked is not None:
            campaign = locked
        existing = [ct for ct in (campaign.contacts or []) if ct.get("phone")]
        if not existing:
            raise ValueError("This campaign has no contacts to call.")

        # Don't call anyone back who was already reached — a lead who picked up
        # (including one who answered then cut the call) is left untouched. Re-run
        # only re-arms contacts we never connected to (no answer / failed / never
        # dialed). Fresh call_link_ids so stale webhooks can't map onto retries.
        def _reached(ct: dict) -> bool:
            return ct.get("status") == "answered" or bool(ct.get("answered_at"))

        # Canonicalize phones (bare 10-digit → 91…) and collapse duplicates while
        # rebuilding. Campaigns created before canonicalization existed stored RAW
        # numbers, which break re-run two ways:
        #   * a bare 10-digit Indian mobile whose leading digits aren't "91" (e.g.
        #     7569672503) is read by Plivo as a *foreign* country code and 403'd
        #     ("Calls to this destination region are barred."), so it never dials —
        #     prepending 91 routes it to India;
        #   * the same person listed BOTH bare and as +91 becomes two contacts and
        #     is dialed twice ("it keeps calling me").
        # Re-run is the fix-up path for those older lists, so dial the corrected,
        # deduped numbers. A person reached under ANY form is never re-dialed; the
        # ``reached`` set wins so a redial twin can't resurrect an answered contact.
        reached_out: list[dict] = []
        reached_canon: set[str] = set()
        seen: set[str] = set()
        for ct in existing:
            if not _reached(ct):
                continue
            canon = _canonical_phone(ct.get("phone"))
            if not canon or canon in seen:
                continue
            seen.add(canon)
            reached_canon.add(canon)
            kept = dict(ct)
            kept["phone"] = canon  # normalize the stored form for the UI
            reached_out.append(kept)

        rearmed: list[dict] = []
        for ct in existing:
            if _reached(ct):
                continue
            canon = _canonical_phone(ct.get("phone"))
            # Drop uncanonicalizable junk, anyone already reached under another
            # form, and duplicates within the redial set itself.
            if not canon or canon in reached_canon or canon in seen:
                continue
            seen.add(canon)
            rearmed.append({
                "phone": canon,
                "name": ct.get("name") or canon,
                "status": "pending",
                "call_id": None,
                "call_link_id": str(uuid.uuid4()),
                "duration_s": None,
                "answered_at": None,
            })
        if not rearmed:
            raise ValueError(
                "Everyone on this list was already reached — there's no one to re-run."
            )
        # Re-scrub the redial set against DND before re-arming — a number may have
        # opted out since the original campaign. Already-reached contacts are left
        # as-is (we're not calling them again either way). Fail-open.
        rearmed, dnd_dropped = await _scrub_dnd(rearmed)
        if not rearmed:
            raise ValueError(
                "Everyone left to re-run is now on the DND register — there's no "
                "one to call."
            )
        cfg = dict(campaign.agent_config or {})
        cfg["dnd_dropped"] = dnd_dropped
        campaign.agent_config = cfg
        contacts = reached_out + rearmed
        campaign.contacts = contacts
        # MUST flag: contacts is a plain JSONB column and the read-modify-write
        # above mutates shared dict refs in place, so SQLAlchemy's old==new check
        # would otherwise SKIP the UPDATE — freezing contacts at the INSERT value
        # and making the dialer re-dial "pending" contacts forever.
        flag_modified(campaign, "contacts")
        campaign.status = CampaignStatus.running
        campaign.started_at = datetime.now(timezone.utc)
        campaign.completed_at = None
        campaign.total_count = len(contacts)
        campaign.answered_count = len(reached_out)  # keep prior answers; recount the rest
        campaign.failed_count = 0
        campaign.from_number = caller_id  # re-resolve in case it changed on re-grant
        db.add(campaign)
        await db.commit()
        await db.refresh(campaign)
        invalidate_outbound_context(campaign.id)

        await OutboundCampaignService._dial_pending(
            campaign,
            db,
            tenant_res=tenant_res,
            base=public_base_url.rstrip("/"),
            prefix=path_prefix.rstrip("/"),
        )
        return campaign

    @staticmethod
    async def set_followup_enabled(
        campaign: OutboundCampaign, enabled: bool, db: AsyncSession
    ) -> OutboundCampaign:
        """Turn the follow-up agent on/off for a campaign.

        Flips ``agent_config.followup_rules.enabled`` — the flag the follow-up
        scheduler already honours (`followup_scheduler_service.py`: enqueue is a
        no-op when it's False). Reassigns ``agent_config`` to a new dict so the
        JSONB change is detected, and invalidates the cached outbound context."""
        cfg = dict(campaign.agent_config or {})
        rules = dict(cfg.get("followup_rules") or {})
        rules["enabled"] = bool(enabled)
        cfg["followup_rules"] = rules
        campaign.agent_config = cfg
        db.add(campaign)
        await db.commit()
        await db.refresh(campaign)
        invalidate_outbound_context(campaign.id)
        return campaign

    @staticmethod
    async def _create_bulk_campaign_v2(
        tenant_res: TenantResources,
        db: AsyncSession,
        *,
        name: str,
        cfg: dict[str, Any],
        raw: bytes,
        filename: str | None,
        caller_id: str,
        public_base_url: str,
        path_prefix: str,
    ) -> OutboundCampaign:
        """V2: create the campaign in ``ingesting`` and stream-ingest contacts into
        per-row storage in the BACKGROUND (COPY), then flip to ``running`` and kick
        the dialer. Returns immediately so the upload request never blocks/timeouts
        on a 1M-row file."""
        campaign = OutboundCampaign(
            id=uuid.uuid4(),
            tenant_id=tenant_res.tenant_id,
            name=name,
            status=CampaignStatus.ingesting,
            agent_config=cfg,
            from_number=caller_id,
            total_count=0,
            started_at=datetime.now(timezone.utc),
        )
        db.add(campaign)
        await db.commit()
        await db.refresh(campaign)
        invalidate_outbound_context(campaign.id)

        cid = campaign.id
        base = public_base_url.rstrip("/")
        prefix = path_prefix.rstrip("/")
        task = asyncio.create_task(
            OutboundCampaignService._ingest_and_dial_v2(
                cid, raw, filename, tenant_res.tenant_id, base, prefix
            )
        )
        _ingest_tasks.add(task)
        task.add_done_callback(_ingest_tasks.discard)
        return campaign

    @staticmethod
    async def _ingest_and_dial_v2(
        campaign_id: uuid.UUID,
        raw: bytes,
        filename: str | None,
        tenant_id: str,
        base: str,
        prefix: str,
        append: bool = False,
    ) -> None:
        """Background: COPY-ingest the file into per-row storage, set the campaign
        ``running``, and kick the dialer. ``append=True`` ADDS to an existing
        campaign (new numbers only — ON CONFLICT dedupe): ``total_count`` is
        incremented (not overwritten) and the campaign is resumed to ``running``.
        Its own DB session; never raises."""
        from app.db.session import AsyncSessionLocal
        from app.services import campaign_contacts_v2 as v2

        try:
            inserted = await v2.ingest_rows(campaign_id, _iter_parsed_contacts(filename, raw))
            async with AsyncSessionLocal() as db:
                if append:
                    # Add to the running total + resume dialing the new pending rows.
                    await db.execute(
                        _sql_text(
                            "UPDATE outbound_campaigns SET total_count = total_count + :n, "
                            "status = CASE WHEN status = 'cancelled' THEN status ELSE 'running' END "
                            "WHERE id = :c"
                        ),
                        {"n": inserted, "c": str(campaign_id)},
                    )
                else:
                    await db.execute(
                        _sql_text(
                            # NEVER resurrect a campaign the user cancelled mid-ingest.
                            "UPDATE outbound_campaigns SET total_count = :n, status = "
                            "CASE WHEN status = 'cancelled' THEN status "
                            "WHEN :n > 0 THEN 'running' ELSE 'completed' END WHERE id = :c"
                        ),
                        {"n": inserted, "c": str(campaign_id)},
                    )
                await db.commit()
            logger.info("NOKVO-INGEST-V2: campaign %s %s %d contacts",
                        campaign_id, "appended" if append else "ingested", inserted)
        except Exception:
            logger.exception("NOKVO-INGEST-V2: ingestion failed for campaign %s", campaign_id)
            try:
                async with AsyncSessionLocal() as db:
                    await db.execute(
                        _sql_text("UPDATE outbound_campaigns SET status='ingest_failed' WHERE id=:c"),
                        {"c": str(campaign_id)},
                    )
                    await db.commit()
            except Exception:
                logger.exception("NOKVO-INGEST-V2: could not mark ingest_failed for %s", campaign_id)
            return

        # Kick the dialer — best-effort, SEPARATE from ingestion so a dial hiccup
        # never flips a successfully-ingested campaign to ``ingest_failed``.
        if inserted <= 0:
            return
        try:
            async with AsyncSessionLocal() as db:
                campaign = (
                    await db.execute(select(OutboundCampaign).where(OutboundCampaign.id == campaign_id))
                ).scalars().first()
                tenant_res = (
                    await db.execute(select(TenantResources).where(TenantResources.tenant_id == tenant_id))
                ).scalars().first()
                # Only kick a campaign that's still running — cancelled mid-ingest
                # must stay silent (this path bypasses dial_next_pending's check).
                if (
                    campaign is not None
                    and tenant_res is not None
                    and campaign.status == CampaignStatus.running
                ):
                    await OutboundCampaignService._dial_pending(
                        campaign, db, tenant_res=tenant_res, base=base, prefix=prefix
                    )
        except Exception:
            logger.exception("NOKVO-INGEST-V2: initial dial kick failed for %s (ingest OK)", campaign_id)

    @staticmethod
    async def add_contacts_to_campaign(
        campaign: OutboundCampaign,
        db: AsyncSession,
        *,
        raw: bytes,
        filename: str | None,
        tenant_res: TenantResources,
        public_base_url: str,
        path_prefix: str = "/api/nokvo-one/agents",
    ) -> None:
        """Add a new CSV/XLSX of contacts to an EXISTING bulk campaign and resume
        dialing. New numbers only — ``unique(campaign_id, phone)`` + ON CONFLICT
        dedupe means re-uploading numbers already on the list is a harmless no-op.
        V2 only (the per-row store); returns immediately, ingest runs in the
        background."""
        if campaign.status == CampaignStatus.cancelled:
            raise ValueError("This campaign was cancelled — create a new one instead.")
        if not settings.CAMPAIGN_CONTACTS_V2:
            raise ValueError(
                "Adding contacts to this campaign isn't supported. Create a new bulk "
                "campaign with the combined list."
            )
        if campaign.contacts is not None:
            # Legacy/empty blob → ONE-WAY upgrade into the per-row store so the
            # exhausted campaign can accept new contacts and resume dialing
            # (rather than telling the user to rebuild the whole campaign).
            from app.services import campaign_contacts_v2 as v2

            await v2.migrate_blob_campaign(db, campaign)
        # Appending resumes THIS campaign to running, so it too must respect the
        # tenant's single active slot (another campaign mid-flight → 409).
        await OutboundCampaignService._assert_no_other_active_campaign(
            db, tenant_res.tenant_id, exclude_id=campaign.id
        )
        task = asyncio.create_task(
            OutboundCampaignService._ingest_and_dial_v2(
                campaign.id, raw, filename, tenant_res.tenant_id,
                public_base_url.rstrip("/"), path_prefix.rstrip("/"), append=True,
            )
        )
        _ingest_tasks.add(task)
        task.add_done_callback(_ingest_tasks.discard)

    # ---- throttled dialer (place up to the cap, refill as calls end) ----------
    @staticmethod
    def _is_bulk(campaign: OutboundCampaign) -> bool:
        """Bulk CSV campaigns dial from dedicated, operator-provisioned telephony
        (a separate Plivo account), not the tenant's inbound DID."""
        return bool((campaign.agent_config or {}).get("bulk_csv"))

    @staticmethod
    def _dial_params(
        campaign: OutboundCampaign, tenant_res: TenantResources
    ) -> tuple[str | None, tuple[str, str] | None]:
        """Resolve (caller_id, auth_override) for placing this campaign's calls.
        Bulk campaigns use the dedicated bulk number + auth; everything else uses
        the tenant's allotted DID on its own subaccount (auth_override=None)."""
        if OutboundCampaignService._is_bulk(campaign):
            return (
                PlivoService.bulk_calling_caller_id(tenant_res),
                PlivoService.bulk_calling_auth(tenant_res),
            )
        return (campaign.from_number or PlivoService.outbound_caller_id(tenant_res), None)

    @staticmethod
    async def _resolve_caller_pool(
        campaign: OutboundCampaign,
        tenant_res: TenantResources,
        *,
        auth_override: tuple[str, str] | None,
        fallback: str | None,
    ) -> list[str]:
        """Caller-ID pool to spread this campaign's calls across.

        Bulk campaigns rotate over EVERY DID rented on the dedicated sub-account
        (listed live from Plivo, cached ~60s by auth_id) so 5 concurrent calls go
        out on 5 different numbers instead of one. Non-bulk campaigns keep their
        single ``fallback`` number — exactly today's behaviour. Always degrades to
        ``[fallback]`` when the pool can't be listed or comes back empty, so we
        never regress to "no caller ID"."""
        fallback_pool = [fallback] if fallback else []
        if not OutboundCampaignService._is_bulk(campaign):
            return fallback_pool
        if not (auth_override and auth_override[0] and auth_override[1]):
            return fallback_pool
        auth_id = auth_override[0]
        now = time.monotonic()
        cached = _CALLER_POOL_CACHE.get(auth_id)
        if cached and (now - cached[0]) < _CALLER_POOL_TTL_S:
            pool = cached[1]
        else:
            pool = await PlivoService.list_account_numbers(auth_override)
            _CALLER_POOL_CACHE[auth_id] = (now, pool)
        if not pool:
            return fallback_pool
        # The granted caller ID is owned + validated; keep it dialable even if a
        # partial listing missed it.
        if fallback and fallback not in pool:
            pool = [*pool, fallback]
        return pool

    @staticmethod
    def _pick_caller(pool: list[str], in_flight: set[str]) -> str:
        """Choose the caller ID for the next call: random among numbers NOT already
        on a live line (so concurrent calls fan out over different DIDs); when every
        number is busy — or the pool holds one — fall back to a plain random pick."""
        free = [n for n in pool if n not in in_flight]
        return random.choice(free) if free else random.choice(pool)

    @staticmethod
    async def _place_call(
        contact: dict,
        *,
        tenant_res: TenantResources,
        caller_id: str,
        base: str,
        prefix: str,
        auth_override: tuple[str, str] | None = None,
    ) -> None:
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
                from_number=caller_id,
                auth_override=auth_override,
            )
            call = result.get("call") if isinstance(result.get("call"), dict) else result
            # Plivo's /Call returns {"message","request_uuid","api_id"} (no sid/id);
            # fall back to request_uuid so a successful placement still records an id.
            contact["call_id"] = (
                call.get("sid")
                or call.get("id")
                or call.get("request_uuid")
                or result.get("request_uuid")
            )
            contact["status"] = "calling"
            contact.pop("error", None)  # clear any prior failure on re-dial
            logger.info(
                "NOKVO-DIAL: placed phone=%s from=%s call_id=%s",
                contact.get("phone"), caller_id, contact.get("call_id"),
            )
        except Exception as exc:
            contact["status"] = "failed"
            contact["ended"] = True  # frees the slot + counts toward batch completion
            contact["error"] = str(exc)[:200]
            # Surface WHY a number didn't dial (Plivo trial/unverified-number/
            # concurrency-cap rejections were previously swallowed silently).
            logger.warning(
                "NOKVO-DIAL: place FAILED phone=%s err=%s",
                contact.get("phone"), str(exc)[:300],
            )

    @staticmethod
    def _inflight_count(contacts: list[dict]) -> int:
        # A contact occupies a line from placement until a terminal webhook
        # (``ended``). A placement ``failed`` frees the slot immediately.
        return sum(
            1
            for c in contacts
            if c.get("status") not in ("pending", "failed") and not c.get("ended")
        )

    @staticmethod
    async def _lock_campaign(
        db: AsyncSession, campaign_id: uuid.UUID
    ) -> OutboundCampaign | None:
        """Row-lock the campaign and refresh its in-memory state to the latest
        committed snapshot.

        EVERY read-modify-write of ``campaign.contacts`` (the inline JSON batch
        state) must go through this. ``SELECT … FOR UPDATE`` serializes concurrent
        Plivo status webhooks and the launch dialer against each other; the lock is
        released by the caller's next commit. ``populate_existing`` overwrites any
        stale attributes the caller still holds — the session is
        ``expire_on_commit=False``, so a campaign loaded before another handler
        committed would otherwise carry a stale ``contacts`` snapshot. Writing that
        stale snapshot back is exactly what reverted just-placed contacts to
        ``pending`` and made the dialer re-place them in an endless loop (the
        "it keeps calling me" report)."""
        res = await db.execute(
            select(OutboundCampaign)
            .where(OutboundCampaign.id == campaign_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return res.scalar_one_or_none()

    @staticmethod
    async def _dial_pending(
        campaign: OutboundCampaign,
        db: AsyncSession,
        *,
        tenant_res: TenantResources,
        base: str,
        prefix: str,
    ) -> None:
        """Place pending contacts until the live-call count reaches the cap.
        Idempotent + safe to call repeatedly (at launch and after each call ends).
        Placed sequentially so the in-flight count stays accurate between calls.

        Runs under a per-campaign row lock (acquired here, held across the
        placement loop, released by the commit below) so a status webhook can't
        read a stale snapshot mid-placement and revert just-placed contacts to
        ``pending`` — which previously made the dialer re-place them forever."""
        _cfg = campaign.agent_config or {}
        _is_deterministic = bool(_cfg.get("deterministic") or _cfg.get("questionnaire"))
        # Resolve the org product tier once — it drives both the Nokvo One outbound
        # kill switch and the prepaid-balance gate below.
        _org_tier: str | None = None
        _tier_resolved = False
        if tenant_res is not None:
            try:
                _org_tier = (
                    await db.execute(
                        select(Organization.product_tier).where(
                            Organization.id == tenant_res.organization_id
                        )
                    )
                ).scalar_one_or_none()
                _tier_resolved = True
            except Exception:
                logger.exception("NOKVO-CAMPAIGN: product-tier lookup failed for %s", campaign.id)

        # Nokvo One outbound kill switch — checked FIRST. Outbound has moved to the
        # dedicated NOKVO APEX product, so a non-APEX org never places a call while
        # NOKVO_ONE_OUTBOUND_ENABLED is off. This stops already-running campaigns and
        # webhook-driven refills, not just new launches (those are blocked at the
        # API). APEX is the SOLE exempt tier and is always tagged
        # product_tier="nokvo_apex"; we fail OPEN when the tier can't be read, so a
        # transient DB error can never silence a live APEX campaign.
        if (
            not settings.NOKVO_ONE_OUTBOUND_ENABLED
            and _tier_resolved
            and (_org_tier or "nokvo_one") != "nokvo_apex"
        ):
            logger.info("NOKVO-CAMPAIGN: Nokvo One outbound disabled — not dialing %s", campaign.id)
            return

        # Scheduled-dialer window — NOKVO APEX only (the scheduled-outbound product).
        # An APEX campaign with a call_window dials only inside 09:00–19:00 IST on its
        # allowed weekdays; outside → pause (return) and the wake-tick poller resumes
        # it when the window reopens. (The per-day cap is enforced under the row lock
        # below.) Nokvo One campaigns — even deterministic ones that carry a
        # call_window for the capacity readout — are NOT schedule-gated.
        # Checked BEFORE the balance gate: this is pure computation, so a paused
        # campaign's every-10-min tick (all night, all weekend) costs nothing —
        # the balance gate's ledger reads only run when we might actually dial.
        _apex_scheduled = _org_tier == "nokvo_apex" and bool(_cfg.get("call_window"))
        if _apex_scheduled:
            _allowed, _reason = call_window_allows(_cfg.get("call_window"), _ist_now())
            if not _allowed:
                logger.info("NOKVO-CAMPAIGN: %s paused (%s) — outside call window", campaign.id, _reason)
                return

        # Prepaid-balance gate (product-aware). Stop placing NEW calls once the
        # org's balance is empty (mid-campaign exhaustion); in-flight calls finish.
        #   • NOKVO APEX (product_tier=nokvo_apex): deterministic IS the product —
        #     gate on the MINUTES balance (apex_has_minutes).
        #   • Nokvo One: only NON-deterministic campaigns deplete the rupee balance;
        #     deterministic stays exempt (billed separately).
        if tenant_res is not None and _tier_resolved:
            try:
                from app.services.minute_balance_service import has_balance

                # APEX: deterministic IS the product and depletes the Call Credits
                # wallet, so gate EVERY APEX campaign on the wallet — and WITHOUT the
                # grandfather (APEX is prepaid-only with no legacy orgs, so a zero
                # balance must block, never fail open to free dialing). Nokvo One:
                # only NON-deterministic campaigns deplete the rupee balance
                # (deterministic stays exempt, billed separately) and keeps the
                # legacy grandfather.
                _blocked = False
                if _org_tier == "nokvo_apex":
                    _blocked = not await has_balance(
                        db, tenant_res.organization_id, grandfather=False
                    )
                elif not _is_deterministic:
                    _blocked = not await has_balance(db, tenant_res.organization_id)
                if _blocked:
                    logger.info("NOKVO-CAMPAIGN: empty prepaid balance — not dialing %s", campaign.id)
                    return
            except Exception:
                logger.exception("NOKVO-CAMPAIGN: prepaid-balance gate check failed")

        # ── V2 scale path: advisory-locked indexed claim (no blob, no whole-row
        # lock, network I/O OUTSIDE the lock). All the gates above (kill switch,
        # balance, window) are shared and already ran. ──
        if settings.CAMPAIGN_CONTACTS_V2 and OutboundCampaignService._is_bulk(campaign):
            await OutboundCampaignService._dial_pending_v2(
                campaign, db,
                tenant_res=tenant_res, base=base, prefix=prefix,
                call_window=(_cfg.get("call_window") if _apex_scheduled else None),
            )
            return

        # Lock + refresh to the latest committed batch state before deciding what
        # to dial. Without this a launch that placed calls and a webhook that ended
        # one could each write a stale ``contacts`` snapshot over the other.
        campaign = await OutboundCampaignService._lock_campaign(db, campaign.id)
        if campaign is None:
            return
        # Per-day cap (calls_per_day) — APEX scheduled campaigns only. The runtime
        # counter lives on the LOCKED campaign's call_window and resets on the IST
        # date rollover. Stop placing once today's placements reach the cap; the
        # wake-tick poller resumes it the next allowed day. 0/absent → no cap.
        _lock_cw = (campaign.agent_config or {}).get("call_window") or {}
        try:
            _calls_per_day = int(_lock_cw.get("calls_per_day") or 0) if _apex_scheduled else 0
        except (TypeError, ValueError):
            _calls_per_day = 0
        _today_str = _ist_now().strftime("%Y-%m-%d") if _calls_per_day > 0 else ""
        _today_count = _dialed_today_count(_lock_cw, _today_str) if _calls_per_day > 0 else 0
        if _calls_per_day > 0 and _today_count >= _calls_per_day:
            logger.info("NOKVO-CAMPAIGN: %s hit daily cap %d — not dialing", campaign.id, _calls_per_day)
            await db.commit()  # release the FOR UPDATE lock
            return
        # Bulk CSV campaigns always fan out up to BULK_DIAL_CONCURRENCY (5) lines
        # at once, independent of the global per-tenant dial concurrency.
        if OutboundCampaignService._is_bulk(campaign):
            cap = OutboundCampaignService.BULK_DIAL_CONCURRENCY
        else:
            cap = max(1, int(settings.OUTBOUND_DIAL_CONCURRENCY or 5))
        caller_id, auth_override = OutboundCampaignService._dial_params(campaign, tenant_res)
        contacts = list(campaign.contacts or [])
        # Spread calls across every DID the (bulk) sub-account owns — pick one per
        # call that isn't already on a live line, so concurrent calls fan out over
        # different numbers instead of hammering one caller ID (which reads as spam).
        # Non-bulk campaigns get a one-number pool, i.e. exactly today's behaviour.
        pool = await OutboundCampaignService._resolve_caller_pool(
            campaign, tenant_res, auth_override=auth_override, fallback=caller_id
        )
        in_flight_numbers = {
            c.get("from_number")
            for c in contacts
            if c.get("from_number")
            and c.get("status") not in ("pending", "failed")
            and not c.get("ended")
        }
        placed_any = False
        placed_count = 0
        if pool:
            for contact in contacts:
                if OutboundCampaignService._inflight_count(contacts) >= cap:
                    break
                # Stop at the per-day cap (today's prior placements + this tick's).
                if _calls_per_day > 0 and (_today_count + placed_count) >= _calls_per_day:
                    break
                # Only place a genuinely-undialed contact: pending, with no call
                # already placed and not already ended. The call_id / ended checks
                # are belt-and-suspenders — even if a stale "pending" ever slips in,
                # we never re-dial a line that already has a placement.
                if (
                    contact.get("status") == "pending"
                    and not contact.get("call_id")
                    and not contact.get("ended")
                ):
                    chosen = OutboundCampaignService._pick_caller(pool, in_flight_numbers)
                    contact["from_number"] = chosen
                    await OutboundCampaignService._place_call(
                        contact,
                        tenant_res=tenant_res,
                        caller_id=chosen,
                        base=base,
                        prefix=prefix,
                        auth_override=auth_override,
                    )
                    in_flight_numbers.add(chosen)
                    placed_any = True
                    placed_count += 1
        if placed_any:
            campaign.contacts = contacts
            # MUST flag — plain JSONB + in-place dict mutation means SQLAlchemy's
            # old==new check skips the UPDATE; without this the placement (call_id,
            # status=calling) never persists and the dialer re-dials forever.
            flag_modified(campaign, "contacts")
            # Advance the per-day counter so the cap holds across ticks + webhooks.
            if _calls_per_day > 0 and _today_str:
                _ac = campaign.agent_config or {}
                _cw = dict(_ac.get("call_window") or {})
                _cw["dialed_today"] = {"date": _today_str, "count": _today_count + placed_count}
                _ac["call_window"] = _cw
                campaign.agent_config = _ac
                flag_modified(campaign, "agent_config")
            db.add(campaign)
            await db.commit()
            await db.refresh(campaign)
        else:
            # Nothing placed (cap full, no pending, or no caller ID): still commit
            # to release the FOR UPDATE lock we took above.
            await db.commit()

    @staticmethod
    async def _dial_pending_v2(
        campaign: OutboundCampaign,
        db: AsyncSession,
        *,
        tenant_res: TenantResources,
        base: str,
        prefix: str,
        call_window: dict | None = None,
    ) -> None:
        """V2 dialer: two-phase claim. Phase 1 = advisory-locked indexed claim
        (``campaign_contacts_v2.claim_pending``, holds no network I/O). Phase 2 =
        dial-time DND scrub + Plivo placement, with NO lock held. Concurrency cap +
        daily cap are enforced in the claim; per-row status is updated O(1)."""
        from app.services import campaign_contacts_v2 as v2

        cap = OutboundCampaignService.BULK_DIAL_CONCURRENCY
        caller_id, auth_override = OutboundCampaignService._dial_params(campaign, tenant_res)
        pool = await OutboundCampaignService._resolve_caller_pool(
            campaign, tenant_res, auth_override=auth_override, fallback=caller_id
        )
        if not pool:
            return

        # Daily cap (APEX scheduled) — bound the claim by today's remaining budget.
        max_rows: int | None = None
        today_str = ""
        today_count = 0
        calls_per_day = 0
        if call_window:
            try:
                calls_per_day = int(call_window.get("calls_per_day") or 0)
            except (TypeError, ValueError):
                calls_per_day = 0
            if calls_per_day > 0:
                today_str = _ist_now().strftime("%Y-%m-%d")
                today_count = _dialed_today_count(call_window, today_str)
                remaining = calls_per_day - today_count
                if remaining <= 0:
                    return
                max_rows = remaining

        claimed = await v2.claim_pending(campaign.id, cap, max_rows=max_rows)
        if not claimed:
            # Nothing left to claim and nothing live → the list is drained; flip
            # to completed so the tenant's single campaign slot frees up.
            await v2.maybe_complete(db, campaign.id)
            return

        # Phase 2 — dial-time DND scrub (only the claimed batch), then place. NO lock.
        result = None
        try:
            from app.services.dnd_scrub_service import scrub_numbers

            result = await scrub_numbers([r["phone"] for r in claimed])
        except Exception:
            logger.exception("NOKVO-DND-V2: dial-time scrub failed — dialing un-scrubbed")

        placed = 0
        for i, row in enumerate(claimed):
            if result is not None and result.is_blocked(row["phone"]):
                await v2.mark_contact(row["id"], "dnd_dropped")
                continue
            chosen = pool[i % len(pool)]  # round-robin fan-out across the DID pool
            contact = {"call_link_id": row["call_link_id"], "phone": row["phone"]}
            await OutboundCampaignService._place_call(
                contact, tenant_res=tenant_res, caller_id=chosen,
                base=base, prefix=prefix, auth_override=auth_override,
            )
            if contact.get("call_id"):
                await v2.mark_contact(row["id"], "ringing", call_id=contact["call_id"])
                placed += 1
            else:
                await v2.mark_contact(row["id"], "failed")

        # Advance counters (best-effort): total dialed + the per-day cap counter.
        if placed:
            try:
                await db.execute(
                    _sql_text("UPDATE outbound_campaigns SET contacts_dialed = contacts_dialed + :n WHERE id = :c"),
                    {"n": placed, "c": str(campaign.id)},
                )
                if calls_per_day > 0 and today_str:
                    _ac = dict(campaign.agent_config or {})
                    _cw = dict(_ac.get("call_window") or {})
                    _cw["dialed_today"] = {"date": today_str, "count": today_count + placed}
                    _ac["call_window"] = _cw
                    await db.execute(
                        _sql_text("UPDATE outbound_campaigns SET agent_config = CAST(:cfg AS jsonb) WHERE id = :c"),
                        {"cfg": json.dumps(_ac), "c": str(campaign.id)},
                    )
                await db.commit()
            except Exception:
                logger.exception("NOKVO-DIAL-V2: counter update failed for %s", campaign.id)
        else:
            # Every claimed row was DND-dropped or failed at placement — those are
            # terminal with no webhook coming, so run the drain check here.
            await v2.maybe_complete(db, campaign.id)

    @staticmethod
    async def _handle_call_status_v2(
        campaign: OutboundCampaign,
        call_link_id: str,
        event_type: str,
        payload: dict,
        db: AsyncSession,
    ) -> None:
        """V2 webhook: O(1) single-row status update keyed on ``call_link_id`` +
        atomic campaign counters. Terminal status derived from the row's current
        state (answered → completed; else no_answer/failed by hangup cause).
        Idempotent. The refill is triggered by the endpoint's dial_next_pending."""
        from app.services import campaign_contacts_v2 as v2

        if event_type == "call.answered":
            if await v2.update_status_by_link(
                db, call_link_id, "answered", answered_at=datetime.now(timezone.utc)
            ):
                await v2.bump_campaign_counter(db, campaign.id, "answered_count")
            await db.commit()
            return

        if event_type in ("call.hangup", "call.failed", "call.machine.detection.ended"):
            cur = (await db.execute(
                _sql_text("SELECT status FROM outbound_campaign_contacts WHERE call_link_id = :c"),
                {"c": call_link_id},
            )).scalar_one_or_none()
            if cur in ("completed", "no_answer", "failed", None):
                return  # already terminal / unknown → idempotent no-op
            if cur == "answered":
                final = "completed"  # post-call scoring sets qualified/lead_score
            else:
                cause = str(payload.get("hangup_cause") or payload.get("HangupCause") or "").upper()
                final = "failed" if any(x in cause for x in ("BUSY", "REJECT", "CONGESTION")) else "no_answer"
            fields: dict[str, Any] = {}
            dur = payload.get("duration") or payload.get("Duration")
            if dur:
                try:
                    fields["duration_s"] = float(dur)
                except (TypeError, ValueError):
                    pass
            await v2.update_status_by_link(db, call_link_id, final, **fields)
            if final in ("failed", "no_answer"):
                await v2.bump_campaign_counter(db, campaign.id, "failed_count")
            await db.commit()
            # Last call in the list just ended → completed (frees the tenant's
            # single campaign slot). No-op while anything is pending or live.
            await v2.maybe_complete(db, campaign.id)

    @staticmethod
    async def dial_next_pending(
        campaign: OutboundCampaign,
        db: AsyncSession,
        *,
        public_base_url: str,
        path_prefix: str = "/api/nokvo-one/agents",
        tenant_res: TenantResources | None = None,
    ) -> None:
        """Webhook-driven refill: top the live batch back up to the cap after a
        call ends. Best-effort — never raise into the status webhook."""
        if campaign.status != CampaignStatus.running:
            return
        try:
            if tenant_res is None:
                res = await db.execute(
                    select(TenantResources).where(TenantResources.tenant_id == campaign.tenant_id)
                )
                tenant_res = res.scalars().first()
                if tenant_res is None:
                    return
            await OutboundCampaignService._dial_pending(
                campaign,
                db,
                tenant_res=tenant_res,
                base=public_base_url.rstrip("/"),
                prefix=path_prefix.rstrip("/"),
            )
        except Exception:
            logger.exception("NOKVO-CAMPAIGN: dial_next_pending failed")

    # ------------------------------------------------------------------
    # Call-level status updates (called from status webhook)
    # ------------------------------------------------------------------

    @staticmethod
    async def mark_answered(campaign_id: uuid.UUID, call_link_id: str) -> None:
        """Stamp a campaign contact ANSWERED the instant its outbound media WS
        connects — the reliable "callee picked up, audio is bridging" signal.

        Plivo's outbound status webhook is wired ONLY as the ``hangup_url`` (see
        ``initiate_outbound_call``), so a ``call.answered`` event NEVER reaches
        ``handle_call_status``. Without this, every connected outbound call hangs
        up with ``status="failed"`` (answered was never set) — which mislabels
        real conversations (incl. qualified leads) and inflates ``failed_count``.
        Marking answered here fixes the status at its source; the later hangup
        webhook then keeps ``status="answered"`` (its guard skips the
        already-answered branch) and just sets ``ended=True``.

        Idempotent (a WS reconnect won't double-count) and runs under the
        ``_lock_campaign`` FOR UPDATE lock in its own short-lived session, so it
        serializes safely with the hangup webhook + the dialer. Best-effort:
        never raises into the call-setup path."""
        from app.db.session import AsyncSessionLocal

        # V2: O(1) single-row answered stamp (idempotent — the SET only counts once
        # because the terminal webhook won't re-answer a completed row).
        if settings.CAMPAIGN_CONTACTS_V2:
            from app.services import campaign_contacts_v2 as v2

            try:
                async with AsyncSessionLocal() as db:
                    cur = (await db.execute(
                        _sql_text("SELECT status, campaign_id FROM outbound_campaign_contacts WHERE call_link_id = :c"),
                        {"c": call_link_id},
                    )).first()
                    if cur is None or cur[0] in ("answered", "completed"):
                        return  # unknown or already answered → idempotent
                    await v2.update_status_by_link(db, call_link_id, "answered",
                                                   answered_at=datetime.now(timezone.utc))
                    await v2.bump_campaign_counter(db, cur[1], "answered_count")
                    await db.commit()
            except Exception:
                logger.exception("NOKVO-DIAL-V2: mark_answered failed for %s", call_link_id)
            return

        try:
            async with AsyncSessionLocal() as db:
                campaign = await OutboundCampaignService._lock_campaign(db, campaign_id)
                if campaign is None:
                    return
                contacts = list(campaign.contacts or [])
                target = next(
                    (c for c in contacts if c.get("call_link_id") == call_link_id), None
                )
                # Unknown contact, or already marked → nothing to do (idempotent).
                if target is None or target.get("status") == "answered" or target.get("answered_at"):
                    return
                target["status"] = "answered"
                target["answered_at"] = datetime.now(timezone.utc).isoformat()
                campaign.answered_count = (campaign.answered_count or 0) + 1
                campaign.contacts = contacts
                flag_modified(campaign, "contacts")
                # Mirror handle_call_status: keep the join row + lead in sync.
                cc_res = await db.execute(
                    select(OutboundCampaignContact).where(
                        OutboundCampaignContact.campaign_id == campaign.id,
                        OutboundCampaignContact.call_link_id == call_link_id,
                    )
                )
                cc = cc_res.scalars().first()
                if cc is not None:
                    cc.status = "answered"
                    db.add(cc)
                if target.get("lead_id"):
                    try:
                        lead = await db.get(OutgoingLead, uuid.UUID(str(target["lead_id"])))
                    except (TypeError, ValueError):
                        lead = None
                    if lead is not None:
                        lead.call_status = LeadCallStatus.called
                        db.add(lead)
                db.add(campaign)
                await db.commit()
                logger.info(
                    "NOKVO-CAMPAIGN: contact answered (media connected) call_link_id=%s",
                    call_link_id,
                )
        except Exception:
            logger.exception(
                "NOKVO-CAMPAIGN: mark_answered failed for call_link_id=%s", call_link_id
            )

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
        # ── V2 scale path: O(1) single-row status update (no blob, no whole-row
        # lock). The endpoint calls dial_next_pending after us for the refill. ──
        if (
            settings.CAMPAIGN_CONTACTS_V2
            and campaign is not None
            and not is_followup_synthetic
            and OutboundCampaignService._is_bulk(campaign)
        ):
            await OutboundCampaignService._handle_call_status_v2(
                campaign, call_link_id, event_type, payload, db
            )
            return
        if is_followup_synthetic:
            contacts: list[dict] = [followup_contact]
            target = followup_contact
        else:
            if campaign is None:
                return
            # Row-lock + refresh so the read-modify-write of ``contacts`` below
            # starts from the freshest committed state and serializes against
            # concurrent webhooks / the launch dialer — the fix for the re-dial
            # loop where a stale snapshot reverted placed calls to "pending".
            campaign = await OutboundCampaignService._lock_campaign(db, campaign.id)
            if campaign is None:
                return
            contacts = list(campaign.contacts or [])
            target = next((c for c in contacts if c.get("call_link_id") == call_link_id), None)
            if not target:
                return

        is_terminal = event_type in (
            "call.hangup", "call.failed", "call.machine.detection.ended"
        )
        hangup_cause = ""

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

        elif is_terminal:
            hangup_cause = payload.get("hangup_cause", "")
            # Terminal: the call no longer occupies a line. ``ended`` is what the
            # throttled dialer counts (an answered-then-hung-up call keeps
            # status="answered", so status alone can't signal "slot free").
            target["ended"] = True
            if target["status"] not in ("answered",):
                target["status"] = "no_answer" if "no_answer" in hangup_cause.lower() else "failed"
                if campaign is not None:
                    campaign.failed_count = (campaign.failed_count or 0) + 1
            duration = payload.get("duration_seconds") or 0
            target["duration_s"] = int(duration)

        # ── Persist the campaign-contact mutation ATOMICALLY, under the lock ──
        # For a regular campaign this is the ONLY write of ``campaign.contacts``;
        # committing here — before the best-effort side effects below, which open
        # their own transactions and would otherwise release the lock early — is
        # what guarantees a concurrent webhook can never overwrite it with a stale
        # snapshot (the lost-update that caused the re-dial loop).
        just_completed = False
        if not is_followup_synthetic:
            campaign.contacts = contacts
            # MUST flag — plain JSONB + in-place dict mutation means SQLAlchemy's
            # old==new check skips the UPDATE; without this the terminal write
            # (ended=True) never persists and the dialer re-dials the contact.
            flag_modified(campaign, "contacts")
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

            # Campaign is done only when every contact has ENDED (or failed at
            # placement). Using ``ended`` — not the status set — avoids a still-live
            # "answered" call tripping premature completion while pending leads
            # remain undialed in the throttled batch.
            if contacts and all(c.get("ended") or c.get("status") == "failed" for c in contacts):
                if campaign.status != CampaignStatus.completed:
                    just_completed = True
                campaign.status = CampaignStatus.completed
                campaign.completed_at = datetime.now(timezone.utc)

            db.add(campaign)
            await db.commit()  # persists contacts + releases the FOR UPDATE lock

        # ── Best-effort post-call side effects (terminal events only) ────────
        # These run AFTER the atomic contacts commit and open their own
        # transactions; they never touch ``campaign.contacts`` so they cannot
        # reintroduce the lost-update race. Each is internally try/excepted.
        if is_terminal:
            converted_outcome = await OutboundCampaignService._close_call_outcomes(
                campaign, target, hangup_cause, db
            )
            await OutboundCampaignService._enqueue_post_call_followup(
                campaign, target, call_link_id, converted_outcome, db
            )

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
    async def _close_call_outcomes(
        campaign: OutboundCampaign | None,
        target: dict,
        hangup_cause: str,
        db: AsyncSession,
    ) -> bool:
        """Derive each agent-created record's outcome from the call disposition.

        Returns True when any created record reached the 'completed' outcome (a
        successful booking/lead), which the follow-up enqueue treats as
        'converted' — cancel pending follow-ups instead of scheduling more.
        Best-effort and self-committing; never raises into the webhook. Split out
        of ``handle_call_status`` so it runs AFTER the atomic ``contacts`` commit
        (it opens its own transaction)."""
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
                    # Note: we no longer call auto_followup_if_needed here — the
                    # follow-up agent owns the scheduling decision (promise > rule
                    # > clamp > caps), and double-firing would create two pending
                    # rows for the same lead.

            # Conversion kill switch: if any created record reached the 'completed'
            # outcome (i.e. a successful booking/lead row), we treat the call as
            # converted and follow-up enqueue will cancel pending follow-ups
            # instead of scheduling more.
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
        return converted_outcome

    @staticmethod
    async def _enqueue_post_call_followup(
        campaign: OutboundCampaign | None,
        target: dict,
        call_link_id: str,
        converted_outcome: bool,
        db: AsyncSession,
    ) -> None:
        """Read the prior call's session memory to surface a callback promise or
        opt-out cue, then enqueue (or suppress) a follow-up. If neither cue is
        present the follow-up service falls back to the campaign's admin-set
        disposition rules. Four kill switches inside ``enqueue_after_call`` gate
        the actual insert. Best-effort and self-committing; never raises into the
        webhook. Split out of ``handle_call_status`` so it runs AFTER the atomic
        ``contacts`` commit (it opens its own transaction)."""
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

            # Inspect session memory for opt-out / promise cues. The session may
            # already have been promoted + GC'd by another post-call hook; we
            # gracefully degrade to disposition-only.
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

            # Opt-out is the legal kill switch — flip consent + cancel pending
            # follow-ups, then stop. Don't enqueue anything.
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
                # Clinic tenants are gated inside enqueue_after_call (kill switch
                # #0): clinics never auto-schedule.
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
    async def get_campaign_for_lead(
        tenant_id: str,
        lead_id: Any,
        phone: str | None,
        db: AsyncSession,
    ) -> OutboundCampaign | None:
        """The campaign a lead is attached to — the one whose ``contacts`` JSONB
        carries this ``lead_id`` (fallback: a matching ``phone``).

        Lets the follow-up scheduler run the follow-up as the lead's OWN campaign
        agent (project knowledge + ``site_visit`` objective) instead of a generic
        one. Scans every campaign for the tenant regardless of status (attachment
        is what matters; the campaign is later loaded by id without a status
        filter). Tenants have few campaigns so a Python scan is fine. Returns
        ``None`` when the lead is attached to no campaign (orphan)."""
        lead_str = str(lead_id) if lead_id is not None else None
        phone_norm = (phone or "").strip()
        if not lead_str and not phone_norm:
            return None
        res = await db.execute(
            select(OutboundCampaign)
            .where(OutboundCampaign.tenant_id == tenant_id)
            .order_by(OutboundCampaign.created_at.desc())
        )
        for campaign in res.scalars().all():
            for contact in campaign.contacts or []:
                if lead_str and str(contact.get("lead_id")) == lead_str:
                    return campaign
                if phone_norm and str(contact.get("phone") or "").strip() == phone_norm:
                    return campaign
        return None

    @staticmethod
    async def get_by_call_link_id(
        call_link_id: str, db: AsyncSession
    ) -> tuple[OutboundCampaign | None, dict | None]:
        """Return (campaign, contact) matching the call_link_id.

        Resolution order:
          1. Campaign contact (regular launch). The contact dict lives
             inline in ``campaign.contacts`` JSONB.
          2. V2 per-row contact (``outbound_campaign_contacts``) — EVERY
             APEX/bulk campaign since the scale migration (their blob is
             empty). Without this branch the answer webhook can't resolve
             the Plivo signing token (sig mismatch → <Hangup/> the moment
             the person picks up) and the media WS closes 1008 — the
             "answers then immediately cuts" bug. Returns a synthetic
             contact dict shaped like a blob contact.
          3. Follow-up schedule row (placed_call_id). Returns a synthetic
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

        # V2 per-row contact — one indexed query on the UNIQUE call_link_id.
        # (After the blob scan so legacy campaigns that ALSO carry OCC rows keep
        # returning their live blob dict, which the webhook mutates in place.)
        occ = (
            await db.execute(
                select(OutboundCampaignContact).where(
                    OutboundCampaignContact.call_link_id == call_link_id
                )
            )
        ).scalars().first()
        if occ is not None:
            v2_campaign = (
                await db.execute(
                    select(OutboundCampaign).where(OutboundCampaign.id == occ.campaign_id)
                )
            ).scalars().first()
            if v2_campaign is not None:
                return v2_campaign, {
                    "call_link_id": call_link_id,
                    "phone": occ.phone,
                    "name": occ.name or "",
                    "status": str(occ.status or "calling"),
                    "call_id": occ.call_id,
                    "_v2": True,
                }

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
