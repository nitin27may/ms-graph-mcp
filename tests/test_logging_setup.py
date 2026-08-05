"""Log configuration for the console entry points.

Two properties, one of which is a protocol correctness issue rather than a
preference:

  1. Records go to **stderr**. On stdio, stdout carries JSON-RPC — a log line
     written there corrupts the stream and the client disconnects with a parse
     error that names nothing useful.
  2. The default is quiet. The per-request ``[Graph]`` lines are INFO, and a
     client that surfaces server output would otherwise show one line per Graph
     call to a user who did not ask for it.
"""

from __future__ import annotations

import logging
import sys

import pytest

from ms_graph_mcp.logging_setup import configure_logging


@pytest.fixture(autouse=True)
def _restore_logging():
    """Put the root logger back — configure_logging uses force=True."""
    root = logging.getLogger()
    saved = (root.level, list(root.handlers))
    yield
    root.setLevel(saved[0])
    root.handlers = saved[1]


def _handler_stream():
    return logging.getLogger().handlers[0].stream


class TestDestination:
    def test_records_go_to_stderr_not_stdout(self, monkeypatch):
        """The one that breaks the protocol if it regresses."""
        monkeypatch.delenv("GRAPH_MCP_LOG_LEVEL", raising=False)
        configure_logging()
        assert _handler_stream() is sys.stderr
        assert _handler_stream() is not sys.stdout


class TestLevel:
    def test_defaults_to_warning_so_a_normal_run_is_quiet(self, monkeypatch):
        monkeypatch.delenv("GRAPH_MCP_LOG_LEVEL", raising=False)
        assert configure_logging() == "WARNING"
        assert logging.getLogger().level == logging.WARNING

    def test_the_graph_lines_are_hidden_by_default(self, monkeypatch):
        """The reason the default is WARNING and not INFO."""
        monkeypatch.delenv("GRAPH_MCP_LOG_LEVEL", raising=False)
        configure_logging()
        assert not logging.getLogger("ms_graph_mcp.client").isEnabledFor(logging.INFO)

    def test_info_turns_the_graph_lines_on(self, monkeypatch):
        """Step one of docs/debugging.md — it has to actually work."""
        monkeypatch.setenv("GRAPH_MCP_LOG_LEVEL", "INFO")
        assert configure_logging() == "INFO"
        assert logging.getLogger("ms_graph_mcp.client").isEnabledFor(logging.INFO)

    @pytest.mark.parametrize("raw", ["debug", "Debug", " DEBUG "])
    def test_the_value_is_forgiving_about_case_and_whitespace(self, raw, monkeypatch):
        monkeypatch.setenv("GRAPH_MCP_LOG_LEVEL", raw)
        assert configure_logging() == "DEBUG"

    def test_a_bad_value_falls_back_rather_than_failing_to_start(self, monkeypatch):
        """Refusing to boot over a misspelled log level would be a poor trade."""
        monkeypatch.setenv("GRAPH_MCP_LOG_LEVEL", "LOUD")
        assert configure_logging() == "WARNING"
        assert logging.getLogger().level == logging.WARNING

    def test_a_numeric_level_is_not_mistaken_for_a_name(self, monkeypatch):
        """getattr(logging, "20") would miss; the fallback must catch it."""
        monkeypatch.setenv("GRAPH_MCP_LOG_LEVEL", "20")
        assert configure_logging() == "WARNING"


class TestItIsOnlyForEntryPoints:
    def test_importing_the_package_configures_nothing(self):
        """A library must not configure logging for its host application.

        Import is a side-effect-free operation; only the console scripts call
        configure_logging().
        """
        root = logging.getLogger()
        root.handlers = []

        import importlib

        import ms_graph_mcp

        importlib.reload(ms_graph_mcp)

        assert not root.handlers
