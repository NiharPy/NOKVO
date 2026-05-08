from __future__ import annotations

from typing import Any

from app.services.mcp_toolkit.reviewer import Reviewer


class Validator:
    @staticmethod
    def validate(generation: dict[str, Any], verified_context: dict[str, Any], existing_tool_names: set[str] | None = None) -> dict[str, Any]:
        return Reviewer.validate(generation, verified_context, existing_tool_names)["validation"]
