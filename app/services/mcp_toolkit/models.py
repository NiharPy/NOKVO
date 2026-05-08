from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


IntegrationType = Literal["database", "crm", "erp", "ecommerce", "his", "payments", "custom_api", "zoho_desk", "shipping"]
OperationType = Literal["read", "create", "update", "delete", "workflow"]
RiskLevel = Literal["low", "medium", "high", "critical"]
ReviewStatus = Literal["draft", "rejected", "approved"]
ValidationStatus = Literal["passed", "failed"]
TestStatus = Literal["not_run", "passed", "failed"]
RetrievalStatus = Literal["passed", "low_confidence", "failed"]


@dataclass(slots=True)
class ToolIntent:
    purpose: str
    integration_type: str
    provider: str
    operation_type: OperationType
    read_write_mode: str
    required_inputs: list[dict[str, Any]]
    expected_outputs: list[dict[str, Any]]
    target_resources: list[str]
    business_preconditions: list[str]
    risk_level: RiskLevel
    human_approval_needed: bool
    missing_context: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ToolPlan:
    name: str
    title: str
    description: str
    integration_type: str
    provider: str
    operation_type: OperationType
    risk_level: RiskLevel
    required_permissions: list[str]
    exact_resources_used: list[str]
    input_fields: list[dict[str, Any]]
    output_fields: list[dict[str, Any]]
    preconditions: list[str]
    postconditions: list[str]
    error_cases: list[dict[str, str]]
    approval_policy: dict[str, Any]
    audit_policy: dict[str, Any]
    missing_context: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ExecutionMapping:
    type: str
    mode: str
    mapping: dict[str, Any]


@dataclass(slots=True)
class SafetyPolicy:
    risk_level: RiskLevel
    least_privilege: bool
    tenant_isolation: dict[str, Any]
    pii_policy: dict[str, Any]
    permissions: dict[str, Any]
    approval_policy: dict[str, Any]
    audit_policy: dict[str, Any]
    output_sanitization: dict[str, Any]


@dataclass(slots=True)
class MCPToolDefinition:
    name: str
    title: str
    description: str
    integration_type: str
    provider: str
    mcp: dict[str, Any]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    execution: dict[str, Any]
    safety: dict[str, Any]
    review: dict[str, Any]
    tests: dict[str, Any]
    version: dict[str, Any]
    status: str
    execution_plan: list[str]
    source_context: list[dict[str, Any]]


@dataclass(slots=True)
class ValidationResult:
    status: ValidationStatus
    errors: list[dict[str, str]] = field(default_factory=list)
    warnings: list[dict[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class ReviewResult:
    status: ReviewStatus
    required: bool
    reviewer_role: str
    reason: str = ""
    required_changes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ToolTestSuite:
    test_run_required: bool
    generated_tests: list[dict[str, Any]]
    status: TestStatus = "not_run"


@dataclass(slots=True)
class PublishGateResult:
    can_publish: bool
    missing_requirements: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ToolGenerationResult:
    tool: dict[str, Any]
    tool_plan: dict[str, Any]
    retrieval: dict[str, Any]
    validation: dict[str, Any]
    review: dict[str, Any]
    tests: dict[str, Any]
    publish_gate: dict[str, Any]


def to_plain(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if isinstance(value, dict):
        return {key: to_plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_plain(item) for item in value]
    return value
