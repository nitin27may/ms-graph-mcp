"""
Phase 4d contract tests for the Graph integration.

Every module under ``ms_graph_mcp.*`` exposes ``@tool``-decorated
adapter functions that agents import. This test enforces the invariants
those adapters must satisfy so an agent can pick them up reliably:

  - The function is async.
  - It carries ``_is_tool`` + ``_tool_spec`` from the @tool decorator.
  - Its first positional parameter is a Pydantic BaseModel.
  - The Pydantic model serialises to a JSON schema with
    ``type=object`` and a ``properties`` block.
  - The ``ToolSpec.description`` is non-empty (LLM function-calling
    systems depend on it).

A second class walks the new package and asserts every module's
public ``@tool`` functions got into the global tool registry on import,
so no file silently stops exposing its tools.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
from pathlib import Path

import pytest
from pydantic import BaseModel

from ms_graph_mcp.tooling import get_registry

_GRAPH_DOMAINS = (
    "ms_graph_mcp.calendar",
    "ms_graph_mcp.directory",
    "ms_graph_mcp.email",
    "ms_graph_mcp.files",
    "ms_graph_mcp.internal",
    "ms_graph_mcp.meetings",
    "ms_graph_mcp.onenote",
    "ms_graph_mcp.people",
    "ms_graph_mcp.tasks",
    "ms_graph_mcp.teams",
)


def _iter_tools(module):
    for _, fn in inspect.getmembers(module, inspect.isfunction):
        if getattr(fn, "_is_tool", False):
            yield fn


def _tool_cases():
    for mod_path in _GRAPH_DOMAINS:
        module = importlib.import_module(mod_path)
        for fn in _iter_tools(module):
            yield pytest.param(mod_path, fn, id=f"{mod_path.rsplit('.', 1)[-1]}.{fn.__name__}")


@pytest.mark.parametrize("mod_path,fn", list(_tool_cases()))
class TestToolContract:
    def test_tool_is_async(self, mod_path, fn):
        assert asyncio.iscoroutinefunction(fn), (
            f"{mod_path}.{fn.__name__} must be async — tool registry only awaits coroutines"
        )

    def test_tool_registered(self, mod_path, fn):
        spec = getattr(fn, "_tool_spec", None)
        assert spec is not None, (
            f"{mod_path}.{fn.__name__} missing _tool_spec — was it imported without @tool?"
        )
        assert spec.name == fn.__name__
        assert spec.description.strip(), "tool description must be non-empty"

    def test_tool_input_is_pydantic_model(self, mod_path, fn):
        sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        assert len(params) >= 2, (
            f"{mod_path}.{fn.__name__} must take (params, context); got {params}"
        )
        hints = inspect.get_annotations(fn, eval_str=True)
        first_hint = hints.get(params[0].name)
        assert first_hint is not None, (
            f"{mod_path}.{fn.__name__} first parameter must be annotated with a Pydantic model"
        )
        assert isinstance(first_hint, type) and issubclass(first_hint, BaseModel), (
            f"{mod_path}.{fn.__name__} first parameter must be a Pydantic BaseModel; got {first_hint}"
        )

    def test_schema_is_object_with_properties(self, mod_path, fn):
        schema = fn._tool_spec.parameters
        assert schema.get("type") == "object", (
            f"{mod_path}.{fn.__name__} input schema must be type=object for OpenAI function calling"
        )
        # Even zero-parameter tools emit an empty properties block; presence, not shape, is the contract.
        assert "properties" in schema

    def test_function_registered_in_global_registry(self, mod_path, fn):
        registry = get_registry()
        assert registry.get(fn.__name__) is fn._tool_spec, (
            f"{mod_path}.{fn.__name__} is not in the global tool registry — "
            "imports may be skipping the @tool decorator"
        )


class TestPackageLayout:
    def test_every_module_imports_cleanly(self):
        """If a module fails to import, the graph integration is broken."""
        for mod_path in _GRAPH_DOMAINS:
            importlib.import_module(mod_path)

    def test_package_init_reexports_each_domain(self):
        import ms_graph_mcp as graph_pkg

        for mod_path in _GRAPH_DOMAINS:
            name = mod_path.rsplit(".", 1)[-1]
            assert hasattr(graph_pkg, name), (
                f"ms_graph_mcp should re-export {name} so "
                f"`from ms_graph_mcp import {name} as graph_{name}` works"
            )

    def test_package_is_self_contained(self):
        """Extraction guard — no module may import the original host application.

        This package was lifted out of a larger monorepo; nothing here may reach
        back into it (or into the internal libraries that were absorbed).
        """
        import ms_graph_mcp

        forbidden = (
            "shared",
            "agents",
            "integrations",
            "control_plane",
            "wg_tool_core",
            "wg_service_auth",
        )
        pkg_dir = Path(ms_graph_mcp.__path__[0])
        offenders: list[str] = []
        for py in pkg_dir.rglob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    if name.split(".")[0] in forbidden:
                        offenders.append(f"{py.name}:{node.lineno} → {name}")
        assert not offenders, "package reaches outside itself: " + "; ".join(offenders)
