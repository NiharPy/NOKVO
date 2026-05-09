from __future__ import annotations

import re
from typing import Any

from app.services.mcp_toolkit.capability_registry import ProviderCapability
from app.services.mcp_toolkit.compilers import ExecutionCompilerRegistry
from app.services.mcp_toolkit.context_retriever import ContextRetriever
from app.services.mcp_toolkit.models import ToolPlan
from app.services.mcp_toolkit.pii_policy import PIIPolicyBuilder


BLOCKED_READ_STATEMENTS = [
    "DROP",
    "ALTER",
    "CREATE",
    "TRUNCATE",
    "GRANT",
    "REVOKE",
    "COPY",
    "EXECUTE",
    "CALL",
    "MERGE",
    "REPLACE",
    "VACUUM",
    "ANALYZE",
    "COMMIT",
    "ROLLBACK",
    "INSERT",
    "UPDATE",
    "DELETE",
]

DANGEROUS_SQL_PATTERN = re.compile(r"\b(" + "|".join(BLOCKED_READ_STATEMENTS) + r")\b", re.IGNORECASE)


class ExecutionGenerator:
    OPERATION_SQL_STATEMENT = {"read": "SELECT", "create": "INSERT", "update": "UPDATE", "delete": "DELETE", "bulk_update": "SELECT", "workflow_bulk_update": "SELECT"}

    @staticmethod
    def generate(plan: ToolPlan, context: dict[str, Any], capability: ProviderCapability | None = None) -> dict[str, Any]:
        if capability is None:
            if plan.integration_type == "database":
                return ExecutionGenerator._database(plan, context)
            return ExecutionGenerator._api(plan, context)
        fallback = ExecutionGenerator._database(plan, context) if capability.execution_backend == "database_sql" else ExecutionGenerator._api(plan, context)
        return ExecutionCompilerRegistry.compile(plan, context, capability, fallback)

    @staticmethod
    def _database(plan: ToolPlan, context: dict[str, Any]) -> dict[str, Any]:
        schema_context = ContextRetriever.verified_database_schema(context)
        relationships = ExecutionGenerator.relationships(schema_context)
        sql_statement = ExecutionGenerator.OPERATION_SQL_STATEMENT.get(plan.operation_type, "SELECT")
        mapping: dict[str, Any] = {
            "sql_template": "",
            "allowed_statements": [sql_statement],
            "blocked_statements": BLOCKED_READ_STATEMENTS if plan.operation_type == "read" else [stmt for stmt in BLOCKED_READ_STATEMENTS if stmt != sql_statement],
            "allowed_tables": plan.exact_resources_used,
            "allowed_columns": {table: schema_context["allowed_columns"].get(table, []) for table in plan.exact_resources_used},
            "relationships": relationships,
            "search_strategy": ExecutionGenerator.search_strategy(plan, schema_context),
            "parameter_binding": {"style": "named", "sources": [f"input.{field['name']}" for field in plan.input_fields if field.get("name")]},
            "limits": ExecutionGenerator.limits(),
            "tenant_scope": ExecutionGenerator.tenant_scope(),
            "explain_validation": {"required_before_publish": True, "statement": "EXPLAIN <generated parameterized SQL with safe sample bindings>"},
        }
        if plan.operation_type == "read":
            mapping["sql_template"] = ExecutionGenerator.fixed_select_sql(plan, schema_context)
            mapping["dry_run_strategy"] = "run EXPLAIN and execute only with LIMIT using bound parameters"
        elif plan.operation_type == "workflow":
            mapping["allowed_statements"] = ["UPDATE"]
            mapping["blocked_statements"] = [stmt for stmt in BLOCKED_READ_STATEMENTS if stmt != "UPDATE"]
            mapping["sql_template"] = ExecutionGenerator.fixed_workflow_sql(plan, schema_context)
            precheck = ExecutionGenerator.workflow_precheck(plan, schema_context)
            if precheck:
                mapping["precheck"] = precheck
            mapping["preconditions"] = plan.preconditions
            mapping["requires_human_approval"] = True
            mapping["strict_where_required"] = True
            mapping["idempotency"] = {"required": True, "source": "input.idempotency_key"}
            mapping["rollback_or_deactivation"] = "admin must define compensating action before publish"
        elif plan.operation_type in {"bulk_update", "workflow_bulk_update"}:
            policy_approved = bool((plan.intent_signals or {}).get("bulk_mutation_policy_approved"))
            mapping["allowed_statements"] = ["UPDATE"] if policy_approved else ["SELECT"]
            mapping["blocked_statements"] = [stmt for stmt in BLOCKED_READ_STATEMENTS if stmt != ("UPDATE" if policy_approved else "SELECT")]
            mapping["sql_template"] = ExecutionGenerator.fixed_bulk_update_sql(plan, schema_context, policy_approved)
            mapping["preconditions"] = plan.preconditions
            mapping["requires_human_approval"] = True
            mapping["requires_admin_approval"] = True
            mapping["dry_run_required"] = True
            mapping["preview_required"] = True
            mapping["batch_limit_required"] = True
            mapping["audit_required"] = True
            mapping["idempotency"] = {"required": True, "source": "input.idempotency_key"}
            mapping["bulk_mutation"] = {
                "detected": True,
                "policy_required": True,
                "policy_approved": policy_approved,
                "preview_sql_required_before_update": True,
                "source_status": (plan.intent_signals or {}).get("source_status"),
                "target_status": (plan.intent_signals or {}).get("target_status"),
            }
            mapping["rollback_or_deactivation"] = "admin must define rollback or compensation before publish"
        else:
            mapping["sql_template"] = ExecutionGenerator.fixed_mutation_sql(plan, schema_context)
            mapping["preconditions"] = plan.preconditions
            mapping["requires_human_approval"] = True
            mapping["strict_where_required"] = plan.operation_type in {"update", "delete"}
            mapping["idempotency"] = {"required": True, "source": "input.idempotency_key"}
            mapping["rollback_or_deactivation"] = "admin must define compensating action before publish"
        return {"type": "database_sql", "mode": "read_only" if plan.operation_type == "read" else "write_requires_human_approval", "mapping": mapping}

    @staticmethod
    def fixed_bulk_update_sql(plan: ToolPlan, schema_context: dict[str, Any], policy_approved: bool) -> str:
        order_table = next((item for item in schema_context.get("tables", []) if item["fqn"] in set(plan.exact_resources_used) and "order" in str(item.get("table") or "").lower()), None)
        if not order_table:
            return ""
        columns = {column["name"] for column in order_table.get("columns", [])}
        status_column = (plan.resolved_target_column or "").split(".")[-1]
        if status_column not in columns:
            status_column = "order_status" if "order_status" in columns else "status" if "status" in columns else ""
        if not status_column:
            return ""
        source_status = (plan.intent_signals or {}).get("source_status") or "pending"
        target_status = (plan.intent_signals or {}).get("target_status") or "delivered"
        if not policy_approved:
            return (
                "SELECT COUNT(*)::int AS affected_count_preview "
                f"FROM {order_table['fqn']} "
                f"WHERE {status_column} = '{source_status}'"
            )
        returning = ["order_id"] if "order_id" in columns else []
        if "order_number" in columns:
            returning.append("order_number")
        returning.append(f"{status_column} AS updated_order_status")
        return (
            f"UPDATE {order_table['fqn']} "
            f"SET {status_column} = '{target_status}' "
            f"WHERE {status_column} = '{source_status}' "
            f"RETURNING {', '.join(returning)}"
        )

    @staticmethod
    def _api(plan: ToolPlan, context: dict[str, Any]) -> dict[str, Any]:
        actions = ContextRetriever.connector_actions(context)
        selected = actions[0] if actions else {}
        method = str(selected.get("method") or ("GET" if plan.operation_type == "read" else "POST")).upper()
        mapping = {
            "provider": plan.provider,
            "connector_action": selected.get("name"),
            "method": method,
            "endpoint": selected.get("endpoint"),
            "query_params": [field["name"] for field in plan.input_fields if field.get("name") and method == "GET"],
            "body_params": [field["name"] for field in plan.input_fields if field.get("name") and method != "GET"],
            "tenant_secret_ref": "tenant_provider_connection_secret_ref",
            "tenant_scope": ExecutionGenerator.tenant_scope(),
            "timeouts": {"connect_seconds": 5, "read_seconds": 20},
            "retry": {"max_attempts": 2, "retry_on": ["429", "500", "502", "503", "504"], "backoff": "exponential_jitter"},
            "rate_limit": {"scope": "organization_provider_connection", "policy": "provider_default_or_lower"},
            "idempotency": {"required": plan.operation_type != "read", "header": "Idempotency-Key" if plan.operation_type != "read" else None},
        }
        return {"type": "integration_action", "mode": "read_only" if plan.operation_type == "read" else "write_requires_human_approval", "mapping": mapping}

    @staticmethod
    def fixed_select_sql(plan: ToolPlan, schema_context: dict[str, Any]) -> str:
        target = plan.exact_resources_used[0] if plan.exact_resources_used else None
        table = next((item for item in schema_context.get("tables", []) if item["fqn"] == target), None)
        if not table:
            return ""
        columns = [column for column in table.get("columns", []) if column["name"] in schema_context["allowed_columns"].get(table["fqn"], [])][:8]
        if not columns:
            return ""
        select_parts = [ExecutionGenerator._select_expression(table["fqn"], column) for column in columns]
        joins = []
        joined_tables = {table["fqn"]: table}
        table_by_fqn = {item["fqn"]: item for item in schema_context.get("tables", [])}
        related_tables = [table_by_fqn[resource] for resource in plan.exact_resources_used[1:] if resource in table_by_fqn]
        for related in related_tables:
            join_condition = ExecutionGenerator._join_condition(related, list(joined_tables.values()))
            if not join_condition:
                continue
            joins.append(f"LEFT JOIN {related['fqn']} ON {join_condition}")
            joined_tables[related["fqn"]] = related
            detail_columns = ExecutionGenerator._related_detail_columns(plan, related)
            if detail_columns:
                for column in detail_columns:
                    select_parts.append(ExecutionGenerator._select_expression(related["fqn"], column))
            else:
                id_column = ExecutionGenerator._identifier_column(related)
                if id_column:
                    select_parts.append(f"COUNT(DISTINCT {related['fqn']}.{id_column}) AS {related['table']}_count")
        has_counts = any("COUNT(" in part.upper() for part in select_parts)
        if has_counts:
            return ExecutionGenerator._fixed_select_sql_with_base_cte(plan, table, columns, related_tables)
        select_list = ", ".join(select_parts)
        filters = ExecutionGenerator._filter_conditions(plan, table, table.get("columns", []), table["fqn"])
        where_clause = f" WHERE {' AND '.join(filters)}" if filters else ""
        join_clause = f" {' '.join(joins)}" if joins else ""
        return f"SELECT {select_list} FROM {table['fqn']}{join_clause}{where_clause} LIMIT :limit"

    @staticmethod
    def _fixed_select_sql_with_base_cte(plan: ToolPlan, table: dict[str, Any], columns: list[dict[str, Any]], related_tables: list[dict[str, Any]]) -> str:
        base_alias = ExecutionGenerator._table_alias(table, set())
        cte_name = f"{table['table'].rstrip('s')}_base"
        cte_columns = ExecutionGenerator._cte_columns(table, columns, related_tables)
        cte_select_parts = [ExecutionGenerator._select_expression(base_alias, column) for column in cte_columns]
        filters = ExecutionGenerator._filter_conditions(plan, table, table.get("columns", []), base_alias)
        where_clause = f" WHERE {' AND '.join(filters)}" if filters else ""
        cte_sql = (
            f"WITH {cte_name} AS ("
            f"SELECT {', '.join(cte_select_parts)} "
            f"FROM {table['fqn']} {base_alias}"
            f"{where_clause}"
            f")"
        )

        output_names = [ExecutionGenerator._select_output_name(column) for column in columns]
        select_parts = [f"cb.{name}" for name in output_names]
        joins: list[str] = []
        joined_tables = {table["fqn"]: table}
        joined_aliases = {table["fqn"]: "cb"}
        used_aliases = {"cb", base_alias}
        for related in related_tables:
            alias = ExecutionGenerator._table_alias(related, used_aliases)
            used_aliases.add(alias)
            join_condition = ExecutionGenerator._join_condition_with_aliases(related, joined_tables, joined_aliases, alias)
            if not join_condition:
                continue
            joins.append(f"LEFT JOIN {related['fqn']} {alias} ON {join_condition}")
            joined_tables[related["fqn"]] = related
            joined_aliases[related["fqn"]] = alias
            detail_columns = ExecutionGenerator._related_detail_columns(plan, related)
            if detail_columns:
                for column in detail_columns:
                    select_parts.append(ExecutionGenerator._select_expression(alias, column))
            else:
                id_column = ExecutionGenerator._identifier_column(related)
                if id_column:
                    select_parts.append(f"COUNT(DISTINCT {alias}.{id_column})::int AS {related['table']}_count")

        group_clause = ", ".join(f"cb.{name}" for name in output_names)
        join_clause = f" {' '.join(joins)}" if joins else ""
        return f"{cte_sql} SELECT {', '.join(select_parts)} FROM {cte_name} cb{join_clause} GROUP BY {group_clause} LIMIT :limit"

    @staticmethod
    def fixed_mutation_sql(plan: ToolPlan, schema_context: dict[str, Any]) -> str:
        target = plan.exact_resources_used[0] if plan.exact_resources_used else None
        table = next((item for item in schema_context.get("tables", []) if item["fqn"] == target), None)
        if not table:
            return ""
        columns = [column["name"] for column in table.get("columns", [])]
        id_column = next((column for column in columns if column in {"id", f"{table['table'].rstrip('s')}_id"} or column.endswith("_id")), None)
        writable = [field["name"] for field in plan.input_fields if field.get("name") in columns and field.get("name") not in {"id", "limit", id_column}]
        if plan.operation_type == "create" and writable:
            column_list = ", ".join(writable)
            value_list = ", ".join(f":{column}" for column in writable)
            returning = f" RETURNING {id_column}" if id_column else ""
            return f"INSERT INTO {table['fqn']} ({column_list}) VALUES ({value_list}){returning}"
        if plan.operation_type == "update" and writable and id_column:
            assignments = ", ".join(f"{column} = :{column}" for column in writable)
            return f"UPDATE {table['fqn']} SET {assignments} WHERE {id_column} = :{id_column}"
        if plan.operation_type == "delete" and id_column:
            return f"DELETE FROM {table['fqn']} WHERE {id_column} = :{id_column}"
        return ""

    @staticmethod
    def fixed_workflow_sql(plan: ToolPlan, schema_context: dict[str, Any]) -> str:
        target_tables = [item for item in schema_context.get("tables", []) if item["fqn"] in set(plan.exact_resources_used)]
        if not target_tables:
            return ""
        target = plan.exact_resources_used[0] if plan.exact_resources_used else None
        table = next((item for item in target_tables if item["fqn"] == target), target_tables[0])
        columns = {column["name"] for column in table.get("columns", [])}
        resolved_target_name = (plan.resolved_target_column or "").split(".")[-1]
        username_column = resolved_target_name if resolved_target_name in columns else next((name for name in ("username", "user_name") if name in columns), None)
        full_name_column = resolved_target_name if plan.target_field == "full_name" and resolved_target_name in columns else next((name for name in ("full_name", "name", "customer_name") if name in columns), None)
        phone_column = next((name for name in ("phone", "mobile", "phone_number", "msisdn") if name in columns), None)
        id_column = ExecutionGenerator._identifier_column(table)
        if plan.workflow_type == "read_then_update" and plan.target_field == "full_name" and full_name_column and phone_column and id_column:
            returning = [f"c.{id_column}"]
            if "customer_code" in columns:
                returning.append("c.customer_code")
            returning.append(f"RIGHT(CAST(c.{phone_column} AS TEXT), 4) AS phone_last4")
            returning.append(f"c.{full_name_column} AS updated_full_name")
            returning_list = ", ".join(returning)
            return (
                "WITH matched_customer AS ("
                f"SELECT c.{id_column} "
                f"FROM {table['fqn']} c "
                f"WHERE c.{phone_column} = :phone_number"
                ") "
                f"UPDATE {table['fqn']} c "
                f"SET {full_name_column} = :new_full_name "
                "FROM matched_customer mc "
                f"WHERE c.{id_column} = mc.{id_column} "
                f"RETURNING {returning_list}"
            )
        if plan.workflow_type == "read_then_update" and plan.target_field == "username" and username_column and phone_column and id_column:
            returning = [f"c.{id_column}"]
            if "customer_code" in columns:
                returning.append("c.customer_code")
            if phone_column in columns:
                returning.append(f"RIGHT(CAST(c.{phone_column} AS TEXT), 4) AS phone_last4")
            returning.append(f"c.{username_column} AS updated_username")
            returning_list = ", ".join(returning)
            return (
                "WITH matched_customer AS ("
                f"SELECT c.{id_column} "
                f"FROM {table['fqn']} c "
                f"WHERE c.{phone_column} = :phone_number "
                "LIMIT 1"
                ") "
                f"UPDATE {table['fqn']} c "
                f"SET {username_column} = :new_username "
                "FROM matched_customer mc "
                f"WHERE c.{id_column} = mc.{id_column} "
                f"RETURNING {returning_list}"
            )
        if plan.workflow_type == "read_then_update" and plan.target_field == "order_status":
            order_table = next((item for item in target_tables if "order" in str(item.get("table") or "").lower()), table)
            customer_table = next((item for item in target_tables if "customer" in str(item.get("table") or "").lower()), None)
            order_columns = {column["name"] for column in order_table.get("columns", [])}
            customer_columns = {column["name"] for column in (customer_table or {}).get("columns", [])}
            order_id_column = "order_id" if "order_id" in order_columns else ExecutionGenerator._identifier_column(order_table)
            order_number_column = "order_number" if "order_number" in order_columns else None
            status_column = resolved_target_name if resolved_target_name in order_columns else next((name for name in ("order_status", "status") if name in order_columns), None)
            customer_id_column = next((name for name in ("customer_id",) if name in order_columns), None)
            customer_name_column = next((name for name in ("full_name", "name", "customer_name") if name in customer_columns), None)
            customer_phone_column = next((name for name in ("phone", "mobile", "phone_number", "msisdn") if name in customer_columns), None)
            latest_order_requested = bool((plan.intent_signals or {}).get("latest_order_requested"))
            latest_order_policy_approved = bool((plan.intent_signals or {}).get("latest_order_policy_approved"))
            if latest_order_requested:
                created_at_column = "created_at" if "created_at" in order_columns else None
                if (
                    latest_order_policy_approved
                    and order_id_column
                    and order_number_column
                    and status_column
                    and created_at_column
                    and customer_table
                    and customer_id_column
                    and customer_phone_column
                ):
                    returning = [f"o.{order_id_column}", f"lo.{order_number_column}"]
                    if "customer_code" in customer_columns:
                        returning.append("mc.customer_code")
                    returning.append("RIGHT(CAST(mc.phone AS TEXT), 4) AS phone_last4")
                    returning.append(f"o.{status_column} AS updated_order_status")
                    returning_list = ", ".join(returning)
                    return (
                        "WITH matched_customer AS ("
                        f"SELECT c.customer_id, c.customer_code, c.{customer_phone_column} AS phone "
                        f"FROM {customer_table['fqn']} c "
                        f"WHERE c.{customer_phone_column} = :phone_number"
                        "), "
                        "latest_order AS ("
                        f"SELECT o.{order_id_column}, o.{order_number_column}, o.{customer_id_column} "
                        f"FROM {order_table['fqn']} o "
                        f"JOIN matched_customer mc ON o.{customer_id_column} = mc.customer_id "
                        f"ORDER BY o.{created_at_column} DESC, o.{order_id_column} DESC "
                        "LIMIT 1"
                        ") "
                        f"UPDATE {order_table['fqn']} AS o "
                        f"SET {status_column} = :new_order_status "
                        "FROM latest_order lo "
                        "JOIN matched_customer mc ON lo.customer_id = mc.customer_id "
                        f"WHERE o.{order_id_column} = lo.{order_id_column} "
                        f"RETURNING {returning_list}"
                    )
                return ""
            if order_id_column and status_column and customer_table and customer_id_column and customer_name_column and customer_phone_column:
                returning = [f"o.{order_id_column}"]
                if order_number_column:
                    returning.append(f"co.{order_number_column}")
                if "customer_code" in customer_columns:
                    returning.append("mc.customer_code")
                if customer_phone_column in customer_columns:
                    returning.append("RIGHT(CAST(mc.phone AS TEXT), 4) AS phone_last4")
                returning.append(f"o.{status_column} AS updated_order_status")
                returning_list = ", ".join(returning)
                order_number_select = f", o.{order_number_column}" if order_number_column else ""
                order_number_filter = f"AND (:order_number IS NULL OR o.{order_number_column} = :order_number)" if order_number_column else ""
                return (
                    "WITH matched_customer AS ("
                    f"SELECT c.customer_id, c.customer_code, c.{customer_phone_column} AS phone "
                    f"FROM {customer_table['fqn']} c "
                    f"WHERE LOWER(c.{customer_name_column}) = LOWER(:customer_name) "
                    f"AND c.{customer_phone_column} = :phone_number"
                    "), "
                    "candidate_orders AS ("
                    f"SELECT o.{order_id_column}{order_number_select}, o.{customer_id_column} "
                    f"FROM {order_table['fqn']} o "
                    f"JOIN matched_customer mc ON o.{customer_id_column} = mc.customer_id "
                    f"WHERE (:order_id IS NULL OR CAST(o.{order_id_column} AS TEXT) = :order_id) "
                    f"{order_number_filter}"
                    "), "
                    "candidate_count AS ("
                    "SELECT COUNT(*) AS match_count FROM candidate_orders"
                    "), "
                    "selected_order AS ("
                    "SELECT co.order_id "
                    "FROM candidate_orders co "
                    "CROSS JOIN candidate_count cc "
                    "WHERE cc.match_count = 1 "
                    "OR :order_id IS NOT NULL "
                    "OR :order_number IS NOT NULL"
                    ") "
                    f"UPDATE {order_table['fqn']} AS o "
                    f"SET {status_column} = :new_order_status "
                    "FROM selected_order so "
                    f"JOIN candidate_orders co ON so.order_id = co.{order_id_column} "
                    "JOIN matched_customer mc ON co.customer_id = mc.customer_id "
                    f"WHERE o.{order_id_column} = so.order_id "
                    f"RETURNING {returning_list}"
                )
        return ""

    @staticmethod
    def workflow_precheck(plan: ToolPlan, schema_context: dict[str, Any]) -> dict[str, Any]:
        if plan.workflow_type != "read_then_update" or plan.target_field != "order_status":
            return {}
        if bool((plan.intent_signals or {}).get("latest_order_requested")):
            return {}
        target_tables = [item for item in schema_context.get("tables", []) if item["fqn"] in set(plan.exact_resources_used)]
        order_table = next((item for item in target_tables if "order" in str(item.get("table") or "").lower()), None)
        customer_table = next((item for item in target_tables if "customer" in str(item.get("table") or "").lower()), None)
        if not order_table or not customer_table:
            return {}
        order_columns = {column["name"] for column in order_table.get("columns", [])}
        customer_columns = {column["name"] for column in customer_table.get("columns", [])}
        if not {"order_id", "customer_id"}.issubset(order_columns) or "customer_id" not in customer_columns:
            return {}
        order_number_column = "order_number" if "order_number" in order_columns else None
        customer_name_column = next((name for name in ("full_name", "name", "customer_name") if name in customer_columns), None)
        customer_phone_column = next((name for name in ("phone", "mobile", "phone_number", "msisdn") if name in customer_columns), None)
        if not customer_name_column or not customer_phone_column:
            return {}
        order_number_select = f", o.{order_number_column}" if order_number_column else ""
        order_number_filter = f"AND (:order_number IS NULL OR o.{order_number_column} = :order_number)" if order_number_column else ""
        sql = (
            "WITH matched_customer AS ("
            f"SELECT c.customer_id, c.customer_code, c.{customer_phone_column} AS phone "
            f"FROM {customer_table['fqn']} c "
            f"WHERE LOWER(c.{customer_name_column}) = LOWER(:customer_name) "
            f"AND c.{customer_phone_column} = :phone_number"
            "), "
            "candidate_orders AS ("
            f"SELECT o.order_id{order_number_select}, o.customer_id "
            f"FROM {order_table['fqn']} o "
            "JOIN matched_customer mc ON o.customer_id = mc.customer_id "
            "WHERE (:order_id IS NULL OR CAST(o.order_id AS TEXT) = :order_id) "
            f"{order_number_filter}"
            "), "
            "candidate_count AS ("
            "SELECT COUNT(*)::int AS match_count FROM candidate_orders"
            ") "
            "SELECT "
            "cc.match_count, "
            "CASE "
            "WHEN cc.match_count = 0 THEN 'NO_MATCH' "
            "WHEN cc.match_count > 1 AND :order_id IS NULL AND :order_number IS NULL THEN 'ORDER_TARGET_AMBIGUOUS' "
            "ELSE 'READY' "
            "END AS precheck_status "
            "FROM candidate_count cc"
        )
        return {
            "required": True,
            "type": "database_sql_precheck",
            "sql_template": sql,
            "allowed_statements": ["SELECT"],
            "parameter_binding": {
                "style": "named",
                "sources": ["input.customer_name", "input.phone_number", "input.order_id", "input.order_number"],
            },
            "result_mapping": {
                "NO_MATCH": {"block_update": True, "error_code": "NO_MATCH", "message": "No matching customer/order was found."},
                "ORDER_TARGET_AMBIGUOUS": {"block_update": True, "error_code": "ORDER_TARGET_AMBIGUOUS", "message": "Multiple matching orders require order_id or order_number."},
                "READY": {"block_update": False},
            },
            "must_pass_before_mutation": True,
        }

    @staticmethod
    def relationships(schema_context: dict[str, Any]) -> list[dict[str, Any]]:
        relationships = []
        tables = schema_context.get("tables", [])
        for left in tables:
            left_columns = {column["name"] for column in left.get("columns", [])}
            for right in tables:
                if left["fqn"] >= right["fqn"]:
                    continue
                right_columns = {column["name"] for column in right.get("columns", [])}
                for column_name in sorted(left_columns & right_columns):
                    if column_name == "id" or column_name.endswith("_id"):
                        relationships.append(
                            {
                                "left": f"{left['fqn']}.{column_name}",
                                "right": f"{right['fqn']}.{column_name}",
                                "join": f"{left['fqn']}.{column_name} = {right['fqn']}.{column_name}",
                                "confidence": 0.72,
                                "reason": "matching identifier column names",
                            }
                        )
        return relationships[:20]

    @staticmethod
    def _identifier_column(table: dict[str, Any]) -> str | None:
        primary_key = table.get("primary_key") or []
        if isinstance(primary_key, str):
            primary_key = [primary_key]
        for column in primary_key:
            if column:
                return str(column)
        columns = [column["name"] for column in table.get("columns", [])]
        for preferred in ("id", f"{table['table'].rstrip('s')}_id"):
            if preferred in columns:
                return preferred
        return next((column for column in columns if column.endswith("_id")), columns[0] if columns else None)

    @staticmethod
    def _join_condition(table: dict[str, Any], joined_tables: list[dict[str, Any]]) -> str:
        table_columns = {column["name"] for column in table.get("columns", [])}
        for joined in joined_tables:
            joined_columns = {column["name"] for column in joined.get("columns", [])}
            for column_name in sorted(table_columns & joined_columns):
                if column_name == "id" or column_name.endswith("_id"):
                    return f"{joined['fqn']}.{column_name} = {table['fqn']}.{column_name}"
        return ""

    @staticmethod
    def _join_condition_with_aliases(
        table: dict[str, Any],
        joined_tables: dict[str, dict[str, Any]],
        joined_aliases: dict[str, str],
        table_alias: str,
    ) -> str:
        table_columns = {column["name"] for column in table.get("columns", [])}
        for joined_fqn, joined in joined_tables.items():
            joined_columns = {column["name"] for column in joined.get("columns", [])}
            for column_name in sorted(table_columns & joined_columns):
                if column_name == "id" or column_name.endswith("_id"):
                    return f"{joined_aliases[joined_fqn]}.{column_name} = {table_alias}.{column_name}"
        return ""

    @staticmethod
    def search_strategy(plan: ToolPlan, schema_context: dict[str, Any]) -> dict[str, Any]:
        searchable = ExecutionGenerator._filter_columns(plan, schema_context)
        if not searchable:
            for table in schema_context.get("tables", []):
                if table["fqn"] not in plan.exact_resources_used:
                    continue
                for column in table.get("columns", []):
                    if any(token in column["name"].lower() for token in ("id", "name", "email", "phone", "status", "order", "ticket")):
                        searchable.append(column["fqn"])
        return {
            "type": "lookup" if plan.operation_type == "read" else "mutation_target_resolution",
            "match_priority": ["exact identifier", "exact normalized phone/email", "case-insensitive text", "prefix text"],
            "normalization": ["trim", "lowercase text", "digits-only phone"],
            "confidence_scoring": {"exact_identifier": 1.0, "exact_phone_or_email": 0.95, "exact_text": 0.85, "prefix_text": 0.7},
            "searchable_columns": searchable,
        }

    @staticmethod
    def limits() -> dict[str, Any]:
        return {"max_entity_matches": 10, "max_rows_per_table": 50, "max_total_rows": 100, "timeout_seconds": 10, "max_response_size_bytes": 262144}

    @staticmethod
    def tenant_scope() -> dict[str, Any]:
        return {
            "requires_organization_id": True,
            "requires_tenant_integration_id": True,
            "requires_provider_connection_id": True,
            "requires_selected_context_snapshot": True,
            "secret_source": "current_tenant_stored_secret_reference_only",
            "forbid_runtime_connection_strings": True,
        }

    @staticmethod
    def _select_expression(table_fqn: str, column: dict[str, Any]) -> str:
        column_name = column["name"]
        qualified = f"{table_fqn}.{column_name}"
        classification = PIIPolicyBuilder.classify_field(column_name, column.get("type", ""))
        redaction = PIIPolicyBuilder.redaction_for(classification)
        output_name = PIIPolicyBuilder.masked_output_name(column_name, classification)
        if redaction == "phone_last4":
            return f"RIGHT(CAST({qualified} AS TEXT), 4) AS {output_name}"
        if redaction == "mask_email":
            return (
                f"CASE WHEN {qualified} IS NULL THEN NULL "
                f"ELSE LEFT({qualified}, 1) || '***' || SUBSTRING({qualified} FROM POSITION('@' IN {qualified})) END AS {output_name}"
            )
        if redaction == "address_summary":
            return f"LEFT(CAST({qualified} AS TEXT), 80) AS {output_name}"
        return qualified

    @staticmethod
    def _select_output_name(column: dict[str, Any]) -> str:
        column_name = column["name"]
        classification = PIIPolicyBuilder.classify_field(column_name, column.get("type", ""))
        return PIIPolicyBuilder.masked_output_name(column_name, classification)

    @staticmethod
    def _filter_conditions(plan: ToolPlan, table: dict[str, Any], columns: list[dict[str, Any]], qualifier: str) -> list[str]:
        filters = []
        column_names = {column["name"].lower(): column["name"] for column in columns}
        for field in plan.input_fields:
            name = str(field.get("name") or "")
            if name == "limit":
                continue
            column = column_names.get(name.lower())
            if not column and name == "customer_name":
                column = next((item["name"] for item in columns if item["name"].lower() in {"full_name", "name", "customer_name", "first_name", "last_name"}), None)
            if not column and name == "phone_number":
                column = next((item["name"] for item in columns if item["name"].lower() in {"phone", "mobile", "phone_number", "msisdn"}), None)
            if not column:
                candidates = [item["name"] for item in columns if item["name"].lower() in name.lower() or name.lower() in item["name"].lower()]
                column = candidates[0] if candidates else None
            if column:
                filters.append(f"{qualifier}.{column} = :{name}")
        return filters

    @staticmethod
    def _cte_columns(table: dict[str, Any], output_columns: list[dict[str, Any]], related_tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_name = {column["name"]: column for column in table.get("columns", [])}
        selected: dict[str, dict[str, Any]] = {column["name"]: column for column in output_columns}
        for related in related_tables:
            related_columns = {column["name"] for column in related.get("columns", [])}
            for column_name in sorted(set(by_name) & related_columns):
                if column_name == "id" or column_name.endswith("_id"):
                    selected.setdefault(column_name, by_name[column_name])
        return list(selected.values())

    @staticmethod
    def _table_alias(table: dict[str, Any], used_aliases: set[str]) -> str:
        preferred = {
            "customers": "c",
            "orders": "o",
            "support_tickets": "st",
            "call_logs": "cl",
        }.get(str(table.get("table") or "").lower())
        if not preferred:
            preferred = "".join(part[:1] for part in str(table.get("table") or "t").split("_") if part)[:3] or "t"
        alias = preferred
        suffix = 2
        while alias in used_aliases:
            alias = f"{preferred}{suffix}"
            suffix += 1
        return alias

    @staticmethod
    def _related_detail_columns(plan: ToolPlan, table: dict[str, Any]) -> list[dict[str, Any]]:
        output_sources = {
            str(field.get("source")): str(field.get("raw_name") or field.get("name") or "")
            for field in plan.output_fields
            if str(field.get("source") or "").startswith(f"{table['fqn']}.")
        }
        if not output_sources:
            return []
        wanted = set(output_sources.values())
        return [column for column in table.get("columns", []) if column["name"] in wanted]

    @staticmethod
    def _group_expression(select_part: str) -> str:
        return re.split(r"\s+AS\s+", select_part, flags=re.IGNORECASE)[0].strip()

    @staticmethod
    def _filter_columns(plan: ToolPlan, schema_context: dict[str, Any]) -> list[str]:
        primary = plan.exact_resources_used[0] if plan.exact_resources_used else None
        if not primary:
            return []
        table = next((item for item in schema_context.get("tables", []) if item["fqn"] == primary), None)
        if not table:
            return []
        columns = {column["name"].lower(): column["name"] for column in table.get("columns", [])}
        searchable = []
        for field in plan.input_fields:
            name = str(field.get("name") or "")
            if name == "limit":
                continue
            column = columns.get(name.lower())
            if not column and name == "phone_number":
                column = next((item["name"] for item in table.get("columns", []) if item["name"].lower() in {"phone", "mobile", "phone_number", "msisdn"}), None)
            if not column and name == "customer_name":
                column = next((item["name"] for item in table.get("columns", []) if item["name"].lower() in {"full_name", "name", "customer_name", "first_name", "last_name"}), None)
            if column:
                searchable.append(f"{primary}.{column}")
        return searchable
