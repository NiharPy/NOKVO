"""Generic outbound-campaign objective → tool-flow gate (P4).

Maps a campaign's selected objectives to the tool-flow keys allowed to START on
an outbound call, for ANY business type. Real-estate keeps its own explicit path
in ``turn_router`` for byte-identical behaviour; this covers clinic / services /
future sectors. The common case today — a non-RE campaign with no structured
objectives — returns ``None`` = all flows allowed (unchanged behaviour).

A booking-type objective maps to the vertical's booking flow; ``clinic_appointment``
is the generic appointment flow used by clinics AND services (see P3).
"""
from __future__ import annotations

from typing import Any

_OBJECTIVE_TO_FLOW: dict[str, str] = {
    "site_visit": "real_estate_site_visit",
    "visit": "real_estate_site_visit",
    "appointment": "clinic_appointment",
    "consultation": "clinic_appointment",
    "booking": "clinic_appointment",
    "book": "clinic_appointment",
    "lead": "leads_create",
    "quote": "leads_create",
    "enquiry": "leads_create",
}


def _normalize(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        items: list[Any] = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        return []
    out: list[str] = []
    for it in items:
        s = str(it).strip().lower().replace("-", "_").replace(" ", "_")
        if s and s not in out:
            out.append(s)
    return out


def allowed_flow_keys_for_objectives(raw_objectives: Any) -> list[str] | None:
    """Flow keys allowed to start given the campaign objectives, or ``None`` (all
    flows) when there are no recognised objectives."""
    keys: list[str] = []
    for obj in _normalize(raw_objectives):
        flow = _OBJECTIVE_TO_FLOW.get(obj)
        if flow and flow not in keys:
            keys.append(flow)
    return keys or None
