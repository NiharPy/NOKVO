"""
Nokvo One dynamic tool resolver.

Given (business_type, schema_overrides), this service produces the full list of
predefined tools available to an org's agents. Each tab (leads, tickets,
appointments, ...) yields a CRUD-shaped family of tools (create, update, get,
list, search, add_note, close, set_status) whose JSON Schemas are generated
from the merged business-template field definitions plus the per-tenant
status vocabulary.

The resolved catalog is the union of:
  1. CROSS_CUTTING tools  — business-agnostic (call log, callback, email draft,
                            escalate, find_contact).
  2. Per-tab resource tools — generated from the merged schema for each tab.

Caller-facing surface:
  - resolve_catalog(business_type, schema_overrides) -> list[PredefinedTool]
  - resolve_index(business_type, schema_overrides)   -> dict[str, PredefinedTool]
  - default_tool_keys(business_type, schema_overrides) -> list[str]
"""
from __future__ import annotations

import re
from typing import Any

from app.services.nokvo_one_business_templates import (
    apply_schema_overrides,
    business_type_config,
    custom_tab_record_type,
    custom_tabs_from_overrides,
    default_custom_tab_status_vocabulary,
    enabled_tabs_for,
    tab_record_type,
    tab_search_keys,
    tab_status_vocabulary,
)
from app.services.predefined_tools_service import (
    CROSS_CUTTING_CATALOG,
    PredefinedTool,
    macros_for_business_type,
)


# ─────────── Field-type → JSON Schema mapping ───────────

_FIELD_TYPE_SCHEMA: dict[str, dict[str, Any]] = {
    "text": {"type": "string", "maxLength": 500},
    "phone": {"type": "string", "minLength": 4, "maxLength": 32},
    "email": {"type": "string", "format": "email"},
    "currency": {"type": "number"},
    "number": {"type": "number"},
    "date": {"type": "string", "format": "date"},
    "datetime": {"type": "string", "format": "date-time"},
    "select": {"type": "string"},
    "textarea": {"type": "string", "maxLength": 4000},
}


def _field_schema(field: dict[str, Any]) -> dict[str, Any]:
    ftype = str(field.get("type") or "text").lower()
    base = dict(_FIELD_TYPE_SCHEMA.get(ftype, _FIELD_TYPE_SCHEMA["text"]))
    if field.get("options"):
        opts = [str(o) for o in field["options"] if o is not None]
        if opts:
            base["enum"] = opts
    if field.get("label"):
        base["description"] = str(field["label"])
    return base


# Special field keys that always get masked out of user-supplied input — they
# are system-managed or set via dedicated verbs.
_SYSTEM_FIELD_KEYS: frozenset[str] = frozenset({"id", "created_at", "updated_at"})

# `status` is also system-managed via {tab}_set_status, never via create/update.
_RESERVED_ON_WRITE: frozenset[str] = frozenset({"status", "id", "created_at", "updated_at"})


def _resource_properties(
    fields: list[dict[str, Any]],
    *,
    skip_keys: frozenset[str] = _RESERVED_ON_WRITE,
) -> tuple[dict[str, Any], list[str]]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for field in fields:
        key = str(field.get("key") or "").strip()
        if not key or key in skip_keys or key in _SYSTEM_FIELD_KEYS:
            continue
        properties[key] = _field_schema(field)
        if field.get("required"):
            required.append(key)
    return properties, required


_TAB_SINGULAR = {"leads": "lead", "tickets": "ticket", "appointments": "appointment"}


def _singular(tab: str) -> str:
    return _TAB_SINGULAR.get(tab, tab.rstrip("s") or tab)


def _make_create_tool(tab: str, fields: list[dict[str, Any]], record_type: str) -> PredefinedTool:
    properties, required = _resource_properties(fields)
    return PredefinedTool(
        key=f"{tab}_create",
        display_name=f"Create {_singular(tab)}",
        description=f"Create a new {_singular(tab)} record using the workspace's {tab} field schema.",
        record_type=record_type,
        handler_name="resource_create",
        input_schema={
            "type": "object",
            "required": required,
            "properties": properties,
            "additionalProperties": False,
        },
    )


