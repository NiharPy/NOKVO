from __future__ import annotations

import re

from app.services.mcp_toolkit.models import RiskLevel


class SafetyClassifier:
    READ_VERBS = ("find", "lookup", "search", "retrieve", "check", "get", "view", "list", "fetch")
    CREATE_VERBS = ("create", "add", "insert", "open", "raise", "log")
    UPDATE_VERBS = ("change", "modify", "update", "mark")
    DELETE_VERBS = ("remove", "delete", "cancel")

    @staticmethod
    def operation_type(prompt: str) -> str:
        text = SafetyClassifier.action_text(prompt)
        if re.search(r"\b(" + "|".join(SafetyClassifier.READ_VERBS) + r")\b", text):
            return "read"
        if re.search(r"\b(" + "|".join(SafetyClassifier.DELETE_VERBS) + r")\b", text):
            return "delete"
        if re.search(r"\b(refund|cancel|void|chargeback|reverse)\b", text):
            return "update"
        if re.search(r"\b(" + "|".join(SafetyClassifier.CREATE_VERBS) + r")\b", text):
            return "create"
        if re.search(r"\b(" + "|".join(SafetyClassifier.UPDATE_VERBS) + r"|patch|assign|close)\b", text):
            return "update"
        return "read"

    @staticmethod
    def action_text(prompt: str) -> str:
        text = re.sub(r"\s+", " ", (prompt or "").strip().lower())
        meta_patterns = [
            r"^(?:please\s+)?(?:create|build|generate|make|design)\s+(?:an?\s+)?(?:mcp\s+)?(?:tool|function|capability|mcp)\s+(?:that\s+can\s+|which\s+can\s+|to\s+|for\s+)?(.+)$",
            r"^(?:please\s+)?(?:create|build|generate|make|design)\s+(?:an?\s+)?(?:toolkit|mcp)\s+(?:tool\s+)?(?:that\s+can\s+|which\s+can\s+|to\s+|for\s+)?(.+)$",
        ]
        for pattern in meta_patterns:
            match = re.match(pattern, text)
            if match and match.group(1).strip():
                return match.group(1).strip()
        return text

    @staticmethod
    def risk_level(prompt: str, integration_type: str, operation_type: str) -> RiskLevel:
        text = (prompt or "").lower()
        if re.search(r"\b(refund|cancel|address|account|bank|kyc|invoice|shipment cancellation)\b", text):
            return "high"
        if re.search(r"\b(payment|medical|diagnosis|prescription|legal|compliance|delete|drop|truncate|password|secret)\b", text):
            return "critical"
        if operation_type in {"create", "update", "delete"}:
            return "medium"
        if integration_type in {"payments", "his"}:
            return "high"
        return "low"

    @staticmethod
    def approval_required(risk_level: str, operation_type: str) -> bool:
        return risk_level in {"medium", "high", "critical"} or operation_type in {"create", "update", "delete", "workflow"}

    @staticmethod
    def execution_mode(operation_type: str, approval_required: bool) -> str:
        if operation_type == "read":
            return "read_only"
        return "write_requires_human_approval" if approval_required else "write_reviewed"
