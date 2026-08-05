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
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, get_type_hints

from pydantic import BaseModel, ValidationError

__all__ = [
    "READ_ONLY",
    "ToolAnnotations",
    "ToolRegistry",
    "ToolSpec",
    "WRITE_CREATE",
    "WRITE_DESTRUCTIVE",
    "WRITE_SEND",
    "WRITE_UPDATE",
    "get_registry",
    "local_registry",
    "tool",
]


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolAnnotations:
    """Behavioural hints a client uses to decide what needs human confirmation.

    MCP's documented default for a tool that declares nothing is the most
    cautious reading available — non-read-only, potentially destructive,
    non-idempotent, open-world. A read tool that stays silent is therefore
    treated as dangerous, and clients may prompt the user before it runs.
    Declaring the hints is how a read tool gets out of that.

    These are hints, not enforcement. The tier system in ``allowlists.py`` is
    what actually stops a write happening; annotations only shape client UX.
    Clients are explicitly told to distrust them from untrusted servers.
    """

    read_only: bool
    destructive: bool
    idempotent: bool
    # Every tool here talks to Microsoft Graph, so this is always True. Kept as
    # a field rather than hardcoded so a future local/offline tool can say so.
    open_world: bool = True


# The five shapes every tool in this package falls into. Using these instead of
# spelling out four booleans per tool keeps the semantics consistent — and makes
# a miscategorised tool visible in review as the wrong constant name.
READ_ONLY = ToolAnnotations(read_only=True, destructive=False, idempotent=True)
#: Creates something new. Not idempotent — calling twice creates two.
WRITE_CREATE = ToolAnnotations(read_only=False, destructive=False, idempotent=False)
#: Sets fields on something that exists. Same call twice lands the same state.
WRITE_UPDATE = ToolAnnotations(read_only=False, destructive=False, idempotent=True)
#: Sends a message. Deliberately NOT idempotent: a retry mails or posts twice.
WRITE_SEND = ToolAnnotations(read_only=False, destructive=False, idempotent=False)
#: Withdraws something already visible to other people (e.g. cancelling a meeting).
WRITE_DESTRUCTIVE = ToolAnnotations(read_only=False, destructive=True, idempotent=True)


@dataclass
class ToolSpec:
    """OpenAI-compatible function definition for an agent tool."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema of the Pydantic input model
    fn: Callable
    annotations: ToolAnnotations | None = None
    #: Superseded names that still dispatch. Advertised nowhere — see
    #: ``ToolRegistry.register``.
    aliases: tuple[str, ...] = ()


def _schema_has_ref(node: Any) -> bool:
    """True if ``node`` contains a ``$ref`` anywhere in its tree."""
    if isinstance(node, dict):
        if "$ref" in node:
            return True
        return any(_schema_has_ref(v) for v in node.values())
    if isinstance(node, list):
        return any(_schema_has_ref(v) for v in node)
    return False


def _pydantic_to_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Extract a JSON Schema compatible with OpenAI function calling from a Pydantic model.

    ``title`` is dropped unconditionally — it is noise that costs tokens and tells
    the model nothing its own field name doesn't.

    ``$defs`` is dropped **only when nothing references it**. Pydantic emits
    ``$defs`` plus ``{"$ref": "#/$defs/Foo"}`` for any nested model or Enum; the
    strip used to be unconditional, which left a pointer to a definition that was
    no longer there. Every input model was flat when that was written, so nothing
    broke — but the first nested model would have shipped a schema no client could
    resolve. MCP requires clients to follow ``$ref`` resolution when validating
    tool inputs, so a dangling ref is a protocol break, not cosmetic noise.
    """
    schema = model.model_json_schema()
    schema.pop("title", None)
    if not _schema_has_ref(schema):
        schema.pop("$defs", None)
    return schema


def _summarize_validation_error(exc: ValidationError) -> list[str]:
    """Turn a Pydantic ValidationError into short, LLM-readable field messages."""
    out: list[str] = []
    for err in exc.errors()[:8]:
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        out.append(f"{loc}: {err.get('msg', 'invalid')}")
    return out


def tool(
    description: str | None = None,
    *,
    annotations: ToolAnnotations | None = None,
    aliases: tuple[str, ...] | str = (),
):
    """
    Decorator that marks an async function as an agent tool and registers it.

    The decorated function must:
      - Be async
      - Accept exactly two arguments: (params: SomePydanticModel, context: dict)
      - Return a JSON-serialisable value

    Args:
        description: What the tool does, when to use it, what it returns, and how
                     it differs from neighbouring tools. This is the only thing a
                     model has to choose by, so terseness here is not a saving —
                     it is the main cause of mis-selection. Aim for 200–400 chars.
                     Falls back to the docstring.
        annotations: One of the module-level constants (``READ_ONLY``,
                     ``WRITE_CREATE``, ``WRITE_UPDATE``, ``WRITE_SEND``,
                     ``WRITE_DESTRUCTIVE``). Omitting it means clients apply MCP's
                     most cautious defaults and may prompt the user before a
                     harmless read.
        aliases:     Superseded names that must keep dispatching. Not advertised.
    """

    def decorator(fn: Callable) -> Callable:
        # C14 (agentic audit): the docstring above has always required "must be
        # async", but nothing enforced it — a sync function decorated by mistake
        # only failed at CALL time (`await spec.fn(...)` in ToolRegistry.call,
        # deep in the LLM tool-calling loop) with a confusing "can't be used in
        # 'await' expression" TypeError. Fail at decoration time instead, same
        # as the other structural checks below.
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(
                f"@tool function '{fn.__name__}' must be async (defined with 'async def')"
            )

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

        alias_tuple = (aliases,) if isinstance(aliases, str) else tuple(aliases)
        spec = ToolSpec(
            name=fn.__name__,
            description=fn_description,
            parameters=_pydantic_to_json_schema(first_param_type),
            fn=fn,
            annotations=annotations,
            aliases=alias_tuple,
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
    """Registry of @tool-decorated functions.

    Aliases are held in a *separate* map from canonical names. Keeping them apart
    is what lets ``tools/list`` advertise the canonical surface while
    ``tools/call`` still honours a superseded name: iterating ``_tools`` can
    never accidentally emit a deprecated duplicate.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._aliases: dict[str, str] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec
        for alias in spec.aliases:
            if alias in self._tools:
                raise ValueError(
                    f"tool '{spec.name}' claims alias '{alias}', which is another tool's "
                    "canonical name"
                )
            self._aliases[alias] = spec.name

    def get(self, name: str) -> ToolSpec | None:
        """Resolve a canonical name, falling back to the alias map."""
        spec = self._tools.get(name)
        if spec is not None:
            return spec
        canonical = self._aliases.get(name)
        if canonical is None:
            return None
        logger.warning(
            "tool '%s' is a deprecated name for '%s'; it will stop working in a future "
            "release. Update the caller.",
            name,
            canonical,
        )
        return self._tools.get(canonical)

    def canonical_name(self, name: str) -> str | None:
        """The canonical name for ``name``, whether it is canonical or an alias."""
        if name in self._tools:
            return name
        return self._aliases.get(name)

    def names(self) -> list[str]:
        """Canonical names only — never aliases."""
        return list(self._tools)

    def aliases(self) -> list[str]:
        """Superseded names still honoured by ``call`` — never advertised."""
        return list(self._aliases)

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
