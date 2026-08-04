"""
Tests for the Entra ID directory tools (``ms_graph_mcp.directory``).

Verifies:
  - Tools use the correct context keys for auth — user-scoped lookups take the
    delegated OBO token, group lookups take the app-only Entra token.
  - No credentials are hardcoded in the tool source.
  - All 7 directory tools are importable and registered.
  - search_users / get_user_groups behave correctly against a mocked Graph.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch


class TestDirectoryToolTokenUsage:
    """Verify tools use correct context keys for auth."""

    def _read_source(self) -> str:
        import ms_graph_mcp.directory

        return Path(ms_graph_mcp.directory.__file__).read_text()

    def test_user_tools_use_access_token(self):
        source = self._read_source()
        # search_users, get_user_details, get_user_manager, get_user_groups
        # should use context["access_token"] (OBO)
        assert 'context["access_token"]' in source

    def test_group_tools_use_entra_app_token(self):
        source = self._read_source()
        # search_groups, get_group_members, get_group_details
        # should use context["entra_app_token"] with fallback
        assert 'context.get("entra_app_token")' in source

    def test_no_hardcoded_credentials(self):
        source = self._read_source()
        non_comment_lines = [
            line for line in source.splitlines() if not line.strip().startswith("#")
        ]
        clean = "\n".join(non_comment_lines)
        assert "client_secret" not in clean.lower()
        assert "password" not in clean.lower()

    def test_uses_graph_get_helper(self):
        source = self._read_source()
        assert "from ms_graph_mcp.client import graph_get" in source


class TestDirectoryToolFunctions:
    """Test tool function signatures and imports."""

    def test_search_users_importable(self):
        from ms_graph_mcp.directory import search_users

        assert callable(search_users)

    def test_get_user_details_importable(self):
        from ms_graph_mcp.directory import get_user_details

        assert callable(get_user_details)

    def test_get_user_manager_importable(self):
        from ms_graph_mcp.directory import get_user_manager

        assert callable(get_user_manager)

    def test_get_user_groups_importable(self):
        from ms_graph_mcp.directory import get_user_groups

        assert callable(get_user_groups)

    def test_search_groups_importable(self):
        from ms_graph_mcp.directory import search_groups

        assert callable(search_groups)

    def test_get_group_members_importable(self):
        from ms_graph_mcp.directory import get_group_members

        assert callable(get_group_members)

    def test_get_group_details_importable(self):
        from ms_graph_mcp.directory import get_group_details

        assert callable(get_group_details)


class TestSearchUsersWithMock:
    """Test search_users with mocked Graph API."""

    async def test_exact_email_match(self):
        from ms_graph_mcp.directory import SearchUsersInput, search_users

        mock_user = {
            "id": "u1",
            "displayName": "Alice Wu",
            "mail": "alice@company.com",
            "userPrincipalName": "alice@company.com",
            "jobTitle": "PM",
            "department": "Product",
            "officeLocation": "Building A",
            "businessPhones": ["+1-555-0100"],
        }

        with patch("ms_graph_mcp.directory.graph_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_user
            result = await search_users(
                SearchUsersInput(query="alice@company.com"),
                {"access_token": "test-token"},
            )

        assert len(result) == 1
        assert result[0]["displayName"] == "Alice Wu"
        assert result[0]["mail"] == "alice@company.com"

    async def test_name_search_returns_list(self):
        from ms_graph_mcp.directory import SearchUsersInput, search_users

        with patch("ms_graph_mcp.directory.graph_get", new_callable=AsyncMock) as mock_get:
            # Name query (no @) goes straight to $search — single call
            mock_get.return_value = {
                "value": [
                    {
                        "id": "u1",
                        "displayName": "Alice Wu",
                        "mail": "alice@co.com",
                        "userPrincipalName": "alice@co.com",
                        "jobTitle": "PM",
                        "department": "Product",
                        "officeLocation": "",
                        "businessPhones": [],
                    },
                    {
                        "id": "u2",
                        "displayName": "Alice Chen",
                        "mail": "achen@co.com",
                        "userPrincipalName": "achen@co.com",
                        "jobTitle": "Dev",
                        "department": "Eng",
                        "officeLocation": "",
                        "businessPhones": [],
                    },
                ]
            }
            result = await search_users(
                SearchUsersInput(query="Alice"),
                {"access_token": "test-token"},
            )

        assert len(result) == 2
        assert result[0]["displayName"] == "Alice Wu"
        assert result[1]["displayName"] == "Alice Chen"


class TestGetUserGroups:
    """Regression: ``get_user_groups`` MUST cast the path to
    ``microsoft.graph.group`` and prefer the entra_app token.

    Symptom of the bug we're guarding against: groups all rendered as
    "Unnamed group" because Graph applies ``$select`` against the mixed-type
    directoryObject collection and returns ``null`` for every typed-only
    field on every row. The fix mirrors what graph_routes.py already does
    for the dedicated /entra page (which renders correctly).
    """

    async def test_uses_cast_path_microsoft_graph_group(self):
        from ms_graph_mcp.directory import UserIdentifierInput, get_user_groups

        with patch("ms_graph_mcp.directory.graph_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {
                "value": [
                    {
                        "id": "g1",
                        "displayName": "Engineering",
                        "description": "Eng team",
                        "groupTypes": ["Unified"],
                        "securityEnabled": False,
                        "mailEnabled": True,
                    },
                ]
            }
            await get_user_groups(
                UserIdentifierInput(user="alice@co.com"),
                {"access_token": "obo-token", "entra_app_token": "app-token"},
            )

        call = mock_get.call_args
        # Path must be cast to /microsoft.graph.group — bare /memberOf returns
        # nulled-out displayName fields on the mixed collection.
        assert call.args[1].endswith("/memberOf/microsoft.graph.group"), (
            f"path was {call.args[1]} — bug regressed; field will be null"
        )

    async def test_prefers_entra_app_token_over_obo(self):
        from ms_graph_mcp.directory import UserIdentifierInput, get_user_groups

        with patch("ms_graph_mcp.directory.graph_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {"value": []}
            await get_user_groups(
                UserIdentifierInput(user="alice@co.com"),
                {"access_token": "obo-token", "entra_app_token": "app-token"},
            )

        # First positional arg of graph_get is the token.
        used_token = mock_get.call_args.args[0]
        assert used_token == "app-token", (
            "must prefer the Entra app-only token; OBO triggers Graph's "
            "limited-information rule which returns null for typed fields"
        )

    async def test_falls_back_to_obo_when_app_token_missing(self):
        from ms_graph_mcp.directory import UserIdentifierInput, get_user_groups

        with patch("ms_graph_mcp.directory.graph_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {"value": []}
            await get_user_groups(
                UserIdentifierInput(user="alice@co.com"),
                {"access_token": "obo-token"},  # no entra_app_token
            )

        used_token = mock_get.call_args.args[0]
        assert used_token == "obo-token"

    async def test_returns_normalized_group_dicts(self):
        from ms_graph_mcp.directory import UserIdentifierInput, get_user_groups

        with patch("ms_graph_mcp.directory.graph_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {
                "value": [
                    {
                        "id": "g1",
                        "displayName": "Engineering",
                        "description": "Eng team",
                        "groupTypes": ["Unified"],
                        "securityEnabled": False,
                        "mailEnabled": True,
                        "mail": "eng@co.com",
                    },
                    {
                        "id": "g2",
                        "displayName": "Security-Admins",
                        "description": "",
                        "groupTypes": [],
                        "securityEnabled": True,
                        "mailEnabled": False,
                    },
                ]
            }
            result = await get_user_groups(
                UserIdentifierInput(user="alice@co.com"),
                {"access_token": "t", "entra_app_token": "app"},
            )

        assert len(result) == 2
        assert result[0]["displayName"] == "Engineering"
        assert result[0]["type"] == "Microsoft 365"
        assert result[1]["displayName"] == "Security-Admins"
        assert result[1]["type"] == "Security"

    async def test_uses_consistency_level_eventual(self):
        """``$count=true`` requires ConsistencyLevel: eventual, same as
        graph_routes.get_user_member_of."""
        from ms_graph_mcp.directory import UserIdentifierInput, get_user_groups

        with patch("ms_graph_mcp.directory.graph_get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {"value": []}
            await get_user_groups(
                UserIdentifierInput(user="alice@co.com"),
                {"access_token": "t", "entra_app_token": "app"},
            )

        kwargs = mock_get.call_args.kwargs
        assert kwargs.get("headers", {}).get("ConsistencyLevel") == "eventual"
        assert kwargs.get("$count") == "true"
