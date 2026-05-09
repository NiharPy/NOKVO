from __future__ import annotations

import asyncio
import logging
from typing import List

from openai import AsyncOpenAI
from app.core.config import settings

logger = logging.getLogger(__name__)

# Lazily initialize the client to prevent instantiation errors if key is missing during import
_openai_client: AsyncOpenAI | None = None

def _get_client() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    return _openai_client

class TextEmbeddingService:
    @staticmethod
    def _zero_vector() -> list[float]:
        return [0.0] * settings.QDRANT_VECTOR_SIZE

    @staticmethod
    def _validate_vector(vector: list[float]) -> list[float]:
        if len(vector) != settings.QDRANT_VECTOR_SIZE:
            raise RuntimeError(
                f"Embedding dimension mismatch: expected {settings.QDRANT_VECTOR_SIZE}, got {len(vector)}"
            )
        return vector

    @staticmethod
    def _embedding_failure(exc: Exception) -> list[float]:
        logger.error("OpenAI embedding request failed: %s", exc)
        if settings.OPENAI_EMBEDDING_ALLOW_ZERO_FALLBACK:
            return TextEmbeddingService._zero_vector()
        raise RuntimeError("OpenAI embeddings are required and the embedding request failed.") from exc

    @staticmethod
    async def embed_text(text: str) -> list[float]:
        """
        Embeds a single string using OpenAI's embedding model.
        Returns a 1536-dimensional vector.
        """
        if not text or not str(text).strip():
            return TextEmbeddingService._zero_vector()
        if not settings.OPENAI_API_KEY:
            if settings.OPENAI_EMBEDDING_ALLOW_ZERO_FALLBACK:
                return TextEmbeddingService._zero_vector()
            raise RuntimeError("OPENAI_API_KEY is required for embeddings.")

        try:
            client = _get_client()
            response = await client.embeddings.create(
                input=[str(text)],
                model=settings.OPENAI_EMBEDDING_MODEL,
                timeout=settings.OPENAI_EMBEDDING_TIMEOUT_SECONDS,
            )
            return TextEmbeddingService._validate_vector(response.data[0].embedding)
        except Exception as e:
            return TextEmbeddingService._embedding_failure(e)

    @staticmethod
    async def embed_texts(texts: list[str]) -> list[list[float]]:
        """
        Batch embeds multiple strings using OpenAI.
        Useful for syncing many records at once.
        Returns a list of 1536-dimensional vectors.
        """
        # Filter empty texts to avoid API errors, keeping track of indices
        valid_texts = []
        valid_indices = []
        for i, t in enumerate(texts):
            if t and str(t).strip():
                valid_texts.append(str(t))
                valid_indices.append(i)

        results = [TextEmbeddingService._zero_vector() for _ in texts]

        if not valid_texts:
            return results
        if not settings.OPENAI_API_KEY:
            if settings.OPENAI_EMBEDDING_ALLOW_ZERO_FALLBACK:
                return results
            raise RuntimeError("OPENAI_API_KEY is required for embeddings.")

        client = _get_client()
        batch_size = max(1, min(int(settings.OPENAI_EMBEDDING_BATCH_SIZE or 96), 2048))
        try:
            for start in range(0, len(valid_texts), batch_size):
                batch = valid_texts[start:start + batch_size]
                response = await client.embeddings.create(
                    input=batch,
                    model=settings.OPENAI_EMBEDDING_MODEL,
                    timeout=settings.OPENAI_EMBEDDING_TIMEOUT_SECONDS,
                )
                for i, data in enumerate(response.data):
                    original_index = valid_indices[start + i]
                    results[original_index] = TextEmbeddingService._validate_vector(data.embedding)
        except Exception as e:
            TextEmbeddingService._embedding_failure(e)
        return results
