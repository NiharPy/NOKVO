from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any, Literal

from app.models.tenant_resources import TenantResources
from app.services.mcp_toolkit.capability_registry import ProviderCapabilityRegistry
from app.services.mcp_toolkit.pii_policy import PIIPolicyBuilder
from app.services.mcp_toolkit.safety_classifier import SafetyClassifier
from app.services.qdrant_service import QdrantService
from app.services.text_embedding_service import TextEmbeddingService


RetrievalStatus = Literal["passed", "low_confidence", "failed"]


@dataclass(slots=True)
class EmbeddingRetrievalRequest:
    organization_id: str
    tenant_integration_id: str
    provider_connection_id: str
    selected_context_snapshot_id: str
    integration_type: str
    provider: str
    user_prompt: str
    tenant_id: str | None = None
    normalized_intent: str | None = None
    operation_type: str | None = None
    candidate_entities: list[str] = field(default_factory=list)
    candidate_inputs: list[str] = field(default_factory=list)
    top_k: int = 20


@dataclass(slots=True)
class RetrievedEmbeddingContext:
    status: RetrievalStatus
    status_reason: str | None
    confidence: float
    fallback_used: bool
    publish_blockers: list[str]
    selected_context: dict[str, Any]
    query_variants: list[str]
    retrieved_chunks: list[dict[str, Any]]
    ranked_chunks: list[dict[str, Any]]
    verified_resources: list[dict[str, Any]]
    rejected_chunks: list[dict[str, Any]]
    retrieval_status: RetrievalStatus
    missing_context: list[str]
    scope_filter: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)


