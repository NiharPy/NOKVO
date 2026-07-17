"""Shared whitespace normalizer for pipeline turn text.

Extracted from nokvo_one_voice_pipeline.py because both the remaining
orchestrator and the extracted pipeline modules (retrieval, and later the
lead/tool-flow movers) normalize user text with the same helper. The
pipeline module re-exports it, so existing references keep working.
"""
from __future__ import annotations

import re


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())
