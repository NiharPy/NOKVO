from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import List, TYPE_CHECKING

from openai import AsyncAzureOpenAI, AsyncOpenAI
from app.core.config import settings
from app.core.secret_crypto import decrypt_secret

if TYPE_CHECKING:
    from app.models.tenant_resources import TenantResources

logger = logging.getLogger(__name__)

# Lazily initialize the client to prevent instantiation errors if key is missing during import
_openai_client: AsyncOpenAI | None = None
_tenant_client_cache: dict[str, tuple[AsyncAzureOpenAI, str]] = {}
_tenant_embedding_cache: dict[str, tuple[float, list[float]]] = {}
_tenant_embedding_inflight: dict[str, asyncio.Task[list[float]]] = {}
# Embedding vectors are stable for a given (tenant, deployment, normalized
# text) pair. A 5-minute TTL keeps the vector for the lifetime of a typical
# voice call so a repeated FAQ question (e.g. caller asks "what are your
# hours" twice, or two callers ask within minutes) avoids the Azure
# embedding round-trip entirely. The 512-entry LRU bound keeps the memory
# footprint bounded (1536 floats × 4 bytes × 512 ≈ 3 MB).
_EMBEDDING_CACHE_TTL_SECONDS = 300.0
_EMBEDDING_CACHE_MAX_ITEMS = 512


def _embedding_cache_key(tenant_res: "TenantResources", text: str) -> str:
    normalized = " ".join(str(text or "").lower().split())
    provider_status = dict(getattr(tenant_res, "provider_status", None) or {})
    deployment = (
        provider_status.get("embedding_deployment")
        or provider_status.get("embedding_model")
        or settings.OPENAI_EMBEDDING_MODEL
    )
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
    return f"{tenant_res.tenant_id}:{deployment}:{digest}"


def _get_cached_embedding(cache_key: str) -> list[float] | None:
    cached = _tenant_embedding_cache.get(cache_key)
    if cached is None:
        return None
    expires_at, vector = cached
    if expires_at <= time.monotonic():
        _tenant_embedding_cache.pop(cache_key, None)
        return None
    return list(vector)


def _set_cached_embedding(cache_key: str, vector: list[float]) -> None:
    if len(_tenant_embedding_cache) >= _EMBEDDING_CACHE_MAX_ITEMS:
        now = time.monotonic()
        expired = [key for key, (expires_at, _) in _tenant_embedding_cache.items() if expires_at <= now]
        for key in expired:
            _tenant_embedding_cache.pop(key, None)
        while len(_tenant_embedding_cache) >= _EMBEDDING_CACHE_MAX_ITEMS:
            _tenant_embedding_cache.pop(next(iter(_tenant_embedding_cache)), None)
    _tenant_embedding_cache[cache_key] = (
        time.monotonic() + _EMBEDDING_CACHE_TTL_SECONDS,
        list(vector),
    )

def _get_client() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY,
            max_retries=settings.OPENAI_EMBEDDING_MAX_RETRIES,
            timeout=settings.OPENAI_EMBEDDING_TIMEOUT_SECONDS,
        )
    return _openai_client


