from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any


@dataclass(slots=True)
class IntentClassification:
    operation_type: str
    workflow_type: str | None
    normalized_prompt: str
    risk_hints: list[str] = field(default_factory=list)
    read_detected: bool = False
    create_detected: bool = False
    update_detected: bool = False
    delete_detected: bool = False
    ambiguous: bool = False
    target_entity: str | None = None
    target_field: str | None = None


class IntentClassifier:
    READ_VERBS = ("retrieve", "find", "lookup", "get", "check", "view", "list", "search", "fetch", "show")
    CREATE_VERBS = ("create", "add", "insert", "open", "raise", "log", "submit", "generate")
    UPDATE_VERBS = ("update", "change", "modify", "set", "mark", "rename", "edit", "revise")
    DELETE_VERBS = ("delete", "remove", "cancel", "deactivate", "close")
    BULK_PATTERNS = re.compile(r"\b(all\s+pending\s+orders|all\s+orders|every\s+order|bulk\s+mark|mass\s+update|update\s+all\s+records)\b", flags=re.IGNORECASE)
    META_PREFIX = re.compile(
        r"^(?:please\s+)?(?:create|build|generate|make|design)\s+(?:an?\s+)?(?:mcp\s+)?(?:tool|function|capability|toolkit|mcp)\s*(?:that\s+can|which\s+can|to|for)?\s*(.+)$",
        flags=re.IGNORECASE,
    )

    @staticmethod
    def classify(prompt: str) -> IntentClassification:
        normalized = IntentClassifier.normalized_prompt(prompt)
        read_detected = IntentClassifier._has_any(normalized, IntentClassifier.READ_VERBS)
        create_detected = IntentClassifier._has_any(normalized, IntentClassifier.CREATE_VERBS)
        update_detected = IntentClassifier._has_any(normalized, IntentClassifier.UPDATE_VERBS) or bool(
            re.search(r"\b(refund|void|chargeback|reverse)\b", normalized)
            or re.search(r"\b(?:must|should|needs?\s+to|has\s+to)\s+be\s+(?:changed|updated|modified|set)\b", normalized)
        )
        delete_detected = IntentClassifier._has_any(normalized, IntentClassifier.DELETE_VERBS)
        mutation_detected = create_detected or update_detected or delete_detected
        latest_order_update = bool(update_detected and re.search(r"\blatest\s+order\b", normalized) and re.search(r"\bphone\b", normalized))
        bulk_mutation_detected = bool(IntentClassifier.BULK_PATTERNS.search(normalized))

        workflow_type = None
        operation_type = "ambiguous"
        ambiguous = False
        if bulk_mutation_detected and update_detected:
            read_detected = True
            operation_type = "bulk_update"
            workflow_type = "preview_then_bulk_update"
        elif latest_order_update:
            read_detected = True
            operation_type = "workflow"
            workflow_type = "read_then_update"
        elif read_detected and mutation_detected:
            operation_type = "workflow"
            if update_detected and not create_detected and not delete_detected:
                workflow_type = "read_then_update"
            elif create_detected and not update_detected and not delete_detected:
                workflow_type = "read_then_create"
            elif delete_detected and not create_detected and not update_detected:
                workflow_type = "read_then_delete"
            else:
                workflow_type = "multi_step_workflow"
        elif delete_detected:
            operation_type = "delete"
        elif update_detected:
            operation_type = "update"
        elif create_detected:
            operation_type = "create"
        elif read_detected:
            operation_type = "read"
        else:
            ambiguous = True

        target_field = None
        if re.search(r"\buser\s*name\b|\busername\b", normalized):
            target_field = "username"
        elif re.search(r"\border\s+status\b|\bstatus\s+of\s+the\s+order\b|\border\b.*\bstatus\b|\bpending\s+orders?\b|\borders?\s+as\s+delivered\b", normalized):
            target_field = "order_status"
        elif re.search(r"\bfull\s*name\b", normalized):
            target_field = "full_name"
        elif re.search(r"\bdisplay\s*name\b", normalized):
            target_field = "display_name"

        target_entity = None
        if re.search(r"\border|orders\b", normalized):
            target_entity = "order"
        elif re.search(r"\bcustomer|user|client\b", normalized):
            target_entity = "customer"

        risk_hints = []
        if re.search(r"\brefund\b|\bcancel\b", normalized):
            risk_hints.append("refund_or_cancel")
        if re.search(r"\bdelete\b|\bremove\b|\bdeactivate\b", normalized):
            risk_hints.append("delete_operation")
        if re.search(r"\bmedical\b|\bpatient\b|\bdiagnosis\b|\bprescription\b", normalized):
            risk_hints.append("his_sensitive_domain")

        return IntentClassification(
            operation_type=operation_type,
            workflow_type=workflow_type,
            normalized_prompt=normalized,
            risk_hints=risk_hints,
            read_detected=read_detected,
            create_detected=create_detected,
            update_detected=update_detected,
            delete_detected=delete_detected,
            ambiguous=ambiguous,
            target_entity=target_entity,
            target_field=target_field,
        )

    @staticmethod
    def normalized_prompt(prompt: str) -> str:
        text = re.sub(r"\s+", " ", (prompt or "").strip().lower())
        match = IntentClassifier.META_PREFIX.match(text)
        return match.group(1).strip() if match and match.group(1).strip() else text

    @staticmethod
    def _has_any(text: str, verbs: tuple[str, ...]) -> bool:
        return bool(re.search(r"\b(" + "|".join(re.escape(verb) for verb in verbs) + r")\b", text))

    @staticmethod
    def to_dict(classification: IntentClassification) -> dict[str, Any]:
        return {
            "operation_type": classification.operation_type,
            "workflow_type": classification.workflow_type,
            "normalized_prompt": classification.normalized_prompt,
            "risk_hints": classification.risk_hints,
            "read_detected": classification.read_detected,
            "create_detected": classification.create_detected,
            "update_detected": classification.update_detected,
            "delete_detected": classification.delete_detected,
            "ambiguous": classification.ambiguous,
            "target_entity": classification.target_entity,
            "target_field": classification.target_field,
            "bulk_mutation_detected": classification.operation_type in {"bulk_update", "workflow_bulk_update"},
        }