class EmbeddingRetriever:
    ALLOWED_SOURCE_KINDS = {
        capability.integration_type: set(capability.allowed_source_kinds)
        for capability in ProviderCapabilityRegistry.list_capabilities()
    }
    SYNONYMS = {
        "customer": ["customer", "customers", "contact", "contacts", "account", "accounts", "patient", "patients", "client", "clients", "customer_id"],
        "order": ["order", "orders", "purchase", "booking", "invoice", "order_status", "payment_status", "created_at"],
        "ticket": ["ticket", "tickets", "support_tickets", "case", "issue", "request", "ticket_id"],
        "call": ["call", "calls", "call_logs", "transcript", "call_history"],
        "payment": ["payment", "payments", "transaction", "transactions", "invoice", "refund", "status"],
        "appointment": ["appointment", "appointments", "booking", "bookings", "visit", "visits"],
        "phone": ["phone", "mobile", "msisdn", "phone_number"],
        "email": ["email", "email_address"],
        "status": ["status", "order_status", "payment_status", "shipment_status"],
    }

    @staticmethod
    async def retrieve(
        tenant_res: TenantResources,
        request: EmbeddingRetrievalRequest,
        snapshot_context: list[dict[str, Any]],
    ) -> dict[str, Any]:
        scope_filter = EmbeddingRetriever.scope_filter(request)
        if not EmbeddingRetriever._scope_complete(scope_filter):
            return EmbeddingRetriever._failed_result(request, scope_filter, ["selected integration context required"])

        query_variants = EmbeddingRetriever.query_variants(request.user_prompt, request.integration_type, request.operation_type, request.candidate_entities)
        retrieved_chunks = await EmbeddingRetriever._qdrant_chunks(tenant_res, request, query_variants, scope_filter)
        retrieved_chunks.extend(EmbeddingRetriever.snapshot_chunks(request, snapshot_context, scope_filter))
        return EmbeddingRetriever.rank_filter_verify(request, retrieved_chunks, query_variants, scope_filter)

    @staticmethod
    def retrieve_from_context(
        context: dict[str, Any],
        user_prompt: str,
        integration_type: str,
        provider: str,
        operation_type: str | None = None,
    ) -> dict[str, Any]:
        request = EmbeddingRetrievalRequest(
            organization_id=str(context.get("organization_id") or ""),
            tenant_id=str(context.get("tenant_id") or "") or None,
            tenant_integration_id=str(context.get("tenant_integration_id") or ""),
            provider_connection_id=str(context.get("provider_connection_id") or ""),
            selected_context_snapshot_id=str(context.get("selected_context_snapshot_id") or ""),
            integration_type=integration_type,
            provider=provider,
            user_prompt=user_prompt,
            normalized_intent=SafetyClassifier.action_text(user_prompt),
            operation_type=operation_type or SafetyClassifier.operation_type(user_prompt),
            candidate_entities=EmbeddingRetriever.extract_candidate_entities(user_prompt),
            candidate_inputs=EmbeddingRetriever.extract_candidate_inputs(user_prompt),
        )
        scope_filter = EmbeddingRetriever.scope_filter(request)
        if not EmbeddingRetriever._scope_complete(scope_filter):
            return EmbeddingRetriever._failed_result(request, scope_filter, ["selected connected integration context is required"])
        if not context.get("snapshot_context") and not context.get("embedding_context"):
            return EmbeddingRetriever._failed_result(request, scope_filter, ["selected integration context required"])
        query_variants = EmbeddingRetriever.query_variants(request.user_prompt, request.integration_type, request.operation_type, request.candidate_entities)
        chunks = EmbeddingRetriever.snapshot_chunks(request, context.get("snapshot_context", []), scope_filter)
        chunks.extend(EmbeddingRetriever.embedding_context_chunks(request, context.get("embedding_context", []), scope_filter))
        return EmbeddingRetriever.rank_filter_verify(request, chunks, query_variants, scope_filter)

    @staticmethod
    def scope_filter(request: EmbeddingRetrievalRequest) -> dict[str, Any]:
        return {
            "organization_id": request.organization_id,
            "tenant_id": request.tenant_id,
            "tenant_integration_id": request.tenant_integration_id,
            "provider_connection_id": request.provider_connection_id,
            "selected_context_snapshot_id": request.selected_context_snapshot_id,
            "context_snapshot_id": request.selected_context_snapshot_id,
            "integration_type": request.integration_type,
            "provider": request.provider,
            "status": ["active", "approved"],
        }

    @staticmethod
    def snapshot_id(snapshot_context: list[dict[str, Any]]) -> str:
        digest = hashlib.sha256(json.dumps(snapshot_context, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        return f"snapshot_{digest[:16]}"

    @staticmethod
    def extract_candidate_entities(prompt: str) -> list[str]:
        text = SafetyClassifier.action_text(prompt)
        candidates: list[str] = []
        for root, values in EmbeddingRetriever.SYNONYMS.items():
            if any(re.search(rf"\b{re.escape(value)}\b", text) for value in values):
                candidates.extend(values)
        if not candidates:
            candidates.extend(word for word in re.findall(r"[a-z0-9_]+", text) if len(word) > 3)
        return list(dict.fromkeys(candidates))

    @staticmethod
    def extract_candidate_inputs(prompt: str) -> list[str]:
        text = SafetyClassifier.action_text(prompt)
        inputs = []
        if re.search(r"\b(phone|mobile)\b", text):
            inputs.extend(["phone_number", "phone"])
        if "email" in text:
            inputs.append("email")
        if "order" in text:
            inputs.append("order_id")
        if "ticket" in text:
            inputs.append("ticket_id")
        return list(dict.fromkeys(inputs))

    @staticmethod
    def query_variants(prompt: str, integration_type: str, operation_type: str | None, candidate_entities: list[str]) -> list[str]:
        text = SafetyClassifier.action_text(prompt)
        variants = [text]
        if re.search(r"\bcustomer\b", text) and re.search(r"\bphone\b", text):
            variants.extend(
                [
                    "customer details by phone",
                    "customers phone customer_id full_name email city",
                    "lookup customer using phone",
                    "orders support tickets call logs linked to customer",
                    "customer_id relationships orders support_tickets call_logs",
                ]
            )
        if re.search(r"\b(change|update|modify|set)\b", text) and re.search(r"\buser\s*name\b|\busername\b", text):
            variants.extend(
                [
                    "update customer username",
                    "customers phone username full_name display_name",
                    "customer identity fields phone full_name username",
                    "update customer profile by customer_id",
                ]
            )
        if re.search(r"\border\b", text) and re.search(r"\bstatus\b", text):
            variants.extend(
                [
                    "order status by phone",
                    "customers phone orders customer_id order_status payment_status",
                    "latest order for customer phone",
                    "orders created_at delivered_at shipment_partner",
                ]
            )
        if re.search(r"\bticket\b", text):
            variants.extend(
                [
                    "support ticket create",
                    "support_tickets insert customer_id issue_type priority status",
                    "customers support_tickets relationship",
                    "ticket_number opened_at channel summary",
                ]
            )
        if re.search(r"\bcall|call history|logs\b", text):
            variants.extend(["customer call history by phone", "call_logs support_tickets customer_id ticket_id", "calls transcripts linked to customer"])
        if candidate_entities:
            variants.append(" ".join(candidate_entities[:12]))
        return list(dict.fromkeys(item for item in variants if item))

    @staticmethod
    async def _qdrant_chunks(
        tenant_res: TenantResources,
        request: EmbeddingRetrievalRequest,
        query_variants: list[str],
        scope_filter: dict[str, Any],
    ) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        for query in query_variants[:5]:
            try:
                vector = TextEmbeddingService.embed_text(query)
                points = await QdrantService.search_points(
                    tenant_res,
                    vector,
                    limit=request.top_k,
                    payload_filters={
                        "organization_id": request.organization_id,
                        "tenant_integration_id": request.tenant_integration_id,
                        "provider_connection_id": request.provider_connection_id,
                        "selected_context_snapshot_id": request.selected_context_snapshot_id,
                        "context_snapshot_id": request.selected_context_snapshot_id,
                        "integration_type": request.integration_type,
                        "provider": request.provider,
                        "status": ["active", "approved"],
                        "source_kind": sorted(EmbeddingRetriever.ALLOWED_SOURCE_KINDS.get(request.integration_type, set())),
                    },
                )
            except Exception as exc:
                error_code = EmbeddingRetriever._retrieval_backend_error_code(str(exc))
                chunks.append(
                    {
                        "context_id": f"qdrant_error:{hashlib.sha1(str(exc).encode()).hexdigest()[:10]}",
                        "source": "qdrant_search_error",
                        "source_kind": "retrieval_error",
                        "status": "rejected",
                        "payload": {"error": str(exc), "error_code": error_code},
                        "text": str(exc),
                        "vector_score": 0.0,
                        "query": query,
                        **{key: value for key, value in scope_filter.items() if key != "status"},
                    }
                )
                continue
            for point in points:
                payload = dict(getattr(point, "payload", {}) or {})
                chunks.append(EmbeddingRetriever._chunk_from_payload(request, payload, scope_filter, getattr(point, "score", 0.0), query))
        return chunks

    @staticmethod
    def embedding_context_chunks(request: EmbeddingRetrievalRequest, embedding_context: list[dict[str, Any]], scope_filter: dict[str, Any]) -> list[dict[str, Any]]:
        chunks = []
        for index, item in enumerate(embedding_context):
            payload = dict(item.get("payload", {}) or {})
            payload.setdefault("source_type", payload.get("source_kind") or "qdrant")
            chunks.append(EmbeddingRetriever._chunk_from_payload(request, payload, scope_filter, float(item.get("score") or 0.55), str(item.get("query") or "context"), index))
        return chunks

    @staticmethod
    def snapshot_chunks(request: EmbeddingRetrievalRequest, snapshot_context: list[dict[str, Any]], scope_filter: dict[str, Any]) -> list[dict[str, Any]]:
        chunks = []
        for index, item in enumerate(snapshot_context):
            payload = dict(item.get("payload", {}) or {})
            source = str(item.get("source") or "")
            source_kind = EmbeddingRetriever._source_kind_from_snapshot(source, payload, request.integration_type)
            resource = EmbeddingRetriever._resource_name(payload, request.integration_type)
            text = EmbeddingRetriever._payload_text(payload)
            context_id = str(payload.get("context_id") or payload.get("id") or hashlib.sha1(f"{source}:{resource}:{index}:{text}".encode("utf-8")).hexdigest())
            chunks.append(
                {
                    "context_id": context_id,
                    "source": source,
                    "source_kind": source_kind,
                    "organization_id": scope_filter["organization_id"],
                    "tenant_integration_id": scope_filter["tenant_integration_id"],
                    "provider_connection_id": scope_filter["provider_connection_id"],
                    "selected_context_snapshot_id": scope_filter["selected_context_snapshot_id"],
                    "context_snapshot_id": scope_filter["selected_context_snapshot_id"],
                    "integration_type": request.integration_type,
                    "provider": request.provider,
                    "resource": resource,
                    "status": str(payload.get("status") or "active"),
                    "payload": payload,
                    "text": text,
                    "vector_score": 0.92,
                    "source_trust_level": "snapshot",
                    "created_at": payload.get("created_at") or payload.get("updated_at"),
                }
            )
        return chunks

    @staticmethod
    def _chunk_from_payload(
        request: EmbeddingRetrievalRequest,
        payload: dict[str, Any],
        scope_filter: dict[str, Any],
        vector_score: float,
        query: str,
        index: int = 0,
    ) -> dict[str, Any]:
        source_kind = EmbeddingRetriever._source_kind_from_payload(payload, request.integration_type)
        resource = EmbeddingRetriever._resource_name(payload, request.integration_type)
        context_id = str(payload.get("context_id") or payload.get("point_id") or payload.get("id") or hashlib.sha1(f"{query}:{resource}:{index}".encode()).hexdigest())
        return {
            "context_id": context_id,
            "source": str(payload.get("source") or payload.get("source_type") or "qdrant"),
            "source_kind": source_kind,
            "organization_id": str(payload.get("organization_id") or ""),
            "tenant_integration_id": str(payload.get("tenant_integration_id") or ""),
            "provider_connection_id": str(payload.get("provider_connection_id") or ""),
            "selected_context_snapshot_id": str(payload.get("selected_context_snapshot_id") or payload.get("context_snapshot_id") or ""),
            "context_snapshot_id": str(payload.get("context_snapshot_id") or payload.get("selected_context_snapshot_id") or ""),
            "integration_type": str(payload.get("integration_type") or ""),
            "provider": str(payload.get("provider") or ""),
            "resource": resource,
            "status": str(payload.get("status") or "active"),
            "payload": payload,
            "text": EmbeddingRetriever._payload_text(payload),
            "vector_score": float(vector_score or 0.0),
            "query": query,
            "source_trust_level": str(payload.get("source_trust_level") or "qdrant"),
            "created_at": payload.get("created_at") or payload.get("updated_at"),
            "expires_at": payload.get("expires_at"),
            "deprecated": bool(payload.get("deprecated") or payload.get("is_deprecated")),
            "superseded": bool(payload.get("superseded") or payload.get("superseded_by")),
        }

    @staticmethod
    def rank_filter_verify(
        request: EmbeddingRetrievalRequest,
        chunks: list[dict[str, Any]],
        query_variants: list[str],
        scope_filter: dict[str, Any],
    ) -> dict[str, Any]:
        allowed_source_kinds = ProviderCapabilityRegistry.allowed_source_kinds(request.integration_type, request.provider) or EmbeddingRetriever.ALLOWED_SOURCE_KINDS.get(request.integration_type, set())
        verified_chunks: list[dict[str, Any]] = []
        rejected_chunks: list[dict[str, Any]] = []
        for chunk in chunks:
            reason = EmbeddingRetriever._rejection_reason(chunk, request, scope_filter, allowed_source_kinds)
            score = EmbeddingRetriever._hybrid_score(chunk, request)
            chunk = {**chunk, "retrieval_score": score}
            if reason:
                rejected_chunks.append({**chunk, "reason": reason})
                continue
            if score < 0.35:
                rejected_chunks.append({**chunk, "reason": "LOW_RELEVANCE"})
                continue
            verified_chunks.append(chunk)

        ranked_chunks = sorted(verified_chunks, key=lambda item: item.get("retrieval_score", 0.0), reverse=True)[: request.top_k]
        candidate_resources = list(dict.fromkeys(chunk.get("resource") for chunk in ranked_chunks if chunk.get("resource")))
        verified_resources = EmbeddingRetriever._verified_resources(request, ranked_chunks)
        if EmbeddingRetriever._ambiguous_prompt(request):
            verified_resources = []
        selected_resources = {item["name"] for item in verified_resources}
        selected_chunks = [chunk for chunk in ranked_chunks if chunk.get("resource") in selected_resources]
        confidence = round(max([chunk.get("retrieval_score", 0.0) for chunk in selected_chunks] or [0.0]), 3)
        backend_errors = [chunk for chunk in rejected_chunks if chunk.get("reason") in {"QDRANT_INDEX_MISSING", "RETRIEVAL_BACKEND_ERROR"}]
        fallback_used = bool(backend_errors and verified_resources)
        status_reason = None
        publish_blockers: list[str] = []
        if not verified_resources:
            status: RetrievalStatus = "failed"
            missing_context = ["verified resources required"]
            status_reason = "verified_resources_missing"
        elif confidence >= 0.80:
            status = "passed"
            missing_context = []
        elif confidence >= 0.60:
            status = "low_confidence"
            missing_context = ["retrieved resources need admin context confirmation"]
            status_reason = "retrieval_confidence_below_publish_threshold"
        else:
            status = "failed"
            missing_context = ["retrieval confidence below threshold"]
            status_reason = "retrieval_confidence_below_minimum_threshold"
        if backend_errors:
            status = "low_confidence" if verified_resources else "failed"
            missing_context = list(dict.fromkeys([*missing_context, "scoped Qdrant retrieval backend error requires resolution"]))
            if any(chunk.get("reason") == "QDRANT_INDEX_MISSING" for chunk in backend_errors):
                status_reason = "fallback_context_used_after_qdrant_index_failure" if verified_resources else "qdrant_index_missing"
                publish_blockers.append("QDRANT_INDEX_MISSING")
            else:
                status_reason = "fallback_context_used_after_scoped_retrieval_failure" if verified_resources else "retrieval_backend_error"
                publish_blockers.append("RETRIEVAL_BACKEND_ERROR")
            if verified_resources:
                publish_blockers.append("FALLBACK_RETRIEVAL_USED")

        source_context = [EmbeddingRetriever._source_context_entry(chunk) for chunk in selected_chunks]
        selected_context = {
            "snapshot_context": [EmbeddingRetriever._snapshot_item_from_chunk(chunk) for chunk in selected_chunks],
            "embedding_context": [],
            "source_context": source_context,
        }
        warnings = EmbeddingRetriever._warnings(status, rejected_chunks, fallback_used)
        errors = EmbeddingRetriever._errors_from_rejected_chunks(rejected_chunks)
        return {
            "status": status,
            "status_reason": status_reason,
            "retrieval_status": status,
            "confidence": confidence,
            "fallback_used": fallback_used,
            "publish_blockers": list(dict.fromkeys(publish_blockers)),
            "query_variants": query_variants,
            "scope_filter": scope_filter,
            "candidate_resources": candidate_resources,
            "selected_context": selected_context,
            "retrieved_chunks": chunks,
            "ranked_chunks": ranked_chunks,
            "verified_resources": verified_resources,
            "rejected_chunks": rejected_chunks,
            "missing_context": missing_context,
            "retrieved_context_ids": [chunk["context_id"] for chunk in ranked_chunks],
            "rejected_context_ids": [chunk["context_id"] for chunk in rejected_chunks],
            "warnings": warnings,
            "errors": errors,
            "source_context": source_context,
        }

    @staticmethod
    def _verified_resources(request: EmbeddingRetrievalRequest, ranked_chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if request.integration_type == "database":
            return EmbeddingRetriever._database_verified_resources(request, ranked_chunks)
        resources = []
        seen = set()
        for chunk in ranked_chunks:
            resource = chunk.get("resource")
            if not resource or resource in seen:
                continue
            payload = chunk.get("payload") or {}
            resources.append(
                {
                    "resource_type": "endpoint" if payload.get("endpoint") else "object",
                    "name": resource,
                    "method": payload.get("method"),
                    "path": payload.get("endpoint"),
                    "operation_id": payload.get("operation_id") or payload.get("action") or payload.get("name"),
                    "request_schema": payload.get("request_schema") or {},
                    "response_schema": payload.get("response_schema") or {},
                    "auth_required": True,
                    "source_context_ids": [chunk["context_id"]],
                    "confidence": chunk["retrieval_score"],
                }
            )
            seen.add(resource)
        return resources

    @staticmethod
    def _database_verified_resources(request: EmbeddingRetrievalRequest, ranked_chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        text = SafetyClassifier.action_text(request.user_prompt)
        candidate_names = set(request.candidate_entities)
        by_resource: dict[str, dict[str, Any]] = {}
        for chunk in ranked_chunks:
            payload = chunk.get("payload") or {}
            resource = chunk.get("resource")
            if not resource:
                continue
            columns = EmbeddingRetriever._columns_from_payload(payload, resource)
            by_resource.setdefault(
                resource,
                {
                    "resource_type": "table",
                    "name": resource,
                    "columns": columns,
                    "relationships": [],
                    "source_context_ids": [],
                    "confidence": chunk["retrieval_score"],
                },
            )
            by_resource[resource]["source_context_ids"].append(chunk["context_id"])
            by_resource[resource]["confidence"] = max(by_resource[resource]["confidence"], chunk["retrieval_score"])

        ordered = sorted(by_resource.values(), key=lambda item: item["confidence"], reverse=True)
        primary = EmbeddingRetriever._primary_database_resource(ordered, candidate_names, text)
        if primary:
            ordered = [primary] + [item for item in ordered if item["name"] != primary["name"]]
        selected = EmbeddingRetriever._select_related_database_resources(ordered, text)
        EmbeddingRetriever._add_relationships(selected)
        return selected

    @staticmethod
    def _primary_database_resource(resources: list[dict[str, Any]], candidate_names: set[str], text: str) -> dict[str, Any] | None:
        if not resources:
            return None
        if "phone" in text:
            customer = next((item for item in resources if "customer" in item["name"].lower() and any(col["name"] in {"phone", "mobile", "phone_number"} for col in item["columns"])), None)
            if customer:
                return customer
        direct = [item for item in resources if any(token in item["name"].lower() for token in candidate_names)]
        return direct[0] if direct else resources[0]

    @staticmethod
    def _select_related_database_resources(resources: list[dict[str, Any]], text: str) -> list[dict[str, Any]]:
        if not resources:
            return []
        selected = [resources[0]]
        names = {resources[0]["name"]}
        def add_matching(tokens: tuple[str, ...]) -> None:
            for item in resources:
                if item["name"] in names:
                    continue
                if any(token in item["name"].lower() for token in tokens):
                    selected.append(item)
                    names.add(item["name"])
        if "order" in text or "status" in text:
            add_matching(("order",))
        if "call" in text or "history" in text:
            add_matching(("support_ticket", "ticket"))
            add_matching(("call", "log"))
        if re.search(r"\b(related|summary|details|records)\b", text):
            add_matching(("order", "support_ticket", "ticket", "call", "log"))
        return selected[:6]

    @staticmethod
    def _add_relationships(resources: list[dict[str, Any]]) -> None:
        for left in resources:
            left_columns = {column["name"] for column in left.get("columns", [])}
            for right in resources:
                if left["name"] >= right["name"]:
                    continue
                right_columns = {column["name"] for column in right.get("columns", [])}
                for column_name in sorted(left_columns & right_columns):
                    if column_name == "id" or column_name.endswith("_id"):
                        relation = {
                            "from": f"{left['name']}.{column_name}",
                            "to": f"{right['name']}.{column_name}",
                            "source": "inferred",
                            "confidence": 0.86,
                        }
                        left["relationships"].append(relation)
                        right["relationships"].append({**relation, "from": relation["to"], "to": relation["from"]})

    @staticmethod
    def _columns_from_payload(payload: dict[str, Any], resource: str) -> list[dict[str, Any]]:
        columns = []
        primary_key = payload.get("primary_key") or payload.get("pk") or []
        primary_keys = {primary_key} if isinstance(primary_key, str) else set(primary_key)
        for column in payload.get("columns") or []:
            if isinstance(column, dict):
                name = str(column.get("name") or "")
                column_type = str(column.get("type") or column.get("data_type") or "unknown")
                nullable = bool(column.get("nullable")) if column.get("nullable") is not None else True
            else:
                name = str(column)
                column_type = "unknown"
                nullable = True
            if not name:
                continue
            classification = PIIPolicyBuilder.classify_field(name, column_type)
            columns.append(
                {
                    "name": name,
                    "type": column_type,
                    "nullable": nullable,
                    "is_primary_key": name in primary_keys,
                    "is_searchable": any(token in name.lower() for token in ("id", "name", "email", "phone", "status")),
                    "pii_classification": classification,
                }
            )
        return columns

    @staticmethod
    def _rejection_reason(chunk: dict[str, Any], request: EmbeddingRetrievalRequest, scope_filter: dict[str, Any], allowed_source_kinds: set[str]) -> str:
        if chunk.get("source") == "qdrant_search_error":
            return str((chunk.get("payload") or {}).get("error_code") or "RETRIEVAL_BACKEND_ERROR")
        for key in ("organization_id", "tenant_integration_id", "provider_connection_id"):
            value = chunk.get(key)
            if value and value != scope_filter[key]:
                return "TENANT_SCOPE_MISMATCH"
        snapshot = chunk.get("selected_context_snapshot_id") or chunk.get("context_snapshot_id")
        if snapshot and snapshot != scope_filter["selected_context_snapshot_id"]:
            return "CONTEXT_SNAPSHOT_MISMATCH"
        if chunk.get("integration_type") and chunk.get("integration_type") != request.integration_type:
            return "PROVIDER_MISMATCH"
        if chunk.get("provider") and chunk.get("provider") != request.provider:
            return "PROVIDER_MISMATCH"
        if chunk.get("source_kind") not in allowed_source_kinds:
            return "SOURCE_KIND_NOT_ALLOWED"
        if chunk.get("status") not in {"active", "approved"}:
            return "DEPRECATED_CONTEXT"
        if chunk.get("deprecated") or chunk.get("superseded"):
            return "DEPRECATED_CONTEXT"
        if EmbeddingRetriever._expired(chunk.get("expires_at")):
            return "DEPRECATED_CONTEXT"
        return ""

    @staticmethod
    def _hybrid_score(chunk: dict[str, Any], request: EmbeddingRetrievalRequest) -> float:
        text = f"{chunk.get('resource') or ''} {chunk.get('text') or ''}".lower()
        candidates = set(request.candidate_entities or EmbeddingRetriever.extract_candidate_entities(request.user_prompt))
        columns = {str(column.get("name") if isinstance(column, dict) else column).lower() for column in (chunk.get("payload") or {}).get("columns") or []}
        vector_score = max(0.0, min(float(chunk.get("vector_score") or 0.0), 1.0))
        entity_overlap = EmbeddingRetriever._overlap_score(candidates, text)
        column_match = EmbeddingRetriever._overlap_score(set(request.candidate_inputs) | candidates, " ".join(columns))
        operation = 1.0 if EmbeddingRetriever._operation_compatible(chunk, request.operation_type) else 0.35
        relationship = 1.0 if any(token in text for token in ("customer_id", "ticket_id", "order_id", "relationship", "foreign")) else 0.3
        freshness = 1.0 if chunk.get("status") in {"active", "approved"} and not chunk.get("deprecated") else 0.0
        scope = 1.0 if chunk.get("integration_type") == request.integration_type and chunk.get("provider") == request.provider else 0.0
        score = (
            0.45 * vector_score
            + 0.20 * entity_overlap
            + 0.15 * column_match
            + 0.10 * operation
            + 0.05 * relationship
            + 0.03 * freshness
            + 0.02 * scope
        )
        if chunk.get("source_trust_level") == "snapshot":
            score += 0.12
        resource_text = str(chunk.get("resource") or "").lower()
        if any(candidate and candidate in resource_text for candidate in candidates):
            score += 0.08
        return round(min(score, 1.0), 3)

    @staticmethod
    def _overlap_score(candidates: set[str], text: str) -> float:
        if not candidates:
            return 0.0
        hits = sum(1 for candidate in candidates if candidate and re.search(rf"\b{re.escape(candidate.lower())}\b", text))
        return min(hits / max(min(len(candidates), 6), 1), 1.0)

    @staticmethod
    def _operation_compatible(chunk: dict[str, Any], operation_type: str | None) -> bool:
        if not operation_type or operation_type == "read":
            return True
        if chunk.get("source_kind") == "db_table" and operation_type in {"create", "update", "delete"}:
            return True
        text = f"{chunk.get('source_kind')} {chunk.get('text')}".lower()
        if operation_type == "create":
            return any(token in text for token in ("create", "insert", "post", "open", "raise", "add"))
        if operation_type == "update":
            return any(token in text for token in ("update", "patch", "change", "modify"))
        if operation_type == "delete":
            return any(token in text for token in ("delete", "cancel", "remove"))
        return True

    @staticmethod
    def _source_context_entry(chunk: dict[str, Any]) -> dict[str, Any]:
        return {
            "context_id": chunk["context_id"],
            "source": chunk.get("source"),
            "source_kind": chunk.get("source_kind"),
            "organization_id": chunk.get("organization_id"),
            "tenant_integration_id": chunk.get("tenant_integration_id"),
            "provider_connection_id": chunk.get("provider_connection_id"),
            "context_snapshot_id": chunk.get("selected_context_snapshot_id") or chunk.get("context_snapshot_id"),
            "integration_type": chunk.get("integration_type"),
            "provider": chunk.get("provider"),
            "resource": chunk.get("resource"),
            "status": chunk.get("status"),
            "retrieval_score": chunk.get("retrieval_score"),
        }

    @staticmethod
    def _snapshot_item_from_chunk(chunk: dict[str, Any]) -> dict[str, Any]:
        source = chunk.get("source") or "retrieval"
        if chunk.get("source_kind") == "db_table" and source == "retrieval":
            source = "provider_status.db_schema_snapshot"
        return {"source": source, "payload": chunk.get("payload") or {}, "context_id": chunk.get("context_id"), "retrieval_score": chunk.get("retrieval_score")}

    @staticmethod
    def _source_kind_from_snapshot(source: str, payload: dict[str, Any], integration_type: str) -> str:
        if integration_type == "database":
            return "db_table"
        if integration_type in {"crm", "zoho_desk"}:
            return "crm_action_template" if payload.get("endpoint") or payload.get("method") or payload.get("action") else "crm_object_schema"
        if integration_type == "erp":
            return "erp_action_template" if payload.get("endpoint") or payload.get("method") or payload.get("action") else "erp_object_schema"
        if integration_type in {"shipping", "ecommerce"}:
            return "ecommerce_action_template" if payload.get("endpoint") or payload.get("method") or payload.get("action") else "ecommerce_object_schema"
        if integration_type == "custom_api":
            return "api_endpoint" if payload.get("endpoint") or payload.get("path") else "openapi_spec"
        return payload.get("source_kind") or payload.get("source_type") or "connector_template"

    @staticmethod
    def _source_kind_from_payload(payload: dict[str, Any], integration_type: str) -> str:
        source_type = payload.get("source_kind") or payload.get("source_type")
        mapping = {
            "database_schema_selection": "db_table",
            "crm_schema": "crm_object_schema",
            "crm_action": "crm_action_template",
            "erp_schema": "erp_object_schema",
            "erp_action": "erp_action_template",
            "shipping_schema": "ecommerce_object_schema",
            "shipping_action": "ecommerce_action_template",
        }
        return mapping.get(str(source_type), str(source_type or EmbeddingRetriever._source_kind_from_snapshot("", payload, integration_type)))

    @staticmethod
    def _resource_name(payload: dict[str, Any], integration_type: str) -> str:
        if integration_type == "database":
            table = payload.get("table") or payload.get("name") or payload.get("module")
            schema = payload.get("schema") or payload.get("schema_name") or "public"
            return f"{schema}.{table}" if table and "." not in str(table) else str(table or "")
        return str(payload.get("endpoint") or payload.get("path") or payload.get("action") or payload.get("name") or payload.get("module") or "")

    @staticmethod
    def _payload_text(payload: dict[str, Any]) -> str:
        if payload.get("text"):
            return str(payload["text"])
        return json.dumps(payload, default=str)

    @staticmethod
    def _scope_complete(scope_filter: dict[str, Any]) -> bool:
        required = ["organization_id", "tenant_integration_id", "provider_connection_id", "selected_context_snapshot_id", "integration_type", "provider"]
        return all(scope_filter.get(key) for key in required)

    @staticmethod
    def _expired(expires_at: Any) -> bool:
        if not expires_at:
            return False
        try:
            return datetime.fromisoformat(str(expires_at).replace("Z", "+00:00")) < datetime.now(timezone.utc)
        except ValueError:
            return False

    @staticmethod
    def _warnings(status: str, rejected_chunks: list[dict[str, Any]], fallback_context_available: bool = False) -> list[str]:
        warnings = []
        if status == "low_confidence":
            warnings.append("Retrieval confidence is below publish threshold and requires admin context confirmation.")
        backend_errors = [chunk for chunk in rejected_chunks if chunk.get("reason") in {"QDRANT_INDEX_MISSING", "RETRIEVAL_BACKEND_ERROR"}]
        if backend_errors:
            warnings.append("RETRIEVAL_BACKEND_ERROR: scoped Qdrant retrieval returned an error.")
        if any(chunk.get("reason") == "QDRANT_INDEX_MISSING" for chunk in backend_errors):
            warnings.append("QDRANT_INDEX_MISSING: create payload indexes for scoped retrieval filter keys.")
            if fallback_context_available:
                warnings.append("QDRANT_INDEX_MISSING_FALLBACK_USED: verified snapshot fallback context was used after Qdrant index failure.")
        elif backend_errors and fallback_context_available:
            warnings.append("FALLBACK_RETRIEVAL_USED: verified snapshot fallback context was used after Qdrant retrieval failure.")
        scope_rejections = [chunk for chunk in rejected_chunks if chunk.get("reason") in {"TENANT_SCOPE_MISMATCH", "CONTEXT_SNAPSHOT_MISMATCH", "PROVIDER_MISMATCH"} and chunk.get("retrieval_score", 0.0) >= 0.6]
        if scope_rejections:
            warnings.append("High-relevance chunks were rejected due to scope/provider/snapshot mismatch.")
        return warnings

    @staticmethod
    def _errors_from_rejected_chunks(rejected_chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        messages = {
            "QDRANT_INDEX_MISSING": "Qdrant payload index is missing for one or more scoped retrieval keys.",
            "RETRIEVAL_BACKEND_ERROR": "Scoped retrieval backend returned an unresolved error.",
            "PROVIDER_MISMATCH": "Retrieved chunk provider did not match the selected integration provider.",
            "CONTEXT_SNAPSHOT_MISMATCH": "Retrieved chunk snapshot did not match the selected context snapshot.",
            "TENANT_SCOPE_MISMATCH": "Retrieved chunk scope did not match the selected integration.",
        }
        for chunk in rejected_chunks:
            code = str(chunk.get("reason") or "")
            if code in messages:
                counts[code] = counts.get(code, 0) + 1
        return [
            {"code": code, "message": messages[code], "occurrence_count": count}
            for code, count in sorted(counts.items())
        ]

    @staticmethod
    def _retrieval_backend_error_code(message: str) -> str:
        lowered = message.lower()
        if "payload" in lowered and "index" in lowered:
            return "QDRANT_INDEX_MISSING"
        if "not indexed" in lowered or "index required" in lowered or "field index" in lowered:
            return "QDRANT_INDEX_MISSING"
        return "RETRIEVAL_BACKEND_ERROR"

    @staticmethod
    def _ambiguous_prompt(request: EmbeddingRetrievalRequest) -> bool:
        text = SafetyClassifier.action_text(request.user_prompt)
        strong_entities = {"customer", "order", "ticket", "phone", "email", "payment", "appointment", "call"}
        has_entity = any(re.search(rf"\b{entity}\b", text) for entity in strong_entities)
        has_action = bool(re.search(r"\b(find|lookup|search|retrieve|check|get|view|list|fetch|create|add|insert|open|raise|log|change|modify|update|mark|remove|delete|cancel)\b", text))
        if has_entity and has_action:
            return False
        return len([word for word in re.findall(r"[a-z0-9_]+", text) if word not in {"make", "tool", "show", "information", "logs"}]) == 0

    @staticmethod
    def _failed_result(request: EmbeddingRetrievalRequest, scope_filter: dict[str, Any], missing_context: list[str]) -> dict[str, Any]:
        return {
            "status": "failed",
            "status_reason": "selected_integration_context_missing",
            "retrieval_status": "failed",
            "confidence": 0.0,
            "fallback_used": False,
            "publish_blockers": ["SELECTED_INTEGRATION_REQUIRED"],
            "query_variants": EmbeddingRetriever.query_variants(request.user_prompt, request.integration_type, request.operation_type, request.candidate_entities),
            "scope_filter": scope_filter,
            "candidate_resources": [],
            "selected_context": {"snapshot_context": [], "embedding_context": [], "source_context": []},
            "retrieved_chunks": [],
            "ranked_chunks": [],
            "verified_resources": [],
            "rejected_chunks": [],
            "missing_context": missing_context,
            "retrieved_context_ids": [],
            "rejected_context_ids": [],
            "warnings": missing_context,
            "errors": [{"code": "SELECTED_INTEGRATION_REQUIRED", "message": missing_context[0], "occurrence_count": 1}] if missing_context else [],
            "source_context": [],
        }