async def _resolve_tenant_embedding_client(
    tenant_res: "TenantResources",
) -> tuple[AsyncAzureOpenAI, str] | None:
    """Return (client, deployment) when the tenant has its own Azure OpenAI
    embedding deployment provisioned and we can fetch the API key. Returns None
    when any required piece is missing — callers should fall back to the
    platform OpenAI client in that case.
    """
    provider_status = dict(getattr(tenant_res, "provider_status", None) or {})
    if (provider_status.get("embedding_provider") or "azure_openai") != "azure_openai":
        return None
    endpoint = provider_status.get("embedding_endpoint") or provider_status.get("llm_endpoint")
    deployment = provider_status.get("embedding_deployment")
    if not endpoint or not deployment:
        return None

    cache_key = f"{tenant_res.tenant_id}:{endpoint}:{deployment}"
    cached = _tenant_client_cache.get(cache_key)
    if cached is not None:
        return cached

    api_key: str | None = None
    secret_ref = provider_status.get("embedding_api_key_secret_ref") or provider_status.get("llm_api_key_secret_ref")
    if secret_ref:
        try:
            # Lazy import — avoid a circular dep at module import time.
            from app.services.azure_keyvault_service import AzureKeyVaultService

            api_key = await AzureKeyVaultService.get_secret_value(secret_ref)
        except Exception as exc:
            logger.warning("Tenant embedding Key Vault lookup failed for %s: %s", secret_ref, exc)

    if not api_key:
        encrypted = provider_status.get("llm_api_key_encrypted")
        if encrypted:
            try:
                api_key = decrypt_secret(encrypted)
            except Exception as exc:
                logger.warning("Tenant embedding key decryption failed for tenant %s: %s", tenant_res.tenant_id, exc)

    if not api_key:
        return None

    api_version = provider_status.get("embedding_api_version") or settings.AZURE_OPENAI_EMBEDDING_API_VERSION
    client = AsyncAzureOpenAI(
        api_key=api_key,
        api_version=api_version,
        azure_endpoint=endpoint,
        timeout=settings.OPENAI_EMBEDDING_TIMEOUT_SECONDS,
        max_retries=settings.OPENAI_EMBEDDING_MAX_RETRIES,
    )
    _tenant_client_cache[cache_key] = (client, deployment)
    return client, deployment

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
    async def embed_text_for_tenant(tenant_res: "TenantResources", text: str) -> list[float]:
        """Embed a single string using the tenant's per-tenant Azure OpenAI
        embedding deployment when one is provisioned; otherwise fall back to the
        platform OpenAI client."""
        if not text or not str(text).strip():
            return TextEmbeddingService._zero_vector()

        cache_key = _embedding_cache_key(tenant_res, text)
        cached = _get_cached_embedding(cache_key)
        if cached is not None:
            return cached

        in_flight = _tenant_embedding_inflight.get(cache_key)
        if in_flight is not None:
            return list(await in_flight)

        async def _compute() -> list[float]:
            resolved = await _resolve_tenant_embedding_client(tenant_res)
            if resolved is None:
                return await TextEmbeddingService.embed_text(text)
            client, deployment = resolved
            try:
                response = await client.embeddings.create(
                    input=[str(text)],
                    model=deployment,
                    timeout=settings.OPENAI_EMBEDDING_TIMEOUT_SECONDS,
                )
                return TextEmbeddingService._validate_vector(response.data[0].embedding)
            except Exception as exc:
                logger.warning(
                    "Tenant Azure embedding failed (tenant=%s), falling back to platform OpenAI: %s",
                    tenant_res.tenant_id,
                    exc,
                )
                return await TextEmbeddingService.embed_text(text)

        task = asyncio.create_task(_compute())
        _tenant_embedding_inflight[cache_key] = task
        try:
            vector = await task
        finally:
            _tenant_embedding_inflight.pop(cache_key, None)
        _set_cached_embedding(cache_key, vector)
        return list(vector)

    @staticmethod
    async def embed_texts_for_tenant(
        tenant_res: "TenantResources", texts: list[str]
    ) -> list[list[float]]:
        """Batch variant of embed_text_for_tenant.

        On 429 from the tenant deployment we sleep up to the smaller of
        Retry-After or 8s, then retry the failing batch ONCE. This handles
        small rate-limit bursts (which is what happens during a bulk KB
        upload of 4-10 documents in quick succession) without giving up
        and silently producing zero vectors.
        """
        # Lazy import to avoid forcing the openai package onto modules that
        # don't need it at import time.
        try:
            from openai import RateLimitError as _OpenAIRateLimit
        except Exception:  # pragma: no cover
            _OpenAIRateLimit = type("__never", (Exception,), {})

        valid_texts: list[str] = []
        valid_indices: list[int] = []
        for i, t in enumerate(texts):
            if t and str(t).strip():
                valid_texts.append(str(t))
                valid_indices.append(i)

        results = [TextEmbeddingService._zero_vector() for _ in texts]
        if not valid_texts:
            return results

        resolved = await _resolve_tenant_embedding_client(tenant_res)
        if resolved is None:
            return await TextEmbeddingService.embed_texts(texts)
        client, deployment = resolved

        batch_size = max(1, min(int(settings.OPENAI_EMBEDDING_BATCH_SIZE or 96), 2048))
        try:
            for start in range(0, len(valid_texts), batch_size):
                batch = valid_texts[start:start + batch_size]
                try:
                    response = await client.embeddings.create(
                        input=batch,
                        model=deployment,
                        timeout=settings.OPENAI_EMBEDDING_TIMEOUT_SECONDS,
                    )
                except _OpenAIRateLimit as rl:
                    # Honour Retry-After when present (Azure typically says
                    # 60s; cap to 8s so a single KB upload doesn't stall).
                    retry_after = 4.0
                    headers = getattr(getattr(rl, "response", None), "headers", None)
                    if headers:
                        try:
                            retry_after = float(headers.get("retry-after", "0") or 0) or 4.0
                        except (TypeError, ValueError):
                            retry_after = 4.0
                    sleep_for = min(retry_after, 8.0)
                    logger.warning(
                        "Tenant Azure embedding 429 (tenant=%s); sleeping %.1fs then retrying once",
                        tenant_res.tenant_id, sleep_for,
                    )
                    await asyncio.sleep(sleep_for)
                    response = await client.embeddings.create(
                        input=batch,
                        model=deployment,
                        timeout=settings.OPENAI_EMBEDDING_TIMEOUT_SECONDS,
                    )
                for i, data in enumerate(response.data):
                    original_index = valid_indices[start + i]
                    results[original_index] = TextEmbeddingService._validate_vector(data.embedding)
        except Exception as exc:
            logger.warning(
                "Tenant Azure embedding batch failed (tenant=%s), falling back to platform OpenAI: %s",
                tenant_res.tenant_id,
                exc,
            )
            return await TextEmbeddingService.embed_texts(texts)
        return results

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