def _make_update_tool(tab: str, fields: list[dict[str, Any]], record_type: str) -> PredefinedTool:
    properties, _ = _resource_properties(fields)
    properties["id"] = {"type": "string", "format": "uuid", "description": f"{_singular(tab)} id"}
    return PredefinedTool(
        key=f"{tab}_update",
        display_name=f"Update {_singular(tab)}",
        description=(
            f"Update fields on an existing {_singular(tab)}. Identify the record either by `id` "
            f"or by `phone`/`email` (latest open record matching wins)."
        ),
        record_type=record_type,
        handler_name="resource_update",
        input_schema={
            "type": "object",
            "required": [],
            "properties": properties,
            "additionalProperties": False,
        },
    )


def _make_get_tool(tab: str, record_type: str) -> PredefinedTool:
    return PredefinedTool(
        key=f"{tab}_get",
        display_name=f"Get {_singular(tab)}",
        description=f"Fetch a single {_singular(tab)} by id.",
        record_type=record_type,
        handler_name="resource_get",
        input_schema={
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "string", "format": "uuid"}},
            "additionalProperties": False,
        },
    )


def _make_list_tool(tab: str, record_type: str, status_vocab: dict[str, Any] | None) -> PredefinedTool:
    status_schema: dict[str, Any] = {"type": "string"}
    if status_vocab and status_vocab.get("all"):
        status_schema["enum"] = list(status_vocab["all"])
    return PredefinedTool(
        key=f"{tab}_list",
        display_name=f"List {tab}",
        description=f"List recent {tab} for this organization, optionally filtered by status.",
        record_type=record_type,
        handler_name="resource_list",
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "status": status_schema,
            },
            "additionalProperties": False,
        },
    )


def _make_search_tool(tab: str, record_type: str) -> PredefinedTool:
    keys = tab_search_keys(tab)
    description = (
        f"Search recent {tab} by a free-text query. Indexed fields: "
        f"{', '.join(keys) if keys else 'core text fields'}."
    )
    return PredefinedTool(
        key=f"{tab}_search",
        display_name=f"Search {tab}",
        description=description,
        record_type=record_type,
        handler_name="resource_search",
        input_schema={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10},
            },
            "additionalProperties": False,
        },
    )


def _make_add_note_tool(tab: str, record_type: str) -> PredefinedTool:
    return PredefinedTool(
        key=f"{tab}_add_note",
        display_name=f"Add {_singular(tab)} note",
        description=f"Append a note to a {_singular(tab)}'s history.",
        record_type=record_type,
        handler_name="resource_add_note",
        input_schema={
            "type": "object",
            "required": ["id", "note"],
            "properties": {
                "id": {"type": "string", "format": "uuid"},
                "note": {"type": "string", "minLength": 1, "maxLength": 2000},
            },
            "additionalProperties": False,
        },
    )


def _make_close_tool(tab: str, record_type: str, status_vocab: dict[str, Any] | None) -> PredefinedTool:
    # Default terminal state varies by tab; resolver picks the last forward state
    # for tickets/leads (closed/converted/lost) and 'cancelled' for appointments.
    if tab == "appointments":
        terminal = "cancelled"
    elif status_vocab and status_vocab.get("forward"):
        terminal = status_vocab["forward"][-1]
    else:
        terminal = "closed"
    return PredefinedTool(
        key=f"{tab}_close",
        display_name=f"Close {_singular(tab)}",
        description=(
            f"Soft-close a {_singular(tab)} by transitioning it to '{terminal}'. "
            f"Hard delete is not supported."
        ),
        record_type=record_type,
        handler_name="resource_close",
        input_schema={
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "string", "format": "uuid"},
                "reason": {"type": "string", "maxLength": 500},
            },
            "additionalProperties": False,
        },
        requires_confirmation=True,
        # Stash the terminal status under display_name? No — pass via tool metadata.
    )


