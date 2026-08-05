"""Deployment-level security posture.

Two properties that are easy to get wrong by omission rather than by mistake:
the default value of signature verification, and whether a read-only deployment
can be talked into a write by a caller that asks nicely.
"""

from __future__ import annotations

import json

import pytest

from ms_graph_mcp.allowlists import WRITE_TOOL_NAMES
from ms_graph_mcp.config import GraphMcpConfig, set_config
from ms_graph_mcp.context import current_request_context


class TestJwtVerifyDefault:
    def test_signature_verification_is_on_by_default(self):
        """The safe value must be the one you get by doing nothing.

        This previously defaulted False, so an HTTP deployment accepted
        unverified tokens unless somebody had read the docs. Documentation does
        not prevent that.
        """
        assert GraphMcpConfig(_env_file=None).jwt_verify is True

    def test_it_can_still_be_turned_off_explicitly(self, monkeypatch):
        """Disabling stays possible — it just has to be a deliberate act."""
        monkeypatch.setenv("GRAPH_MCP_JWT_VERIFY", "false")
        assert GraphMcpConfig(_env_file=None).jwt_verify is False

    def test_the_default_reaches_the_auth_config(self):
        """A default that never propagates to the verifier would be theatre."""
        cfg = GraphMcpConfig(_env_file=None)
        assert cfg.to_auth_config().jwt_verify is True


class TestReadOnlyDeployment:
    """GRAPH_MCP_READ_ONLY removes the write tier from the deployment."""

    def test_defaults_to_off(self):
        assert GraphMcpConfig(_env_file=None).read_only is False

    async def test_write_tools_are_not_advertised(self, list_tools):
        set_config(GraphMcpConfig(_env_file=None, read_only=True, toolsets="all"))
        cv = current_request_context.set({"access_token": "tok", "write_scope": True})
        try:
            names = {tool.name for tool in await list_tools()}
        finally:
            current_request_context.reset(cv)
        leaked = names & set(WRITE_TOOL_NAMES)
        assert not leaked, f"read-only deployment advertised write tools: {sorted(leaked)}"

    @pytest.mark.parametrize("name", WRITE_TOOL_NAMES)
    async def test_write_tools_are_refused_even_with_write_scope(self, name, call_tool):
        """The important half.

        Hiding a tool from tools/list is a context-efficiency measure, not a
        security boundary — a caller can name any tool it likes. The refusal has
        to happen at dispatch, and it has to ignore the write scope entirely.
        """
        set_config(GraphMcpConfig(_env_file=None, read_only=True, toolsets="all"))
        cv = current_request_context.set({"access_token": "tok", "write_scope": True})
        try:
            result = await call_tool(name, {})
        finally:
            current_request_context.reset(cv)

        assert result.is_error is True
        payload = json.loads(result.content[0].text)
        assert payload["error"] == "read_only_deployment"
        # The model must understand this is not something a retry can fix.
        assert "will not change it" in payload["message"]

    async def test_read_tools_still_work(self, call_tool, monkeypatch):
        """Read-only must not break reading."""
        set_config(GraphMcpConfig(_env_file=None, read_only=True, toolsets="all"))

        class _Reg:
            def canonical_name(self, name):
                return name

            async def call(self, name, arguments_json, context):
                return {"ok": True}

        monkeypatch.setattr("ms_graph_mcp.server.get_registry", lambda: _Reg())
        cv = current_request_context.set({"access_token": "tok"})
        try:
            result = await call_tool("people_get_my_profile", {})
        finally:
            current_request_context.reset(cv)
        assert result.is_error is False

    async def test_write_tools_work_again_when_the_flag_is_off(self, list_tools):
        """Guards against the flag being read once and cached wrongly."""
        # toolsets="all" so this asserts the read_only flag, not profile filtering.
        set_config(GraphMcpConfig(_env_file=None, read_only=False, toolsets="all"))
        cv = current_request_context.set({"access_token": "tok", "write_scope": True})
        try:
            names = {tool.name for tool in await list_tools()}
        finally:
            current_request_context.reset(cv)
        assert set(WRITE_TOOL_NAMES) <= names

    def test_the_setting_is_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("GRAPH_MCP_READ_ONLY", "true")
        assert GraphMcpConfig(_env_file=None).read_only is True


class TestNoGatewayTrustMode:
    def test_there_is_no_setting_that_disables_authentication(self):
        """Recorded as a test so nobody adds one without deleting this.

        A flag that skips validation because 'the gateway already did it' gets
        deployed without the gateway eventually, and the result is an
        unauthenticated Graph proxy. See ADR 0003.
        """
        forbidden = {"auth_mode", "skip_auth", "trust_gateway", "disable_auth", "allow_anonymous"}
        present = forbidden & set(GraphMcpConfig.model_fields)
        assert not present, f"a gateway-trust style setting was added: {sorted(present)}"
