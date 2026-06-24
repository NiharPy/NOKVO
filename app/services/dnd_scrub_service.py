"""DND (NCPR) scrubbing for the bulk CSV calling add-on.

Before a bulk dial list goes out, every number is checked against India's
National Customer Preference Register (the "DND registry") so we don't dial
people who opted out of telemarketing. Provider = **nixinfo**
(https://nixinfo.in/dnd-checking-api — free tier; GET with query params, JSON
``{number, status, preference}``) rather than the heavyweight RTM/DLT route.

The endpoint URL and request param NAMES are config-driven
(``DND_SCRUB_API_URL`` / ``DND_SCRUB_*_PARAM``) because nixinfo only hands you
the exact values after registering, and other vendors differ — switching
providers is a config change, not a code change. The response parser is already
shape-tolerant (single record, list, or list wrapped under data/result/…).

Design choices:
  * **Batched** — numbers are chunked (``DND_SCRUB_BATCH_SIZE``) and joined with
    commas into one request per chunk; the whole list costs ~ceil(N/batch)
    round-trips. nixinfo's free lookup is single-number, so the default batch
    size is 1 (one GET per number); raise it for a vendor that accepts a
    comma-separated batch.
  * **Fail-OPEN by default** — if the API errors/times out we DON'T drop the
    campaign; we dial the un-scrubbed list and flag that the scrub didn't run, so
    a flaky vendor never silently kills outreach. (A strict-compliance caller can
    invert this at the call site by checking ``result.ok``.)

This is a convenience filter, NOT a substitute for registering as a telemarketer
(RTM) on a DLT platform. See [[project_bulk_csv_calling]].
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Response tokens (case-insensitive) that mean "on the DND register → don't call".
# The vendor returns e.g. {"number": "...", "status": "Registered",
# "preference": "Fully Blocked"}. A number is treated as blocked only when it's
# registered AND fully blocked; "Not Registered" / partial preferences pass.
_BLOCKED_STATUS = {"registered", "dnd", "yes", "y", "true", "1"}
_BLOCKED_PREFERENCE = {"fully blocked", "full", "all", "fully"}


@dataclass
class ScrubResult:
    """Outcome of scrubbing a list of canonical phone numbers."""

    ok: bool                                  # did the scrub actually run end-to-end?
    blocked: set[str] = field(default_factory=set)   # canonical numbers on DND
    error: str | None = None                  # why it didn't run (when ok is False)

    def is_blocked(self, phone: str) -> bool:
        return phone in self.blocked


def _enabled() -> bool:
    return bool(
        settings.DND_SCRUB_ENABLED
        and settings.DND_SCRUB_API_URL
        and settings.DND_SCRUB_API_KEY
    )


def _to_10_digit(canon: str) -> str:
    """The DND API wants the bare 10-digit mobile; our canonical form is
    ``91XXXXXXXXXX``. Strip the country code for the request."""
    if len(canon) == 12 and canon.startswith("91"):
        return canon[2:]
    return canon


def _normalize_records(payload: object) -> list[dict]:
    """Coax the vendor's varied JSON shapes into a flat list of record dicts.

    Seen in the wild: a top-level list of records, or an object wrapping the list
    under ``data`` / ``result`` / ``records``, or a single record object."""
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "result", "results", "records", "response"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [r for r in inner if isinstance(r, dict)]
        # A single {number, status, ...} record.
        if "number" in payload or "mobile" in payload:
            return [payload]
    return []


def _record_is_blocked(rec: dict) -> bool:
    status = str(rec.get("status") or rec.get("dnd") or "").strip().lower()
    pref = str(rec.get("preference") or rec.get("category") or "").strip().lower()
    if status in _BLOCKED_STATUS:
        # Registered but partially-blocked (specific categories only) → we still
        # treat as blocked unless the preference explicitly names categories. The
        # safe default for outbound promo calls is: registered means don't call.
        return True
    if pref in _BLOCKED_PREFERENCE:
        return True
    return False


def _record_number(rec: dict) -> str:
    """Pull the number from a record and re-canonicalize to ``91XXXXXXXXXX`` so it
    matches our contact keys regardless of how the vendor echoes it back."""
    raw = str(rec.get("number") or rec.get("mobile") or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 10:
        return "91" + digits
    return digits


def _chunks(items: list[str], size: int) -> list[list[str]]:
    size = max(1, size)
    return [items[i : i + size] for i in range(0, len(items), size)]


async def scrub_numbers(canonical_numbers: list[str]) -> ScrubResult:
    """Check ``canonical_numbers`` (``91XXXXXXXXXX`` form) against the DND register.

    Returns a :class:`ScrubResult`. When scrubbing is disabled or unconfigured,
    returns ``ok=True`` with an empty blocked set (nothing filtered) — callers can
    treat "no scrub configured" the same as "nothing on DND". On a transport/parse
    error returns ``ok=False`` (fail-open: blocked set empty), so the caller can
    decide whether to proceed or surface a warning.
    """
    nums = [n for n in dict.fromkeys(canonical_numbers) if n]  # dedupe, keep order
    if not nums:
        return ScrubResult(ok=True)
    if not _enabled():
        return ScrubResult(ok=True)

    blocked: set[str] = set()
    by_request: dict[str, str] = {}  # 10-digit sent → canonical, to map results back
    for n in nums:
        by_request[_to_10_digit(n)] = n

    try:
        async with httpx.AsyncClient(timeout=settings.DND_SCRUB_TIMEOUT_S) as client:
            for batch in _chunks(list(by_request.keys()), settings.DND_SCRUB_BATCH_SIZE):
                params = {
                    settings.DND_SCRUB_KEY_PARAM: settings.DND_SCRUB_API_KEY,
                    settings.DND_SCRUB_MOBILE_PARAM: ",".join(batch),
                }
                # Omit the password param entirely when unset (nixinfo's free tier
                # may not use one) or when no param name is configured for it.
                if settings.DND_SCRUB_PASS_PARAM and settings.DND_SCRUB_API_PASS:
                    params[settings.DND_SCRUB_PASS_PARAM] = settings.DND_SCRUB_API_PASS
                resp = await client.get(settings.DND_SCRUB_API_URL, params=params)
                resp.raise_for_status()
                payload = resp.json()
                # Vendors return HTTP 200 with an error envelope for auth / quota /
                # bad-input failures. Treat that as a hard failure (fail-open at the
                # call site) rather than an empty "nobody on DND" result, so a
                # rejected key never silently passes the whole list through as clean.
                #   * nixinfo: {"error": "..."}
                #   * msgclub: {"response":"Unauthorized","responseCode":"3009"}
                if isinstance(payload, dict):
                    if payload.get("error"):
                        raise RuntimeError(f"vendor error: {payload['error']}")
                    resp_msg = str(payload.get("response") or "")
                    if resp_msg and resp_msg.lower() in {"unauthorized", "invalid", "error", "failed"}:
                        raise RuntimeError(
                            f"vendor error: {resp_msg} (code {payload.get('responseCode')})"
                        )
                # TODO(msgclub): _normalize_records handles per-record {number,status,
                # preference} shapes. msgclub's getNdncNonNdncNumbers likely returns
                # NDNC/non-NDNC *lists* instead — finalize this once a real success
                # response is captured with a valid AUTH_KEY.
                records = _normalize_records(payload)
                for rec in records:
                    if not _record_is_blocked(rec):
                        continue
                    canon = _record_number(rec)
                    if canon in by_request.values():
                        blocked.add(canon)
                    elif _to_10_digit(canon) in by_request:
                        blocked.add(by_request[_to_10_digit(canon)])
    except Exception as exc:  # noqa: BLE001 — fail-open, never block a campaign on a vendor hiccup
        logger.warning("NOKVO-DND: scrub failed (%s) — proceeding without scrub", exc)
        return ScrubResult(ok=False, error=str(exc))

    logger.info("NOKVO-DND: scrubbed %d numbers, %d on DND", len(nums), len(blocked))
    return ScrubResult(ok=True, blocked=blocked)
