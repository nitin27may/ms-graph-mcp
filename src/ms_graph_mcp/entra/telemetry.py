"""Audit hook seam.

The middleware emits ``audit.auth_success`` / ``audit.auth_failure`` events
through an injectable callback so the package stays free of an OpenTelemetry
dependency. The default logs; the backend passes a hook that records OTEL span
events (its existing ``_emit_auth_event``), keeping audit-via-OTEL intact.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger("ms_graph_mcp.entra")

AuditHook = Callable[[str, dict], None]


def default_audit(event: str, attrs: dict) -> None:
    """Fallback audit sink — structured log line when no OTEL hook is injected."""
    logger.info("[auth-audit] %s %s", event, attrs)
