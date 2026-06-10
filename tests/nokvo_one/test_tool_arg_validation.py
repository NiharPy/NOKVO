"""Tolerant tool-argument validation.

The voice pipeline intentionally over-supplies field aliases
(name→customer_name/patient_name/…, phone→mobile/contact_phone/…) so it's robust
across tool schemas. A strict ``additionalProperties: false`` schema used to RAISE
on those extras, which failed the whole tool action, triggered a fresh-session
retry, and cascaded into ``MissingGreenlet`` (and the record was lost). Validation
now DROPS unsupported fields instead of raising.
"""
from __future__ import annotations

import pytest

from app.services.predefined_tools_service import PredefinedTool, _validate_tool_arguments


def _strict_tool() -> PredefinedTool:
    return PredefinedTool(
        key="leads_create",
        display_name="Create lead",
        description="test",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}, "phone": {"type": "string"}, "budget": {"type": "string"}},
            "required": ["name", "phone"],
            "additionalProperties": False,
        },
        record_type="lead",
        handler_name="_handle_resource_create",
    )


def test_unsupported_alias_fields_are_dropped_not_raised():
    tool = _strict_tool()
    args = {
        "name": "Nihar", "phone": "7569672503", "budget": "45Cr",
        # alias noise the schema doesn't accept:
        "customer_name": "Nihar", "patient_name": "Nihar", "guest_name": "Nihar",
        "full_name": "Nihar", "bhk": "3 BHK", "mobile": "7569672503",
        "contact_phone": "7569672503", "patient_phone": "7569672503",
    }
    # Must NOT raise.
    _validate_tool_arguments(tool, args)
    # Extras stripped in place; canonical/accepted fields preserved.
    assert args == {"name": "Nihar", "phone": "7569672503", "budget": "45Cr"}


def test_required_fields_still_enforced():
    tool = _strict_tool()
    with pytest.raises(ValueError, match="Missing required fields"):
        _validate_tool_arguments(tool, {"name": "Nihar"})  # no phone


def test_lenient_schema_keeps_extras():
    # additionalProperties not False → nothing dropped.
    tool = PredefinedTool(
        key="x", display_name="x", description="x",
        input_schema={"type": "object", "properties": {"name": {}}, "required": []},
        record_type=None, handler_name="_handle_resource_create",
    )
    args = {"name": "A", "extra": "keep"}
    _validate_tool_arguments(tool, args)
    assert args == {"name": "A", "extra": "keep"}
