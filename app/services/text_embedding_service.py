from __future__ import annotations

import math
import re
from collections import defaultdict

from app.core.config import settings


class TextEmbeddingService:
    @staticmethod
    def embed_text(text: str) -> list[float]:
        """
        Lightweight deterministic embedding for structured database sync.
        This avoids a hard dependency on an external embedding provider while
        still producing stable vectors that can be stored in Qdrant.
        """
        size = settings.QDRANT_VECTOR_SIZE
        vector = [0.0] * size
        tokens = re.findall(r"[a-z0-9_@.\-:/]+", (text or "").lower())
        if not tokens:
            return vector

        counts: dict[int, float] = defaultdict(float)
        for token in tokens:
            bucket = hash(token) % size
            counts[bucket] += 1.0

        norm = math.sqrt(sum(value * value for value in counts.values()))
        if norm == 0:
            return vector

        for bucket, value in counts.items():
            vector[bucket] = value / norm
        return vector
