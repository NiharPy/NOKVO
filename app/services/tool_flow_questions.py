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
            "date": "Preferred date क्या है?",
            "time": "Preferred time क्या है?",
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
            "date": "Preferred date ఏది?",
            "time": "Preferred time ఏది?",
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
        "date": "What date would you prefer?",
        "time": "What time would you prefer?",
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


def _real_estate_visit_slots(
    site_visit_fields: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build the slot list for the real-estate site-visit FSM, driven by the
    admin's **Site Visit Fields** (the tickets schema) — NOT the lead schema.

    A booking captures exactly what the admin configured on the site-visit
    form. Name + phone come first, the configured conversational fields fill
    the middle, project + date + time come last. Categorical/admin-only
    fields (status, owner, priority, issue_type) are never asked; they get
    server-side defaults. Each slot records ``source_field`` — the configured
    field key — so the booking tool can store the captured value back under
    that key and the Site Visits tab renders a populated cell.
    """
    fields = _writable_fields(site_visit_fields or [])

    def _field_of_kind(kind: str) -> dict[str, Any] | None:
        for field in fields:
            if _kind_for_field(field) == kind:
                return field
        return None

    slots: list[dict[str, Any]] = []
    used_keys: set[str] = set()

    # Name + phone first. Bind to the matching configured field when present
    # (so the value lands under e.g. ``customer``), else canonical keys.
    name_field = _field_of_kind("name")
    name_key = str((name_field or {}).get("key") or "name")
    slots.append(_slot_entry(name_key, _field_label(name_field) if name_field else "Name", "name", source_field=(name_field or {}).get("key")))
    used_keys.add(name_key)

    phone_field = _field_of_kind("phone")
    phone_key = str((phone_field or {}).get("key") or "phone")
    slots.append(_slot_entry(phone_key, _field_label(phone_field) if phone_field else "Phone", "phone", source_field=(phone_field or {}).get("key")))
    used_keys.add(phone_key)

    # Middle: EVERY other configured Site Visit Field, in schema order, so the
    # agent asks the full ticket schema — not just a hardcoded subset. Only the
    # system/admin-only fields (status, owner, …) are withheld; those get
    # server-side defaults. name/phone/project/date/time are scaffolded
    # separately (above and below) for a natural ask order. Each field keeps its
    # admin-configured required flag — the FSM asks the required ones and the
    # field-questions prompt surfaces the rest for the agent to ask too.
    _categorical_keys = {"status", "assigned_to", "owner", "issue_type", "priority"}
    for field in fields:
        key = str(field.get("key") or "")
        if not key or key in used_keys or key in _categorical_keys:
            continue
        kind = _kind_for_field(field)
        if kind in {"name", "phone", "date", "time", "project"}:
            continue
        slots.append(
            _slot_entry(
                key,
                _field_label(field),
                kind,
                required=bool(field.get("required")),
                source_field=key,
            )
        )
        used_keys.add(key)

    # Project — always asked. Bind to a configured project/"Property" field
    # when present so the value renders in that column, else canonical.
    project_field = _field_of_kind("project")
    if project_field is None:
        project_field = next(
            (
                f for f in fields
                if str(f.get("key")) not in used_keys
                and "property" in f"{f.get('key')} {_field_label(f)}".lower()
            ),
            None,
        )
    project_key = str((project_field or {}).get("key") or "project_name")
    if project_key not in used_keys:
        slots.append(_slot_entry(project_key, _field_label(project_field) if project_field else "Project", "project", required=True, source_field=(project_field or {}).get("key")))
        used_keys.add(project_key)

    # Date + time last. Bind to configured date/time fields when present.
    date_field = _field_of_kind("date")
    date_key = str((date_field or {}).get("key") or "visit_date")
    if date_key not in used_keys:
        slots.append(_slot_entry(date_key, _field_label(date_field) if date_field else "Visit Date", "date", source_field=(date_field or {}).get("key")))
        used_keys.add(date_key)

    time_field = _field_of_kind("time")
    time_key = str((time_field or {}).get("key") or "visit_time")
    if time_key not in used_keys:
        slots.append(_slot_entry(time_key, _field_label(time_field) if time_field else "Visit Time", "time", source_field=(time_field or {}).get("key")))
        used_keys.add(time_key)

    return slots


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
    for tab_key, tab in tabs.items():
        if not isinstance(tab, dict):
            continue
        fields = tab.get("fields") or {}
        if not isinstance(fields, dict) or not fields:
            continue
        # Skip tabs that are also present as flows — avoids duplication
        # since the flow already lists those slots.
        if tab_key in flows:
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
        "# FIELD-COLLECTION SCRIPT — MANDATORY for the records listed below\n"
        "Operators configured these field lists. You MUST collect every line\n"
        "marked `(required)` for the flow in progress BEFORE confirming or\n"
        "closing the record — including any custom fields beyond the obvious\n"
        "name / phone / date / time / project defaults. \"It feels complete\"\n"
        "is NEVER a valid reason to skip a required field; the admin put it on\n"
        "the form for a reason.\n\n"
        "Hard rules:\n"
        "1. Ask using the EXACT operator-configured phrasing shown in quotes.\n"
        "   Do not paraphrase, translate freely, or invent your own wording.\n"
        "2. Ask ONE field per turn, in the order listed below for that flow.\n"
        "3. Optional fields may be skipped if the caller doesn't volunteer\n"
        "   them, but always offer (\"Anything else you'd like noted?\") once\n"
        "   the required set is complete.\n"
        "4. PRE-CONFIRM CHECK: before you say anything like \"booked\",\n"
        "   \"confirmed\", \"all set\", \"captured\", or \"thank you, our team\n"
        "   will…\", silently verify that EVERY required field for the active\n"
        "   flow has a captured value. If any required field is missing — even\n"
        "   ONE — ask for it first. NEVER confirm a record with gaps.\n"
        "5. If the caller asks a side question mid-collection, answer briefly\n"
        "   then resume with: \"Coming back to your booking — \" followed by\n"
        "   the next pending field's exact question."
    )
    return header + "\n\n" + "\n\n".join(sections)
