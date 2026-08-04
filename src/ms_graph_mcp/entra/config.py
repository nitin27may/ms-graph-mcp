"""Configuration for token validation + App-Role authorization.

Fields read ``WG_AUTH_*`` env names (retained so an existing deployment's
environment keeps working) or the conventional ``AZURE_AD_*`` names via aliases.
Most callers never touch these: ``ms_graph_mcp.config.GraphMcpConfig`` builds an
``AuthConfig`` explicitly via ``to_auth_config()``.

Access the active config through :func:`get_config` (cached singleton); tests
override fields on the returned instance or call :func:`set_config` /
:func:`reset_config`. ``ServiceAuthMiddleware`` also accepts an explicit
``config=`` so a service can construct its own posture (agent edge vs MCP
downstream) without env coupling.
"""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class AuthConfig(BaseSettings):
    """Runtime settings for the auth middleware."""

    model_config = SettingsConfigDict(
        env_file=("../.env.local", ".env"),
        case_sensitive=True,
        extra="ignore",
        populate_by_name=True,
    )

    # Verify the RS256 signature against Entra's JWKS. Defaults ON (secure).
    # Forced ON regardless whenever a role gate is configured — see
    # ``verify_signature``. Set False only for local dev on a trusted host.
    jwt_verify: bool = Field(
        default=True,
        validation_alias=AliasChoices("WG_AUTH_JWT_VERIFY", "JWT_VERIFY_SIGNATURE"),
    )
    tenant_id: str = Field(
        default="",
        validation_alias=AliasChoices("WG_AUTH_TENANT_ID", "AZURE_AD_TENANT_ID"),
    )
    # Fleet machine-identity secret (the current stopgap until Managed Identity —
    # see the service-to-service-identity ADR). When set, a request whose bearer
    # equals it is treated as a trusted fleet machine call: token verification
    # and the role gate are bypassed. Empty = no machine bypass (user JWT only).
    # This is the ONLY place the secret is checked across the fleet.
    shared_secret: str = Field(
        default="",
        validation_alias=AliasChoices("WG_AUTH_SHARED_SECRET", "AGENT_SHARED_SECRET"),
    )
    # App registration client id — used to derive the default audience
    # (``api://<client_id>`` + bare ``<client_id>``) when ``audience`` is unset.
    client_id: str = Field(
        default="",
        validation_alias=AliasChoices("WG_AUTH_CLIENT_ID", "AZURE_AD_CLIENT_ID"),
    )
    # Comma-separated accepted audiences. Empty → derived from client_id
    # (agents); set explicitly for downstream services (e.g. Graph / ADO).
    audience: str = Field(
        default="",
        validation_alias=AliasChoices("WG_AUTH_AUDIENCE"),
    )
    # Optional explicit issuer override; empty → derived from tenant_id (v1 + v2).
    issuer: str = Field(default="", validation_alias=AliasChoices("WG_AUTH_ISSUER"))
    # Optional explicit JWKS URL override; empty → derived from tenant_id.
    jwks_url: str = Field(default="", validation_alias=AliasChoices("WG_AUTH_JWKS_URL"))
    jwks_cache_ttl_seconds: int = Field(
        default=3600, validation_alias=AliasChoices("WG_AUTH_JWKS_CACHE_TTL")
    )
    # Comma-separated allowlist of authorized parties (azp/appid). Empty = no
    # azp enforcement; downstream MCP services set this to the app client_id so
    # only our fleet's OBO tokens (generic Graph/ADO audience) are accepted.
    allowed_azp: str = Field(default="", validation_alias=AliasChoices("WG_AUTH_ALLOWED_AZP"))
    # Comma-separated required App Roles. Empty = authenticate-only (no authz
    # gate). When set, the caller's `roles` claim must contain at least one.
    required_roles: str = Field(default="", validation_alias=AliasChoices("WG_AUTH_REQUIRED_ROLES"))
    # Permit app-only (client-credentials) tokens — for future automation agents.
    allow_app_only: bool = Field(
        default=False, validation_alias=AliasChoices("WG_AUTH_ALLOW_APP_ONLY")
    )
    clock_skew_seconds: int = Field(default=60, validation_alias=AliasChoices("WG_AUTH_CLOCK_SKEW"))
    public_paths: str = Field(
        default="/health,/.well-known/agent-card.json",
        validation_alias=AliasChoices("WG_AUTH_PUBLIC_PATHS"),
    )

    # ── Derived views ────────────────────────────────────────────────────────

    @property
    def verify_signature(self) -> bool:
        """Whether to RS256-verify. Forced ON when a role gate is configured —
        a role check over an unverified token is forgeable."""
        if self.required_roles_set:
            return True
        return self.jwt_verify

    @property
    def required_roles_set(self) -> set[str]:
        return set(_csv(self.required_roles))

    @property
    def allowed_azp_set(self) -> set[str]:
        return set(_csv(self.allowed_azp))

    @property
    def public_paths_set(self) -> frozenset[str]:
        return frozenset(_csv(self.public_paths))

    @property
    def audience_list(self) -> list[str]:
        if self.audience:
            return _csv(self.audience)
        if self.client_id:
            return [f"api://{self.client_id}", self.client_id]
        return []

    @property
    def effective_issuers(self) -> set[str]:
        if self.issuer:
            return {self.issuer}
        t = self.tenant_id
        if not t:
            return set()
        return {
            f"https://login.microsoftonline.com/{t}/v2.0",  # v2 access tokens
            f"https://sts.windows.net/{t}/",  # v1 access tokens
            f"https://login.microsoftonline.com/{t}/",  # tolerant v1/v2 base
        }

    @property
    def effective_jwks_url(self) -> str:
        if self.jwks_url:
            return self.jwks_url
        return f"https://login.microsoftonline.com/{self.tenant_id}/discovery/v2.0/keys"


_config: AuthConfig | None = None


def get_config() -> AuthConfig:
    """Return the cached package config, building it from the environment once."""
    global _config
    if _config is None:
        _config = AuthConfig()
    return _config


def set_config(cfg: AuthConfig) -> None:
    """Replace the active config (standalone callers / tests)."""
    global _config
    _config = cfg


def reset_config() -> None:
    """Drop the cached config so the next get_config() rebuilds from env (tests)."""
    global _config
    _config = None
