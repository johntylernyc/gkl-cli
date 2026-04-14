"""Yahoo Fantasy Sports OAuth2 authentication.

Implements the Authorization Code flow with out-of-band (OOB) redirect:
1. Opens browser to Yahoo auth page
2. User approves and Yahoo displays an auth code
3. User pastes the code back into the terminal
4. Exchanges the auth code for access + refresh tokens
5. Persists tokens to disk and refreshes automatically

Supports two modes controlled by the GKL_MODE environment variable:
- "local" (default): flat-file credentials, paste-a-code flow
- "web": env-var credentials, server-side redirect flow
"""

from __future__ import annotations

import base64
import json
import os
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from time import time
from urllib.parse import urlencode

import httpx

AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
DEFAULT_REDIRECT_URI = "https://localhost:8080"
DEFAULT_TOKEN_PATH = Path.home() / ".config" / "gkl" / "token.json"
DEFAULT_CREDENTIALS_PATH = Path.home() / ".config" / "gkl" / "credentials.json"


def is_web_mode() -> bool:
    """Check if running in web (server) mode."""
    return os.environ.get("GKL_MODE", "local").lower() == "web"


def get_redirect_uri() -> str:
    """Get the OAuth redirect URI for the current mode."""
    return os.environ.get("GKL_YAHOO_REDIRECT_URI", DEFAULT_REDIRECT_URI)


def load_credentials(
    path: Path = DEFAULT_CREDENTIALS_PATH,
) -> tuple[str, str] | None:
    """Load client_id and client_secret from env vars or a JSON file.

    Returns a (client_id, client_secret) tuple, or None if unavailable.
    """
    # Environment variables take precedence
    env_id = os.environ.get("GKL_YAHOO_CLIENT_ID")
    env_secret = os.environ.get("GKL_YAHOO_CLIENT_SECRET")
    if env_id and env_secret:
        return env_id, env_secret

    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        client_id = data["client_id"]
        client_secret = data["client_secret"]
        if client_id and client_secret:
            return client_id, client_secret
        return None
    except (json.JSONDecodeError, KeyError):
        return None


def save_credentials(
    client_id: str,
    client_secret: str,
    path: Path = DEFAULT_CREDENTIALS_PATH,
) -> None:
    """Persist client_id and client_secret to a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"client_id": client_id, "client_secret": client_secret}, indent=2
        )
    )


@dataclass
class TokenData:
    access_token: str
    refresh_token: str
    expires_at: float
    token_type: str = "bearer"

    @property
    def expired(self) -> bool:
        return time() >= self.expires_at - 60  # 60s buffer

    def auth_header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "token_type": self.token_type,
        }

    @classmethod
    def from_dict(cls, data: dict) -> TokenData:
        return cls(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=data["expires_at"],
            token_type=data.get("token_type", "bearer"),
        )


@dataclass
class YahooAuth:
    client_id: str
    client_secret: str
    token_path: Path = DEFAULT_TOKEN_PATH
    token: TokenData | None = field(default=None, init=False)

    def _basic_auth(self) -> str:
        creds = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        return f"Basic {creds}"

    def load_token(self) -> bool:
        """Load persisted token from env vars or disk.

        Returns True if a valid token was loaded.
        """
        # In web mode, tokens come from environment variables
        env_access = os.environ.get("GKL_YAHOO_ACCESS_TOKEN")
        env_refresh = os.environ.get("GKL_YAHOO_REFRESH_TOKEN")
        env_expires = os.environ.get("GKL_YAHOO_TOKEN_EXPIRES_AT")
        if env_access and env_refresh and env_expires:
            try:
                self.token = TokenData(
                    access_token=env_access,
                    refresh_token=env_refresh,
                    expires_at=float(env_expires),
                )
                return True
            except (ValueError, TypeError):
                pass

        if not self.token_path.exists():
            return False
        try:
            data = json.loads(self.token_path.read_text())
            self.token = TokenData.from_dict(data)
            return True
        except (json.JSONDecodeError, KeyError):
            return False

    def save_token(self) -> None:
        """Persist current token to disk. No-op in web mode."""
        if self.token is None:
            return
        if is_web_mode():
            return  # tokens managed server-side in web mode
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps(self.token.to_dict(), indent=2))

    def get_auth_url(self) -> str:
        """Build the Yahoo OAuth authorization URL."""
        params = {
            "client_id": self.client_id,
            "redirect_uri": get_redirect_uri(),
            "response_type": "code",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def authorize(self) -> TokenData:
        """Run the full OAuth2 authorization code flow with OOB redirect.

        Opens the user's browser, then prompts them to paste the auth code.
        Not used in web mode — the server handles the redirect flow instead.
        """
        if is_web_mode():
            raise RuntimeError(
                "Interactive authorize() not supported in web mode. "
                "Use the server-side OAuth callback instead."
            )

        auth_url = self.get_auth_url()

        print("\n=== Yahoo Fantasy Authorization ===")
        print(f"Opening browser to:\n  {auth_url}\n")
        webbrowser.open(auth_url)
        print("After approving access, Yahoo will display an authorization code.")
        code = input("Paste the code here: ").strip()

        if not code:
            raise RuntimeError("No authorization code provided")

        self.token = self.exchange_code(code)
        self.save_token()
        return self.token

    def exchange_code(self, code: str) -> TokenData:
        """Exchange an authorization code for tokens."""
        resp = httpx.post(
            TOKEN_URL,
            headers={
                "Authorization": self._basic_auth(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": get_redirect_uri(),
            },
        )
        resp.raise_for_status()
        return self._parse_token_response(resp.json())

    def refresh(self) -> TokenData:
        """Refresh the access token using the stored refresh token."""
        if self.token is None:
            raise RuntimeError("No token to refresh — run authorize() first")

        resp = httpx.post(
            TOKEN_URL,
            headers={
                "Authorization": self._basic_auth(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.token.refresh_token,
            },
        )
        resp.raise_for_status()
        self.token = self._parse_token_response(resp.json())
        self.save_token()
        return self.token

    def get_token(self) -> TokenData:
        """Get a valid token — loading, refreshing, or authorizing as needed."""
        if self.token is None:
            self.load_token()

        if self.token is not None and not self.token.expired:
            return self.token

        if self.token is not None:
            try:
                return self.refresh()
            except httpx.HTTPStatusError:
                pass  # refresh token may be revoked, re-authorize

        return self.authorize()

    @staticmethod
    def _parse_token_response(data: dict) -> TokenData:
        return TokenData(
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=time() + data["expires_in"],
            token_type=data.get("token_type", "bearer"),
        )
