from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


IntegrationType = Literal["database", "crm", "erp", "ecommerce", "his", "payments", "custom_api", "zoho_desk", "shipping"]
OperationType = Literal["read", "create", "update", "delete", "workflow", "ambiguous"]
RiskLevel = Literal["low", "medium", "high", "critical"]
ReviewStatus = Literal["draft", "needs_context_confirmation", "rejected", "approved"]
ValidationStatus = Literal["passed", "failed"]
TestStatus = Literal["not_run", "passed", "failed"]
RetrievalStatus = Literal["passed", "low_confidence", "failed"]


@dataclass(slots=True)
class VerifiedColumn:
    name: str
    type: str
    nullable: bool
    is_primary_key: bool
    is_searchable: bool
    pii_classification: str


@dataclass(slots=True)
class VerifiedRelationship:
    from_: str
    to: str
    source: str
    confidence: float
    approved_by: str | None = None


@dataclass(slots=True)
class VerifiedResource:
    resource_type: str
    name: str
    provider: str
    integration_type: str
    organization_id: str
    tenant_integration_id: str
    provider_connection_id: str
    context_snapshot_id: str
    source_context_ids: list[str]
    confidence: float
    columns: list[dict[str, Any]] | None = None
    relationships: list[dict[str, Any]] | None = None
    endpoint: dict[str, Any] | None = None
    action: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RetrievedEmbeddingContext:
    status: RetrievalStatus
    status_reason: str | None
    confidence: float
    fallback_used: bool
    publish_blockers: list[str]
    query_variants: list[str]
    scope_filter: dict[str, Any]
    candidate_resources: list[str]
    verified_resources: list[dict[str, Any]]
    retrieved_context_ids: list[str]
    rejected_context_ids: list[str]
    rejected_chunks: list[dict[str, Any]]
    warnings: list[str]
    errors: list[dict[str, Any]]
    source_context: list[dict[str, Any]] = field(default_factory=list)


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
    workflow_type: str | None = None
    target_field: str | None = None
    resolved_target_column: str | None = None
    intent_signals: dict[str, Any] = field(default_factory=dict)
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
    workflow_type: str | None = None
    target_field: str | None = None
    resolved_target_column: str | None = None
    trusted_resources_used: list[str] = field(default_factory=list)
    intent_signals: dict[str, Any] = field(default_factory=dict)
    trusted_resources: list[dict[str, Any]] = field(default_factory=list)
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
    test_run_results: list[dict[str, Any]] = field(default_factory=list)
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
