"""Retry queue namespace re-export.

The actual implementation lives in :mod:`app.services.tool_retry_service`
(model + queue + worker hook). This module exists so all session-scoped
state can be reached through one package — ``from app.services.session
import retry`` — and so the boundary between confirmation / audit /
retry / outcome is explicit at the import site.
"""

from app.services.tool_retry_service import (  # noqa: F401
    MAX_ATTEMPTS,
    ToolRetryService,
)
