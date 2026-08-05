"""Contract tests for ms_graph_mcp.tooling: registration, dispatch, validation, isolation."""

from __future__ import annotations

import json
import re
from enum import StrEnum

import pytest
from pydantic import BaseModel, Field

from ms_graph_mcp.tooling import (
    ToolRegistry,
    ToolSpec,
    _pydantic_to_json_schema,
    get_registry,
    local_registry,
    tool,
)


class _Echo(BaseModel):
    text: str
    times: int = 1


def test_tool_decorator_sets_attrs_and_registers_into_global():
    @tool(description="echo the text N times")
    async def echo_global(params: _Echo, context: dict) -> str:
        return params.text * params.times

    assert echo_global._is_tool is True
    spec: ToolSpec = echo_global._tool_spec
    assert spec.name == "echo_global"
    assert spec.description == "echo the text N times"
    # JSON Schema is an object with the model's properties, no $defs/title noise.
    assert spec.parameters["type"] == "object"
    assert set(spec.parameters["properties"]) == {"text", "times"}
    assert "title" not in spec.parameters and "$defs" not in spec.parameters
    # Registered into the global (default) registry.
    assert get_registry().get("echo_global") is spec


def test_tool_requires_pydantic_first_param():
    with pytest.raises(TypeError):

        @tool()
        async def bad(params: dict, context: dict) -> None:  # type: ignore[arg-type]
            ...


def test_tool_rejects_sync_function():
    """C14 (agentic audit) — a sync function decorated by mistake used to only
    fail at CALL time inside ToolRegistry.call's `await spec.fn(...)`, deep in
    the LLM tool-calling loop, with a confusing 'can't be used in await
    expression' TypeError. Must fail at decoration time instead."""
    with pytest.raises(TypeError, match="must be async"):

        @tool()
        def sync_tool(params: _Echo, context: dict) -> str:  # type: ignore[misc]
            return params.text


def test_description_falls_back_to_docstring():
    @tool()
    async def documented(params: _Echo, context: dict) -> str:
        """Doc body becomes the description."""
        return params.text

    assert documented._tool_spec.description == "Doc body becomes the description."


def test_openai_specs_shape():
    @tool(description="d")
    async def specced(params: _Echo, context: dict) -> str:
        return params.text

    specs = get_registry().openai_specs([specced])
    assert specs == [
        {
            "type": "function",
            "function": {
                "name": "specced",
                "description": "d",
                "parameters": specced._tool_spec.parameters,
            },
        }
    ]


def test_openai_specs_rejects_non_tool():
    async def plain(params, context): ...

    with pytest.raises(ValueError):
        get_registry().openai_specs([plain])


async def test_call_happy_path():
    @tool(description="d")
    async def call_ok(params: _Echo, context: dict) -> str:
        return params.text * params.times

    out = await get_registry().call("call_ok", '{"text": "ab", "times": 2}', {})
    assert out == "abab"


async def test_call_invalid_json_returns_structured_error():
    @tool(description="d")
    async def call_badjson(params: _Echo, context: dict) -> str:
        return params.text

    out = await get_registry().call("call_badjson", "{not json", {})
    assert out["error"] == "invalid_arguments"
    assert "valid JSON" in out["message"]
    assert out["expected_schema"]["type"] == "object"


async def test_call_schema_violation_returns_structured_error():
    @tool(description="d")
    async def call_badschema(params: _Echo, context: dict) -> str:
        return params.text

    out = await get_registry().call("call_badschema", "{}", {})  # missing required 'text'
    assert out["error"] == "invalid_arguments"
    assert "text" in out["message"]


async def test_call_unknown_tool_raises():
    with pytest.raises(ValueError):
        await get_registry().call("does_not_exist", "{}", {})


def test_local_registry_isolates_and_restores():
    global_reg = get_registry()

    with local_registry() as reg:
        assert get_registry() is reg
        assert reg is not global_reg

        @tool(description="local only")
        async def only_local(params: _Echo, context: dict) -> str:
            return params.text

        # Lands in the local registry, not the global one.
        assert reg.get("only_local") is only_local._tool_spec
        assert global_reg.get("only_local") is None

    # Context restored after the block.
    assert get_registry() is global_reg
    assert global_reg.get("only_local") is None


def test_nested_local_registries():
    outer_default = get_registry()
    with local_registry() as a:
        with local_registry() as b:
            assert get_registry() is b
            assert b is not a
        assert get_registry() is a
    assert get_registry() is outer_default


def test_registry_get_returns_none_for_missing():
    assert ToolRegistry().get("nope") is None


# ── JSON Schema emission ──────────────────────────────────────────────────────
# `$defs` used to be stripped unconditionally, which left `$ref` pointers to
# definitions that were no longer in the document. Every input model was flat at
# the time, so nothing broke — these tests exist so it stays that way once nested
# models arrive.


class _Priority(StrEnum):
    low = "low"
    high = "high"


class _Nested(BaseModel):
    email: str
    kind: str = "required"


class _WithRefs(BaseModel):
    subject: str
    people: list[_Nested] = Field(default_factory=list)
    priority: _Priority = _Priority.low


def _refs(schema: dict) -> set[str]:
    blob = json.dumps(schema)
    return set(re.findall(r'"\$ref":\s*"#/\$defs/([^"]+)"', blob))


def test_nested_model_schema_keeps_its_defs_resolvable():
    schema = _pydantic_to_json_schema(_WithRefs)
    dangling = _refs(schema) - set(schema.get("$defs", {}))
    assert not dangling, f"schema references definitions that were stripped: {sorted(dangling)}"


def test_enum_field_ref_resolves():
    schema = _pydantic_to_json_schema(_WithRefs)
    assert "_Priority" in schema.get("$defs", {})


def test_flat_model_schema_carries_no_defs():
    """The lean path is preserved — a model with no refs still drops $defs."""
    schema = _pydantic_to_json_schema(_Echo)
    assert "$defs" not in schema
    assert not _refs(schema)


def test_title_is_always_stripped():
    for model in (_Echo, _WithRefs):
        assert "title" not in _pydantic_to_json_schema(model)
