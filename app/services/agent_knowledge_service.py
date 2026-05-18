from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
import uuid
import zipfile
from io import BytesIO
from typing import Any
from xml.etree import ElementTree

import tiktoken
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.ext.asyncio import AsyncSession

_TOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")

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
AGENT_POLICY_CARDS_KEY = "agent_policy_cards"
AGENT_POLICY_VERSION_KEY = "agent_policy_version"

_HEADING_RE = re.compile(
    r"^(?:#+\s*)?([A-Z][A-Za-z0-9 /&\-:]{2,80})\s*[:\-]?\s*$"
)
_POLICY_HEADING_RE = re.compile(
    r"^(?:#+\s*|\s*)?(cancellation(?:\s+policy)?|refund(?:\s+policy)?|cancellation\s*&\s*refund|cancel\s*/\s*refund)(?:\s*[:\-–])?\s*$",
    re.IGNORECASE,
)

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
    def _policy_cards(provider_status: dict) -> list[dict]:
        return list(provider_status.get(AGENT_POLICY_CARDS_KEY) or [])

    @staticmethod
    def _set_policy_cards(provider_status: dict, cards: list[dict]) -> None:
        provider_status[AGENT_POLICY_CARDS_KEY] = cards[-500:]

    @staticmethod
    def _build_policy_cards(document: dict, policy_version: str) -> list[dict]:
        """Run the structured PolicyCardExtractor over the joined chunk text
        and stamp each card with the document/version/approval metadata so
        PolicyDecisionEngine can apply the same active/approved/version
        filters used for chunks."""
        # Lazy import to avoid a cycle (the extractor pulls only stdlib).
        from app.services.policy_card_extractor import PolicyCardExtractor

        source_text = "\n\n".join(str(chunk.get("text") or "") for chunk in (document.get("chunks") or []))
        extracted = PolicyCardExtractor.extract(source_text)
        cards: list[dict] = []
        for index, card in enumerate(extracted):
            cards.append(
                {
                    **card,
                    "id": f"{document['id']}:policy:{index}",
                    "document_id": document["id"],
                    "document_version_id": document.get("document_version_id"),
                    "policy_version": policy_version,
                    "approval_status": document.get("approval_status"),
                    "status": document.get("status"),
                }
            )
        return cards

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
    def _split_sections(text: str) -> list[dict]:
        """Split the doc into best-effort sections by heading lines so each
        chunk can carry ``section_id``/``section_title``/``parent_section_text``
        metadata. Enables parent-section retrieval expansion for tables/lists
        that would otherwise be sliced into useless single rows."""
        cleaned = re.sub(r"\r\n?", "\n", text or "").strip()
        if not cleaned:
            return []
        lines = cleaned.split("\n")
        sections: list[dict] = []
        current_title = "Document"
        current_lines: list[str] = []

        def flush() -> None:
            body = "\n".join(current_lines).strip()
            if body:
                sections.append(
                    {
                        "section_id": str(uuid.uuid4())[:12],
                        "section_title": current_title.strip()[:160],
                        "body": body,
                    }
                )

        for raw_line in lines:
            line = raw_line.rstrip()
            stripped = line.strip()
            is_heading = False
            if _POLICY_HEADING_RE.match(stripped):
                is_heading = True
            elif _HEADING_RE.match(stripped) and len(stripped.split()) <= 8 and not stripped.endswith("."):
                # All-caps or Title-Case short line — likely a heading.
                is_heading = True
            if is_heading:
                flush()
                current_title = re.sub(r"^#+\s*", "", stripped).strip(": -–")
                current_lines = []
                continue
            current_lines.append(raw_line)
        flush()
        if not sections:
            sections.append({"section_id": str(uuid.uuid4())[:12], "section_title": "Document", "body": cleaned})
        return sections

    @staticmethod
    def _chunk_section_body(body: str, max_tokens: int = 450, overlap_tokens: int = 50) -> list[dict]:
        """Paragraph-aware semantic chunking with tiktoken-accurate sizes.

        Strategy: split on paragraph boundaries, greedily pack paragraphs up
        to ``max_tokens``, then carry the trailing ``overlap_tokens`` into the
        next chunk for boundary recall. A paragraph that on its own exceeds
        ``max_tokens`` is sliced mid-paragraph as a last resort. Token counts
        come from ``cl100k_base`` so they line up with embedding model
        behavior (text-embedding-3-small uses the same tokenizer family).
        """
        cleaned = re.sub(r"\r\n?", "\n", body or "").strip()
        if not cleaned:
            return []
        paragraphs = [
            re.sub(r"\s+", " ", item).strip()
            for item in re.split(r"\n{2,}", cleaned)
            if item.strip()
        ]
        chunks: list[dict] = []
        current: list[str] = []
        current_tokens = 0
        cursor = 0  # char position into the original body
        chunk_start = 0

        def _flush() -> None:
            nonlocal current, current_tokens, chunk_start
            if not current:
                return
            chunk_text = "\n\n".join(current)
            chunks.append(
                {
                    "text": chunk_text,
                    "char_start": chunk_start,
                    "char_end": chunk_start + len(chunk_text),
                }
            )
            # Carry the last ``overlap_tokens`` of this chunk into the next as
            # a single seeded paragraph. Falls back to no overlap when the
            # chunk is already shorter than the overlap window.
            if overlap_tokens > 0 and current_tokens > overlap_tokens:
                tail_ids = _TOKEN_ENCODER.encode(chunk_text)[-overlap_tokens:]
                tail_text = _TOKEN_ENCODER.decode(tail_ids)
                current = [tail_text]
                current_tokens = len(tail_ids)
                chunk_start = max(0, chunk_start + len(chunk_text) - len(tail_text))
            else:
                current = []
                current_tokens = 0
                chunk_start = cursor

        for paragraph in paragraphs:
            para_tokens = len(_TOKEN_ENCODER.encode(paragraph))
            if not current:
                chunk_start = cursor

            if para_tokens > max_tokens:
                # Paragraph alone is too big — flush, then slice it.
                _flush()
                token_ids = _TOKEN_ENCODER.encode(paragraph)
                step = max(1, max_tokens - overlap_tokens)
                for i in range(0, len(token_ids), step):
                    window = token_ids[i : i + max_tokens]
                    slice_text = _TOKEN_ENCODER.decode(window)
                    chunks.append(
                        {
                            "text": slice_text,
                            "char_start": cursor,
                            "char_end": cursor + len(slice_text),
                        }
                    )
                cursor += len(paragraph) + 2  # account for the \n\n separator
                continue

            if current_tokens + para_tokens > max_tokens:
                _flush()
                if not current:
                    chunk_start = cursor
            current.append(paragraph)
            current_tokens += para_tokens
            cursor += len(paragraph) + 2

        _flush()
        return chunks

    @staticmethod
    def _chunk_text(text: str, max_tokens: int = 450, overlap_tokens: int = 50) -> list[dict]:
        sections = AgentKnowledgeService._split_sections(text)
        chunks: list[dict] = []
        for section in sections:
            body = section["body"]
            section_chunks = AgentKnowledgeService._chunk_section_body(body, max_tokens, overlap_tokens)
            # Cap parent_section_text size to keep payloads manageable but
            # large enough to expand a sliced cancellation/refund table.
            parent_text = body[:4000]
            for chunk in section_chunks:
                chunk["section_id"] = section["section_id"]
                chunk["section_title"] = section["section_title"]
                chunk["parent_section_text"] = parent_text
                chunks.append(chunk)
                if len(chunks) >= 200:
                    return chunks
        return chunks

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
            "section_id": chunk.get("section_id"),
            "section_title": chunk.get("section_title"),
            "parent_section_text": chunk.get("parent_section_text"),
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
        vectors = await TextEmbeddingService.embed_texts_for_tenant(tenant_res, texts)

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

        # Stage 1: blob upload. If this fails (Azure storage misconfigured,
        # credentials expired, etc.) we STILL want the document to land in
        # the registry with an error — never silently vanish.
        blob_error: str | None = None
        try:
            blob = await AzureBlobService.upload_agent_knowledge_document(
                tenant_res.tenant_id,
                tenant_res.blob_prefix,
                document_id,
                filename,
                content,
                content_type,
            )
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            print(f"[NOKVO-KB] blob upload failed for {filename!r}: {exc!r}\n{tb[:1200]}")
            blob = {"blob_path": None, "blob_name": None}
            blob_error = f"Blob upload failed: {str(exc)[:200]}"

        # Stage 2: text extraction + chunking. _extract_text has its own
        # try/except so it shouldn't raise. _chunk_text uses tiktoken and is
        # safe. Guarding anyway.
        try:
            extracted_text = AgentKnowledgeService._extract_text(filename, content)
            chunks = AgentKnowledgeService._chunk_text(extracted_text)
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            print(f"[NOKVO-KB] extract/chunk failed for {filename!r}: {exc!r}\n{tb[:1200]}")
            chunks = []
            extracted_text = ""
            blob_error = (blob_error + "; " if blob_error else "") + f"Extract/chunk failed: {str(exc)[:200]}"

        if chunks:
            status = "pending"
            initial_approval = "approved"
            initial_last_error = blob_error
        elif blob_error:
            status = "error"
            initial_approval = "rejected"
            initial_last_error = blob_error
        else:
            status = "empty"
            initial_approval = "rejected"
            initial_last_error = "No usable text was found in this document."
        document = {
            "id": document_id,
            "document_version_id": str(uuid.uuid4()),
            "name": name.strip()[:160],
            "document_type": document_type,
            "description": (description or "").strip()[:1000] or None,
            "tags": AgentKnowledgeService._normalize_tags(tags),
            "status": status,
            "approval_status": initial_approval,
            "blob_path": blob.get("blob_path"),
            "blob_name": blob.get("blob_name"),
            "content_type": content_type,
            "size_bytes": len(content),
            "chunk_count": len(chunks),
            "qdrant_point_count": 0,
            "chunks": chunks,
            "uploaded_by": str(current_user.id),
            "created_at": created_at,
            "approved_at": created_at if chunks and not blob_error else None,
            "approved_by": str(current_user.id) if chunks and not blob_error else None,
            "last_error": initial_last_error,
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
                # Generate policy cards for the freshly-approved document. We
                # only attach them when the upsert above succeeded so a half-
                # ingested doc never becomes the source of deterministic
                # answers.
                policy_cards = AgentKnowledgeService._build_policy_cards(document, document["policy_version"])
                if policy_cards:
                    existing = AgentKnowledgeService._policy_cards(provider_status)
                    AgentKnowledgeService._set_policy_cards(provider_status, existing + policy_cards)
            except Exception as exc:
                # Log the actual failure cause so we don't have to guess
                # later from a 400. Embedding 429s, Qdrant unreachable,
                # tiktoken edge cases — all should surface here.
                import traceback
                tb = traceback.format_exc()
                print(f"[NOKVO-KB] upload_document chunk-upsert failed: {exc!r}\n{tb[:1500]}")
                document["status"] = "error"
                document["approval_status"] = "rejected"
                document["approved_at"] = None
                document["approved_by"] = None
                document["last_error"] = str(exc)[:500]

        documents = AgentKnowledgeService._documents(provider_status)
        documents.append(document)
        AgentKnowledgeService._set_documents(provider_status, documents)
        print(f"[NOKVO-KB] registered document {document['id']} ({document['name']!r}) status={document['status']} approval={document['approval_status']} chunks={document['chunk_count']}")
        if not (chunks and document["status"] == "ok"):
            provider_status.setdefault(AGENT_POLICY_VERSION_KEY, "pv_default")
        tenant_res.provider_status = provider_status
        flag_modified(tenant_res, "provider_status")
        try:
            db.add(tenant_res)
            await db.commit()
            await db.refresh(tenant_res)
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            print(f"[NOKVO-KB] upload_document commit failed: {exc!r}\n{tb[:1500]}")
            await db.rollback()
            # If we got this far, surface the doc-with-error rather than
            # raising — the caller asked for an upload and we managed to
            # at least record the failure.
            document["status"] = "error"
            document["approval_status"] = "rejected"
            document["last_error"] = f"persistence failed: {str(exc)[:400]}"
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
        existing_policy_cards = [
            card for card in AgentKnowledgeService._policy_cards(provider_status)
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
            existing_policy_cards.extend(
                AgentKnowledgeService._build_policy_cards(selected, policy_version)
            )
        else:
            selected["qdrant_point_count"] = 0
            await QdrantService.delete_points_by_filter(
                tenant_res,
                {"source_type": AGENT_KNOWLEDGE_SOURCE_TYPE, "document_id": selected["id"]},
                db=db,
            )
            AgentKnowledgeService._bump_policy_version(provider_status)
        AgentKnowledgeService._set_answer_cards(provider_status, existing_cards)
        AgentKnowledgeService._set_policy_cards(provider_status, existing_policy_cards)

        AgentKnowledgeService._set_documents(provider_status, documents)
        tenant_res.provider_status = provider_status
        flag_modified(tenant_res, "provider_status")
        db.add(tenant_res)
        await db.commit()
        await db.refresh(tenant_res)
        return AgentKnowledgeService._document_response(selected)

    @staticmethod
    async def reconcile_from_qdrant(
        tenant_res: TenantResources,
        db: AsyncSession,
    ) -> dict:
        """Rebuild the document registry from chunks that exist in Qdrant.

        Useful when an earlier upload succeeded in writing vectors to Qdrant
        but failed to persist the metadata into ``provider_status`` (Azure
        Blob outage, transient DB error, etc.). Walks every point in the
        tenant collection with ``source_type=agent_knowledge``, groups by
        ``document_id``, and for each orphaned doc creates a stub entry with
        the chunk texts reconstructed from the payload — marked approved+
        active so the agent immediately starts using them.

        Returns a summary: how many docs were reconciled, how many chunks
        each, and the new total.
        """
        provider_status = AgentKnowledgeService._provider_status(tenant_res)
        existing_doc_ids = {
            str(doc.get("id"))
            for doc in AgentKnowledgeService._documents(provider_status)
        }

        try:
            points = await QdrantService.scroll_points(
                tenant_res,
                payload_filters={"source_type": AGENT_KNOWLEDGE_SOURCE_TYPE},
            )
        except Exception as exc:
            raise RuntimeError(f"Qdrant scan failed: {exc}") from exc

        # Group points by document_id
        by_doc: dict[str, list[Any]] = {}
        for p in points:
            payload = dict(getattr(p, "payload", {}) or {})
            doc_id = str(payload.get("document_id") or "")
            if not doc_id or doc_id in existing_doc_ids:
                continue
            by_doc.setdefault(doc_id, []).append(p)

        if not by_doc:
            return {"reconciled": 0, "documents": [], "total_after": len(existing_doc_ids)}

        reconciled_docs: list[dict] = []
        documents = AgentKnowledgeService._documents(provider_status)
        for doc_id, doc_points in by_doc.items():
            # Sort by chunk_index so order is preserved.
            doc_points.sort(key=lambda p: int((getattr(p, "payload", {}) or {}).get("chunk_index") or 0))
            first = dict(getattr(doc_points[0], "payload", {}) or {})
            chunks: list[dict] = []
            for p in doc_points:
                pay = dict(getattr(p, "payload", {}) or {})
                chunks.append(
                    {
                        "text": str(pay.get("text") or ""),
                        "char_start": int(pay.get("char_start") or 0),
                        "char_end": int(pay.get("char_end") or 0),
                        "section_id": pay.get("section_id"),
                        "section_title": pay.get("section_title"),
                        "parent_section_text": pay.get("parent_section_text"),
                    }
                )
            reconciled_at = AgentKnowledgeService._now()
            document = {
                "id": doc_id,
                "document_version_id": str(first.get("document_version_id") or str(uuid.uuid4())),
                "name": str(first.get("document_name") or first.get("source_title") or f"reconciled:{doc_id[:8]}"),
                "document_type": str(first.get("document_type") or first.get("doc_type") or "other"),
                "description": None,
                "tags": list(first.get("tags") or []),
                "status": "active",
                "approval_status": "approved",
                "blob_path": str(first.get("blob_path") or "") or None,
                "blob_name": None,
                "content_type": None,
                "size_bytes": 0,
                "chunk_count": len(chunks),
                "qdrant_point_count": len(chunks),
                "chunks": chunks,
                "uploaded_by": None,
                "created_at": str(first.get("created_at") or reconciled_at),
                "approved_at": reconciled_at,
                "approved_by": None,
                "last_error": None,
                "policy_version": str(first.get("policy_version") or "pv_default"),
                "reconciled_at": reconciled_at,
            }
            documents.append(document)
            reconciled_docs.append(
                {"document_id": doc_id, "name": document["name"], "chunks": len(chunks)}
            )
            print(f"[NOKVO-KB] reconciled document {doc_id} ({document['name']!r}) with {len(chunks)} chunks")

            # Generate policy cards on the fly so cancellation/refund routing
            # works immediately for the reconciled doc.
            try:
                policy_cards = AgentKnowledgeService._build_policy_cards(document, document["policy_version"])
                if policy_cards:
                    existing_cards = AgentKnowledgeService._policy_cards(provider_status)
                    AgentKnowledgeService._set_policy_cards(provider_status, existing_cards + policy_cards)
            except Exception as exc:
                print(f"[NOKVO-KB] reconcile: policy card generation failed for {doc_id}: {exc!r}")

        AgentKnowledgeService._set_documents(provider_status, documents)
        # Align the global agent_policy_version with what the reconciled
        # documents actually have (taken from the chunk payloads in Qdrant).
        # If we BUMP instead, retrieval's policy_version filter would no
        # longer match any of the chunks we just resurfaced.
        chunk_versions = [
            doc.get("policy_version")
            for doc in documents
            if doc.get("policy_version")
        ]
        if chunk_versions:
            # Pick the most common version from the reconciled docs.
            from collections import Counter
            most_common, _count = Counter(chunk_versions).most_common(1)[0]
            provider_status[AGENT_POLICY_VERSION_KEY] = str(most_common)
        tenant_res.provider_status = provider_status
        flag_modified(tenant_res, "provider_status")
        try:
            db.add(tenant_res)
            await db.commit()
            await db.refresh(tenant_res)
        except Exception as exc:
            await db.rollback()
            raise RuntimeError(f"Reconcile persistence failed: {exc}") from exc
        return {
            "reconciled": len(reconciled_docs),
            "documents": reconciled_docs,
            "total_after": len(documents),
        }

    @staticmethod
    def get_document_chunks(tenant_res: TenantResources, document_id: str) -> dict:
        """Return all chunks for one document in display-ready shape.

        Reads from ``provider_status.agent_knowledge_documents[*].chunks`` —
        the same data we already embedded into Qdrant — so the admin can see
        exactly what the agent is grounding on for this document. Raises
        ``ValueError`` if the document doesn't exist on this tenant.
        """
        provider_status = AgentKnowledgeService._provider_status(tenant_res)
        for document in AgentKnowledgeService._documents(provider_status):
            if document.get("id") != document_id:
                continue
            chunks = []
            for index, chunk in enumerate(document.get("chunks") or []):
                chunks.append(
                    {
                        "index": index,
                        "text": str(chunk.get("text") or ""),
                        "section_id": chunk.get("section_id"),
                        "section_title": chunk.get("section_title"),
                        "char_start": int(chunk.get("char_start") or 0),
                        "char_end": int(chunk.get("char_end") or 0),
                        "token_count": int(chunk.get("token_count") or 0) or max(1, len((chunk.get("text") or "").split())),
                    }
                )
            return {
                "document_id": document_id,
                "document_name": document.get("name"),
                "document_type": document.get("document_type"),
                "approval_status": document.get("approval_status"),
                "status": document.get("status"),
                "chunk_count": len(chunks),
                "qdrant_point_count": int(document.get("qdrant_point_count") or 0),
                "policy_version": document.get("policy_version"),
                "chunks": chunks,
            }
        raise ValueError("Agent Knowledge document not found")

    @staticmethod
    async def delete_document(
        tenant_res: TenantResources,
        db: AsyncSession,
        document_id: str,
    ) -> dict:
        """Hard-delete a knowledge-base document and everything it produced.

        Steps (in order):
          1. Qdrant points for the document → embeddings gone so retrieval
             can't surface them.
          2. Answer cards and policy cards generated from this document.
          3. Source blob(s) in Azure Blob Storage — sweeps the entire
             ``agent-knowledge/{document_id}/`` prefix so retry-orphans get
             cleaned up too, not just the single ``blob_path`` we stored.
          4. Registry entry in ``provider_status``.
          5. Bump ``agent_policy_version`` so cached answers age out.

        Returns ``{document_id, deleted, qdrant_deleted, blobs_deleted}`` so
        the caller / UI can show exactly what was reclaimed.
        Raises ``ValueError`` if the document_id isn't in the registry.
        """
        provider_status = AgentKnowledgeService._provider_status(tenant_res)
        documents = AgentKnowledgeService._documents(provider_status)
        target = None
        for document in documents:
            if document.get("id") == document_id:
                target = document
                break
        if not target:
            raise ValueError("Agent Knowledge document not found")

        # 1) Drop the vectors first — best to make them unreachable before
        #    we forget the document existed, in case anything fails midway.
        qdrant_deleted = True
        try:
            await QdrantService.delete_points_by_filter(
                tenant_res,
                {"source_type": AGENT_KNOWLEDGE_SOURCE_TYPE, "document_id": document_id},
                db=db,
            )
            print(f"[NOKVO-KB] delete: removed Qdrant points for document_id={document_id}")
        except Exception as exc:
            qdrant_deleted = False
            import traceback
            tb = traceback.format_exc()
            print(f"[NOKVO-KB] delete: Qdrant delete failed for {document_id}: {exc!r}\n{tb[:1000]}")

        # 2) Prune answer + policy cards belonging to this document.
        remaining_answer_cards = [
            card for card in AgentKnowledgeService._answer_cards(provider_status)
            if card.get("document_id") != document_id
        ]
        AgentKnowledgeService._set_answer_cards(provider_status, remaining_answer_cards)

        remaining_policy_cards = [
            card for card in AgentKnowledgeService._policy_cards(provider_status)
            if card.get("document_id") != document_id
        ]
        AgentKnowledgeService._set_policy_cards(provider_status, remaining_policy_cards)

        # 3) Sweep ALL blobs for this document_id (not just the recorded
        #    blob_path — earlier retries may have orphaned others under the
        #    same agent-knowledge/{document_id}/ prefix).
        blobs_deleted = 0
        blob_path = target.get("blob_path")
        try:
            blobs_deleted = await AzureBlobService.delete_agent_knowledge_document(
                tenant_res.tenant_id,
                blob_path or "",
                blob_prefix=tenant_res.blob_prefix,
                document_id=document_id,
            )
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            print(f"[NOKVO-KB] delete: blob sweep failed for {document_id}: {exc!r}\n{tb[:1000]}")

        # 4) Remove from the registry and bump policy_version so any cached
        #    answers tied to the prior version age out.
        documents = [doc for doc in documents if doc.get("id") != document_id]
        AgentKnowledgeService._set_documents(provider_status, documents)
        AgentKnowledgeService._bump_policy_version(provider_status)

        tenant_res.provider_status = provider_status
        flag_modified(tenant_res, "provider_status")
        db.add(tenant_res)
        await db.commit()
        await db.refresh(tenant_res)
        print(
            f"[NOKVO-KB] delete: document_id={document_id} qdrant_ok={qdrant_deleted} "
            f"blobs_deleted={blobs_deleted}"
        )
        return {
            "document_id": document_id,
            "deleted": True,
            "qdrant_deleted": qdrant_deleted,
            "blobs_deleted": blobs_deleted,
        }

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
            active_policy_cards: list[dict] = []
            policy_version = str(provider_status.get(AGENT_POLICY_VERSION_KEY) or "pv_default")
            for document in documents:
                if document.get("approval_status") == "approved" and document.get("status") in {"active", "ok"}:
                    active_cards.extend(AgentKnowledgeService._build_answer_cards(document, policy_version))
                    active_policy_cards.extend(AgentKnowledgeService._build_policy_cards(document, policy_version))
            AgentKnowledgeService._set_answer_cards(provider_status, active_cards)
            AgentKnowledgeService._set_policy_cards(provider_status, active_policy_cards)

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
        """Source of truth for whether a chunk is usable: the Qdrant payload's
        ``approval_status``/``status`` fields (set at upsert time). The
        ``provider_status.agent_knowledge_documents`` registry is treated as
        a HINT for the document_id whitelist, but if the registry is empty
        we trust Qdrant directly — otherwise tenants whose registry got
        desynced from Qdrant (failed DB commit after a successful upsert)
        see "No approved documents" even though their vectors are there.
        """
        provider_status = AgentKnowledgeService._provider_status(tenant_res)
        approved_document_ids = {
            str(document.get("id"))
            for document in AgentKnowledgeService._documents(provider_status)
            if document.get("approval_status") == "approved" and document.get("status") in {"active", "ok"}
        }
        registry_empty = not approved_document_ids
        if registry_empty:
            print(
                f"[NOKVO-KB] test_retrieval: registry empty for tenant {tenant_res.tenant_id}; "
                f"trusting Qdrant payload filters as the source of truth"
            )

        policy_version = str(provider_status.get(AGENT_POLICY_VERSION_KEY) or "")
        language = detect_language(query)
        filters = {
            "organization_id": str(tenant_res.organization_id),
            "source_type": AGENT_KNOWLEDGE_SOURCE_TYPE,
            "source_kind": AGENT_CHUNK_SOURCE_KIND,
            "approval_status": "approved",
            "status": "active",
        }
        # Don't filter by policy_version when the registry is empty — chunks
        # in Qdrant might have a different version stamped at their original
        # upsert time, and we shouldn't hide them from the user just because
        # we lost the metadata. Once they reconcile, the version filter
        # re-engages.
        if policy_version and not registry_empty:
            filters["policy_version"] = policy_version
        if language != "en":
            filters["language"] = [language, "en"]

        vector = await TextEmbeddingService.embed_text_for_tenant(tenant_res, query)
        results = await QdrantService.search_points(
            tenant_res,
            vector,
            limit=max(1, min(top_k, 12)),
            payload_filters=filters,
            db=db,
        )

        def _is_approved_chunk(point) -> bool:
            doc_id = str((getattr(point, "payload", {}) or {}).get("document_id") or "")
            if registry_empty:
                # Trust the Qdrant approval_status/status filters that have
                # already been applied above. Any returned chunk is approved.
                return True
            return doc_id in approved_document_ids

        chunks = [
            AgentKnowledgeService._map_search_result(point)
            for point in results
            if _is_approved_chunk(point)
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
