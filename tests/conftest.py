"""Shared fixtures for ms-graph-mcp tests."""

from __future__ import annotations

import pytest

from ms_graph_mcp.config import reset_config


@pytest.fixture(autouse=True)
def _reset_graph_config():
    """build_app() mutates the cached config singleton; reset between tests so
    a shared_secret set by one test never leaks into another."""
    reset_config()
    yield
    reset_config()
