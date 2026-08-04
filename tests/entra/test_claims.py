"""Principal extraction — delegated vs app-only, roles coercion."""

from __future__ import annotations

from ms_graph_mcp.entra.claims import extract_principal


def test_delegated_user():
    p = extract_principal(
        {
            "preferred_username": "Alice@Example.com",
            "oid": "o1",
            "tid": "t1",
            "roles": ["meeting-prep.user"],
            "azp": "app",
            "scp": "access_as_user",
        }
    )
    assert p.email == "alice@example.com"
    assert p.is_app_only is False
    assert p.roles == frozenset({"meeting-prep.user"})
    assert p.azp == "app"
    assert p.subject_id == "o1"


def test_app_only_idtyp():
    p = extract_principal(
        {
            "idtyp": "app",
            "appid": "svc",
            "roles": ["meeting-prep.automation"],
            "oid": "sp",
        }
    )
    assert p.is_app_only is True
    assert p.azp == "svc"
    assert p.email == ""


def test_app_only_inferred_when_no_user_and_no_scp():
    p = extract_principal({"appid": "svc", "oid": "sp"})
    assert p.is_app_only is True


def test_real_app_only_token_is_never_is_machine():
    """S2 (agentic audit) — is_machine distinguishes the machine-secret
    bypass from a real app-only Entra token. Both are is_app_only=True, but
    only the shared-secret bypass path (middleware._machine_principal) may
    ever set is_machine=True. extract_principal handles real verified JWTs
    exclusively and must never set it, regardless of idtyp."""
    p = extract_principal({"idtyp": "app", "appid": "svc", "oid": "sp"})
    assert p.is_app_only is True
    assert p.is_machine is False


def test_roles_string_is_coerced_to_set():
    p = extract_principal(
        {"preferred_username": "a@b.com", "scp": "x", "roles": "single-role"}
    )
    assert p.roles == frozenset({"single-role"})


def test_missing_roles_is_empty():
    p = extract_principal({"preferred_username": "a@b.com", "scp": "x"})
    assert p.roles == frozenset()


def test_upn_and_email_fallbacks():
    assert extract_principal({"upn": "U@b.com", "scp": "x"}).email == "u@b.com"
    assert extract_principal({"email": "E@b.com", "scp": "x"}).email == "e@b.com"
