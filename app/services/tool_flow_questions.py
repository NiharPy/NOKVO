from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from app.services.nokvo_one_business_templates import (
    apply_schema_overrides,
    business_type_config,
    custom_tabs_from_overrides,
    enabled_tabs_for,
    normalize_business_type,
)


TOOL_FLOW_QUESTIONS_KEY = "tool_flow_questions"
# v2: real-estate site-visit FSM now pulls slots from BOTH lead and tickets
# (site-visit) schemas, adds a Project slot, and is project-aware.
TOOL_FLOW_QUESTIONS_VERSION = "v2"
FLOW_LANGUAGES = ("en", "hi", "te")
_SKIP_FIELD_KEYS = {"id", "status", "created_at", "updated_at"}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _schema_hash(business_type: str | None, schema_overrides: dict[str, Any] | None, custom_tabs: list[dict[str, Any]]) -> str:
    payload = {
        "version": TOOL_FLOW_QUESTIONS_VERSION,
        "business_type": business_type,
        "schema_overrides": schema_overrides or {},
        "custom_tabs": custom_tabs or [],
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:24]


def _writable_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for field in fields or []:
        if not isinstance(field, dict):
            continue
        key = str(field.get("key") or "").strip()
        if not key or key in _SKIP_FIELD_KEYS:
            continue
        out.append(deepcopy(field))
    return out


def _field_label(field: dict[str, Any]) -> str:
    return str(field.get("label") or field.get("key") or "detail").strip()


def _kind_for_field(field: dict[str, Any]) -> str:
    key = str(field.get("key") or "").lower()
    label = _field_label(field).lower()
    ftype = str(field.get("type") or "").lower()
    text = f"{key} {label}"
    if "phone" in text or "mobile" in text or ftype == "phone":
        return "phone"
    if "email" in text or ftype == "email":
        return "email"
    if "name" in text and "project" not in text:
        return "name"
    if "budget" in text or "price" in text or ftype == "currency":
        return "budget"
    if "location" in text or "area" in text:
        return "location"
    if "project" in text:
        return "project"
    if "property" in text and ("type" in text or "looking" in text):
        return "property_type"
    if "service" in text:
        return "service"
    if "possession" in text or "ready to move" in text or "move-in" in text:
        return "possession_timeline"
    if "purpose" in text or "investment" in text or "end use" in text or "end-use" in text:
        return "purpose"
    if "financ" in text or "loan" in text or "mortgage" in text:
        return "financing"
    # datetime FIRST — a combined field (type=datetime, or a label mentioning
    # both "date" and "time", e.g. "Date and Time") must NOT fall through to the
    # date-only branch. If it does, the FSM treats it as date-only and bolts on a
    # phantom canonical visit_time slot — the value lands under a key the admin
    # never configured. (See _real_estate_visit_slots.)
    if ftype == "datetime" or ("date" in text and "time" in text):
        return "datetime"
    if "date" in text and ftype in {"date", "datetime", "text"}:
        return "date"
    if "time" in text or ftype == "datetime":
        return "time"
    if "reason" in text or "need" in text or "concern" in text:
        return "reason"
    return "generic"


def _question_for_kind(kind: str, label: str, language: str) -> str:
    if language == "hi":
        return {
            "name": "कृपया आपका name बताइए.",
            "phone": "हमारी team किस phone number पर contact करे?",
            "email": "आपका email address क्या है?",
            "budget": "आपका approximate budget क्या है?",
            "location": "आप किस location या area में देख रहे हैं?",
            "property_type": "आप किस type की property देख रहे हैं?",
            "project": "आप किस project में interested हैं?",
            "possession_timeline": "Possession कब तक चाहिए — ready-to-move या under-construction?",
            "purpose": "यह investment के लिए है या self-use के लिए?",
            "financing": "क्या आपको home loan की ज़रूरत होगी?",
            "date": "Preferred date क्या है?",
            "time": "Preferred time क्या है?",
            "datetime": "Visit के लिए कौन सी date और time prefer करेंगे?",
            "service": "आपको कौन सी service चाहिए?",
            "reason": f"{label} के बारे में थोड़ा बताइए.",
            "generic": f"कृपया {label} बताइए.",
        }.get(kind, f"कृपया {label} बताइए.")
    if language == "te":
        return {
            "name": "మీ name చెప్పండి.",
            "phone": "మా team contact చేయడానికి phone number చెప్పండి.",
            "email": "మీ email address చెప్పండి.",
            "budget": "Approx budget ఎంత?",
            "location": "ఏ location లేదా area లో చూస్తున్నారు?",
            "property_type": "ఏ type property చూస్తున్నారు?",
            "project": "ఏ project గురించి interested గా ఉన్నారు?",
            "possession_timeline": "Possession ఎప్పటికి కావాలి — ready-to-move లేదా under-construction?",
            "purpose": "ఇది investment కోసమా లేదా self-use కోసమా?",
            "financing": "మీకు home loan అవసరమా?",
            "date": "Preferred date ఏది?",
            "time": "Preferred time ఏది?",
            "datetime": "Visit కోసం ఏ date మరియు time prefer చేస్తారు?",
            "service": "మీకు ఏ service కావాలి?",
            "reason": f"{label} గురించి short గా చెప్పండి.",
            "generic": f"{label} చెప్పండి.",
        }.get(kind, f"{label} చెప్పండి.")
    return {
        "name": "May I have your name?",
        "phone": "What phone number should our team use?",
        "email": "What email address should we use?",
        "budget": "What approximate budget should I note?",
        "location": "Which location or area are you interested in?",
        "property_type": "What type of property are you looking for?",
        "project": "Which project are you interested in?",
        "possession_timeline": "What possession timeline works for you — ready-to-move or under-construction?",
        "purpose": "Is this for investment or your own use?",
        "financing": "Will you need a home loan?",
        "date": "What date would you prefer?",
        "time": "What time would you prefer?",
        "datetime": "What date and time would you prefer for the visit?",
        "service": "Which service would you like to book?",
        "reason": f"Please share {label}.",
        "generic": f"Please share {label}.",
    }.get(kind, f"Please share {label}.")