def _make_set_status_tool(tab: str, record_type: str, status_vocab: dict[str, Any] | None) -> PredefinedTool | None:
    if not status_vocab or not status_vocab.get("all"):
        return None
    return PredefinedTool(
        key=f"{tab}_set_status",
        display_name=f"Set {_singular(tab)} status",
        description=(
            f"Transition a {_singular(tab)} to a new status. Allowed values: "
            f"{', '.join(status_vocab['all'])}. Regressive transitions require confirmation."
        ),
        record_type=record_type,
        handler_name="resource_set_status",
        input_schema={
            "type": "object",
            "required": ["id", "status"],
            "properties": {
                "id": {"type": "string", "format": "uuid"},
                "status": {"type": "string", "enum": list(status_vocab["all"])},
            },
            "additionalProperties": False,
        },
    )


def _resolve_tab_tools(
    tab: str,
    fields: list[dict[str, Any]],
    business_type: str | None,
) -> list[PredefinedTool]:
    record_type = tab_record_type(tab)
    if record_type is None:
        return []
    status_vocab = tab_status_vocabulary(business_type, tab)
    tools: list[PredefinedTool] = [
        _make_create_tool(tab, fields, record_type),
        _make_update_tool(tab, fields, record_type),
        _make_get_tool(tab, record_type),
        _make_list_tool(tab, record_type, status_vocab),
        _make_search_tool(tab, record_type),
        _make_add_note_tool(tab, record_type),
    ]
    set_status = _make_set_status_tool(tab, record_type, status_vocab)
    if set_status is not None:
        tools.append(set_status)
    tools.append(_make_close_tool(tab, record_type, status_vocab))
    # Stamp business_type into metadata so handlers can resolve status vocab.
    return [_with_metadata(t, business_type=business_type, tab=tab) for t in tools]


