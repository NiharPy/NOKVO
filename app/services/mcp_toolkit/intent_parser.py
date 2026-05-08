from __future__ import annotations

import re
from typing import Any

from app.services.mcp_toolkit.context_retriever import ContextRetriever
from app.services.mcp_toolkit.models import ToolIntent
from app.services.mcp_toolkit.safety_classifier import SafetyClassifier


class IntentParser:
    @staticmethod
    def parse(prompt: str, integration_type: str, provider: str, context: dict[str, Any]) -> ToolIntent:
        operation_type = SafetyClassifier.operation_type(prompt)
        risk_level = SafetyClassifier.risk_level(prompt, integration_type, operation_type)
        schema_context = ContextRetriever.database_schema(context) if integration_type == "database" else {}
        actions = ContextRetriever.connector_actions(context) if integration_type != "database" else []
        target_resources = IntentParser._target_resources(prompt, integration_type, schema_context, actions)
        missing_context = []
        if integration_type == "database" and not target_resources:
            missing_context.append("No verified database table/column context matched the request.")
        if integration_type != "database" and not actions:
            missing_context.append("No verified connector action/template/OpenAPI context is available for this provider.")
        required_inputs = IntentParser._required_inputs(prompt, schema_context)
        if integration_type == "database" and operation_type != "read":
            required_inputs = IntentParser._database_write_inputs(prompt, operation_type, schema_context, target_resources, required_inputs)
        return ToolIntent(
            purpose=prompt.strip(),
            integration_type=integration_type,
            provider=provider,
            operation_type=operation_type,  # type: ignore[arg-type]
            read_write_mode="read" if operation_type == "read" else "write",
            required_inputs=required_inputs,
            expected_outputs=IntentParser._expected_outputs(prompt, schema_context),
            target_resources=target_resources,
            business_preconditions=IntentParser._preconditions(prompt, operation_type),
            risk_level=risk_level,
            human_approval_needed=SafetyClassifier.approval_required(risk_level, operation_type),
            missing_context=missing_context,
        )

    @staticmethod
    def _target_resources(prompt: str, integration_type: str, schema_context: dict[str, Any], actions: list[dict[str, Any]]) -> list[str]:
        text = SafetyClassifier.action_text(prompt)
        if integration_type == "database":
            direct_matches = []
            column_matches = []
            for table in schema_context.get("tables", []):
                table_tokens = IntentParser._resource_tokens(table["table"])
                table_tokens.update({table["table"].lower(), table["fqn"].lower(), table["table"].lower().rstrip("s")})
                column_match = any(column["name"].lower() in text for column in table.get("columns", []))
                token_match = any(token and re.search(rf"\b{re.escape(token)}\b", text) for token in table_tokens)
                if token_match:
                    direct_matches.append(table["fqn"])
                elif column_match:
                    column_matches.append(table["fqn"])
            matches = direct_matches or column_matches
            return IntentParser._expand_related_resources(matches, schema_context, text)[:6]
        ranked = []
        for action in actions:
            haystack = " ".join(str(action.get(key) or "") for key in ("name", "endpoint", "module", "description")).lower()
            score = sum(1 for word in re.findall(r"[a-z0-9]+", text) if len(word) > 3 and word in haystack)
            if score:
                ranked.append((score, action))
        ranked.sort(key=lambda item: item[0], reverse=True)
        return [str(item[1].get("name") or item[1].get("endpoint")) for item in ranked[:3]]

    @staticmethod
    def _required_inputs(prompt: str, schema_context: dict[str, Any]) -> list[dict[str, Any]]:
        text = SafetyClassifier.action_text(prompt)
        inputs: list[dict[str, Any]] = []
        for label, pattern, schema in [
            ("customer_name", r"\b(customer\s+name|full\s+name|by\s+name|named)\b", {"type": "string", "minLength": 2, "maxLength": 120}),
            ("phone_number", r"\b(phone|mobile)\b", {"type": "string", "pattern": "^[+0-9()\\-\\s]{7,20}$", "minLength": 7, "maxLength": 20}),
            ("email", r"\b(email)\b", {"type": "string", "format": "email", "maxLength": 254}),
            ("order_id", r"\b(order[_\s-]?id|by\s+order|for\s+order\s+#?)\b", {"type": "string", "minLength": 1, "maxLength": 80}),
            ("ticket_id", r"\b(ticket[_\s-]?id|by\s+ticket|for\s+ticket\s+#?)\b", {"type": "string", "minLength": 1, "maxLength": 80}),
            ("limit", r"\b(list|search|find|query|fetch|get)\b", {"type": "integer", "minimum": 1, "maximum": 100, "default": 25}),
        ]:
            if re.search(pattern, text):
                inputs.append({"name": label, **schema})
        if not inputs and schema_context.get("tables"):
            searchable = []
            for table in schema_context["tables"]:
                for column in table.get("columns", []):
                    if any(token in column["name"].lower() for token in ("id", "name", "email", "phone", "status")):
                        searchable.append(column["name"])
            if searchable:
                inputs.append({"name": searchable[0], "type": "string", "minLength": 1, "maxLength": 120})
        return inputs

    @staticmethod
    def _expected_outputs(prompt: str, schema_context: dict[str, Any]) -> list[dict[str, Any]]:
        outputs: list[dict[str, Any]] = []
        for table in schema_context.get("tables", [])[:3]:
            for column in table.get("columns", [])[:8]:
                outputs.append({"name": column["name"], "type": IntentParser._json_type(column.get("type", "")), "source": column["fqn"]})
        if outputs:
            return outputs[:20]
        return [
            {"name": "success", "type": "boolean"},
            {"name": "message", "type": "string"},
            {"name": "provider_reference", "type": "string"},
        ]

    @staticmethod
    def _json_type(db_type: str) -> str:
        value = db_type.lower()
        if any(token in value for token in ("int", "numeric", "decimal", "float", "double", "real")):
            return "number"
        if "bool" in value:
            return "boolean"
        return "string"

    @staticmethod
    def _preconditions(prompt: str, operation_type: str) -> list[str]:
        preconditions = ["current organization and tenant integration must be active"]
        if operation_type != "read":
            preconditions.extend(["admin-approved business policy must allow this mutation", "caller must provide an idempotency key when supported"])
        return preconditions

    @staticmethod
    def _database_write_inputs(
        prompt: str,
        operation_type: str,
        schema_context: dict[str, Any],
        target_resources: list[str],
        existing: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        text = SafetyClassifier.action_text(prompt)
        table = next((item for item in schema_context.get("tables", []) if item["fqn"] in target_resources), None)
        if not table:
            return existing
        columns = {column["name"]: column for column in table.get("columns", [])}
        fields: dict[str, dict[str, Any]] = {
            str(item.get("name")): item
            for item in existing
            if item.get("name") in columns or item.get("name") == "limit"
        }
        id_column = next((name for name in columns if name in {"id", f"{table['table'].rstrip('s')}_id"} or name.endswith("_id")), None)
        if operation_type == "create" and id_column:
            fields.pop(id_column, None)
        if operation_type in {"update", "delete"} and id_column:
            fields[id_column] = {"name": id_column, "type": "string", "minLength": 1, "maxLength": 80, "required": True}
        for column_name, column in columns.items():
            lowered = column_name.lower()
            if column_name == id_column:
                continue
            if lowered in text or any(token in text and token in lowered for token in ("phone", "email", "status", "address", "name")):
                field_schema: dict[str, Any] = {"name": column_name, "type": IntentParser._json_type(column.get("type", "")), "required": operation_type != "delete"}
                if field_schema["type"] == "string":
                    field_schema.update({"minLength": 1, "maxLength": 240})
                if "phone" in lowered:
                    field_schema.update({"pattern": "^[+0-9()\\-\\s]{7,20}$", "maxLength": 20})
                if "email" in lowered:
                    field_schema.update({"format": "email", "maxLength": 254})
                fields[column_name] = field_schema
        if operation_type == "create":
            writable_fields = [name for name in fields if name in columns and name != id_column]
            for column_name, column in columns.items():
                if writable_fields:
                    break
                if column_name == id_column:
                    continue
                if column.get("nullable") is False or column_name.lower() in {"subject", "title", "status", "description", "message", "name", "full_name"}:
                    field_schema = {"name": column_name, "type": IntentParser._json_type(column.get("type", "")), "required": column.get("nullable") is False}
                    if field_schema["type"] == "string":
                        field_schema.update({"minLength": 1, "maxLength": 240})
                    fields[column_name] = field_schema
                    writable_fields.append(column_name)
        fields["idempotency_key"] = {"name": "idempotency_key", "type": "string", "minLength": 8, "maxLength": 120, "required": True}
        return list(fields.values())

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

    @staticmethod
    def _expand_related_resources(matches: list[str], schema_context: dict[str, Any], text: str) -> list[str]:
        if not matches or not re.search(r"\b(related|associated|linked|history|records|details|summary)\b", text):
            return matches
        graph: dict[str, set[str]] = {table["fqn"]: set() for table in schema_context.get("tables", [])}
        column_sets = {
            table["fqn"]: {column["name"] for column in table.get("columns", []) if column["name"] == "id" or column["name"].endswith("_id")}
            for table in schema_context.get("tables", [])
        }
        for left, left_columns in column_sets.items():
            for right, right_columns in column_sets.items():
                if left >= right:
                    continue
                if left_columns & right_columns:
                    graph[left].add(right)
                    graph[right].add(left)
        ordered = list(matches)
        frontier = list(matches)
        for _ in range(2):
            next_frontier = []
            for table in frontier:
                for related in sorted(graph.get(table, set())):
                    if related not in ordered:
                        ordered.append(related)
                        next_frontier.append(related)
            frontier = next_frontier
        return ordered
