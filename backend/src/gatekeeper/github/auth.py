"""GitHub authentication: a personal token, or a GitHub App installation token.

App mode (per the GitHub docs "Generating a JWT for a GitHub App" and "Authenticating as
a GitHub App installation"):
- an RS256 JWT with ``iat`` 60 s in the past (clock drift), ``exp`` at most 10 minutes
  ahead and ``iss`` = the App ID, signed with the private key read from
  GITHUB_APP_PRIVATE_KEY_PATH at use time (never cached in logs or memory longer
  than one signing);
- ``GET /repos/{owner}/{repo}/installation`` finds the installation for a repo;
- ``POST /app/installations/{id}/access_tokens`` returns a token valid for one hour,
  cached here until five minutes before it expires.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol

import httpx
import jwt

from gatekeeper.config import Settings
from gatekeeper.errors import ConfigError, ExternalServiceError, NotFoundError

API_VERSION: Final = "2026-03-10"
JWT_BACKDATE_S: Final = 60
JWT_LIFETIME_S: Final = 540  # 9 minutes: safely under GitHub's 10-minute maximum
TOKEN_REFRESH_MARGIN: Final = timedelta(minutes=5)


class GitHubAuth(Protocol):
    """Returns the Authorization header value for requests about one repository."""

    @property
    def mode(self) -> str: ...

    def authorization(self, owner: str, name: str) -> str: ...


class TokenAuth:
    def __init__(self, token: str) -> None:
        self._token = token

    @property
    def mode(self) -> str:
        return "token"

    def authorization(self, owner: str, name: str) -> str:
        return f"Bearer {self._token}"


class AppAuth:
    """Installation tokens for a GitHub App, cached per installation."""

    def __init__(
        self,
        *,
        app_id: int,
        private_key_path: Path,
        api_base_url: str,
        http: httpx.Client | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._app_id = app_id
        self._key_path = private_key_path
        self._base = api_base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        self._now = now
        self._lock = threading.Lock()
        self._installations: dict[str, int] = {}
        self._tokens: dict[int, tuple[str, datetime]] = {}

    @property
    def mode(self) -> str:
        return "app"

    def app_jwt(self) -> str:
        try:
            private_key = self._key_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(
                "cannot read the GitHub App private key at GITHUB_APP_PRIVATE_KEY_PATH"
            ) from exc
        issued = int(self._now()) - JWT_BACKDATE_S
        claims = {
            "iat": issued,
            "exp": issued + JWT_BACKDATE_S + JWT_LIFETIME_S,
            "iss": str(self._app_id),
        }
        return jwt.encode(claims, private_key, algorithm="RS256")

    def authorization(self, owner: str, name: str) -> str:
        installation = self._installation_id(owner, name)
        with self._lock:
            cached = self._tokens.get(installation)
            now = datetime.fromtimestamp(self._now(), UTC)
            if cached and cached[1] - TOKEN_REFRESH_MARGIN > now:
                return f"Bearer {cached[0]}"
        data = self._app_request("POST", f"/app/installations/{installation}/access_tokens")
        token, expires = str(data["token"]), datetime.fromisoformat(str(data["expires_at"]))
        with self._lock:
            self._tokens[installation] = (token, expires)
        return f"Bearer {token}"

    def _installation_id(self, owner: str, name: str) -> int:
        key = f"{owner}/{name}".lower()
        with self._lock:
            if key in self._installations:
                return self._installations[key]
        data = self._app_request("GET", f"/repos/{owner}/{name}/installation")
        installation = int(data["id"])
        with self._lock:
            self._installations[key] = installation
        return installation

    def _app_request(self, method: str, path: str) -> dict[str, Any]:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.app_jwt()}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "gatekeeper",
        }
        try:
            response = self._http.request(method, f"{self._base}{path}", headers=headers)
        except httpx.TransportError as exc:
            raise ExternalServiceError(
                f"GitHub App auth request failed ({type(exc).__name__})"
            ) from exc
        if response.status_code == 404:
            raise NotFoundError("the GitHub App is not installed on this repository")
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"GitHub App authentication failed (HTTP {response.status_code})",
                retryable=response.status_code >= 500,
                details={"status": response.status_code},
            )
        data = response.json()
        if not isinstance(data, dict):
            raise ExternalServiceError("unexpected GitHub App auth response", retryable=False)
        return data


class LazyAuth:
    """Defers choosing and validating credentials until the first request."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._auth: GitHubAuth | None = None

    @property
    def mode(self) -> str:
        return self._resolve().mode

    def authorization(self, owner: str, name: str) -> str:
        return self._resolve().authorization(owner, name)

    def _resolve(self) -> GitHubAuth:
        if self._auth is None:
            self._auth = auth_from_settings(self._settings)
        return self._auth


def auth_from_settings(settings: Settings) -> GitHubAuth:
    """App mode when both App keys are set, else token mode, else ConfigError."""
    if settings.github_app_id is not None and settings.github_app_private_key_path is not None:
        return AppAuth(
            app_id=settings.github_app_id,
            private_key_path=settings.github_app_private_key_path,
            api_base_url=settings.github_api_base_url,
        )
    if settings.github_token_dev is not None:
        return TokenAuth(settings.github_token_dev.get_secret_value())
    raise ConfigError(
        "no GitHub credentials: set GITHUB_TOKEN_DEV, or GITHUB_APP_ID and "
        "GITHUB_APP_PRIVATE_KEY_PATH, in .env"
    )
