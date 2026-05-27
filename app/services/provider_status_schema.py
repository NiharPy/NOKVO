from __future__ import annotations

import math
from typing import Any


_MAX_DEPTH = 8
_MAX_DICT_KEYS = 256
_MAX_STRING_CHARS = 20000
_DEFAULT_LIST_LIMIT = 1000
_LIST_LIMITS = {
    "agent_knowledge_documents": 500,
    "agent_answer_cards": 1000,
    "agent_policy_cards": 500,
    "business_template_custom_tabs": 12,
    "toolkit_audit_events": 500,
    "toolkit_drafts": 200,
    "agent_phone_links": 20,
    "tool_flow_questions": 200,
}


def _json_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return str(value)


def _sanitize(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return None
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for ix, (raw_key, raw_value) in enumerate(value.items()):
            if ix >= _MAX_DICT_KEYS:
                break
            child_key = str(raw_key)[:200]
            out[child_key] = _sanitize(raw_value, key=child_key, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        limit = _LIST_LIMITS.get(key, _DEFAULT_LIST_LIMIT)
        return [_sanitize(item, key=key, depth=depth + 1) for item in list(value)[:limit]]
    scalar = _json_scalar(value)
    if isinstance(scalar, str) and len(scalar) > _MAX_STRING_CHARS:
        return scalar[:_MAX_STRING_CHARS]
    return scalar


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any, *, limit_key: str) -> list[Any]:
    if not isinstance(value, list):
        return []
    return value[: _LIST_LIMITS.get(limit_key, _DEFAULT_LIST_LIMIT)]


def _normalize_single_prompt(provider_status: dict[str, Any]) -> None:
    raw = provider_status.get("single_prompt_voice_agent")
    if raw is None:
        return
    value = _as_dict(raw)
    prompt = str(value.get("prompt") or "")[:8000]
    provider_status["single_prompt_voice_agent"] = {
        "enabled": bool(value.get("enabled") and prompt),
        "prompt": prompt,
        "updated_at": str(value.get("updated_at") or "")[:80] or None,
        "updated_by": str(value.get("updated_by") or "")[:80] or None,
    }


def _normalize_custom_tabs(provider_status: dict[str, Any]) -> None:
    raw_tabs = provider_status.get("business_template_custom_tabs")
    if raw_tabs is None:
        return
    tabs: list[dict[str, Any]] = []
    for raw in _as_list(raw_tabs, limit_key="business_template_custom_tabs"):
        tab = _as_dict(raw)
        slug = str(tab.get("slug") or tab.get("key") or "").strip()[:64]
        label = str(tab.get("label") or slug).strip()[:80]
        if not slug or not label:
            continue
        fields = []
        for raw_field in _as_list(tab.get("fields"), limit_key="business_template_custom_tabs")[:80]:
            field = _as_dict(raw_field)
            field_key = str(field.get("key") or "").strip()[:64]
            field_label = str(field.get("label") or field_key).strip()[:120]
            if not field_key or not field_label:
                continue
            fields.append(
                {
                    "key": field_key,
                    "label": field_label,
                    "type": str(field.get("type") or "text").strip()[:32] or "text",
                    "required": bool(field.get("required")),
                    "options": [str(item)[:120] for item in _as_list(field.get("options"), limit_key="business_template_custom_tabs")[:80]],
                }
            )
        tabs.append(
            {
                "slug": slug,
                "key": slug,
                "label": label,
                "fields": fields,
                "status_vocabulary": _as_dict(tab.get("status_vocabulary")),
                "search_keys": [str(item)[:64] for item in _as_list(tab.get("search_keys"), limit_key="business_template_custom_tabs")[:20]],
            }
        )
    provider_status["business_template_custom_tabs"] = tabs


def _normalize_known_lists(provider_status: dict[str, Any]) -> None:
    for key, limit in _LIST_LIMITS.items():
        if key in provider_status and isinstance(provider_status[key], list):
            provider_status[key] = provider_status[key][-limit:]


def sanitize_provider_status(value: Any) -> dict[str, Any]:
    """Return a JSON-safe, bounded provider_status object.

    The platform stores many integration-specific sub-documents here, so the
    schema is intentionally permissive for unknown keys. Validation enforces the
    contract that the root is an object, all values are JSON-safe, and known
    high-growth arrays / nested config shapes stay bounded.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("provider_status must be a JSON object")
    provider_status = _sanitize(value)
    if not isinstance(provider_status, dict):
        raise ValueError("provider_status must be a JSON object")
    _normalize_known_lists(provider_status)
    _normalize_single_prompt(provider_status)
    _normalize_custom_tabs(provider_status)
    return provider_status
