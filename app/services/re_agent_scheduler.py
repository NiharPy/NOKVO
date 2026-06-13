"""RE_agent_scheduler — post-call layer that turns a minimal real-estate
site-visit ticket into a structured, agent-assigned one.

The inbound call deliberately leaves the site visit MINIMAL — ANI + name + the
post-call note; the date/time/project the caller mentioned live in the note
prose, not as fields (see ``_create_inbound_site_visit`` in
``nokvo_one_voice_pipeline``). This layer runs AFTER the call, once the condenser
has written the note onto the ticket:

  1. an LLM reads the call note and extracts the visit details,
  2. those fields are filled onto the SAME site-visit ticket, and
  3. the agent whose free slot is nearest the confirmed time is assigned
     (reusing the existing time-first assignment service).

Best-effort + idempotent: it never raises out, never re-schedules a ticket
twice, and a bad/empty LLM response leaves the ticket safely unchanged-but-saved.
Nothing here is on the call's latency path — it runs in the post-call background.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_JSON_OBJ = re.compile(r"\{[\s\S]*\}")

_EXTRACT_SYSTEM = (
    "You read a short real-estate call note and extract the site-visit details as "
    "STRICT JSON with exactly these keys: visit_date (YYYY-MM-DD), visit_time "
    "(24-hour HH:MM), project_name (string), customer_name (string), phone "
    "(string), requirements (one short sentence of what the caller wants / "
    "context). Resolve relative dates ('Saturday', 'tomorrow', 'this weekend') to "
    "a concrete YYYY-MM-DD using the Current date provided. Use null for any field "
    "the note does not state — never invent a date or time. Output ONLY the JSON "
    "object, nothing else."
)


class REAgentScheduler:
    """Enrich + assign a real-estate site-visit ticket from its call note."""

    @staticmethod
    async def schedule_for_site_visit(db, tenant_res, site_visit_record_id) -> dict[str, Any]:
        """Fill the site-visit ticket's fields from its call note and assign the
        nearest-free agent. Best-effort; returns a small status dict."""
        from sqlalchemy.orm.attributes import flag_modified

        from app.models.nokvo_one_tool_record import NokvoOneToolRecord

        try:
            rec_uuid = uuid.UUID(str(site_visit_record_id))
        except (TypeError, ValueError):
            return {"ok": False, "reason": "bad_record_id"}
        try:
            rec = await db.get(NokvoOneToolRecord, rec_uuid)
        except Exception:
            logger.debug("NOKVO-RE-SCHED: record load failed", exc_info=True)
            return {"ok": False, "reason": "load_failed"}
        if rec is None or getattr(rec, "record_type", None) != "ticket":
            return {"ok": False, "reason": "not_a_ticket"}

        data = dict(rec.data or {})
        # Idempotency: never re-schedule a ticket we already processed/assigned.
        if data.get("re_scheduled") or data.get("assigned_member_id"):
            return {"ok": False, "reason": "already_scheduled"}
        if str(data.get("issue_type") or data.get("request_type") or "") != "site_visit":
            return {"ok": False, "reason": "not_site_visit"}
        org_id = getattr(rec, "organization_id", None)
        note = str(data.get("handoff_note") or "").strip()
        if not note or org_id is None:
            return {"ok": False, "reason": "no_note"}

        extracted = await REAgentScheduler._extract(note)

        # ── 1) Fill the Site Visit Fields onto the SAME ticket ──────────────────
        await REAgentScheduler._fill_fields(db, org_id, data, extracted)
        rec.data = data
        _safe_flag_modified(flag_modified, rec)

        # ── 2) Assign the nearest-free agent at the confirmed time ──────────────
        requested_at = REAgentScheduler._parse_visit_datetime(
            extracted.get("visit_date"), extracted.get("visit_time")
        )
        assignment: dict[str, Any] | None = None
        if requested_at is not None:
            try:
                from app.services.predefined_tools_service import _assign_existing_record

                assignment = await _assign_existing_record(
                    db,
                    org_id,
                    rec,
                    request_type="site_visit",
                    requested_time=requested_at,
                    summary=note[:300],
                    metadata={"assignment_source": "re_agent_scheduler"},
                )
            except Exception:
                logger.exception("NOKVO-RE-SCHED: assignment failed")
                assignment = None

        # ── 3) Persist the outcome (re-read rec.data so the assignment service's
        #       own mutations are preserved alongside our extracted fields) ──────
        data = dict(rec.data or {})
        if assignment and assignment.get("selected_member_id"):
            data["assigned_member_id"] = assignment["selected_member_id"]
            data["assigned_member_name"] = assignment.get("selected_member_name")
            data["scheduled_time"] = assignment.get("scheduled_time")
            data["time_adjusted"] = bool(assignment.get("time_adjusted"))
            data["assignment_status"] = assignment.get("assignment_status") or "assigned"
        elif requested_at is None:
            # No firm date/time in the note → leave for a human to schedule.
            data["assignment_status"] = "needs_manual_scheduling"
            data["assignment_reason"] = "no_firm_visit_time_in_note"
        else:
            data["assignment_status"] = (assignment or {}).get("assignment_status") or "no_available_member"
        data["re_scheduled"] = True
        data["re_scheduled_at"] = datetime.now(timezone.utc).isoformat()
        rec.data = data
        _safe_flag_modified(flag_modified, rec)
        db.add(rec)
        try:
            await db.commit()
        except Exception:
            logger.exception("NOKVO-RE-SCHED: commit failed")
            try:
                await db.rollback()
            except Exception:
                pass
            return {"ok": False, "reason": "commit_failed"}

        logger.info(
            "NOKVO-RE-SCHED: ticket %s status=%s member=%s time_adjusted=%s",
            rec.id, data.get("assignment_status"), data.get("assigned_member_name"),
            data.get("time_adjusted"),
        )
        return {
            "ok": True,
            "record_id": str(rec.id),
            "assigned_member_id": data.get("assigned_member_id"),
            "assignment_status": data.get("assignment_status"),
            "time_adjusted": data.get("time_adjusted"),
        }

    # ── helpers ─────────────────────────────────────────────────────────────────

    @staticmethod
    async def _fill_fields(db, org_id, data: dict[str, Any], extracted: dict[str, Any]) -> None:
        """Merge the extracted Site Visit Fields into the ticket data, resolving
        the project to a real record and preserving the ANI/name already there."""
        from app.services.real_estate_project_service import find_project_match, load_active_projects

        try:
            projects = await load_active_projects(db, org_id)
        except Exception:
            projects = []
        spoken_project = extracted.get("project_name")
        matched = find_project_match(projects, project_name=str(spoken_project)) if spoken_project else None
        if matched is None and len(projects) == 1:
            matched = projects[0]
        if matched is not None:
            data["project_name"] = matched.name
            data["project_id"] = str(matched.id)
        elif spoken_project:
            data["project_name"] = str(spoken_project)
        # Name/phone: the call already put the ANI (+ name-if-known) on the ticket;
        # only fill from the note when the ticket doesn't already have them.
        if extracted.get("customer_name") and not data.get("name"):
            data["name"] = str(extracted["customer_name"])[:200]
        if extracted.get("phone") and not data.get("phone"):
            data["phone"] = str(extracted["phone"])[:40]
        if extracted.get("requirements"):
            data["requirements"] = str(extracted["requirements"])[:500]
        if extracted.get("visit_date"):
            data["visit_date"] = str(extracted["visit_date"])
        if extracted.get("visit_time"):
            data["visit_time"] = str(extracted["visit_time"])

    @staticmethod
    async def _extract(note: str) -> dict[str, Any]:
        """LLM-extract the visit details from the call note. Safe-default {}."""
        try:
            from zoneinfo import ZoneInfo

            today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
        except Exception:
            today = datetime.now(timezone.utc).date().isoformat()
        try:
            from app.services.nokvo_one_voice_pipeline import AzureGroundedLLM

            raw = await AzureGroundedLLM.complete_global(
                [
                    {"role": "system", "content": _EXTRACT_SYSTEM},
                    {"role": "user", "content": f"Current date: {today}\n\nCall note:\n{note}"},
                ],
                max_tokens=220,
            )
        except Exception:
            logger.debug("NOKVO-RE-SCHED: extraction LLM call failed", exc_info=True)
            return {}
        match = _JSON_OBJ.search((raw or "").strip())
        if not match:
            return {}
        try:
            obj = json.loads(match.group(0))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _parse_visit_datetime(date_val: Any, time_val: Any) -> datetime | None:
        """Combine the extracted date + time into a concrete UTC datetime, reusing
        the pipeline's lenient parsers. Returns None when either is missing/vague."""
        if not date_val or not time_val:
            return None
        try:
            from app.services.nokvo_one_voice_pipeline import (
                _APPOINTMENT_LOCAL_TZ,
                NokvoOneVoicePipeline as _P,
            )

            d = _P._parse_appointment_date(date_val)
            t = _P._parse_appointment_time(time_val)
            return datetime.combine(d, t, tzinfo=_APPOINTMENT_LOCAL_TZ).astimezone(timezone.utc)
        except Exception:
            return None


def _safe_flag_modified(flag_modified, rec) -> None:
    # Non-ORM stand-ins (tests) have no instrumentation; ignore.
    try:
        flag_modified(rec, "data")
    except Exception:
        pass
