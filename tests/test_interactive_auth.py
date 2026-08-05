"""Interactive sign-in for the stdio transport.

The properties that matter are mostly about not breaking things: prompts must
never reach stdout, refresh tokens must not be world-readable, and a missing
credential must not stop the server starting — a client that cannot connect
gives the user nothing to read.
"""

from __future__ import annotations

import stat
from unittest.mock import MagicMock, patch

import pytest

from ms_graph_mcp.config import GraphMcpConfig, set_config
from ms_graph_mcp.interactive_auth import InteractiveAuthError, InteractiveTokenProvider
from ms_graph_mcp.stdio import _build_context


class TestProviderConfiguration:
    def test_a_missing_client_id_is_refused_with_setup_guidance(self):
        with pytest.raises(InteractiveAuthError) as exc:
            InteractiveTokenProvider(client_id="", tenant_id="t", scopes=["User.Read"])
        assert "GRAPH_MCP_CLIENT_ID" in str(exc.value)
        assert "public client" in str(exc.value)

    def test_reserved_scopes_are_stripped(self):
        """MSAL adds these itself and errors if they are passed in."""
        provider = InteractiveTokenProvider(
            client_id="c",
            tenant_id="t",
            scopes=["User.Read", "openid", "profile", "offline_access"],
        )
        assert provider._scopes == ["User.Read"]

    def test_tenant_defaults_to_common_when_unset(self):
        provider = InteractiveTokenProvider(client_id="c", tenant_id="", scopes=["User.Read"])
        assert provider._tenant_id == "common"


class TestTokenAcquisition:
    def test_a_cached_token_is_served_without_signing_in(self):
        provider = InteractiveTokenProvider(client_id="c", tenant_id="t", scopes=["User.Read"])
        app = MagicMock()
        app.get_accounts.return_value = [{"username": "a@x.com"}]
        app.acquire_token_silent.return_value = {"access_token": "cached-token"}
        provider._app = app

        with patch.object(provider, "_sign_in") as sign_in:
            assert provider.get_token() == "cached-token"
        sign_in.assert_not_called()

    def test_sign_in_happens_only_when_the_cache_cannot_serve(self):
        provider = InteractiveTokenProvider(client_id="c", tenant_id="t", scopes=["User.Read"])
        app = MagicMock()
        app.get_accounts.return_value = []
        provider._app = app
        provider._cache = MagicMock(has_state_changed=False)

        with patch.object(provider, "_sign_in", return_value="fresh-token") as sign_in:
            assert provider.get_token() == "fresh-token"
        sign_in.assert_called_once()

    def test_a_failed_device_flow_start_explains_the_likely_cause(self):
        provider = InteractiveTokenProvider(client_id="c", tenant_id="t", scopes=["User.Read"])
        app = MagicMock()
        app.initiate_device_flow.return_value = {"error_description": "nope"}
        with pytest.raises(InteractiveAuthError) as exc:
            provider._device_code(app)
        assert "public client flows" in str(exc.value)


class TestStdoutIsNotPolluted:
    """stdout carries the MCP protocol. Anything printed there corrupts it."""

    def test_device_code_instructions_go_to_stderr(self, capsys):
        provider = InteractiveTokenProvider(client_id="c", tenant_id="t", scopes=["User.Read"])
        app = MagicMock()
        app.initiate_device_flow.return_value = {
            "user_code": "ABCD-EFGH",
            "message": "Go to microsoft.com/devicelogin and enter ABCD-EFGH",
        }
        app.acquire_token_by_device_flow.return_value = {"access_token": "t"}

        provider._device_code(app)
        captured = capsys.readouterr()
        assert captured.out == "", "sign-in output must never reach stdout"
        assert "ABCD-EFGH" in captured.err

    def test_the_no_credentials_warning_goes_to_stderr(self, capsys, monkeypatch):
        monkeypatch.delenv("GRAPH_MCP_ACCESS_TOKEN", raising=False)
        set_config(GraphMcpConfig(_env_file=None, client_id=""))
        _build_context()
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "No credentials configured" in captured.err


class TestCacheSafety:
    def test_the_cache_file_is_owner_only(self, tmp_path, monkeypatch):
        """It holds refresh tokens — a group- or world-readable file is a leak."""
        import ms_graph_mcp.interactive_auth as mod

        cache_file = tmp_path / "token_cache.json"
        monkeypatch.setattr(mod, "_CACHE_DIR", tmp_path)
        monkeypatch.setattr(mod, "_CACHE_FILE", cache_file)

        cache = MagicMock(has_state_changed=True)
        cache.serialize.return_value = '{"RefreshToken": {}}'
        mod._save_cache(cache)

        mode = stat.S_IMODE(cache_file.stat().st_mode)
        assert mode == 0o600, f"cache is {oct(mode)}, expected 0o600"

    def test_an_unchanged_cache_is_not_rewritten(self, tmp_path, monkeypatch):
        import ms_graph_mcp.interactive_auth as mod

        cache_file = tmp_path / "token_cache.json"
        monkeypatch.setattr(mod, "_CACHE_FILE", cache_file)
        mod._save_cache(MagicMock(has_state_changed=False))
        assert not cache_file.exists()

    def test_a_corrupt_cache_does_not_prevent_sign_in(self, tmp_path, monkeypatch):
        import ms_graph_mcp.interactive_auth as mod

        cache_file = tmp_path / "token_cache.json"
        cache_file.write_text("not json at all")
        monkeypatch.setattr(mod, "_CACHE_FILE", cache_file)
        assert mod._load_cache() is not None


class TestStdioContext:
    def test_a_supplied_token_takes_precedence_and_needs_no_client_id(self, monkeypatch):
        monkeypatch.setenv("GRAPH_MCP_ACCESS_TOKEN", "supplied")
        set_config(GraphMcpConfig(_env_file=None, client_id=""))
        context = _build_context()
        assert context["access_token"] == "supplied"
        assert "token_provider" not in context

    def test_a_client_id_selects_interactive_sign_in(self, monkeypatch):
        monkeypatch.delenv("GRAPH_MCP_ACCESS_TOKEN", raising=False)
        set_config(GraphMcpConfig(_env_file=None, client_id="app-1", tenant_id="tenant-1"))
        context = _build_context()
        assert callable(context["token_provider"])
        # Empty until the first call, so nothing signs in at launch and the
        # client's connect does not block on a browser.
        assert context["access_token"] == ""

    def test_missing_credentials_still_produce_a_usable_context(self, monkeypatch, capsys):
        """Dying at launch leaves the user with a client that just says 'failed'."""
        monkeypatch.delenv("GRAPH_MCP_ACCESS_TOKEN", raising=False)
        set_config(GraphMcpConfig(_env_file=None, client_id=""))
        context = _build_context()
        capsys.readouterr()
        assert context["access_token"] == ""
        assert "token_provider" not in context

    def test_write_scope_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("GRAPH_MCP_ACCESS_TOKEN", "t")
        monkeypatch.setenv("GRAPH_MCP_WRITE_SCOPE", "true")
        set_config(GraphMcpConfig(_env_file=None))
        assert _build_context()["write_scope"] is True
