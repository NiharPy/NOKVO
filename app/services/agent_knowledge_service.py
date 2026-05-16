from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
import uuid
import zipfile
from io import BytesIO
from typing import Any
from xml.etree import ElementTree

from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization_user import OrganizationUser
from app.models.tenant_resources import TenantResources
from app.services.azure_blob_service import AzureBlobService
from app.services.qdrant_service import QdrantService
from app.services.text_embedding_service import TextEmbeddingService
from app.services.agent_intent_service import detect_language


AGENT_DOCUMENT_TYPES = {"policy", "faq", "script", "compliance", "product_docs", "training", "other"}
AGENT_KNOWLEDGE_SOURCE_TYPE = "agent_knowledge"
AGENT_CHUNK_SOURCE_KIND = "agent_document_chunk"
AGENT_ANSWER_CARDS_KEY = "agent_answer_cards"
AGENT_POLICY_VERSION_KEY = "agent_policy_version"

_SENSITIVE_POLICY_RE = re.compile(
    r"\b(refund|payment|card|upi|bank|cancel|delete|account|medical|legal|password|otp|kyc)\b",
    re.IGNORECASE,
)


def normalize_for_match(text: str) -> str:
    value = re.sub(r"[^\w\s]", " ", (text or "").lower())
    value = re.sub(r"\s+", " ", value).strip()
    return value