def _question_entry(field: dict[str, Any]) -> dict[str, Any]:
    label = _field_label(field)
    kind = _kind_for_field(field)
    return {
        "key": str(field.get("key") or "").strip(),
        "label": label,
        "type": str(field.get("type") or "text"),
        "required": bool(field.get("required")),
        "kind": kind,
        "questions": {lang: _question_for_kind(kind, label, lang) for lang in FLOW_LANGUAGES},
    }


def _slot_entry(key: str, label: str, kind: str, *, required: bool = True, source_field: str | None = None) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "type": "text",
        "required": required,
        "kind": kind,
        "source_field": source_field,
        "questions": {lang: _question_for_kind(kind, label, lang) for lang in FLOW_LANGUAGES},
    }


def _lead_schema_fields(config: dict[str, Any] | None) -> list[dict[str, Any]]:
    return _writable_fields(((config or {}).get("schemas") or {}).get("leads") or [])


def _selection_slots(
    fields: list[dict[str, Any]] | None,
    *,
    categorical_keys: set[str],
) -> list[dict[str, Any]]:
    """Strict, selection-driven slots: EXACTLY the admin's configured writable
    fields, in schema order, minus system/admin-only ones. Nothing is force-added
    — the agent asks only what the admin selected. Each slot binds to its
    configured key (``source_field``) and carries a ``kind`` (from
    :func:`_kind_for_field`) that drives question phrasing + answer parsing
    (name/phone/date/time/datetime/budget/…). A combined date+time field stays one
    slot; separate date/time fields stay two — whatever the admin configured.
    """
    slots: list[dict[str, Any]] = []
    for field in _writable_fields(fields or []):
        key = str(field.get("key") or "")
        if not key or key in categorical_keys:
            continue
        slots.append(
            _slot_entry(
                key,
                _field_label(field),
                _kind_for_field(field),
                required=bool(field.get("required")),
                source_field=key,
            )
        )
    return slots


