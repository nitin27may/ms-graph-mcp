"""Interactive sign-in for the stdio transport.

A locally-run MCP server has no way to be handed a token: Graph access tokens
last about an hour, and asking a user to paste a fresh one into their client
config before every session is not a workflow anyone will follow. So when no
token is supplied, the server signs the user in itself.

Two flows, tried in that order:

**Interactive browser** — opens the system browser, the user signs in with SSO
(including MFA and conditional access), and the redirect lands on a loopback
port. Best experience, but needs a desktop session and a redirect URI of
``http://localhost`` registered on the app.

**Device code** — prints a short code and a URL to *stderr*; the user completes
sign-in on any device. Works over SSH and in containers.

Both use MSAL's ``PublicClientApplication``: no client secret is involved, which
is what makes this safe to run on a user's own machine.

**stdout is the MCP protocol channel.** Every prompt here goes to stderr — a
stray print to stdout corrupts the JSON-RPC stream and the client disconnects
with no useful error.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Tokens are cached so a user signs in once, not once per client restart. MSAL
# refreshes silently from here until the refresh token itself expires.
_CACHE_DIR = Path(os.getenv("GRAPH_MCP_CACHE_DIR", Path.home() / ".ms-graph-mcp"))
_CACHE_FILE = _CACHE_DIR / "token_cache.json"


class InteractiveAuthError(RuntimeError):
    """Sign-in could not be completed."""


def _load_cache() -> Any:
    import msal

    cache = msal.SerializableTokenCache()
    if _CACHE_FILE.exists():
        try:
            cache.deserialize(_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:  # pragma: no cover - a corrupt cache should not be fatal
            logger.warning("token cache at %s was unreadable; starting fresh", _CACHE_FILE)
    return cache


def _save_cache(cache: Any) -> None:
    if not cache.has_state_changed:
        return
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(cache.serialize(), encoding="utf-8")
        # The cache holds refresh tokens. Owner-only, always.
        _CACHE_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:  # pragma: no cover - disk problems are not fatal
        logger.warning("could not persist the token cache: %s", exc)


class InteractiveTokenProvider:
    """Acquires and refreshes a delegated Graph token by signing the user in.

    Call :meth:`get_token` before each Graph call. It is cheap: MSAL serves from
    its cache and only reaches the network when the access token has actually
    expired.
    """

    def __init__(self, client_id: str, tenant_id: str, scopes: list[str]) -> None:
        if not client_id:
            raise InteractiveAuthError(
                "GRAPH_MCP_CLIENT_ID is required for interactive sign-in. Register an app in "
                "Microsoft Entra ID, enable it as a public client with the redirect URI "
                "http://localhost, and set the client id."
            )
        self._client_id = client_id
        self._tenant_id = tenant_id or "common"
        # MSAL rejects reserved scopes; it adds openid/profile/offline_access itself.
        self._scopes = [s for s in scopes if s.lower() not in _RESERVED_SCOPES]
        self._app: Any = None
        self._cache: Any = None

    def _application(self) -> Any:
        import msal

        if self._app is None:
            self._cache = _load_cache()
            self._app = msal.PublicClientApplication(
                client_id=self._client_id,
                authority=f"https://login.microsoftonline.com/{self._tenant_id}",
                token_cache=self._cache,
            )
        return self._app

    def _silent(self) -> str | None:
        app = self._application()
        for account in app.get_accounts():
            result = app.acquire_token_silent(self._scopes, account=account)
            if result and "access_token" in result:
                return result["access_token"]
        return None

    def get_token(self) -> str:
        """Return a valid access token, signing in only if the cache cannot serve one."""
        token = self._silent()
        if token:
            return token
        result = self._sign_in()
        _save_cache(self._cache)
        return result

    def _sign_in(self) -> str:
        app = self._application()

        # Interactive first — it is the better experience where a browser exists.
        if _browser_available():
            print(
                "\n[ms-graph-mcp] Opening your browser to sign in to Microsoft 365…",
                file=sys.stderr,
                flush=True,
            )
            try:
                result = app.acquire_token_interactive(self._scopes, prompt="select_account")
                if result and "access_token" in result:
                    print("[ms-graph-mcp] Signed in.\n", file=sys.stderr, flush=True)
                    return result["access_token"]
                logger.warning(
                    "interactive sign-in did not return a token (%s); falling back to device code",
                    (result or {}).get("error", "unknown"),
                )
            except Exception as exc:
                logger.warning("interactive sign-in unavailable (%s); using device code", exc)

        return self._device_code(app)

    def _device_code(self, app: Any) -> str:
        flow = app.initiate_device_flow(scopes=self._scopes)
        if "user_code" not in flow:
            raise InteractiveAuthError(
                "Could not start device sign-in: "
                f"{flow.get('error_description') or flow.get('error') or 'unknown error'}. "
                "Check that the app registration allows public client flows."
            )
        # stderr, never stdout — stdout carries the MCP protocol.
        print(f"\n[ms-graph-mcp] {flow['message']}\n", file=sys.stderr, flush=True)
        result = app.acquire_token_by_device_flow(flow)
        if not result or "access_token" not in result:
            raise InteractiveAuthError(
                "Sign-in failed: "
                f"{(result or {}).get('error_description') or 'no token was returned'}"
            )
        print("[ms-graph-mcp] Signed in.\n", file=sys.stderr, flush=True)
        return result["access_token"]


# MSAL adds these itself and errors if they are passed in explicitly.
_RESERVED_SCOPES = {"openid", "profile", "offline_access"}


def _browser_available() -> bool:
    """Whether an interactive browser flow has any chance of working.

    Over SSH or in a container there is no display to open a browser on, and
    attempting it wastes time before falling back. Honours the conventional
    override so a user can force device code.
    """
    if os.getenv("GRAPH_MCP_FORCE_DEVICE_CODE", "").lower() == "true":
        return False
    if os.getenv("SSH_CONNECTION") or os.getenv("SSH_TTY"):
        return False
    if sys.platform.startswith("linux") and not os.getenv("DISPLAY"):
        return False
    return True
