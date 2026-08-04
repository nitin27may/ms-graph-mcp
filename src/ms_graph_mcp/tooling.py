"""
Lightweight tool decorator and registry backing the MCP tool surface.

Provides a @tool decorator that:
  1. Marks an async function as an agent tool
  2. Extracts an OpenAI function-calling spec from the Pydantic input model
  3. Registers it in the *active* ToolRegistry

Usage:
    from ms_graph_mcp.tooling import tool, ToolRegistry

    @tool(description="Search emails by keyword")
    async def search_emails(params: SearchEmailsInput, context: dict) -> list[dict]:
        ...

Registry scoping
----------------
By default every ``@tool`` registers into one process-global registry — the
the right thing for a single MCP server
running in its own process. When two tool packages must coexist in ONE process
(e.g. an embedder loading both the Graph and DevOps MCP surfaces), wrap the
imports in ``local_registry()`` to capture an isolated registry instead:

    from ms_graph_mcp.tooling import local_registry
    with local_registry() as graph_reg:
        import ms_graph_mcp.tools  # @tool side effects land in graph_reg

``get_registry()`` always returns the registry active for the current context,
so a server built inside that ``with`` block dispatches against its own tools.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, get_type_hints

from pydantic import BaseModel, ValidationError

__all__ = [
    "ToolSpec",
    "ToolRegistry",
    "tool",
    "get_registry",
    "local_registry",
]


@dataclass
class ToolSpec:
    """OpenAI-compatible function definition for an agent tool."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema of the Pydantic input model
    fn: Callable


def _pydantic_to_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Extract a JSON Schema compatible with OpenAI function calling from a Pydantic model."""
    schema = model.model_json_schema()
    # Remove $defs / title noise that confuses some LLMs
    schema.pop("title", None)
    schema.pop("$defs", None)
    return schema


def _summarize_validation_error(exc: ValidationError) -> list[str]:
    """Turn a Pydantic ValidationError into short, LLM-readable field messages."""
    out: list[str] = []
    for err in exc.errors()[:8]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        out.append(f"{loc}: {err.get('msg', 'invalid')}")
    return out


def tool(description: str | None = None):
    """
    Decorator that marks an async function as an agent tool and registers it.

    The decorated function must:
      - Be async
      - Accept exactly two arguments: (params: SomePydanticModel, context: dict)
      - Return a JSON-serialisable value

    Args:
        description: Human-readable description sent to the LLM as the tool's purpose.
                     If omitted, the function docstring is used.
    """

    def decorator(fn: Callable) -> Callable:
        # C14 (agentic audit): the docstring above has always required "must be
        # async", but nothing enforced it — a sync function decorated by mistake
        # only failed at CALL time (`await spec.fn(...)` in ToolRegistry.call,
        # deep in the LLM tool-calling loop) with a confusing "can't be used in
        # 'await' expression" TypeError. Fail at decoration time instead, same
        # as the other structural checks below.
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"@tool function '{fn.__name__}' must be async (defined with 'async def')")

        fn_description = description or (inspect.getdoc(fn) or fn.__name__)

        # Extract the Pydantic model from the first parameter annotation
        hints = get_type_hints(fn)
        param_names = list(inspect.signature(fn).parameters.keys())
        if not param_names:
            raise TypeError(f"@tool function '{fn.__name__}' must accept at least one parameter")

        first_param_type = hints.get(param_names[0])
        if first_param_type is None or not (
            isinstance(first_param_type, type) and issubclass(first_param_type, BaseModel)
        ):
            raise TypeError(
                f"@tool function '{fn.__name__}': first parameter must be a Pydantic BaseModel, "
                f"got {first_param_type}"
            )

        spec = ToolSpec(
            name=fn.__name__,
            description=fn_description,
            parameters=_pydantic_to_json_schema(first_param_type),
            fn=fn,
        )
        # Attach spec to function for easy access
        fn._tool_spec = spec  # type: ignore[attr-defined]
        fn._is_tool = True  # type: ignore[attr-defined]

        # Register into the active registry (global by default; isolated inside
        # local_registry()).
        _active.get().register(spec)

        return fn

    return decorator


class ToolRegistry:
    """Registry of @tool-decorated functions."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def openai_specs(self, tools: list[Callable]) -> list[dict[str, Any]]:
        """
        Convert a list of tool functions into the OpenAI function-calling format:
        [{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}]
        """
        specs = []
        for fn in tools:
            if not getattr(fn, "_is_tool", False):
                raise ValueError(f"Function '{fn.__name__}' is not decorated with @tool")
            ts: ToolSpec = fn._tool_spec
            specs.append(
                {
                    "type": "function",
                    "function": {
                        "name": ts.name,
                        "description": ts.description,
                        "parameters": ts.parameters,
                    },
                }
            )
        return specs

    async def call(self, name: str, arguments_json: str, context: dict) -> Any:
        """
        Invoke a registered tool by name with JSON arguments.
        The Pydantic model is instantiated from the JSON string before calling.

        Malformed JSON or schema-violating arguments do NOT raise — they return
        a structured ``invalid_arguments`` dict so the executor can feed a
        clear, actionable correction back to the LLM (which then self-fixes)
        instead of surfacing a cryptic exception repr.
        """
        spec = self._tools.get(name)
        if spec is None:
            raise ValueError(f"Tool '{name}' not found in registry")

        hints = get_type_hints(spec.fn)
        param_names = list(inspect.signature(spec.fn).parameters.keys())
        input_model_type = hints[param_names[0]]

        try:
            arguments = json.loads(arguments_json or "{}")
        except json.JSONDecodeError as exc:
            return {
                "error": "invalid_arguments",
                "message": (
                    f"The arguments for tool '{name}' were not valid JSON ({exc}). "
                    "Call the tool again with a valid JSON arguments object."
                ),
                "expected_schema": spec.parameters,
            }

        try:
            params = input_model_type(**arguments)
        except ValidationError as exc:
            return {
                "error": "invalid_arguments",
                "message": (
                    f"The arguments for tool '{name}' did not match its schema: "
                    f"{exc.error_count()} error(s) — {'; '.join(_summarize_validation_error(exc))}. "
                    "Call the tool again with corrected arguments."
                ),
                "expected_schema": spec.parameters,
            }

        return await spec.fn(params, context)


# Module-level singleton — the default registry. Tool files auto-register here
# on import unless a local_registry() context is active.
_REGISTRY = ToolRegistry()

# The registry @tool registers into / get_registry() reads, swappable per
# context. Defaults to the global singleton so existing single-process
# behaviour is byte-for-byte unchanged.
_active: ContextVar[ToolRegistry] = ContextVar("wg_active_tool_registry", default=_REGISTRY)


def get_registry() -> ToolRegistry:
    """Return the registry active for the current context (global by default)."""
    return _active.get()


@contextmanager
def local_registry() -> Iterator[ToolRegistry]:
    """Activate a fresh, isolated ToolRegistry for the duration of the block.

    ``@tool`` decorations and ``get_registry()`` calls inside the block resolve
    to this registry instead of the process global, so two tool packages can be
    imported into one process without colliding. The previous active registry
    is restored on exit.
    """
    reg = ToolRegistry()
    token = _active.set(reg)
    try:
        yield reg
    finally:
        _active.reset(token)
