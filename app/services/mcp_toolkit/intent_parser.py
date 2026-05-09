from __future__ import annotations

import re
from typing import Any

from app.services.mcp_toolkit.context_retriever import ContextRetriever
from app.services.mcp_toolkit.intent_classifier import IntentClassifier
from app.services.mcp_toolkit.models import ToolIntent
from app.services.mcp_toolkit.safety_classifier import SafetyClassifier


class IntentParser:
    @staticmethod
    def parse(prompt: str, integration_type: str, provider: str, context: dict[str, Any]) -> ToolIntent:
        classification = IntentClassifier.classify(prompt)
        latest_order_requested = bool(re.search(r"\blatest\s+order\b", classification.normalized_prompt))
        latest_order_policy = IntentParser._latest_order_policy(context)
        bulk_mutation_detected = classification.operation_type in {"bulk_update", "workflow_bulk_update"}
        bulk_policy = IntentParser._bulk_mutation_policy(context)
        verified_resources = ContextRetriever.verified_resources(context)
        schema_context = ContextRetriever.verified_database_schema(context) if integration_type == "database" else {}
        actions = ContextRetriever.connector_actions(context) if integration_type != "database" else []
        target_resources = IntentParser._target_resources(classification.normalized_prompt, integration_type, verified_resources, schema_context, actions)
        required_inputs = IntentParser._required_inputs(classification.normalized_prompt, schema_context)
        if classification.operation_type != "read":
            required_inputs = [field for field in required_inputs if field.get("name") != "limit"]
        missing_context: list[str] = []
        resolved_target_column: str | None = None

        if integration_type == "database" and not verified_resources:
            missing_context.append("selected connected integration context is required")
        if integration_type == "database" and not target_resources:
            missing_context.append("No verified database table/column context matched the request.")
        if integration_type != "database" and not verified_resources:
            missing_context.append("No verified connector action/template/OpenAPI context is available for this provider.")

        target_field = classification.target_field
        if integration_type == "database" and classification.operation_type in {"create", "update", "workflow", "delete", "bulk_update", "workflow_bulk_update"}:
            required_inputs = IntentParser._database_mutation_inputs(
                classification.normalized_prompt,
                classification.operation_type,
                schema_context,
                target_resources,
                required_inputs,
            )
            if target_field == "username":
                resolved_target_column = IntentParser._resolved_username_column(context, schema_context, target_resources)
                if not resolved_target_column:
                    missing_context.append("No verified username column found. Confirm whether full_name should be used as the username field.")
            elif target_field == "order_status":
                resolved_target_column = IntentParser._resolved_database_column(schema_context, target_resources, ("order_status", "status"))
                if not resolved_target_column:
                    missing_context.append("No verified order_status column found in selected integration context.")
                if bulk_mutation_detected:
                    if not bulk_policy.get("approved"):
                        missing_context.append("Approved bulk mutation policy is required before generating executable UPDATE SQL.")
                    if not IntentParser._bulk_status_values(classification.normalized_prompt):
                        missing_context.append("Could not extract source and target order status values from prompt.")
                if latest_order_requested:
                    if not latest_order_policy.get("approved"):
                        missing_context.append("Confirm whether latest order means the order with the greatest created_at for the customer.")
                    elif not IntentParser._resolved_database_column(schema_context, target_resources, ("created_at",)):
                        missing_context.append("No verified created_at column found for latest-order ordering policy.")
            elif target_field == "full_name":
                resolved_target_column = IntentParser._resolved_database_column(schema_context, target_resources, ("full_name", "name", "customer_name"))
                if not resolved_target_column:
                    missing_context.append("No verified full_name column found in selected integration context.")

        operation_type = classification.operation_type
        if missing_context and operation_type == "ambiguous":
            operation_type = "read"
        risk_level = SafetyClassifier.risk_level(prompt, integration_type, operation_type)
        if classification.operation_type == "workflow" and risk_level == "low":
            risk_level = "medium"
        if bulk_mutation_detected:
            risk_level = "high"
        if target_field == "username" and classification.operation_type in {"update", "workflow"} and risk_level == "low":
            risk_level = "medium"
        if target_field == "order_status" and classification.operation_type in {"update", "workflow"} and risk_level == "low":
            risk_level = "medium"
        if target_field == "full_name" and classification.operation_type in {"update", "workflow"} and risk_level == "low":
            risk_level = "medium"

        return ToolIntent(
            purpose=prompt.strip(),
            integration_type=integration_type,
            provider=provider,
            operation_type=operation_type,  # type: ignore[arg-type]
            read_write_mode="read" if operation_type == "read" else "write",
            required_inputs=required_inputs,
            expected_outputs=IntentParser._expected_outputs(
                classification.normalized_prompt,
                schema_context,
                target_resources,
                operation_type,
                target_field,
                resolved_target_column,
            ),
            target_resources=target_resources,
            business_preconditions=IntentParser._preconditions(classification.normalized_prompt, operation_type, classification.workflow_type, target_field, latest_order_requested),
            risk_level=risk_level,
            human_approval_needed=SafetyClassifier.approval_required(risk_level, operation_type),
            workflow_type=classification.workflow_type,
            target_entity=classification.target_entity,
            target_field=target_field,
            resolved_target_column=resolved_target_column,
            intent_signals={
                **IntentClassifier.to_dict(classification),
                "latest_order_requested": latest_order_requested,
                "latest_order_policy_approved": bool(latest_order_policy.get("approved")),
                "latest_order_policy_column": latest_order_policy.get("column"),
                "bulk_mutation_detected": bulk_mutation_detected,
                "bulk_mutation_policy_approved": bool(bulk_policy.get("approved")),
                **IntentParser._bulk_status_values(classification.normalized_prompt),
            },
            missing_context=list(dict.fromkeys(missing_context)),
        )

    @staticmethod
    def _target_resources(
        text: str,
        integration_type: str,
        verified_resources: list[dict[str, Any]],
        schema_context: dict[str, Any],
        actions: list[dict[str, Any]],
    ) -> list[str]:
        if integration_type == "database":
            table_resources = [resource for resource in verified_resources if resource.get("resource_type") == "table" and resource.get("name")]
            if not table_resources:
                return []
            names = [str(resource["name"]) for resource in table_resources]
            if re.search(r"\b(all\s+pending\s+orders|all\s+orders|every\s+order|bulk\s+mark|mass\s+update|update\s+all\s+records|orders?\s+as\s+delivered)\b", text):
                ordered = [name for name in names if "order" in name]
                return ordered[:2]
            if re.search(r"\bcustomer\b", text) and re.search(r"\bphone\b", text) and re.search(r"\buser\s*name\b|\busername\b|\bfull\s*name\b", text):
                ordered = [name for name in names if "customer" in name]
                if re.search(r"\border|orders\b", text):
                    ordered.extend(name for name in names if "order" in name and name not in ordered)
                if re.search(r"\bticket|support|case\b", text):
                    ordered.extend(name for name in names if any(token in name for token in ("support_ticket", "ticket")) and name not in ordered)
                if re.search(r"\bcall|history|logs\b", text):
                    ordered.extend(name for name in names if any(token in name for token in ("call", "log")) and name not in ordered)
                return ordered[:6]
            if re.search(r"\border|orders\b", text) and re.search(r"\bstatus\b", text) and re.search(r"\bchange|update|modify|must\s+be\s+changed\b", text):
                ordered = [name for name in names if "order" in name]
                ordered.extend(name for name in names if "customer" in name and name not in ordered)
                return ordered[:4]
            if re.search(r"\bcustomer\b", text) and re.search(r"\bphone\b", text):
                ordered = [name for name in names if "customer" in name]
                if re.search(r"\b(order|orders|related|details|summary|record|records)\b", text):
                    ordered.extend(name for name in names if "order" in name and name not in ordered)
                if re.search(r"\b(related|details|summary|record|records)\b", text):
                    ordered.extend(name for name in names if any(token in name for token in ("support_ticket", "ticket", "call")) and name not in ordered)
                if re.search(r"\b(ticket|support|case|call|history)\b", text):
                    ordered.extend(name for name in names if any(token in name for token in ("support_ticket", "ticket", "call")) and name not in ordered)
                return ordered[:6]
            if re.search(r"\border\b", text) and re.search(r"\bstatus\b", text):
                ordered = [name for name in names if "customer" in name or "order" in name]
                return ordered or names[:2]
            if re.search(r"\buser\s*name\b|\busername\b|\bfull\s*name\b", text):
                ordered = [name for name in names if "customer" in name or "user" in name]
                return ordered[:2]
            direct = []
            for table in schema_context.get("tables", []):
                if any(re.search(rf"\b{re.escape(token)}\b", text) for token in IntentParser._resource_tokens(table["table"])):
                    direct.append(table["fqn"])
            return direct or names[:1]

        ranked = []
        for resource in verified_resources:
            resource_name = str(resource.get("name") or "")
            payload_text = json_text = " ".join(
                str(resource.get(key) or "")
                for key in ("name", "resource_type", "provider", "integration_type")
            ).lower()
            score = sum(1 for word in re.findall(r"[a-z0-9]+", text) if len(word) > 3 and word in (resource_name.lower() + " " + json_text))
            if score:
                ranked.append((score, resource_name))
        if ranked:
            ranked.sort(key=lambda item: item[0], reverse=True)
            return [item[1] for item in ranked[:3]]
        return [str(item.get("name") or item.get("endpoint")) for item in actions[:3] if item.get("name") or item.get("endpoint")]

    @staticmethod
    def _required_inputs(text: str, schema_context: dict[str, Any]) -> list[dict[str, Any]]:
        inputs: list[dict[str, Any]] = []
        for label, pattern, schema in [
            ("customer_name", r"\b(customer\s+name|full\s+name|by\s+name|named|their\s+name|using\s+their\s+name|user\s+name)\b", {"type": "string", "minLength": 2, "maxLength": 120}),
            ("phone_number", r"\b(phone|mobile)\b", {"type": "string", "pattern": "^[+0-9()\\-\\s]{7,20}$", "minLength": 7, "maxLength": 20}),
            ("email", r"\b(email)\b", {"type": "string", "format": "email", "maxLength": 254}),
            ("order_id", r"\b(order[_\s-]?id|by\s+order|for\s+order\s+#?)\b", {"type": "string", "minLength": 1, "maxLength": 80}),
            ("ticket_id", r"\b(ticket[_\s-]?id|by\s+ticket|for\s+ticket\s+#?)\b", {"type": "string", "minLength": 1, "maxLength": 80}),
            ("new_username", r"\b(user\s*name|username)\b", {"type": "string", "minLength": 3, "maxLength": 120}),
            ("new_order_status", r"\b(status\s+must\s+be\s+changed|change\s+(?:the\s+)?status|update\s+(?:the\s+)?(?:latest\s+)?order\s+status|new\s+order\s+status)\b", {"type": "string", "minLength": 2, "maxLength": 80}),
        ]:
            if re.search(pattern, text):
                inputs.append({"name": label, **schema})
        if re.search(r"\b(list|search|find|query|fetch|get|retrieve|lookup|show|check)\b", text):
            inputs.append({"name": "limit", "type": "integer", "minimum": 1, "maximum": 100, "default": 25})
        if not inputs and schema_context.get("tables") and re.search(r"\b(find|lookup|retrieve|get|check|view|list|search|fetch|show)\b", text):
            for table in schema_context["tables"]:
                for column in table.get("columns", []):
                    if any(token in column["name"].lower() for token in ("id", "name", "email", "phone", "status")):
                        inputs.append({"name": column["name"], "type": "string", "minLength": 1, "maxLength": 120})
                        return inputs
        deduped: list[dict[str, Any]] = []
        seen = set()
        for field in inputs:
            name = str(field.get("name") or "")
            if name and name not in seen:
                deduped.append(field)
                seen.add(name)
        return deduped

    @staticmethod
    def _expected_outputs(
        text: str,
        schema_context: dict[str, Any],
        target_resources: list[str],
        operation_type: str,
        target_field: str | None,
        resolved_target_column: str | None = None,
    ) -> list[dict[str, Any]]:
        tables = [table for table in schema_context.get("tables", []) if table["fqn"] in target_resources]
        if operation_type in {"update", "workflow"} and target_field == "username":
            outputs = [{"name": "affected_count", "type": "integer"}]
            resolved_target_name = (resolved_target_column or "").split(".")[-1]
            for table in tables[:1]:
                for column in table.get("columns", []):
                    if column["name"] in {"customer_id", "customer_code", "username", "user_name", "full_name", "phone"}:
                        alias = "updated_username" if column["name"] in {"username", "user_name", resolved_target_name} else column["name"]
                        outputs.append({"name": alias, "type": IntentParser._json_type(column.get("type", "")), "source": column["fqn"], "raw_name": column["name"]})
            return outputs
        if operation_type in {"update", "workflow"} and target_field == "order_status":
            outputs = [{"name": "affected_count", "type": "integer"}]
            resolved_target_name = (resolved_target_column or "").split(".")[-1]
            for table in tables:
                for column in table.get("columns", []):
                    if column["name"] in {"order_id", "order_number", "customer_code", "phone", "order_status", "status"}:
                        alias = "updated_order_status" if column["name"] == resolved_target_name else column["name"]
                        outputs.append({"name": alias, "type": IntentParser._json_type(column.get("type", "")), "source": column["fqn"], "raw_name": column["name"]})
            return outputs
        if operation_type in {"update", "workflow"} and target_field == "full_name":
            outputs = [{"name": "affected_count", "type": "integer"}]
            resolved_target_name = (resolved_target_column or "").split(".")[-1]
            for table in tables[:1]:
                for column in table.get("columns", []):
                    if column["name"] in {"customer_id", "customer_code", "full_name", "name", "customer_name", "phone"}:
                        alias = "updated_full_name" if column["name"] == resolved_target_name else column["name"]
                        outputs.append({"name": alias, "type": IntentParser._json_type(column.get("type", "")), "source": column["fqn"], "raw_name": column["name"]})
            return outputs
        if operation_type in {"bulk_update", "workflow_bulk_update"} and target_field == "order_status":
            outputs = [{"name": "affected_count", "type": "integer"}]
            resolved_target_name = (resolved_target_column or "").split(".")[-1]
            for table in tables[:1]:
                for column in table.get("columns", []):
                    if column["name"] in {"order_id", "order_number", "order_status", "status"}:
                        alias = "updated_order_status" if column["name"] == resolved_target_name else column["name"]
                        outputs.append({"name": alias, "type": IntentParser._json_type(column.get("type", "")), "source": column["fqn"], "raw_name": column["name"]})
            return outputs
        outputs: list[dict[str, Any]] = []
        for table in tables:
            for column in table.get("columns", [])[:8]:
                outputs.append({"name": column["name"], "type": IntentParser._json_type(column.get("type", "")), "source": column["fqn"]})
        return outputs[:20]

    @staticmethod
    def _json_type(db_type: str) -> str:
        value = db_type.lower()
        if any(token in value for token in ("int", "numeric", "decimal", "float", "double", "real")):
            return "integer" if "int" in value else "number"
        if "bool" in value:
            return "boolean"
        return "string"

    @staticmethod
    def _preconditions(text: str, operation_type: str, workflow_type: str | None, target_field: str | None, latest_order_requested: bool = False) -> list[str]:
        preconditions = ["current organization and tenant integration must be active"]
        if operation_type != "read":
            preconditions.extend(["admin-approved business policy must allow this mutation", "caller must provide an idempotency key when supported"])
        if operation_type in {"bulk_update", "workflow_bulk_update"}:
            preconditions.extend([
                "approved bulk mutation policy must exist",
                "dry-run preview must be reviewed before execution",
                "max_rows must cap the number of rows affected",
                "rollback or compensation plan must be documented",
            ])
        if workflow_type:
            preconditions.append("workflow steps must execute in declared order without dropping user intent")
        if target_field == "username":
            preconditions.append("username target field must be explicitly verified in selected integration context")
        if target_field == "order_status":
            if latest_order_requested:
                preconditions.append("latest-order policy must explicitly define latest as greatest created_at for the customer")
            else:
                preconditions.append("if multiple orders match customer_name and phone_number, caller must provide order_id or order_number confirmation before updating order_status")
        return preconditions

    @staticmethod
    def _database_mutation_inputs(
        text: str,
        operation_type: str,
        schema_context: dict[str, Any],
        target_resources: list[str],
        existing: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        tables = [item for item in schema_context.get("tables", []) if item["fqn"] in target_resources]
        if not tables:
            return existing
        columns = {
            column["name"]: column
            for table in tables
            for column in table.get("columns", [])
        }
        fields = {str(field["name"]): dict(field) for field in existing if field.get("name")}
        if operation_type in {"bulk_update", "workflow_bulk_update"}:
            return [
                {"name": "idempotency_key", "type": "string", "minLength": 8, "maxLength": 120, "required": True},
                {"name": "approval_reason", "type": "string", "minLength": 10, "maxLength": 500, "required": True, "runtime_control": "bulk_approval_reason"},
                {"name": "dry_run", "type": "boolean", "required": True, "default": True, "runtime_control": "bulk_preview_required"},
                {"name": "max_rows", "type": "integer", "minimum": 1, "maximum": 10000, "required": True, "runtime_control": "bulk_batch_limit"},
                {"name": "effective_before", "type": "string", "format": "date-time", "required": False, "runtime_control": "optional_bulk_cutoff"},
            ]
        if operation_type in {"update", "workflow"} and "phone_number" not in fields and any(name in columns for name in {"phone", "mobile", "phone_number"}):
            fields["phone_number"] = {"name": "phone_number", "type": "string", "pattern": "^[+0-9()\\-\\s]{7,20}$", "minLength": 7, "maxLength": 20}
        latest_order_requested = bool(re.search(r"\blatest\s+order\b", text))
        if operation_type in {"update", "workflow"} and not latest_order_requested and re.search(r"\border|orders\b", text) and "customer_name" not in fields and any(name in columns for name in {"full_name", "name", "customer_name"}):
            fields["customer_name"] = {"name": "customer_name", "type": "string", "minLength": 2, "maxLength": 120}
        if re.search(r"\buser\s*name\b|\busername\b", text):
            fields["new_username"] = {"name": "new_username", "type": "string", "minLength": 3, "maxLength": 120, "required": True}
        if re.search(r"\bfull\s*name\b|\bnew\s+name\b", text) and not re.search(r"\buser\s*name\b|\busername\b", text):
            fields.pop("customer_name", None)
            fields["new_full_name"] = {"name": "new_full_name", "type": "string", "minLength": 2, "maxLength": 120, "required": True}
        if re.search(r"\border\s+status\b|\bstatus\s+of\s+the\s+order\b|\border\b.*\bstatus\b", text):
            fields["new_order_status"] = {"name": "new_order_status", "type": "string", "minLength": 2, "maxLength": 80, "required": True}
            if latest_order_requested:
                fields.pop("customer_name", None)
                fields.pop("order_id", None)
                fields.pop("order_number", None)
            elif "order_id" not in fields and any(name in columns for name in {"order_id", "order_number"}):
                fields["order_id"] = {"name": "order_id", "type": "string", "minLength": 1, "maxLength": 80, "required": False, "runtime_control": "required_when_multiple_matches"}
                fields.setdefault("order_number", {"name": "order_number", "type": "string", "minLength": 1, "maxLength": 80, "required": False, "runtime_control": "optional_order_disambiguation"})
        if operation_type == "create":
            for column_name, column in columns.items():
                if column_name.endswith("_id"):
                    continue
                if column.get("nullable") is False and column_name not in fields:
                    fields[column_name] = {"name": column_name, "type": IntentParser._json_type(column.get("type", "")), "required": True}
        fields["idempotency_key"] = {"name": "idempotency_key", "type": "string", "minLength": 8, "maxLength": 120, "required": True}
        return list(fields.values())

    @staticmethod
    def _latest_order_policy(context: dict[str, Any]) -> dict[str, Any]:
        policy = context.get("admin_policy_confirmations") or context.get("admin_field_confirmations") or {}
        if not isinstance(policy, dict):
            policy = {}
        value = policy.get("latest_order_policy") or policy.get("latest_order_by_phone")
        approved = bool(context.get("latest_order_policy_approved") or value in {True, "created_at_desc", "greatest_created_at"})
        return {"approved": approved, "column": "created_at" if approved else None}

    @staticmethod
    def _bulk_mutation_policy(context: dict[str, Any]) -> dict[str, Any]:
        policy = context.get("admin_policy_confirmations") or {}
        if not isinstance(policy, dict):
            policy = {}
        value = policy.get("bulk_mutation_policy") or policy.get("bulk_order_status_policy")
        return {"approved": bool(context.get("bulk_mutation_policy_approved") or value in {True, "approved", "order_status_pending_to_delivered"})}

    @staticmethod
    def _bulk_status_values(text: str) -> dict[str, str]:
        if re.search(r"\bpending\b", text) and re.search(r"\bdelivered\b", text):
            return {"source_status": "pending", "target_status": "delivered"}
        return {}

    @staticmethod
    def _has_verified_target_field(schema_context: dict[str, Any], target_resources: list[str], field_names: set[str]) -> bool:
        for table in schema_context.get("tables", []):
            if table["fqn"] not in target_resources:
                continue
            if any(column["name"] in field_names for column in table.get("columns", [])):
                return True
        return False

    @staticmethod
    def _resolved_username_column(context: dict[str, Any], schema_context: dict[str, Any], target_resources: list[str]) -> str | None:
        username_column = IntentParser._resolved_database_column(schema_context, target_resources, ("username", "user_name"))
        if username_column:
            return username_column
        confirmation = context.get("admin_field_confirmations") or {}
        confirmed = confirmation.get("username_field") if isinstance(confirmation, dict) else None
        if confirmed == "full_name":
            full_name_column = IntentParser._resolved_database_column(schema_context, target_resources, ("full_name",))
            if full_name_column:
                return full_name_column
        return None

    @staticmethod
    def _resolved_database_column(schema_context: dict[str, Any], target_resources: list[str], field_names: tuple[str, ...]) -> str | None:
        for table in schema_context.get("tables", []):
            if table["fqn"] not in target_resources:
                continue
            for column in table.get("columns", []):
                if column["name"] in field_names:
                    return column["fqn"]
        return None

    @staticmethod
    def _resource_tokens(name: str) -> set[str]:
        parts = set(filter(None, re.split(r"[^a-z0-9]+", name.lower())))
        tokens = set(parts)
        tokens.add(name.lower())
        tokens.add(name.lower().rstrip("s"))
        for part in parts:
            tokens.add(part.rstrip("s"))
            if part.endswith("ies"):
                tokens.add(f"{part[:-3]}y")
        return tokens