def _resolve_custom_tab_tools(
    spec: dict[str, Any],
    business_type: str | None,
) -> list[PredefinedTool]:
    """Generate CRUD tools for a tenant-defined custom tab."""
    slug = spec["slug"]
    label = spec.get("label") or slug.replace("_", " ").title()
    record_type = custom_tab_record_type(slug)
    fields = spec.get("fields") or []
    status_vocab = spec.get("status_vocabulary") or default_custom_tab_status_vocabulary()

    properties, required = _resource_properties(fields)
    create = PredefinedTool(
        key=f"{slug}_create",
        display_name=f"Create {label}",
        description=f"Create a new {label} record using the workspace's custom-tab schema.",
        record_type=record_type,
        handler_name="resource_create",
        input_schema={
            "type": "object",
            "required": required,
            "properties": properties,
            "additionalProperties": False,
        },
    )

    update_properties, _ = _resource_properties(fields)
    update_properties["id"] = {"type": "string", "format": "uuid", "description": f"{label} id"}
    update = PredefinedTool(
        key=f"{slug}_update",
        display_name=f"Update {label}",
        description=(
            f"Update fields on an existing {label}. Identify the record either by `id` "
            f"or by `phone`/`email` (latest open record matching wins)."
        ),
        record_type=record_type,
        handler_name="resource_update",
        input_schema={
            "type": "object",
            "required": [],
            "properties": update_properties,
            "additionalProperties": False,
        },
    )

    get_tool = PredefinedTool(
        key=f"{slug}_get",
        display_name=f"Get {label}",
        description=f"Fetch a single {label} by id.",
        record_type=record_type,
        handler_name="resource_get",
        input_schema={
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "string", "format": "uuid"}},
            "additionalProperties": False,
        },
    )

    list_status_schema: dict[str, Any] = {"type": "string"}
    if status_vocab and status_vocab.get("all"):
        list_status_schema["enum"] = list(status_vocab["all"])
    list_tool = PredefinedTool(
        key=f"{slug}_list",
        display_name=f"List {label}",
        description=f"List recent {label} records, optionally filtered by status.",
        record_type=record_type,
        handler_name="resource_list",
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "status": list_status_schema,
            },
            "additionalProperties": False,
        },
    )

    search_tool = PredefinedTool(
        key=f"{slug}_search",
        display_name=f"Search {label}",
        description=f"Search recent {label} records by a free-text query.",
        record_type=record_type,
        handler_name="resource_search",
        input_schema={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10},
            },
            "additionalProperties": False,
        },
    )

    add_note = PredefinedTool(
        key=f"{slug}_add_note",
        display_name=f"Add {label} note",
        description=f"Append a note to a {label} record's history.",
        record_type=record_type,
        handler_name="resource_add_note",
        input_schema={
            "type": "object",
            "required": ["id", "note"],
            "properties": {
                "id": {"type": "string", "format": "uuid"},
                "note": {"type": "string", "minLength": 1, "maxLength": 2000},
            },
            "additionalProperties": False,
        },
    )

    tools: list[PredefinedTool] = [create, update, get_tool, list_tool, search_tool, add_note]

    if status_vocab and status_vocab.get("all"):
        set_status = PredefinedTool(
            key=f"{slug}_set_status",
            display_name=f"Set {label} status",
            description=(
                f"Transition a {label} record to a new status. Allowed values: "
                f"{', '.join(status_vocab['all'])}."
            ),
            record_type=record_type,
            handler_name="resource_set_status",
            input_schema={
                "type": "object",
                "required": ["id", "status"],
                "properties": {
                    "id": {"type": "string", "format": "uuid"},
                    "status": {"type": "string", "enum": list(status_vocab["all"])},
                },
                "additionalProperties": False,
            },
        )
        tools.append(set_status)

    terminal = (status_vocab or {}).get("forward")
    terminal_status = terminal[-1] if terminal else "archived"
    close_tool = PredefinedTool(
        key=f"{slug}_close",
        display_name=f"Close {label}",
        description=(
            f"Soft-close a {label} record by transitioning it to '{terminal_status}'. "
            f"Hard delete is not supported."
        ),
        record_type=record_type,
        handler_name="resource_close",
        input_schema={
            "type": "object",
            "required": ["id"],
            "properties": {
                "id": {"type": "string", "format": "uuid"},
                "reason": {"type": "string", "maxLength": 500},
            },
            "additionalProperties": False,
        },
        requires_confirmation=True,
    )
    tools.append(close_tool)

    # Stamp metadata: business_type (for handlers that read it), slug, label, group.
    return [
        _with_metadata(
            t,
            business_type=business_type,
            tab=slug,
            custom_tab_slug=slug,
            custom_tab_label=label,
            custom_tab_status_vocabulary=status_vocab,
            custom_tab_terminal_status=terminal_status,
            custom_tab_search_keys=spec.get("search_keys") or [],
            group=label,
        )
        for t in tools
    ]


def _with_metadata(tool: PredefinedTool, **extra: Any) -> PredefinedTool:
    merged = dict(tool.metadata or {})
    merged.update({k: v for k, v in extra.items() if v is not None})
    return PredefinedTool(
        key=tool.key,
        display_name=tool.display_name,
        description=tool.description,
        input_schema=tool.input_schema,
        record_type=tool.record_type,
        handler_name=tool.handler_name,
        requires_confirmation=tool.requires_confirmation,
        metadata=merged,
    )


def resolve_catalog(
    business_type: str | None,
    schema_overrides: dict[str, Any] | None = None,
    custom_tabs: list[dict[str, Any]] | None = None,
) -> list[PredefinedTool]:
    """Return the full ordered catalog for an org."""
    config = business_type_config(business_type)
    tools: list[PredefinedTool] = []

    if config is not None:
        merged = apply_schema_overrides(config, schema_overrides)
        schemas: dict[str, list[dict[str, Any]]] = dict(merged.get("schemas") or {})
        for tab in enabled_tabs_for(business_type):
            fields = schemas.get(tab) or []
            tools.extend(_resolve_tab_tools(tab, fields, business_type))

        # Macros are gated to a business_type and reuse resource storage.
        for macro in macros_for_business_type(business_type):
            tools.append(_with_metadata(macro, business_type=business_type, group="Macros"))

    # Custom tabs are tenant-defined. They generate the same 8 CRUD verbs as
    # built-in tabs but with record_type=custom_<slug>. They are independent of
    # business_type — every org can define them.
    for spec in custom_tabs or []:
        tools.extend(_resolve_custom_tab_tools(spec, business_type))

    tools.extend(CROSS_CUTTING_CATALOG)
    return tools


