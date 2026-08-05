"""Logging configuration for the two console entry points.

Kept in one place because the destination is not a free choice: on stdio,
**stdout is the JSON-RPC channel**, so a log record written there corrupts the
protocol stream and the client disconnects with a parse error naming nothing
useful. Everything goes to stderr.

A library should not configure logging for its host, so this runs only from the
console scripts. Embedding applications keep their own setup.
"""

from __future__ import annotations

import logging
import os
import sys

__all__ = ["configure_logging"]

DEFAULT_LEVEL = "WARNING"


def configure_logging() -> str:
    """Apply ``GRAPH_MCP_LOG_LEVEL``, returning the level actually used.

    Defaults to WARNING so an ordinary run is quiet: the per-request ``[Graph]``
    lines are INFO, and a client that shows server output would otherwise
    surface one line per Graph call. ``GRAPH_MCP_LOG_LEVEL=INFO`` turns them on,
    which is the first step in docs/debugging.md.

    An unparseable value falls back rather than raising — failing to start
    because a log level was misspelled would be a poor trade.
    """
    requested = os.getenv("GRAPH_MCP_LOG_LEVEL", DEFAULT_LEVEL).strip().upper()
    level = getattr(logging, requested, None)
    if not isinstance(level, int):
        level = logging.WARNING
        requested = DEFAULT_LEVEL

    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )
    return requested
