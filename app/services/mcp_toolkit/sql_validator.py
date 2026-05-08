from __future__ import annotations

import re
from typing import Any


class SQLValidator:
    """Static SQL validation for generated MCP database tools.

    Production execution must still run the generated EXPLAIN against the
    tenant connection during test-run/publish. This validator blocks malformed
    SQL before a tool can enter review/publish flow.
    """

    STATEMENTS = {"SELECT", "INSERT", "UPDATE", "DELETE"}

    @staticmethod
    def validate(sql: str, provider: str, mapping: dict[str, Any] | None = None) -> dict[str, Any]:
        parser = SQLValidator.parser_validation(sql)
        explain = SQLValidator.explain_validation(sql, provider, mapping or {}, parser)
        status = "passed" if parser["status"] == "passed" and explain["status"] == "passed" else "failed"
        return {"status": status, "parser": parser, "explain": explain}

    @staticmethod
    def parser_validation(sql: str) -> dict[str, Any]:
        cleaned = SQLValidator.clean_sql(sql)
        if not cleaned:
            return SQLValidator._failed("SQL_SYNTAX_INVALID", "SQL template is empty.")
        statements = SQLValidator._split_statements(cleaned)
        if len(statements) != 1:
            return SQLValidator._failed("SQL_SYNTAX_INVALID", "SQL must contain exactly one statement.")
        statement_sql = statements[0]
        balance_error = SQLValidator._balance_error(statement_sql)
        if balance_error:
            return SQLValidator._failed("SQL_SYNTAX_INVALID", balance_error)
        if re.search(r"<[^>]+>|\$\{|\{|\}", statement_sql):
            return SQLValidator._failed("SQL_SYNTAX_INVALID", "SQL contains placeholders or dynamic interpolation.")

        first_token = SQLValidator.first_token(statement_sql)
        if first_token == "WITH":
            statement = "SELECT" if SQLValidator.main_select_sql(statement_sql) else ""
        else:
            statement = first_token if first_token in SQLValidator.STATEMENTS else ""
        if not statement:
            return SQLValidator._failed("SQL_SYNTAX_INVALID", "SQL first statement must be SELECT, INSERT, UPDATE, or DELETE.")

        validator = {
            "SELECT": SQLValidator._validate_select,
            "INSERT": SQLValidator._validate_insert,
            "UPDATE": SQLValidator._validate_update,
            "DELETE": SQLValidator._validate_delete,
        }[statement]
        syntax_error = validator(statement_sql)
        if syntax_error:
            return SQLValidator._failed("SQL_SYNTAX_INVALID", syntax_error, statement)

        return {
            "status": "passed",
            "error_code": None,
            "message": "SQL parser validation passed.",
            "statement": statement,
            "normalized_sql": statement_sql,
        }

    @staticmethod
    def explain_validation(sql: str, provider: str, mapping: dict[str, Any], parser: dict[str, Any]) -> dict[str, Any]:
        if parser.get("status") != "passed":
            return {
                "status": "failed",
                "error_code": "SQL_EXPLAIN_FAILED",
                "message": "EXPLAIN validation cannot run until SQL parser validation passes.",
            }
        explain_config = mapping.get("explain_validation") if isinstance(mapping.get("explain_validation"), dict) else {}
        if str(explain_config.get("status") or "").lower() in {"failed", "fail", "error"} or explain_config.get("error"):
            return {
                "status": "failed",
                "error_code": "SQL_EXPLAIN_FAILED",
                "message": str(explain_config.get("error") or "Configured EXPLAIN validation failed."),
            }
        if provider and provider.lower() != "postgresql":
            return {
                "status": "passed",
                "error_code": None,
                "message": "EXPLAIN validation is required at provider test-run for this database provider.",
                "statement": f"EXPLAIN {parser.get('normalized_sql')}",
                "sample_bindings": SQLValidator.safe_sample_bindings(sql, mapping),
            }

        return {
            "status": "passed",
            "error_code": None,
            "message": "PostgreSQL EXPLAIN validation prepared with safe sample bound parameters.",
            "statement": f"EXPLAIN {parser.get('normalized_sql')}",
            "sample_bindings": SQLValidator.safe_sample_bindings(sql, mapping),
        }

    @staticmethod
    def safe_sample_bindings(sql: str, mapping: dict[str, Any]) -> dict[str, Any]:
        bindings = {}
        params = sorted(set(re.findall(r"(?<!:):([a-zA-Z_][\w]*)\b", sql or "")))
        allowed_columns = mapping.get("allowed_columns") if isinstance(mapping.get("allowed_columns"), dict) else {}
        column_names = {column.lower() for columns in allowed_columns.values() for column in (columns or [])}
        for param in params:
            lowered = param.lower()
            if lowered == "limit":
                bindings[param] = 1
            elif "phone" in lowered:
                bindings[param] = "9999999999"
            elif "email" in lowered:
                bindings[param] = "sample@example.com"
            elif "id" in lowered or lowered in column_names and lowered.endswith("_id"):
                bindings[param] = 1
            elif lowered.startswith(("is_", "has_")):
                bindings[param] = False
            else:
                bindings[param] = "sample"
        return bindings

    @staticmethod
    def postgres_explain_query(sql: str, mapping: dict[str, Any]) -> tuple[str, list[Any], dict[str, Any]]:
        bindings = SQLValidator.safe_sample_bindings(sql, mapping)
        ordered_names: list[str] = []

        def _replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in ordered_names:
                ordered_names.append(name)
            return f"${ordered_names.index(name) + 1}"

        prepared_sql = re.sub(r"(?<!:):([a-zA-Z_][\w]*)\b", _replace, sql)
        return f"EXPLAIN {prepared_sql}", [bindings[name] for name in ordered_names], bindings

    @staticmethod
    def clean_sql(sql: str) -> str:
        cleaned = re.sub(r"/\*.*?\*/", " ", sql or "", flags=re.DOTALL)
        cleaned = re.sub(r"--.*?$", " ", cleaned, flags=re.MULTILINE)
        return re.sub(r"\s+", " ", cleaned).strip()

    @staticmethod
    def first_token(sql: str) -> str:
        match = re.match(r"\s*([a-zA-Z_][\w]*)\b", sql or "")
        return match.group(1).upper() if match else ""

    @staticmethod
    def statement_type(sql: str) -> str:
        parser = SQLValidator.parser_validation(sql)
        return str(parser.get("statement") or "")

    @staticmethod
    def main_select_sql(sql: str) -> str:
        cleaned = SQLValidator.clean_sql(sql).rstrip(";")
        if re.match(r"\s*SELECT\b", cleaned, flags=re.IGNORECASE):
            return cleaned
        if not re.match(r"\s*WITH\b", cleaned, flags=re.IGNORECASE):
            return ""
        depth = 0
        quote: str | None = None
        index = 0
        while index < len(cleaned):
            char = cleaned[index]
            if quote:
                if char == quote:
                    quote = None
                index += 1
                continue
            if char in {"'", '"'}:
                quote = char
                index += 1
                continue
            if char == "(":
                depth += 1
            elif char == ")" and depth:
                depth -= 1
            if depth == 0 and re.match(r"SELECT\b", cleaned[index:], flags=re.IGNORECASE):
                return cleaned[index:]
            index += 1
        return ""

    @staticmethod
    def select_list(sql: str) -> str:
        main = SQLValidator.main_select_sql(sql)
        match = re.match(r"\s*SELECT\s+", main, flags=re.IGNORECASE)
        if not match:
            return ""
        start = match.end()
        from_index = SQLValidator._top_level_keyword_index(main, "FROM", start)
        return main[start:from_index].strip() if from_index != -1 else ""

    @staticmethod
    def _validate_select(sql: str) -> str:
        main = SQLValidator.main_select_sql(sql)
        if not main:
            return "SELECT statement could not be located."
        select_list = SQLValidator.select_list(main)
        if not select_list:
            return "SELECT list is empty."
        if select_list.endswith(","):
            return "SELECT list has a dangling comma."
        if SQLValidator._top_level_keyword_index(main, "FROM") == -1:
            return "SELECT statement is missing FROM."
        if re.search(r"\bSELECT\s+FROM\b", main, flags=re.IGNORECASE):
            return "SELECT statement is missing selected columns."
        if re.search(r",\s+FROM\b", main, flags=re.IGNORECASE):
            return "SELECT statement has a dangling comma before FROM."
        group_by_index = SQLValidator._top_level_phrase_index(main, "GROUP BY")
        if group_by_index != -1:
            group_list = SQLValidator._clause_after(main, group_by_index + len("GROUP BY"), ("HAVING", "ORDER BY", "LIMIT", "OFFSET"))
            if not group_list.strip():
                return "GROUP BY list is empty."
            if re.search(r"\bFROM\b", group_list, flags=re.IGNORECASE):
                return "GROUP BY clause contains malformed FROM token."
            if group_list.strip().endswith(","):
                return "GROUP BY list has a dangling comma."
        if re.search(r"\bGROUP\s+BY\s+FROM\b", main, flags=re.IGNORECASE):
            return "GROUP BY clause is malformed."
        return ""

    @staticmethod
    def _validate_insert(sql: str) -> str:
        match = re.search(
            r"\bINSERT\s+INTO\s+[a-zA-Z_][\w]*\.[a-zA-Z_][\w]*\s*\(([^)]*)\)\s+VALUES\s*\(([^)]*)\)",
            sql,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return "INSERT must use fully qualified table, explicit columns, and VALUES."
        columns = SQLValidator._split_sql_list(match.group(1))
        values = SQLValidator._split_sql_list(match.group(2))
        if not columns:
            return "INSERT column list is empty."
        if len(columns) != len(values):
            return "INSERT column count must match VALUES count."
        return ""

    @staticmethod
    def _validate_update(sql: str) -> str:
        match = re.search(r"\bUPDATE\s+[a-zA-Z_][\w]*\.[a-zA-Z_][\w]*\s+SET\s+(.+?)(?:\s+WHERE\s+|$)", sql, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            return "UPDATE must use fully qualified table and SET assignments."
        assignments = SQLValidator._split_sql_list(match.group(1))
        if not assignments or any("=" not in item for item in assignments):
            return "UPDATE SET assignments are malformed."
        return ""

    @staticmethod
    def _validate_delete(sql: str) -> str:
        if not re.search(r"\bDELETE\s+FROM\s+[a-zA-Z_][\w]*\.[a-zA-Z_][\w]*\b", sql, flags=re.IGNORECASE):
            return "DELETE must use fully qualified table."
        return ""

    @staticmethod
    def _split_statements(sql: str) -> list[str]:
        statements = []
        current: list[str] = []
        quote: str | None = None
        index = 0
        while index < len(sql):
            char = sql[index]
            if quote:
                current.append(char)
                if char == quote:
                    quote = None
                index += 1
                continue
            if char in {"'", '"'}:
                quote = char
                current.append(char)
                index += 1
                continue
            if char == ";":
                statement = "".join(current).strip()
                if statement:
                    statements.append(statement)
                current = []
                index += 1
                continue
            current.append(char)
            index += 1
        statement = "".join(current).strip()
        if statement:
            statements.append(statement)
        return statements

    @staticmethod
    def _balance_error(sql: str) -> str:
        depth = 0
        quote: str | None = None
        for char in sql:
            if quote:
                if char == quote:
                    quote = None
                continue
            if char in {"'", '"'}:
                quote = char
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth < 0:
                    return "SQL has unmatched closing parenthesis."
        if quote:
            return "SQL has unterminated quoted string or identifier."
        if depth:
            return "SQL has unmatched opening parenthesis."
        return ""

    @staticmethod
    def _top_level_keyword_index(sql: str, keyword: str, start: int = 0) -> int:
        return SQLValidator._top_level_phrase_index(sql, keyword, start)

    @staticmethod
    def _top_level_phrase_index(sql: str, phrase: str, start: int = 0) -> int:
        depth = 0
        quote: str | None = None
        index = start
        pattern = re.compile(rf"\b{re.escape(phrase).replace('\\ ', r'\\s+')}\b", flags=re.IGNORECASE)
        while index < len(sql):
            char = sql[index]
            if quote:
                if char == quote:
                    quote = None
                index += 1
                continue
            if char in {"'", '"'}:
                quote = char
                index += 1
                continue
            if char == "(":
                depth += 1
            elif char == ")" and depth:
                depth -= 1
            if depth == 0:
                match = pattern.match(sql, index)
                if match:
                    return index
            index += 1
        return -1

    @staticmethod
    def _clause_after(sql: str, start: int, terminators: tuple[str, ...]) -> str:
        indexes = [SQLValidator._top_level_phrase_index(sql, term, start) for term in terminators]
        indexes = [index for index in indexes if index != -1]
        end = min(indexes) if indexes else len(sql)
        return sql[start:end].strip()

    @staticmethod
    def _split_sql_list(value: str) -> list[str]:
        items: list[str] = []
        current: list[str] = []
        depth = 0
        quote: str | None = None
        for char in value:
            if quote:
                current.append(char)
                if char == quote:
                    quote = None
                continue
            if char in {"'", '"'}:
                quote = char
                current.append(char)
                continue
            if char == "(":
                depth += 1
            elif char == ")" and depth:
                depth -= 1
            if char == "," and depth == 0:
                item = "".join(current).strip()
                if item:
                    items.append(item)
                current = []
            else:
                current.append(char)
        item = "".join(current).strip()
        if item:
            items.append(item)
        return items

    @staticmethod
    def _failed(error_code: str, message: str, statement: str = "") -> dict[str, Any]:
        return {"status": "failed", "error_code": error_code, "message": message, "statement": statement}
