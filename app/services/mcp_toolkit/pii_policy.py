from __future__ import annotations

from typing import Any


class PIIPolicyBuilder:
    SECRET_TOKENS = ("password", "secret", "token", "api_key", "apikey", "connection_string", "credential", "auth")

    @staticmethod
    def classify_field(name: str, field_type: str = "") -> str:
        value = name.lower()
        if any(token in value for token in PIIPolicyBuilder.SECRET_TOKENS):
            return "secret"
        if "email" in value:
            return "email"
        if any(token in value for token in ("phone", "mobile", "msisdn")):
            return "phone"
        if any(token in value for token in ("address", "street", "city", "state", "postal", "zip")):
            return "address"
        if any(token in value for token in ("name", "first_name", "last_name", "full_name")):
            return "person_name"
        if any(token in value for token in ("amount", "price", "total", "balance", "refund")):
            return "financial"
        if value == "id" or value.endswith("_id"):
            return "identifier"
        return "none"

    @staticmethod
    def redaction_for(classification: str) -> str:
        if classification == "secret":
            return "drop"
        if classification == "email":
            return "mask_email"
        if classification == "phone":
            return "phone_last4"
        if classification == "address":
            return "address_summary"
        if classification == "financial":
            return "mask_unless_admin_allowed"
        return "none"

    @staticmethod
    def masked_output_name(name: str, classification: str | None = None) -> str:
        classification = classification or PIIPolicyBuilder.classify_field(name)
        value = name.lower()
        if classification == "email":
            return "email_masked"
        if classification == "phone":
            return "phone_last4"
        if classification == "address":
            return "city_summary" if "city" in value else "address_summary"
        return name

    @staticmethod
    def output_field_name(name: str, field_type: str = "") -> str:
        classification = PIIPolicyBuilder.classify_field(name, field_type)
        redaction = PIIPolicyBuilder.redaction_for(classification)
        if redaction in {"mask_email", "phone_last4", "address_summary"}:
            return PIIPolicyBuilder.masked_output_name(name, classification)
        return name

    @staticmethod
    def build(integration_type: str, schema_context: dict[str, Any] | None = None, output_fields: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        fields: dict[str, dict[str, str]] = {}
        for table in (schema_context or {}).get("tables", []):
            for column in table.get("columns", []):
                classification = PIIPolicyBuilder.classify_field(column["name"], column.get("type", ""))
                fields[column["fqn"]] = {
                    "classification": classification,
                    "redaction": PIIPolicyBuilder.redaction_for(classification),
                    "output_name": PIIPolicyBuilder.masked_output_name(column["name"], classification),
                }
        for field in output_fields or []:
            name = str(field.get("name") or "")
            if not name:
                continue
            classification = PIIPolicyBuilder.classify_field(name, str(field.get("type") or ""))
            fields.setdefault(
                name,
                {
                    "classification": classification,
                    "redaction": PIIPolicyBuilder.redaction_for(classification),
                    "output_name": PIIPolicyBuilder.masked_output_name(name, classification),
                },
            )
        return {
            "default_action": "mask_or_drop_sensitive_fields",
            "field_policy": fields,
            "never_return_fields_matching": list(PIIPolicyBuilder.SECRET_TOKENS),
            "customer_service_defaults": {
                "phone": "phone_last4",
                "email": "email_masked",
                "address": "address_summary",
            },
            "validation": "response fields must be checked against this policy before returning to an agent",
        }

    @staticmethod
    def redact_sensitive_values(value: Any) -> Any:
        if isinstance(value, dict):
            redacted = {}
            for key, item in value.items():
                key_text = str(key).lower()
                if key_text.endswith("_ref") or key_text.endswith("_reference"):
                    redacted[key] = PIIPolicyBuilder.redact_sensitive_values(item)
                elif any(token in key_text for token in PIIPolicyBuilder.SECRET_TOKENS) and isinstance(item, str) and item:
                    redacted[key] = "[redacted]"
                else:
                    redacted[key] = PIIPolicyBuilder.redact_sensitive_values(item)
            return redacted
        if isinstance(value, list):
            return [PIIPolicyBuilder.redact_sensitive_values(item) for item in value]
        return value