class AgentKnowledgeService:
    """Organization-scoped RAG knowledge lifecycle for the voice/answering agent."""

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _provider_status(tenant_res: TenantResources) -> dict:
        return dict(tenant_res.provider_status or {})

    @staticmethod
    def _documents(provider_status: dict) -> list[dict]:
        return list(provider_status.get("agent_knowledge_documents") or [])

    @staticmethod
    def _set_documents(provider_status: dict, documents: list[dict]) -> None:
        provider_status["agent_knowledge_documents"] = documents[-500:]

    @staticmethod
    def policy_version(tenant_res: TenantResources) -> str:
        provider_status = AgentKnowledgeService._provider_status(tenant_res)
        return str(provider_status.get(AGENT_POLICY_VERSION_KEY) or "pv_default")

    @staticmethod
    def _bump_policy_version(provider_status: dict) -> str:
        version = f"pv_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
        provider_status[AGENT_POLICY_VERSION_KEY] = version
        return version

    @staticmethod
    def _answer_cards(provider_status: dict) -> list[dict]:
        return list(provider_status.get(AGENT_ANSWER_CARDS_KEY) or [])

    @staticmethod
    def _set_answer_cards(provider_status: dict, cards: list[dict]) -> None:
        provider_status[AGENT_ANSWER_CARDS_KEY] = cards[-1000:]

    @staticmethod
    def _normalize_tags(tags: str | list[str] | None) -> list[str]:
        if not tags:
            return []
        if isinstance(tags, str):
            values = tags.split(",")
        else:
            values = tags
        normalized: list[str] = []
        for tag in values:
            item = str(tag).strip().lower()
            if item and item not in normalized:
                normalized.append(item[:64])
        return normalized[:20]

    @staticmethod
    def _document_response(document: dict) -> dict:
        return {
            "id": document.get("id"),
            "name": document.get("name"),
            "document_type": document.get("document_type"),
            "description": document.get("description"),
            "tags": list(document.get("tags") or []),
            "status": document.get("status"),
            "approval_status": document.get("approval_status"),
            "blob_path": document.get("blob_path"),
            "chunk_count": int(document.get("chunk_count") or 0),
            "qdrant_point_count": int(document.get("qdrant_point_count") or 0),
            "uploaded_by": document.get("uploaded_by"),
            "created_at": document.get("created_at"),
            "approved_at": document.get("approved_at"),
            "approved_by": document.get("approved_by"),
            "last_error": document.get("last_error"),
        }

    @staticmethod
    def list_documents(tenant_res: TenantResources) -> list[dict]:
        provider_status = AgentKnowledgeService._provider_status(tenant_res)
        documents = sorted(
            AgentKnowledgeService._documents(provider_status),
            key=lambda item: item.get("created_at") or "",
            reverse=True,
        )
        return [AgentKnowledgeService._document_response(document) for document in documents]

    @staticmethod
    def _extract_text(filename: str, content: bytes) -> str:
        suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if suffix == "docx":
            try:
                with zipfile.ZipFile(BytesIO(content)) as archive:
                    xml_bytes = archive.read("word/document.xml")
                root = ElementTree.fromstring(xml_bytes)
                texts = [node.text or "" for node in root.iter() if node.tag.endswith("}t")]
                return "\n".join(texts)
            except Exception:
                return ""
        if suffix == "pdf":
            try:
                import pypdf

                reader = pypdf.PdfReader(BytesIO(content))
                return "\n\n".join(page.extract_text() or "" for page in reader.pages)
            except Exception:
                pass
            decoded = content.decode("utf-8", errors="ignore")
            return re.sub(r"\s+", " ", decoded)
        return content.decode("utf-8", errors="ignore")

    @staticmethod
    def _chunk_text(text: str, max_tokens: int = 450, overlap_tokens: int = 50) -> list[dict]:
        cleaned = re.sub(r"\r\n?", "\n", text or "").strip()
        if not cleaned:
            return []
        paragraphs = [re.sub(r"\s+", " ", item).strip() for item in re.split(r"\n{2,}", cleaned) if item.strip()]
        if len(paragraphs) == 1 and len(paragraphs[0].split()) > max_tokens:
            paragraphs = [item.strip() for item in re.split(r"(?<=[.!?।])\s+", paragraphs[0]) if item.strip()]
        chunks: list[dict] = []
        current_words: list[str] = []
        cursor = 0
        start = 0

        def flush() -> None:
            nonlocal current_words, start
            if not current_words:
                return
            chunk_text = " ".join(current_words).strip()
            chunks.append({"text": chunk_text, "char_start": start, "char_end": start + len(chunk_text)})
            current_words = current_words[-overlap_tokens:] if overlap_tokens else []
            start = max(start + len(chunk_text) - len(" ".join(current_words)), 0)

        for paragraph in paragraphs:
            words = paragraph.split()
            if not words:
                continue
            for word in words:
                if not current_words:
                    start = cursor
                if len(current_words) >= max_tokens:
                    flush()
                current_words.append(word)
                cursor += len(word) + 1
            if len(current_words) >= max_tokens:
                flush()
        if current_words:
            flush()
        return chunks[:200]

    @staticmethod
    def _chunk_payload(
        tenant_res: TenantResources,
        document: dict,
        chunk: dict,
        chunk_index: int,
        approval_status: str,
        status: str,
        policy_version: str | None = None,
    ) -> dict:
        chunk_id = f"{document['id']}:chunk:{chunk_index}"
        text = chunk["text"]
        language = chunk.get("language") or detect_language(text)
        sensitivity = "sensitive" if _SENSITIVE_POLICY_RE.search(text) else "normal"
        active = approval_status == "approved" and status in {"active", "ok"}
        return {
            "organization_id": str(tenant_res.organization_id),
            "tenant_id": tenant_res.tenant_id,
            "source_type": AGENT_KNOWLEDGE_SOURCE_TYPE,
            "source_kind": AGENT_CHUNK_SOURCE_KIND,
            "resource_type": "document_chunk",
            "resource": f"agent_knowledge/{document['id']}/{chunk_index}",
            "document_id": document["id"],
            "document_version_id": document["document_version_id"],
            "document_name": document["name"],
            "document_type": document["document_type"],
            "doc_type": document["document_type"],
            "document_status": status,
            "approval_status": approval_status,
            "status": "active" if active else "pending_approval",
            "active": active,
            "chunk_id": chunk_id,
            "chunk_index": chunk_index,
            "chunk_count": document["chunk_count"],
            "text": text,
            "language": language,
            "policy_version": policy_version or document.get("policy_version") or "pv_default",
            "topic": AgentKnowledgeService._topic_for_text(text),
            "sensitivity": sensitivity,
            "source_title": document.get("name"),
            "char_start": chunk.get("char_start", 0),
            "char_end": chunk.get("char_end", 0),
            "token_count": max(1, len(text.split())),
            "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "blob_path": document.get("blob_path"),
            "created_at": document.get("created_at"),
            "approved_at": document.get("approved_at"),
            "approved_by": document.get("approved_by"),
            "tags": list(document.get("tags") or []),
        }

    @staticmethod
    async def _upsert_document_chunks(
        tenant_res: TenantResources,
        document: dict,
        approval_status: str,
        status: str,
        db: AsyncSession | None = None,
    ) -> int:
        await QdrantService.delete_points_by_filter(
            tenant_res,
            {"source_type": AGENT_KNOWLEDGE_SOURCE_TYPE, "document_id": document["id"]},
            db=db,
        )
        chunks_list = document.get("chunks") or []
        if not chunks_list:
            return 0
            
        policy_version = str(document.get("policy_version") or AgentKnowledgeService.policy_version(tenant_res))
        document["policy_version"] = policy_version
        texts = [
            AgentKnowledgeService._chunk_payload(
                tenant_res, document, chunk, i, approval_status, status, policy_version
            )["text"]
            for i, chunk in enumerate(chunks_list)
        ]
        vectors = await TextEmbeddingService.embed_texts(texts)

        points: list[dict[str, Any]] = []
        for index, chunk in enumerate(chunks_list):
            payload = AgentKnowledgeService._chunk_payload(
                tenant_res, document, chunk, index, approval_status, status, policy_version
            )
            points.append(
                {
                    "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{tenant_res.tenant_id}:{payload['chunk_id']}")),
                    "vector": vectors[index],
                    "payload": payload,
                }
            )
        if points:
            await QdrantService.upsert_points(tenant_res, points, db=db)
        return len(points)

    @staticmethod
    def _topic_for_text(text: str) -> str:
        lower = (text or "").lower()
        for topic, terms in {
            "refund": ("refund", "compensation", "money back", "रिफंड"),
            "cancellation": ("cancel", "cancellation", "कैंसल"),
            "delivery": ("delivery", "late", "delivered", "rider"),
            "payment": ("payment", "charged", "upi", "card", "wallet"),
            "food_quality": ("cold", "stale", "wrong item", "missing item", "food safety"),
            "account": ("account", "login", "otp", "password"),
        }.items():
            if any(term in lower for term in terms):
                return topic
        return "general"

    @staticmethod
    def _answer_card_sensitivity(text: str) -> str:
        return "sensitive" if _SENSITIVE_POLICY_RE.search(text or "") else "normal"

    @staticmethod
    def _build_answer_cards(document: dict, policy_version: str) -> list[dict]:
        """Create low-latency answer cards from Q/A-style knowledge.

        This is intentionally conservative: only explicit Q:/A: pairs become
        cards. Free-form policy prose still uses Qdrant + grounded LLM.
        """
        cards: list[dict] = []
        for index, chunk in enumerate(document.get("chunks") or []):
            text = str(chunk.get("text") or "")
            pairs = re.findall(
                r"(?is)(?:^|\n)\s*(?:q|question)\s*[:\-]\s*(.{8,220}?)\s*\n\s*(?:a|answer)\s*[:\-]\s*(.{8,700}?)(?=\n\s*(?:q|question)\s*[:\-]|\Z)",
                text,
            )
            for q, a in pairs:
                question = re.sub(r"\s+", " ", q).strip()
                answer = re.sub(r"\s+", " ", a).strip()
                if not question or not answer:
                    continue
                language = detect_language(question + " " + answer)
                sensitivity = AgentKnowledgeService._answer_card_sensitivity(question + " " + answer)
                cards.append(
                    {
                        "id": f"{document['id']}:card:{index}:{len(cards)}",
                        "document_id": document["id"],
                        "topic": AgentKnowledgeService._topic_for_text(question + " " + answer),
                        "canonical_questions": [question[:220]],
                        "short_answer_by_language": {language: answer[:420]},
                        "source_chunk_ids": [f"{document['id']}:chunk:{index}"],
                        "policy_version": policy_version,
                        "sensitivity": sensitivity,
                        "cacheable": sensitivity == "normal",
                        "requires_tool": False,
                        "approval_status": document.get("approval_status"),
                        "status": document.get("status"),
                    }
                )
        return cards[:100]

    @staticmethod
    def find_answer_card(tenant_res: TenantResources, query: str, language: str = "en") -> dict[str, Any] | None:
        provider_status = AgentKnowledgeService._provider_status(tenant_res)
        policy_version = AgentKnowledgeService.policy_version(tenant_res)
        query_norm = normalize_for_match(query)
        if not query_norm:
            return None
        best: tuple[float, dict[str, Any]] | None = None
        query_terms = set(query_norm.split())
        for card in AgentKnowledgeService._answer_cards(provider_status):
            if card.get("policy_version") != policy_version:
                continue
            if card.get("approval_status") != "approved" or card.get("status") != "active":
                continue
            for question in card.get("canonical_questions") or []:
                q_norm = normalize_for_match(str(question))
                q_terms = set(q_norm.split())
                if not q_terms:
                    continue
                overlap = len(query_terms & q_terms) / max(len(query_terms), len(q_terms))
                exact_bonus = 0.2 if query_norm == q_norm else 0.0
                score = min(1.0, overlap + exact_bonus)
                if score >= 0.72 and (best is None or score > best[0]):
                    best = (score, card)
        if not best:
            return None
        card = dict(best[1])
        answers = dict(card.get("short_answer_by_language") or {})
        card["answer"] = answers.get(language) or answers.get("en") or next(iter(answers.values()), "")
        card["score"] = best[0]
        return card

    @staticmethod
    async def upload_document(
        tenant_res: TenantResources,
        db: AsyncSession,
        current_user: OrganizationUser,
        *,
        name: str,
        document_type: str,
        description: str | None,
        tags: str | list[str] | None,
        filename: str,
        content: bytes,
        content_type: str | None,
    ) -> dict:
        document_type = (document_type or "other").strip().lower()
        if document_type not in AGENT_DOCUMENT_TYPES:
            document_type = "other"
        document_id = str(uuid.uuid4())
        created_at = AgentKnowledgeService._now()
        blob = await AzureBlobService.upload_agent_knowledge_document(
            tenant_res.tenant_id,
            tenant_res.blob_prefix,
            document_id,
            filename,
            content,
            content_type,
        )
        extracted_text = AgentKnowledgeService._extract_text(filename, content)
        chunks = AgentKnowledgeService._chunk_text(extracted_text)
        status = "pending" if chunks else "empty"
        document = {
            "id": document_id,
            "document_version_id": str(uuid.uuid4()),
            "name": name.strip()[:160],
            "document_type": document_type,
            "description": (description or "").strip()[:1000] or None,
            "tags": AgentKnowledgeService._normalize_tags(tags),
            "status": status,
            "approval_status": "approved" if chunks else "rejected",
            "blob_path": blob["blob_path"],
            "blob_name": blob["blob_name"],
            "content_type": content_type,
            "size_bytes": len(content),
            "chunk_count": len(chunks),
            "qdrant_point_count": 0,
            "chunks": chunks,
            "uploaded_by": str(current_user.id),
            "created_at": created_at,
            "approved_at": created_at if chunks else None,
            "approved_by": str(current_user.id) if chunks else None,
            "last_error": None if chunks else "No usable text was found in this document.",
        }
        provider_status = AgentKnowledgeService._provider_status(tenant_res)
        if chunks:
            try:
                document["policy_version"] = AgentKnowledgeService._bump_policy_version(provider_status)
                document["status"] = "ok"
                document["qdrant_point_count"] = await AgentKnowledgeService._upsert_document_chunks(
                    tenant_res,
                    document,
                    "approved",
                    "ok",
                    db=db,
                )
            except Exception as exc:
                document["status"] = "error"
                document["approval_status"] = "rejected"
                document["approved_at"] = None
                document["approved_by"] = None
                document["last_error"] = str(exc)[:500]

        documents = AgentKnowledgeService._documents(provider_status)
        documents.append(document)
        AgentKnowledgeService._set_documents(provider_status, documents)
        if not (chunks and document["status"] == "ok"):
            provider_status.setdefault(AGENT_POLICY_VERSION_KEY, "pv_default")
        tenant_res.provider_status = provider_status
        flag_modified(tenant_res, "provider_status")
        db.add(tenant_res)
        await db.commit()
        await db.refresh(tenant_res)
        return AgentKnowledgeService._document_response(document)

    @staticmethod
    async def review_document(
        tenant_res: TenantResources,
        db: AsyncSession,
        current_user: OrganizationUser,
        document_id: str,
        approve: bool,
        notes: str | None = None,
    ) -> dict:
        provider_status = AgentKnowledgeService._provider_status(tenant_res)
        documents = AgentKnowledgeService._documents(provider_status)
        selected = None
        for document in documents:
            if document.get("id") == document_id:
                selected = document
                break
        if not selected:
            raise ValueError("Agent Knowledge document not found")
        if not selected.get("chunks"):
            raise ValueError("Document has no indexed chunks to approve")

        selected["approval_status"] = "approved" if approve else "rejected"
        selected["status"] = "active" if approve else "rejected"
        selected["review_notes"] = (notes or "").strip()[:1000] or None
        selected["approved_at"] = AgentKnowledgeService._now() if approve else None
        selected["approved_by"] = str(current_user.id) if approve else None
        existing_cards = [
            card for card in AgentKnowledgeService._answer_cards(provider_status)
            if card.get("document_id") != selected["id"]
        ]
        if approve:
            policy_version = AgentKnowledgeService._bump_policy_version(provider_status)
            selected["policy_version"] = policy_version
            selected["qdrant_point_count"] = await AgentKnowledgeService._upsert_document_chunks(
                tenant_res,
                selected,
                "approved",
                "active",
                db=db,
            )
            existing_cards.extend(AgentKnowledgeService._build_answer_cards(selected, policy_version))
        else:
            selected["qdrant_point_count"] = 0
            await QdrantService.delete_points_by_filter(
                tenant_res,
                {"source_type": AGENT_KNOWLEDGE_SOURCE_TYPE, "document_id": selected["id"]},
                db=db,
            )
            AgentKnowledgeService._bump_policy_version(provider_status)
        AgentKnowledgeService._set_answer_cards(provider_status, existing_cards)

        AgentKnowledgeService._set_documents(provider_status, documents)
        tenant_res.provider_status = provider_status
        flag_modified(tenant_res, "provider_status")
        db.add(tenant_res)
        await db.commit()
        await db.refresh(tenant_res)
        return AgentKnowledgeService._document_response(selected)

    @staticmethod
    async def rechunk_documents(
        tenant_res: TenantResources,
        db: AsyncSession,
        *,
        document_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        provider_status = AgentKnowledgeService._provider_status(tenant_res)
        documents = AgentKnowledgeService._documents(provider_status)
        updated_documents = 0
        updated_points = 0
        for document in documents:
            if document_ids and str(document.get("id")) not in document_ids:
                continue
            existing_chunks = document.get("chunks") or []
            if not existing_chunks:
                continue
            source_text = "\n\n".join(str(chunk.get("text") or "") for chunk in existing_chunks).strip()
            chunks = AgentKnowledgeService._chunk_text(source_text)
            if not chunks:
                continue
            document["chunks"] = chunks
            document["chunk_count"] = len(chunks)
            document["document_version_id"] = document.get("document_version_id") or str(uuid.uuid4())
            approval_status = str(document.get("approval_status") or "pending")
            status = str(document.get("status") or ("active" if approval_status == "approved" else "pending_approval"))
            if approval_status == "approved" and status == "active":
                document["policy_version"] = AgentKnowledgeService._bump_policy_version(provider_status)
            document["qdrant_point_count"] = await AgentKnowledgeService._upsert_document_chunks(
                tenant_res,
                document,
                approval_status,
                status,
                db=db,
            )
            updated_documents += 1
            updated_points += int(document.get("qdrant_point_count") or 0)

        if updated_documents:
            active_cards: list[dict] = []
            policy_version = str(provider_status.get(AGENT_POLICY_VERSION_KEY) or "pv_default")
            for document in documents:
                if document.get("approval_status") == "approved" and document.get("status") in {"active", "ok"}:
                    active_cards.extend(AgentKnowledgeService._build_answer_cards(document, policy_version))
            AgentKnowledgeService._set_answer_cards(provider_status, active_cards)

        AgentKnowledgeService._set_documents(provider_status, documents)
        tenant_res.provider_status = provider_status
        flag_modified(tenant_res, "provider_status")
        db.add(tenant_res)
        await db.commit()
        await db.refresh(tenant_res)
        return {"updated_documents": updated_documents, "updated_points": updated_points}

    @staticmethod
    def _map_search_result(point: Any) -> dict:
        payload = dict(getattr(point, "payload", {}) or {})
        return {
            "document_id": str(payload.get("document_id") or ""),
            "document_name": str(payload.get("document_name") or "Untitled"),
            "chunk_id": str(payload.get("chunk_id") or getattr(point, "id", "")),
            "text": str(payload.get("text") or ""),
            "score": float(getattr(point, "score", 0.0) or 0.0),
            "metadata": {
                "document_type": payload.get("document_type"),
                "doc_type": payload.get("doc_type"),
                "chunk_index": payload.get("chunk_index"),
                "source_type": payload.get("source_type"),
                "source_kind": payload.get("source_kind"),
                "approval_status": payload.get("approval_status"),
                "status": payload.get("status"),
                "active": payload.get("active"),
                "language": payload.get("language"),
                "policy_version": payload.get("policy_version"),
                "topic": payload.get("topic"),
                "sensitivity": payload.get("sensitivity"),
                "source_title": payload.get("source_title"),
                "blob_path": payload.get("blob_path"),
                "tags": payload.get("tags") or [],
            },
        }

    @staticmethod
    async def test_retrieval(
        tenant_res: TenantResources,
        query: str,
        *,
        top_k: int = 5,
        db: AsyncSession | None = None,
    ) -> dict:
        provider_status = AgentKnowledgeService._provider_status(tenant_res)
        approved_document_ids = {
            str(document.get("id"))
            for document in AgentKnowledgeService._documents(provider_status)
            if document.get("approval_status") == "approved" and document.get("status") in {"active", "ok"}
        }
        if not approved_document_ids:
            return {
                "query": query,
                "chunks": [],
                "refusal": "No approved Agent Knowledge documents are active for this organization.",
            }

        policy_version = str(provider_status.get(AGENT_POLICY_VERSION_KEY) or "")
        language = detect_language(query)
        filters = {
            "organization_id": str(tenant_res.organization_id),
            "source_type": AGENT_KNOWLEDGE_SOURCE_TYPE,
            "source_kind": AGENT_CHUNK_SOURCE_KIND,
            "approval_status": "approved",
            "status": "active",
        }
        if policy_version:
            filters["policy_version"] = policy_version
        # Prefer same-language chunks when available; English docs remain usable
        # for Indian-English/Hinglish tenants because many policies are English.
        if language != "en":
            filters["language"] = [language, "en"]

        vector = await TextEmbeddingService.embed_text(query)
        results = await QdrantService.search_points(
            tenant_res,
            vector,
            limit=max(1, min(top_k, 12)),
            payload_filters=filters,
            db=db,
        )
        chunks = [
            AgentKnowledgeService._map_search_result(point)
            for point in results
            if str((getattr(point, "payload", {}) or {}).get("document_id") or "") in approved_document_ids
        ]
        refusal = None
        if not chunks:
            refusal = "I can only answer from approved organization knowledge. No approved context matched this question."
        return {"query": query, "chunks": chunks, "refusal": refusal}

    @staticmethod
    async def test_answer(
        tenant_res: TenantResources,
        query: str,
        *,
        top_k: int = 5,
        db: AsyncSession | None = None,
    ) -> dict:
        retrieval = await AgentKnowledgeService.test_retrieval(tenant_res, query, top_k=top_k, db=db)
        chunks = retrieval["chunks"]
        if not chunks:
            return {
                "query": query,
                "answer": retrieval["refusal"],
                "refused": True,
                "citations": [],
                "chunks": [],
            }

        snippets = []
        citations = []
        for index, chunk in enumerate(chunks[:3], start=1):
            text = re.sub(r"\s+", " ", chunk["text"]).strip()
            snippets.append(f"[{index}] {text[:700]}")
            citations.append(
                {
                    "document_id": chunk["document_id"],
                    "document_name": chunk["document_name"],
                    "chunk_id": chunk["chunk_id"],
                    "score": chunk["score"],
                }
            )
        answer = (
            "Based only on approved organization knowledge:\n"
            + "\n".join(snippets)
            + "\n\nIf this does not answer the question, upload and approve the missing policy or document first."
        )
        return {"query": query, "answer": answer, "refused": False, "citations": citations, "chunks": chunks}
