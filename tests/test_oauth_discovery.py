"""RFC 9728 protected-resource metadata and the 401 challenge.

The MCP authorization spec expects an unauthenticated client to be able to work
out *how* to authenticate from the 401 alone: the response carries a
``WWW-Authenticate`` header pointing at a metadata document, and that document
names the authorization server. Without both halves a client only learns it was
refused, and every integration needs bespoke configuration instead.

Two properties are easy to break and are asserted here directly:

  1. The metadata document must be reachable **without a token**. Serving OAuth
     discovery behind the authentication it describes makes it useless — and
     since this server authenticates everything by default, the carve-out is a
     deliberate exception rather than the natural state of the code.
  2. The carve-out must not leak. ``/mcp`` stays authenticated, and a path that
     merely *contains* ``.well-known`` must not slip through.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from ms_graph_mcp.app import build_app
from ms_graph_mcp.config import GraphMcpConfig

ORIGIN = "https://graph-mcp.example.com"
RESOURCE_URL = f"{ORIGIN}/mcp"
TENANT = "bcb80eda-c944-4481-b417-3a3d39dcc8ef"
METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"


def _config(**overrides) -> GraphMcpConfig:
    base = {
        "_env_file": None,
        "tenant_id": TENANT,
        "client_id": "mcp-client-id",
        # The default posture is resource server, which build_app() refuses to
        # start without a confidential-client credential. These tests are about
        # discovery rather than auth, but they should still run against the
        # posture a real deployment gets.
        "client_secret": "s3cret",
        "shared_secret": "s3cret",
        "resource_url": RESOURCE_URL,
    }
    return GraphMcpConfig(**{**base, **overrides})


@pytest.fixture
def client():
    with TestClient(build_app(_config()), base_url=ORIGIN) as c:
        yield c


class TestMetadataDocument:
    def test_it_is_served_without_a_token(self, client):
        """The property the whole feature rests on.

        A client that already had a token would not need to read this.
        """
        response = client.get(METADATA_PATH)
        assert response.status_code == 200, (
            "discovery metadata is behind auth — an unauthenticated client cannot bootstrap"
        )

    def test_it_names_the_resource_and_its_authorization_server(self, client):
        body = client.get(METADATA_PATH).json()
        assert body["resource"] == RESOURCE_URL
        assert body["authorization_servers"] == [f"https://login.microsoftonline.com/{TENANT}/v2.0"]

    def test_it_advertises_the_configured_scopes(self, client):
        body = client.get(METADATA_PATH).json()
        assert "Mail.Read" in body["scopes_supported"]

    def test_the_authorization_server_follows_the_tenant(self):
        """A single-tenant deployment must not point clients at /common."""
        cfg = _config(tenant_id="contoso-tenant-id")
        assert cfg.authorization_server.endswith("/contoso-tenant-id/v2.0")

    def test_a_tenantless_config_falls_back_to_common(self):
        cfg = _config(tenant_id="")
        assert "/common/" in cfg.authorization_server


class TestTheCarveOutDoesNotLeak:
    """Making one path public must not make anything else public."""

    def test_mcp_still_requires_a_token(self, client):
        response = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert response.status_code == 401

    def test_a_token_still_works(self, client):
        response = client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer s3cret",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert response.status_code == 200

    def test_a_path_merely_containing_well_known_is_not_public(self, client):
        """Guards against the prefix check becoming a substring check.

        ``/mcp/.well-known/x`` is not a discovery endpoint, and a caller must
        not be able to reach the transport by decorating the path.
        """
        response = client.get("/mcp/.well-known/anything")
        assert response.status_code != 200


class TestTheChallenge:
    def test_a_401_points_at_the_metadata_document(self, client):
        response = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert response.status_code == 401
        challenge = response.headers["www-authenticate"]
        assert challenge.startswith("Bearer ")
        assert f'resource_metadata="{ORIGIN}' in challenge
        assert METADATA_PATH in challenge

    def test_the_challenge_url_is_actually_fetchable(self, client):
        """A pointer to a document that 404s would be worse than no pointer.

        This is the join between the two halves — the header and the route are
        produced by different code paths, and nothing else checks they agree.
        """
        response = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        challenge = response.headers["www-authenticate"]
        url = challenge.split('resource_metadata="', 1)[1].split('"', 1)[0]
        path = url.replace(ORIGIN, "")

        assert client.get(path).status_code == 200

    def test_the_body_still_explains_the_refusal(self, client):
        """Adding a header must not cost the human-readable reason."""
        response = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert "error" in json.loads(response.text)


class TestDiscoveryIsOptional:
    """Behind a proxy the server cannot know its own public URL.

    Publishing a guess would send clients somewhere wrong, so with no
    ``GRAPH_MCP_RESOURCE_URL`` the feature is simply off — and the transport
    must behave exactly as it did before any of this existed.
    """

    @pytest.fixture
    def bare_client(self):
        # No resource_url, so localhost is the only accepted host — which is
        # exactly the posture a developer run has.
        with TestClient(build_app(_config(resource_url="")), base_url="http://localhost:8094") as c:
            yield c

    def test_no_metadata_route_is_mounted(self, bare_client):
        assert bare_client.get(METADATA_PATH).status_code == 404

    def test_no_challenge_header_is_emitted(self, bare_client):
        """Better to say nothing than to point at a document that is not there."""
        response = bare_client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert response.status_code == 401
        assert "www-authenticate" not in response.headers

    def test_authentication_is_unaffected(self, bare_client):
        response = bare_client.post(
            "/mcp",
            headers={
                "Authorization": "Bearer s3cret",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert response.status_code == 200

    def test_health_stays_public_either_way(self, bare_client, client):
        assert bare_client.get("/health").status_code == 200
        assert client.get("/health").status_code == 200


class TestTheTransportAcceptsItsOwnPublicHost:
    """The bug this nearly shipped with.

    The SDK keys DNS-rebinding protection off the ``host`` argument to
    ``streamable_http_app()``; left unset it defaults to ``127.0.0.1`` and
    trusts localhost alone. This process binds ``0.0.0.0``, so behind an
    ingress every request arrives with a real hostname and is refused with
    **421 before reaching any handler** — while the metadata document happily
    advertises that same hostname as the resource identifier.

    Advertising a resource the transport will not serve is incoherent, so the
    two are asserted together.
    """

    def test_a_request_with_the_public_host_is_served(self):
        with TestClient(build_app(_config()), base_url=ORIGIN) as c:
            response = c.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer s3cret",
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
        assert response.status_code != 421, (
            "the transport rejected the very host its metadata advertises"
        )
        assert response.status_code == 200

    def test_localhost_still_works_for_local_development(self):
        with TestClient(build_app(_config()), base_url="http://localhost:8094") as c:
            response = c.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer s3cret",
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
        assert response.status_code == 200

    def test_an_unrelated_host_is_still_refused(self):
        """The protection has to still protect — this is not a blanket disable."""
        with TestClient(build_app(_config()), base_url="http://evil.example.net") as c:
            response = c.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer s3cret",
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
        assert response.status_code == 421

    def test_extra_hosts_can_be_declared(self):
        """For names the resource URL does not cover — split-horizon DNS, mesh."""
        cfg = _config(allowed_hosts="graph-mcp.internal")
        with TestClient(build_app(cfg), base_url="http://graph-mcp.internal") as c:
            response = c.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer s3cret",
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            )
        assert response.status_code == 200
