from __future__ import annotations

import re

from app.services.mcp_toolkit.intent_classifier import IntentClassifier
from app.services.mcp_toolkit.models import RiskLevel


class SafetyClassifier:
    READ_VERBS = ("find", "lookup", "search", "retrieve", "check", "get", "view", "list", "fetch")
    CREATE_VERBS = ("create", "add", "insert", "open", "raise", "log")
    UPDATE_VERBS = ("change", "modify", "update", "mark")
    DELETE_VERBS = ("remove", "delete", "cancel")

    @staticmethod
    def operation_type(prompt: str) -> str:
        return IntentClassifier.classify(prompt).operation_type

    @staticmethod
    def action_text(prompt: str) -> str:
        return IntentClassifier.normalized_prompt(prompt)

    @staticmethod
    def risk_level(prompt: str, integration_type: str, operation_type: str) -> RiskLevel:
        text = (prompt or "").lower()
        if operation_type == "workflow":
            return "medium" if integration_type not in {"payments", "his"} else "high"
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
