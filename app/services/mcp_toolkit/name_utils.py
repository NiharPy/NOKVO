from __future__ import annotations

import re
import uuid


READ_PREFIXES = ("lookup", "get", "check", "list", "search", "retrieve")
CREATE_PREFIXES = ("create", "add", "open", "raise", "log")
UPDATE_PREFIXES = ("update", "change", "modify", "mark")
BULK_UPDATE_PREFIXES = ("bulk", "bulk_update", "mark", "update")
DELETE_PREFIXES = ("delete", "remove", "cancel")


def normalize_tool_name(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", value or "").strip("_").lower()
    if not normalized:
        return f"generated_tool_{uuid.uuid4().hex[:8]}"
    if normalized[0].isdigit():
        normalized = f"tool_{normalized}"
    return normalized[:64]


def humanize_tool_name(value: str) -> str:
    return normalize_tool_name(value).replace("_", " ").title()


def operation_prefixes(operation_type: str) -> tuple[str, ...]:
    if operation_type == "read":
        return READ_PREFIXES
    if operation_type == "create":
        return CREATE_PREFIXES
    if operation_type == "update":
        return UPDATE_PREFIXES
    if operation_type == "delete":
        return DELETE_PREFIXES
    if operation_type == "workflow":
        return UPDATE_PREFIXES + CREATE_PREFIXES + DELETE_PREFIXES
    if operation_type in {"bulk_update", "workflow_bulk_update"}:
        return BULK_UPDATE_PREFIXES
    return ()


def has_valid_operation_prefix(name: str, operation_type: str) -> bool:
    normalized = normalize_tool_name(name)
    return any(normalized == prefix or normalized.startswith(f"{prefix}_") for prefix in operation_prefixes(operation_type))


def is_table_name_only(name: str, resources: list[str]) -> bool:
    normalized = normalize_tool_name(name)
    for resource in resources:
        table = normalize_tool_name(resource.split(".")[-1])
        if normalized in {table, table.rstrip("s")}:
            return True
    return False


def action_name_from_prompt(prompt: str, fallback: str = "generated_tool") -> str:
    prompt = (prompt or "").lower()
    words = re.findall(r"[a-z0-9]+", prompt)
    stop = {
        "a",
        "an",
        "and",
        "as",
        "by",
        "for",
        "from",
        "i",
        "in",
        "into",
        "me",
        "of",
        "on",
        "that",
        "the",
        "to",
        "tool",
        "use",
        "with",
    }
    selected = [word for word in words if word not in stop][:8]
    return normalize_tool_name("_".join(selected) or fallback)
