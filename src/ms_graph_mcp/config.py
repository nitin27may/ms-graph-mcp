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
    # The server's own public URL, e.g. https://graph-mcp.example.com/mcp.
    # Required for OAuth discovery: RFC 9728 protected-resource metadata has to
    # state the canonical resource identifier, and the server cannot infer it
    # from behind a proxy. Empty disables discovery — the transport still
    # authenticates exactly as before.
    resource_url: str = Field(
        default="",
        validation_alias=AliasChoices("GRAPH_MCP_RESOURCE_URL"),
    )

    # Extra Host header values the HTTP transport accepts, comma-separated.
    # The SDK enables DNS-rebinding protection and, absent an explicit host,
    # trusts only localhost — which is wrong the moment the server sits behind
    # an ingress, since every request then arrives with a real hostname and is
    # refused with 421. The public host is usually already known from
    # `resource_url`, so this setting is only needed for additional names
    # (a split-horizon DNS name, an internal service address, a second domain).
    allowed_hosts: str = Field(
        default="",
        validation_alias=AliasChoices("GRAPH_MCP_ALLOWED_HOSTS"),
    )

    # Named toolset profiles this deployment exposes, comma-separated. This is
    # the ceiling: an X-Toolsets header may narrow it, never widen it. Defaults
    # to "core" rather than "all" so a client is not handed 85 tool definitions
    # it will not use. See toolsets.py.
    toolsets: str = Field(
        default="core",
        validation_alias=AliasChoices("GRAPH_MCP_TOOLSETS"),
    )

    # Remove the write tier entirely at startup: never resolved, never
    # advertised, never dispatchable. Independent of, and stricter than, the
    # per-request write scope — an operator can deploy a provably read-only
    # instance without trusting every caller to omit a header.
    read_only: bool = Field(
        default=False,
        validation_alias=AliasChoices("GRAPH_MCP_READ_ONLY"),
    )
    # Delegated scopes requested when the stdio transport signs the user in
    # interactively. Deliberately a read-only default: a user running this
    # locally for the first time should not be consenting to send mail on their
    # behalf. Add write scopes explicitly when write tools are wanted.
    scopes: str = Field(
        default=(
            "User.Read,Mail.Read,Calendars.Read,Files.Read.All,People.Read,"
            "Chat.Read,Tasks.Read,Notes.Read,Contacts.Read"
        ),
        validation_alias=AliasChoices("GRAPH_MCP_SCOPES"),
    )

    # Comma-separated allowlist of recipient domains for mail_send / mail_forward
    # — the two tools where the caller picks the recipients. Replies are not
    # gated: the thread already fixes who they go to.
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

    # ── Resource-server OBO ──────────────────────────────────────────────────
    # When ``mcp_does_obo`` is on, graph-mcp is a real OAuth resource server: it
    # receives a token audienced to ITSELF (single-reg: ``api://<client_id>``;
    # Path B: ``obo_audience``) and exchanges it for a Microsoft Graph token via
    # the OBO flow before calling Graph. This needs a confidential-client
    # credential — see ``client_credential``.
    #
    # Defaults TRUE. The alternative — accepting a token whose audience is Graph
    # and narrowing it with an azp check — is the token-passthrough pattern the
    # MCP authorization spec forbids: a server must validate that a token was
    # issued *for it*, and azp says who minted a token rather than who it is for.
    # Microsoft says the same thing about relaying middle-tier tokens, and names
    # the consequence that bites here: it cannot satisfy Conditional Access
    # claim step-up. Passthrough remains available, deliberately and loudly.
    #
    # **This is an HTTP-transport setting.** The stdio transport never runs the
    # auth middleware that performs the exchange, so this has no effect there —
    # which is deliberate, because a stdio token is already a Graph token and
    # Entra will not redeem a token audienced to another app.
    mcp_does_obo: bool = Field(
        default=True,
        validation_alias=AliasChoices("GRAPH_MCP_DOES_OBO"),
    )
    # Confidential-client secret used for the OBO exchange (only needed when
    # mcp_does_obo). Reuses the app registration's own client secret.
    #
    # Lowest-precedence credential. Microsoft's Agent ID guidance is that client
    # secrets "shouldn't be used as client credentials in production" — prefer a
    # certificate or a federated identity credential below.
    client_secret: str = Field(
        default="",
        validation_alias=AliasChoices("GRAPH_MCP_CLIENT_SECRET", "AZURE_AD_CLIENT_SECRET"),
    )
    # PEM bundle (private key + certificate) for certificate authentication.
    # Highest precedence: a leftover secret in the environment must not silently
    # win over a deliberately configured certificate.
    client_cert_path: str = Field(
        default="",
        validation_alias=AliasChoices("GRAPH_MCP_CLIENT_CERT_PATH"),
    )
    client_cert_password: str = Field(
        default="",
        validation_alias=AliasChoices("GRAPH_MCP_CLIENT_CERT_PASSWORD"),
    )
    # Projected token file for workload-identity federation (FIC). On AKS the
    # workload-identity webhook mounts this; MSAL re-reads it per token request,
    # so rotation is handled without restarting.
    federated_token_file: str = Field(
        default="",
        validation_alias=AliasChoices(
            "GRAPH_MCP_FEDERATED_TOKEN_FILE", "AZURE_FEDERATED_TOKEN_FILE"
        ),
    )
    # Comma-separated allowlist of authorized parties (azp/appid) permitted to
    # call this server. Empty = audience binding alone is the gate, which is the
    # spec's requirement. Set it to pin specific agent identities or client apps
    # as defence in depth — an Entra Agent ID presents the *agent identity's*
    # client id here, not the blueprint's.
    allowed_azp: str = Field(
        default="",
        validation_alias=AliasChoices("GRAPH_MCP_ALLOWED_AZP"),
    )
    # The delegated scope a caller's token must carry (`scp`) to reach any tool,
    # and the further one required for the write tier. Audience binding says the
    # token is for this server; scopes say what its bearer may do.
    #
    # Both default empty (no gate) so this release changes one default rather
    # than two — see deprecations.py, which records that they become
    # `access_as_user` / `access_as_user.write` in 0.5.0.
    required_scope: str = Field(
        default="",
        validation_alias=AliasChoices("GRAPH_MCP_REQUIRED_SCOPE"),
    )
    write_scope_name: str = Field(
        default="",
        validation_alias=AliasChoices("GRAPH_MCP_WRITE_SCOPE_NAME"),
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
    def scopes_list(self) -> list[str]:
        return [s.strip() for s in self.scopes.split(",") if s.strip()]

    @property
    def obo_scopes_list(self) -> list[str]:
        return [s.strip() for s in self.obo_scopes.split(",") if s.strip()]

    @property
    def client_credential(self) -> str | dict | None:
        """The MSAL client credential, or ``None`` when none is configured.

        Precedence is certificate → federated → secret. Raises
        ``obo.CredentialError`` if a configured credential cannot be assembled —
        a missing certificate file is a deployment fault worth failing on, not
        something to silently fall back from.
        """
        from ms_graph_mcp.obo import build_client_credential

        return build_client_credential(
            client_secret=self.client_secret,
            cert_path=self.client_cert_path,
            cert_password=self.client_cert_password,
            federated_token_file=self.federated_token_file,
        )

    @property
    def credential_kind(self) -> str:
        """Which credential is in play, for startup logging. Never its value."""
        if self.client_cert_path:
            return "certificate"
        if self.federated_token_file:
            return "federated identity credential"
        if self.client_secret:
            return "client secret"
        return "none"

    @property
    def authorization_server(self) -> str:
        """The Entra issuer clients should authenticate against."""
        tenant = self.tenant_id or "common"
        return f"https://login.microsoftonline.com/{tenant}/v2.0"

    @property
    def resource_metadata_url(self) -> str:
        """Where the RFC 9728 document lives, or empty when discovery is off."""
        if not self.resource_url:
            return ""
        from mcp.server.auth.routes import build_resource_metadata_url
        from pydantic import AnyHttpUrl

        return str(build_resource_metadata_url(AnyHttpUrl(self.resource_url)))

    @property
    def allowed_hosts_list(self) -> list[str]:
        """Host header values the transport accepts.

        Localhost always stays valid so a developer run and the MCP Inspector
        keep working. The public host from ``resource_url`` is added
        automatically — an operator who has already declared the canonical
        resource identifier should not have to repeat it to avoid a 421.

        Ports are wildcarded (`host:*`) because the port a client connects on
        is a deployment detail; the protection that matters is on the *name*,
        which is what a DNS-rebinding attack has to control.
        """
        hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", "127.0.0.1", "localhost"]

        for extra in filter(None, (h.strip() for h in self.allowed_hosts.split(","))):
            hosts += [extra, f"{extra}:*"]

        if self.resource_url:
            from urllib.parse import urlsplit

            hostname = urlsplit(self.resource_url).hostname
            if hostname:
                hosts += [hostname, f"{hostname}:*"]

        return list(dict.fromkeys(hosts))

    def to_auth_config(self) -> AuthConfig:
        """Build the entra AuthConfig for the Graph downstream surface.

        Two postures:
          - **Resource server (default)** — the inbound token is the *user* token
            audienced to this MCP. Audience binding (RFC 8707) is the gate, so
            the azp check is not needed and is opt-in defence in depth. The
            delegated scope gate applies here.
          - **Passthrough (opt-in, deprecated)** — the inbound token is an
            already-OBO'd Graph token. Validate the Graph audience + azp == our
            app, because a Graph audience alone is generic across every app in
            the tenant.
        """
        if self.mcp_does_obo:
            return AuthConfig(
                jwt_verify=self.jwt_verify,
                tenant_id=self.tenant_id,
                client_id=self.client_id,
                # Empty obo_audience → AuthConfig derives api://<client_id> + GUID.
                audience=self.obo_audience,
                allowed_azp=self.allowed_azp,
                required_scopes=self.required_scope,
                shared_secret=self.shared_secret,
            )
        return AuthConfig(
            jwt_verify=self.jwt_verify,
            tenant_id=self.tenant_id,
            audience=GRAPH_AUDIENCE,
            # Restrict to OBO tokens minted by our app (empty client_id → no azp
            # gate, e.g. an unconfigured standalone server). An explicit
            # allowlist overrides, so the two postures configure the same way.
            allowed_azp=self.allowed_azp or self.client_id,
            required_scopes=self.required_scope,
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
