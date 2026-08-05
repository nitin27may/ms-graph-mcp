"""Configuration for the Microsoft Graph MCP server.

Every setting is read from the environment via pydantic-settings. The canonical
names are all ``GRAPH_MCP_*``; the three Entra app-registration fields also
accept the conventional ``AZURE_AD_*`` names, so an existing Azure environment
drives the server without renaming anything:

    GRAPH_MCP_TENANT_ID      or  AZURE_AD_TENANT_ID
    GRAPH_MCP_CLIENT_ID      or  AZURE_AD_CLIENT_ID
    GRAPH_MCP_CLIENT_SECRET  or  AZURE_AD_CLIENT_SECRET

A ``.env`` file in the working directory is loaded if present.

Access the active config through :func:`get_config` (cached singleton). Tests
override fields on the returned instance, or call :func:`set_config` /
:func:`reset_config`.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from ms_graph_mcp.entra import AuthConfig

# Microsoft Graph resource — the audience an OBO Graph token carries.
GRAPH_AUDIENCE = "https://graph.microsoft.com"


class GraphMcpConfig(BaseSettings):
    """Runtime settings for the Graph tool surface + MCP server."""

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
        # Allow construction by field name too (not only the env aliases), so an
        # embedding app / tests can pass GraphMcpConfig(shared_secret=...).
        populate_by_name=True,
    )

    # httpx TLS verification toggle (corporate proxy / Zscaler).
    disable_ssl_verify: bool = Field(
        default=False,
        validation_alias=AliasChoices("GRAPH_MCP_DISABLE_SSL_VERIFY"),
    )
    # Remove the write tier entirely at startup: never resolved, never
    # advertised, never dispatchable. Independent of, and stricter than, the
    # per-request write scope — an operator can deploy a provably read-only
    # instance without trusting every caller to omit a header.
    read_only: bool = Field(
        default=False,
        validation_alias=AliasChoices("GRAPH_MCP_READ_ONLY"),
    )
    # Comma-separated allowlist of recipient domains for send_email / propose_email.
    # Empty string = gate disabled (any domain).
    send_email_allowed_domains: str = Field(
        default="",
        validation_alias=AliasChoices("GRAPH_MCP_SEND_EMAIL_ALLOWED_DOMAINS"),
    )
    # Cap on the number of files returned across a browse/selection.
    browse_max_files: int = Field(
        default=500,
        validation_alias=AliasChoices("GRAPH_MCP_BROWSE_MAX_FILES"),
    )
    # Inter-service shared secret for the Streamable-HTTP transport. When set,
    # the HTTP server requires `Authorization: Bearer <secret>`.
    # Empty = no service-auth gate (standalone / stdio): callers still supply the
    # per-request Graph token, but the shared-secret check is skipped.
    shared_secret: str = Field(
        default="",
        validation_alias=AliasChoices("GRAPH_MCP_SHARED_SECRET"),
    )

    # ── Token validation (ms_graph_mcp.entra, DOWNSTREAM mode) ───────────────
    # The OBO Graph token forwarded by an agent is validated as a real Entra JWT:
    # signature (when jwt_verify), audience (Graph), and azp == our app (so only
    # OBO tokens minted by this registration are accepted — Graph tokens are
    # generic across apps). No-user calls (a client's startup tools/list
    # hydration) present the shared secret instead and take the machine bypass.
    tenant_id: str = Field(
        default="",
        validation_alias=AliasChoices("GRAPH_MCP_TENANT_ID", "AZURE_AD_TENANT_ID"),
    )
    client_id: str = Field(
        default="",
        validation_alias=AliasChoices("GRAPH_MCP_CLIENT_ID", "AZURE_AD_CLIENT_ID"),
    )
    # Verify the inbound JWT's signature against the tenant's JWKS.
    #
    # Defaults TRUE. It used to default False so a server without JWKS
    # connectivity still started, but that made the unsafe value the one you got
    # by doing nothing: an HTTP deployment accepted unverified tokens unless
    # somebody had read the docs. Documentation does not prevent that class of
    # mistake. stdio is unaffected — it validates no tokens at all.
    #
    # Turning it off is still possible and is a deliberate, auditable act.
    jwt_verify: bool = Field(
        default=True,
        validation_alias=AliasChoices("GRAPH_MCP_JWT_VERIFY"),
    )

    # ── Resource-server OBO (D4) ─────────────────────────────────────────────
    # When ``mcp_does_obo`` is on, graph-mcp is a real OAuth resource server: it
    # receives a token audienced to ITSELF (single-reg: ``api://<client_id>``;
    # Path B: ``obo_audience``) and exchanges it for a Microsoft Graph token via
    # the OBO flow before calling Graph. This needs a confidential-client secret.
    mcp_does_obo: bool = Field(
        default=False,
        validation_alias=AliasChoices("GRAPH_MCP_DOES_OBO"),
    )
    # Confidential-client secret used for the OBO exchange (only needed when
    # mcp_does_obo). Reuses the app registration's own client secret.
    client_secret: str = Field(
        default="",
        validation_alias=AliasChoices("GRAPH_MCP_CLIENT_SECRET", "AZURE_AD_CLIENT_SECRET"),
    )
    # The audience graph-mcp validates in OBO mode. Empty → derived from
    # client_id (``api://<client_id>`` + bare GUID), which is the user token's
    # audience under the single registration. Set explicitly for Path B
    # (``api://ms-graph-mcp-api``).
    obo_audience: str = Field(
        default="",
        validation_alias=AliasChoices("GRAPH_MCP_AUDIENCE"),
    )
    # Fixed least-privilege Graph scope set requested during the OBO exchange.
    # ``.default`` requests exactly the delegated permissions the MCP's app
    # registration is consented for — the resource owner bounds the surface, no
    # per-agent tailoring. Comma-separated for an explicit override.
    obo_scopes: str = Field(
        default="https://graph.microsoft.com/.default",
        validation_alias=AliasChoices("GRAPH_MCP_OBO_SCOPES"),
    )

    @property
    def obo_scopes_list(self) -> list[str]:
        return [s.strip() for s in self.obo_scopes.split(",") if s.strip()]

    def to_auth_config(self) -> AuthConfig:
        """Build the entra AuthConfig for the Graph downstream surface.

        Two postures:
          - **OBO (mcp_does_obo)** — the inbound token is the *user* token
            audienced to this MCP. Validate the MCP's own audience (RFC 8707);
            drop the azp gate (audience binding is the gate).
          - **Interim (default)** — the inbound token is the agent's OBO'd Graph
            token. Validate the Graph audience + azp == our app.
        """
        if self.mcp_does_obo:
            return AuthConfig(
                jwt_verify=self.jwt_verify,
                tenant_id=self.tenant_id,
                client_id=self.client_id,
                # Empty obo_audience → AuthConfig derives api://<client_id> + GUID.
                audience=self.obo_audience,
                allowed_azp="",
                shared_secret=self.shared_secret,
            )
        return AuthConfig(
            jwt_verify=self.jwt_verify,
            tenant_id=self.tenant_id,
            audience=GRAPH_AUDIENCE,
            # Restrict to OBO tokens minted by our app (empty client_id → no azp
            # gate, e.g. an unconfigured standalone server).
            allowed_azp=self.client_id,
            shared_secret=self.shared_secret,
        )


_config: GraphMcpConfig | None = None


def get_config() -> GraphMcpConfig:
    """Return the cached package config, building it from the environment once."""
    global _config
    if _config is None:
        _config = GraphMcpConfig()
    return _config


def set_config(cfg: GraphMcpConfig) -> None:
    """Replace the active config (standalone callers / tests)."""
    global _config
    _config = cfg


def reset_config() -> None:
    """Drop the cached config so the next get_config() rebuilds from env (tests)."""
    global _config
    _config = None