def resolve_index(
    business_type: str | None,
    schema_overrides: dict[str, Any] | None = None,
    custom_tabs: list[dict[str, Any]] | None = None,
) -> dict[str, PredefinedTool]:
    return {tool.key: tool for tool in resolve_catalog(business_type, schema_overrides, custom_tabs)}


def default_tool_keys(
    business_type: str | None,
    schema_overrides: dict[str, Any] | None = None,
    custom_tabs: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Sensible default tool selection when an agent is first created."""
    catalog = resolve_catalog(business_type, schema_overrides, custom_tabs)
    # Default: all CRUD verbs for every enabled tab + all cross-cutting tools.
    return [t.key for t in catalog]


# ─────────── Catalog → portal-facing JSON ───────────


_TAB_GROUP_LABEL = {
    "leads": "Leads",
    "tickets": "Tickets",
    "appointments": "Appointments",
}


def _group_for(tool: PredefinedTool, enabled_tabs: list[str]) -> str:
    # Macros are explicitly tagged in metadata by the resolver.
    metadata_group = (tool.metadata or {}).get("group")
    if metadata_group:
        return str(metadata_group)
    for tab in enabled_tabs:
        if tool.key.startswith(f"{tab}_"):
            return _TAB_GROUP_LABEL.get(tab, tab.title())
    # Custom tabs stamp the group label directly in metadata.
    return "General"


def catalog_as_groups(
    business_type: str | None,
    schema_overrides: dict[str, Any] | None = None,
    custom_tabs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Group the resolved catalog by tab for the agent-create UI."""
    enabled = enabled_tabs_for(business_type)
    catalog = resolve_catalog(business_type, schema_overrides, custom_tabs)
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for tool in catalog:
        group = _group_for(tool, enabled)
        if group not in groups:
            groups[group] = []
            order.append(group)
        groups[group].append(
            {
                "key": tool.key,
                "display_name": tool.display_name,
                "description": tool.description,
                "input_schema": tool.input_schema,
                "requires_confirmation": tool.requires_confirmation,
            }
        )
    # Stable order: enabled tabs first (in their declared order), then custom-tab
    # groups (in declared order), then Macros, then General. Unknown groups fall
    # through at the end.
    tab_order = [_TAB_GROUP_LABEL[t] for t in enabled if t in _TAB_GROUP_LABEL]
    ordered: list[dict[str, Any]] = []
    emitted: set[str] = set()
    for label in tab_order:
        if label in groups and label not in emitted:
            ordered.append({"label": label, "tools": groups[label]})
            emitted.add(label)
    # Custom-tab groups land here in the order they were encountered.
    for label in order:
        if label in emitted:
            continue
        if label in {"Macros", "General"} or label in _TAB_GROUP_LABEL.values():
            continue
        ordered.append({"label": label, "tools": groups[label]})
        emitted.add(label)
    if "Macros" in groups and "Macros" not in emitted:
        ordered.append({"label": "Macros", "tools": groups["Macros"]})
        emitted.add("Macros")
    if "General" in groups and "General" not in emitted:
        ordered.append({"label": "General", "tools": groups["General"]})
        emitted.add("General")
    for label in order:
        if label not in emitted:
            ordered.append({"label": label, "tools": groups[label]})
            emitted.add(label)
    return ordered


# ─────────── Validation helpers ───────────

_VALID_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def validate_resolved_tool_keys(
    keys: list[str],
    business_type: str | None,
    schema_overrides: dict[str, Any] | None = None,
    custom_tabs: list[dict[str, Any]] | None = None,
) -> list[str]:
    index = resolve_index(business_type, schema_overrides, custom_tabs)
    invalid = [k for k in keys if not _VALID_KEY_RE.match(k) or k not in index]
    if invalid:
        raise ValueError(f"Unknown tool keys for this business_type: {invalid}")
    return list(keys)
