from __future__ import annotations

from copy import deepcopy

from app.services.mcp_toolkit.publish_gate import PublishGate
from app.services.mcp_toolkit.test_reviewer import valid_insert_generation


def ready_for_publish_result() -> dict:
    result = valid_insert_generation()
    result["validation"] = {
        "status": "passed",
        "errors": [],
        "warnings": [],
        "checks": {
            "sql_parser": "passed",
            "sql_explain": "passed",
            "parameter_binding": "passed",
            "resource_subset": "passed",
            "output_sql_alignment": "passed",
        },
    }
    result["review"] = {
        "status": "approved",
        "required": True,
        "reviewer_role": "admin",
        "reason": "Approved for publish.",
        "required_changes": [],
    }
    result["tool"]["review"] = result["review"]
    result["tool"]["version"] = {
        "tool_id": "tool_1",
        "version": "1.0.0",
        "created_by": "admin_1",
        "reviewed_by": "admin_1",
        "approved_by": "admin_1",
        "created_at": "2026-05-09T00:00:00+00:00",
        "updated_at": "2026-05-09T00:00:00+00:00",
        "status": "ready_for_publish",
        "changelog": ["approved draft"],
        "previous_version_id": None,
        "rollback": {"supported": True, "strategy": "disable and revert to previous version"},
    }
    result["tool"]["version"]["version"] = "1.0.0"
    result["tests"] = {
        "test_run_required": True,
        "generated_tests": [{"name": "happy_path", "type": "dry_run"}],
        "test_run_results": [{"name": "happy_path", "status": "passed"}],
        "status": "passed",
    }
    result["tool"]["tests"] = result["tests"]
    return result


def test_publish_gate_blocks_when_tests_not_run():
    result = ready_for_publish_result()
    result["tests"]["status"] = "not_run"
    result["tool"]["tests"]["status"] = "not_run"

    gate = PublishGate.evaluate(result, admin_approval_exists=True)

    assert gate["can_publish"] is False
    assert "tests.status must be passed" in gate["missing_requirements"]


def test_publish_gate_blocks_when_admin_approval_missing():
    result = ready_for_publish_result()

    gate = PublishGate.evaluate(result, admin_approval_exists=False)

    assert gate["can_publish"] is False
    assert "admin approval record is required" in gate["missing_requirements"]


def test_publish_gate_blocks_fallback_retrieval_without_explicit_approval():
    result = ready_for_publish_result()
    result["retrieval"]["status"] = "low_confidence"
    result["retrieval"]["confidence"] = 0.91
    result["retrieval"]["fallback_used"] = True
    result["retrieval"]["status_reason"] = "fallback_context_used_after_qdrant_index_failure"
    result["retrieval"]["publish_blockers"] = ["QDRANT_INDEX_MISSING", "FALLBACK_RETRIEVAL_USED"]
    result["retrieval"]["warnings"] = [
        "RETRIEVAL_BACKEND_ERROR: scoped Qdrant retrieval returned an error.",
        "QDRANT_INDEX_MISSING: create payload indexes for scoped retrieval filter keys.",
        "QDRANT_INDEX_MISSING_FALLBACK_USED: verified snapshot fallback context was used after Qdrant index failure.",
    ]
    result["retrieval"]["errors"] = [{"code": "QDRANT_INDEX_MISSING", "message": "index missing", "occurrence_count": 3}]

    gate = PublishGate.evaluate(result, admin_approval_exists=True)

    assert gate["can_publish"] is False
    assert "retrieval.status must be passed" in gate["missing_requirements"]
    assert "qdrant payload indexes must exist for scoped retrieval filters" in gate["missing_requirements"]
    assert "no fallback context unless admin explicitly approves" in gate["missing_requirements"]


def test_publish_gate_passes_only_when_all_controls_are_satisfied():
    result = ready_for_publish_result()

    gate = PublishGate.evaluate(result, admin_approval_exists=True)

    assert gate["can_publish"] is True
    assert gate["missing_requirements"] == []


def test_publish_gate_blocks_when_generated_resources_are_not_verified():
    result = ready_for_publish_result()
    result["tool_plan"]["exact_resources_used"] = ["public.orders"]

    gate = PublishGate.evaluate(result, admin_approval_exists=True)

    assert gate["can_publish"] is False
    assert "every generated resource must be present in retrieval.verified_resources" in gate["missing_requirements"]