def _real_estate_visit_slots(
    site_visit_fields: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Site-visit slots = EXACTLY the admin's configured **Site Visit Fields**
    (the tickets schema), in their order. Nothing is auto-added: if the admin's
    form doesn't include a project/date/time field, the agent doesn't ask for it.
    Categorical/admin-only fields (status, owner, priority, issue_type) are never
    asked — they get server-side defaults.
    """
    return _selection_slots(
        site_visit_fields,
        categorical_keys={"status", "assigned_to", "owner", "issue_type", "priority"},
    )


def _clinic_appointment_slots(appointment_fields: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Appointment slots = EXACTLY the admin's configured **Appointment Fields**
    (the clinic `appointments` schema), in their order. Nothing is auto-added. The
    doctor is chosen by the assignment engine from the service mapping, so
    doctor/department/status are never asked.
    """
    return _selection_slots(
        appointment_fields,
        categorical_keys={"status", "doctor", "department", "assigned_to", "owner", "priority"},
    )


def build_tool_flow_questions(
    business_type: str | None,
    schema_overrides: dict[str, Any] | None = None,
    custom_tabs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    normalized_business_type = normalize_business_type(business_type)
    config = business_type_config(normalized_business_type)
    resolved = apply_schema_overrides(config, schema_overrides) if config else None
    custom_tabs = list(custom_tabs or [])
    schema_hash = _schema_hash(normalized_business_type, schema_overrides, custom_tabs)
    tabs: dict[str, Any] = {}
    if resolved:
        for tab in enabled_tabs_for(normalized_business_type):
            fields = _writable_fields(((resolved.get("schemas") or {}).get(tab) or []))
            tabs[tab] = {
                "tab": tab,
                "fields": {entry["key"]: entry for entry in (_question_entry(field) for field in fields)},
            }
    for spec in custom_tabs:
        slug = str(spec.get("slug") or "")
        if not slug:
            continue
        fields = _writable_fields(spec.get("fields") or [])
        tabs[slug] = {
            "tab": slug,
            "label": spec.get("label") or slug.replace("_", " ").title(),
            "fields": {entry["key"]: entry for entry in (_question_entry(field) for field in fields)},
        }

    lead_fields = _lead_schema_fields(resolved)
    flows: dict[str, Any] = {}
    if lead_fields:
        # All writable Lead Fields become slots so an enquiry captures per the
        # admin's Lead Fields. The FSM only *asks* the required ones; optional
        # fields (looking-for, budget, area) are filled opportunistically when
        # the caller volunteers them (see _infer_domain_slots) — we never
        # interrogate a caller for optional details.
        flows["leads_create"] = {
            "flow": "leads_create",
            "tool_key": "leads_create",
            "tab": "leads",
            "slots": [_question_entry(field) for field in lead_fields],
        }
    if normalized_business_type == "real_estate":
        site_visit_fields = _writable_fields(
            ((resolved or {}).get("schemas") or {}).get("tickets") or []
        )
        flows["real_estate_site_visit"] = {
            "flow": "real_estate_site_visit",
            "tool_key": "qualify_lead_and_schedule_visit",
            # A booking lands in the Site Visits (tickets) tab, captured per
            # the admin's Site Visit Fields.
            "tab": "tickets",
            "slots": _real_estate_visit_slots(site_visit_fields),
        }
    if normalized_business_type == "clinics":
        appointment_fields = _writable_fields(
            ((resolved or {}).get("schemas") or {}).get("appointments") or []
        )
        flows["clinic_appointment"] = {
            "flow": "clinic_appointment",
            "tool_key": "book_appointment_with_lead_capture",
            # A booking lands in the Appointments tab, captured per the admin's
            # Appointment Fields; the tool routes to a doctor who provides the
            # chosen service via the assignment engine.
            "tab": "appointments",
            "slots": _clinic_appointment_slots(appointment_fields),
        }
    return {
        "version": TOOL_FLOW_QUESTIONS_VERSION,
        "schema_hash": schema_hash,
        "business_type": normalized_business_type,
        "languages": list(FLOW_LANGUAGES),
        "tabs": tabs,
        "flows": flows,
    }


def ensure_tool_flow_questions(
    provider_status: dict[str, Any] | None,
    business_type: str | None,
) -> tuple[dict[str, Any], bool]:
    status = dict(provider_status or {})
    overrides = dict(status.get("business_template_schema_overrides") or {})
    custom_tabs = custom_tabs_from_overrides(status)
    expected = build_tool_flow_questions(business_type, overrides, custom_tabs)
    current = status.get(TOOL_FLOW_QUESTIONS_KEY)
    if not isinstance(current, dict) or current.get("schema_hash") != expected["schema_hash"] or current.get("version") != TOOL_FLOW_QUESTIONS_VERSION:
        status[TOOL_FLOW_QUESTIONS_KEY] = expected
        return status, True
    return status, False


def generated_questions_from_status(provider_status: dict[str, Any] | None) -> dict[str, Any]:
    value = (provider_status or {}).get(TOOL_FLOW_QUESTIONS_KEY) or {}
    return value if isinstance(value, dict) else {}


_RECORD_TAB_LABELS = {
    "leads": "lead",
    "leads_create": "lead",
    "appointments": "appointment",
    "real_estate_site_visit": "site visit",
    "clinic_appointment": "appointment",
    "tickets": "ticket",
    "callbacks": "callback",
    "complaints": "complaint",
}


def format_field_questions_prompt(
    catalog: dict[str, Any] | None,
    *,
    language: str = "en",
    project_names: list[str] | None = None,
) -> str:
    """Format a ``build_tool_flow_questions`` catalog into a prompt block.

    The voice agent paraphrases slot questions on its own when no
    deterministic FSM is driving the turn — fine for free-form chat but
    wrong for record creation, where operators expect the agent to ask
    using the exact field labels they configured (e.g. a clinic that
    renamed ``patient_name`` to ``guest_name`` should hear "guest name",
    not "patient name"). This formatter renders the catalog into a
    "use these exact phrasings" block the LLM is told to honour
    verbatim when collecting fields.

    Returns an empty string when the catalog has no usable flows / tabs.
    """
    if not isinstance(catalog, dict):
        return ""
    flows = catalog.get("flows") or {}
    tabs = catalog.get("tabs") or {}
    if not isinstance(flows, dict):
        flows = {}
    if not isinstance(tabs, dict):
        tabs = {}
    if not flows and not tabs:
        return ""

    def _pick_question(qmap: dict[str, Any] | None, label: str) -> str:
        if isinstance(qmap, dict):
            for candidate in (language, "en"):
                value = qmap.get(candidate)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return f"Please share {label}."

    def _project_question(default: str) -> str:
        """When DB projects are available, build a question that enumerates
        them so the LLM can't paraphrase in the admin's hardcoded project
        names. Localised for hi / te so the enumeration isn't English-only.
        Falls back to the generic prompt if the list is empty."""
        if not project_names:
            return default
        names = [n.strip() for n in project_names if n and n.strip()]
        if not names:
            return default
        # Per-language sentence frame + "or" connector for the final item.
        if language == "hi":
            stem, connector = "आप कौन सा project visit करना चाहेंगे — ", " या "
        elif language == "te":
            stem, connector = "మీరు ఏ project visit చేయాలనుకుంటున్నారు — ", " లేదా "
        else:
            stem, connector = "Which project would you like to visit — ", " or "
        if len(names) == 1:
            listing = names[0]
        elif len(names) == 2:
            listing = f"{names[0]}{connector}{names[1]}"
        else:
            listing = ", ".join(names[:-1]) + "," + connector + names[-1]
        return f"{stem}{listing}?"

    sections: list[str] = []

    # Flows first — these are the booking / lead / appointment FSMs,
    # the most common record-creation paths.
    for flow_key, flow in flows.items():
        if not isinstance(flow, dict):
            continue
        slots = flow.get("slots") or []
        if not isinstance(slots, list) or not slots:
            continue
        record_label = _RECORD_TAB_LABELS.get(str(flow.get("tab") or flow_key), str(flow_key))
        lines = [f"## {record_label} ({flow_key})"]
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            key = str(slot.get("key") or "").strip()
            label = str(slot.get("label") or key or "field").strip()
            question = _pick_question(slot.get("questions") or {}, label)
            if str(slot.get("kind") or "") == "project":
                question = _project_question(question)
            required = "required" if slot.get("required") else "optional"
            lines.append(f'  - {key} ({label}, {required}): "{question}"')
        sections.append("\n".join(lines))

    # Tabs (writable fields per record type). These cover ticket /
    # callback / custom tabs that aren't necessarily wrapped in a flow
    # but still need consistent phrasing when the agent collects info.
    # A flow's record lands in its `tab`, so skip any tab a flow already
    # covers (matched by the flow's tab, not just the flow key) — otherwise
    # e.g. the real_estate_site_visit flow AND the "tickets" tab both emit the
    # same field list (the duplicate `## ticket (...)` blocks).
    _flow_tabs = {str(f.get("tab")) for f in flows.values() if isinstance(f, dict) and f.get("tab")}
    for tab_key, tab in tabs.items():
        if not isinstance(tab, dict):
            continue
        fields = tab.get("fields") or {}
        if not isinstance(fields, dict) or not fields:
            continue
        # Skip tabs already covered by a flow (by flow key OR the flow's tab).
        if tab_key in flows or tab_key in _flow_tabs:
            continue
        record_label = _RECORD_TAB_LABELS.get(str(tab_key), str(tab.get("label") or tab_key))
        lines = [f"## {record_label} ({tab_key})"]
        for field_key, field in fields.items():
            if not isinstance(field, dict):
                continue
            label = str(field.get("label") or field_key or "field").strip()
            question = _pick_question(field.get("questions") or {}, label)
            required = "required" if field.get("required") else "optional"
            lines.append(f'  - {field_key} ({label}, {required}): "{question}"')
        if len(lines) > 1:
            sections.append("\n".join(lines))

    if not sections:
        return ""

    header = (
        "# FIELD-COLLECTION SCRIPT — collect these per record\n"
        "Collect every `(required)` line for the active flow before confirming or\n"
        "closing the record (custom fields included — never skip one).\n"
        "1. Ask using the EXACT phrasing in quotes — don't paraphrase or translate.\n"
        "2. One field per turn, in the listed order.\n"
        "3. Optional fields: skip if not volunteered, but offer \"Anything else to note?\" once required are done.\n"
        "4. Before any \"booked / confirmed / all set\", silently verify EVERY required field has a value; if one is missing, ask for it — never confirm with gaps.\n"
        "5. Side question mid-collection: answer briefly, then \"Coming back to your booking — \" + the next field's exact question."
    )
    return header + "\n\n" + "\n\n".join(sections)
