"""Auth posture presets.

A service runs the same middleware in one of two postures:

- ``AGENT_EDGE`` — the first hop that receives the user's ``api://<client_id>``
  token. Verifies the token and enforces the App-Role gate (+ app-only policy).
- ``DOWNSTREAM_SERVICE`` — an MCP server receiving an OBO token (Graph audience).
  Verifies the token and enforces the ``azp`` allowlist, but does NOT role-gate
  (the role decision was made at the edge; the OBO token's audience is generic
  across apps, so ``azp`` is what restricts the surface to known callers).

This server runs ``DOWNSTREAM_SERVICE``.
"""

from __future__ import annotations

from enum import StrEnum


class AuthMode(StrEnum):
    AGENT_EDGE = "agent_edge"
    DOWNSTREAM_SERVICE = "downstream_service"
