"""App-Role gate — ANY semantics, auth-only, app-only policy."""

from __future__ import annotations

from ms_graph_mcp.entra.authz import check_roles
from ms_graph_mcp.entra.claims import Principal


def _p(roles=(), app_only=False) -> Principal:
    return Principal(
        subject_id="oid",
        email="a@b.com",
        tenant_id="t",
        roles=frozenset(roles),
        azp="app",
        is_app_only=app_only,
        raw={},
    )


def test_auth_only_admits_any_user():
    assert check_roles(_p(), set(), allow_app_only=False) is True


def test_required_role_present():
    assert check_roles(_p({"x"}), {"x"}, allow_app_only=False) is True


def test_required_role_absent():
    assert check_roles(_p({"y"}), {"x"}, allow_app_only=False) is False


def test_any_of_required_roles():
    assert check_roles(_p({"b"}), {"a", "b"}, allow_app_only=False) is True


def test_app_only_denied_by_default():
    assert check_roles(_p({"x"}, app_only=True), {"x"}, allow_app_only=False) is False


def test_app_only_allowed_with_flag_and_role():
    assert check_roles(_p({"x"}, app_only=True), {"x"}, allow_app_only=True) is True


def test_app_only_denied_in_auth_only_mode():
    assert check_roles(_p(app_only=True), set(), allow_app_only=False) is False
