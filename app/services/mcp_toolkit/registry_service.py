from __future__ import annotations

from typing import Any

from app.services.mcp_toolkit.publish_gate import PublishGate


class RegistryService:
    @staticmethod
    def registry_tool_payload(generation_result: dict[str, Any], admin_approval_exists: bool) -> dict[str, Any]:
        gate = PublishGate.evaluate(generation_result, admin_approval_exists=admin_approval_exists)
        if not gate["can_publish"]:
            raise ValueError(f"Tool cannot be published: {', '.join(gate['missing_requirements'])}")
        return generation_result["tool"]
