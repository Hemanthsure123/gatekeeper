"""GitHub client and auth against mocked HTTP (respx). Synthetic data only."""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import respx
import structlog
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from gatekeeper.config import Settings
from gatekeeper.errors import ConfigError, ExternalServiceError, NotFoundError
from gatekeeper.github.auth import API_VERSION, AppAuth, TokenAuth, auth_from_settings
from gatekeeper.github.client import GitHubClient, next_link
from gatekeeper.logging import configure_logging

API = "https://api.github.test"
TOKEN = "synthetic-token-value-123"


def settings(**values: Any) -> Settings:
    base: dict[str, Any] = {"github_api_base_url": API, "github_token_dev": TOKEN}
    base.update(values)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def pull_json(number: int = 7) -> dict[str, Any]:
    return {
        "number": number,
        "state": "open",
        "title": "Add parser",
        "body": "Fixes #3",
        "draft": False,
        "user": {"login": "synthetic-user", "id": 1001},
        "author_association": "FIRST_TIME_CONTRIBUTOR",
        "labels": [{"name": "enhancement"}],
        "base": {"sha": "a" * 40, "repo": {"id": 55, "full_name": "octo/play"}},
        "head": {"sha": "b" * 40, "label": "synthetic-user:feature"},
        "additions": 3,
        "deletions": 1,
        "changed_files": 2,
    }


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(sleeps: list[float]) -> GitHubClient:
    return GitHubClient(settings(), sleep=sleeps.append, now=lambda: 1_000.0, rand=lambda: 0.0)


def test_get_pull_sends_version_and_auth_headers(
    mock: respx.MockRouter, client: GitHubClient
) -> None:
    route = mock.get(f"{API}/repos/octo/play/pulls/7").respond(json=pull_json())
    pr = client.get_pull("octo", "play", 7)
    assert (pr.number, pr.user.login, pr.head.sha) == (7, "synthetic-user", "b" * 40)
    headers = route.calls.last.request.headers
    assert headers["X-GitHub-Api-Version"] == API_VERSION == "2026-03-10"
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Accept"] == "application/vnd.github+json"
    assert route.calls.last.request.method == "GET"


def test_pagination_follows_link_header_and_flags_missing_patches(
    mock: respx.MockRouter, client: GitHubClient
) -> None:
    page2 = f"{API}/repositories/55/pulls/7/files?per_page=100&page=2"
    mock.get(f"{API}/repos/octo/play/pulls/7/files").respond(
        json=[{"filename": "a.py", "status": "modified", "additions": 1, "patch": "@@ -1 +1 @@"}],
        headers={"link": f'<{page2}>; rel="next", <{page2}>; rel="last"'},
    )
    mock.get(page2).respond(json=[{"filename": "logo.png", "status": "added", "changes": 0}])
    result = client.list_pull_files("octo", "play", 7, max_files=10)
    assert [f.filename for f in result.files] == ["a.py", "logo.png"]
    assert result.files[1].patch is None
    assert result.truncated is False


def test_pull_files_respect_max_files(mock: respx.MockRouter, client: GitHubClient) -> None:
    files = [{"filename": f"f{i}.py", "status": "added"} for i in range(5)]
    mock.get(f"{API}/repos/octo/play/pulls/7/files").respond(json=files)
    result = client.list_pull_files("octo", "play", 7, max_files=3)
    assert (len(result.files), result.truncated) == (3, True)


def test_pagination_refuses_foreign_hosts(mock: respx.MockRouter, client: GitHubClient) -> None:
    mock.get(f"{API}/repos/octo/play/pulls/7/files").respond(
        json=[], headers={"link": '<https://evil.test/steal>; rel="next"'}
    )
    with pytest.raises(ExternalServiceError, match="another host"):
        client.list_pull_files("octo", "play", 7, max_files=10)


def test_check_runs(mock: respx.MockRouter, client: GitHubClient) -> None:
    mock.get(f"{API}/repos/octo/play/commits/{'b' * 40}/check-runs").respond(
        json={
            "total_count": 1,
            "check_runs": [
                {"name": "ci", "status": "completed", "conclusion": "success", "app": {"owner": {}}}
            ],
        }
    )
    (run,) = client.list_check_runs("octo", "play", "b" * 40)
    assert (run.name, run.status, run.conclusion) == ("ci", "completed", "success")


def test_user_created_at_by_id(mock: respx.MockRouter, client: GitHubClient) -> None:
    mock.get(f"{API}/user/1001").respond(
        json={"login": "synthetic-user", "id": 1001, "created_at": "2020-01-02T03:04:05Z"}
    )
    assert client.get_user_created_at("octo", "play", 1001) == datetime(
        2020, 1, 2, 3, 4, 5, tzinfo=UTC
    )


def test_retries_5xx_and_transport_errors_with_backoff(
    mock: respx.MockRouter, client: GitHubClient, sleeps: list[float]
) -> None:
    mock.get(f"{API}/repos/octo/play/pulls/7").mock(
        side_effect=[
            httpx.ConnectError("x"),
            httpx.Response(502),
            httpx.Response(200, json=pull_json()),
        ]
    )
    assert client.get_pull("octo", "play", 7).number == 7
    assert sleeps == [0.5, 1.0]


def test_primary_rate_limit_waits_until_reset(
    mock: respx.MockRouter, client: GitHubClient, sleeps: list[float]
) -> None:
    limited = httpx.Response(
        403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1030"}, json={}
    )
    mock.get(f"{API}/repos/octo/play/pulls/7").mock(
        side_effect=[limited, httpx.Response(200, json=pull_json())]
    )
    client.get_pull("octo", "play", 7)
    assert sleeps == [30.0]


@pytest.mark.parametrize(
    ("response", "expected_wait"),
    [
        (httpx.Response(429, headers={"retry-after": "5"}, json={}), 5.0),
        (httpx.Response(403, json={"message": "You have exceeded a secondary rate limit"}), 60.0),
    ],
)
def test_secondary_rate_limits(
    mock: respx.MockRouter,
    client: GitHubClient,
    sleeps: list[float],
    response: httpx.Response,
    expected_wait: float,
) -> None:
    mock.get(f"{API}/repos/octo/play/pulls/7").mock(
        side_effect=[response, httpx.Response(200, json=pull_json())]
    )
    client.get_pull("octo", "play", 7)
    assert sleeps == [expected_wait]


def test_long_rate_limit_waits_are_deferred_to_the_queue(
    mock: respx.MockRouter, client: GitHubClient, sleeps: list[float]
) -> None:
    mock.get(f"{API}/repos/octo/play/pulls/7").respond(
        403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "4600"}, json={}
    )
    with pytest.raises(ExternalServiceError) as caught:
        client.get_pull("octo", "play", 7)
    assert caught.value.retryable
    assert caught.value.details["retry_after"] == 3600.0
    assert sleeps == []


@pytest.mark.parametrize(
    ("status", "error", "retryable"),
    [
        (404, NotFoundError, False),
        (401, ExternalServiceError, False),
        (403, ExternalServiceError, False),
    ],
)
def test_client_errors_are_not_retried(
    mock: respx.MockRouter,
    client: GitHubClient,
    sleeps: list[float],
    status: int,
    error: type[Exception],
    retryable: bool,
) -> None:
    mock.get(f"{API}/repos/octo/play/pulls/7").respond(status, json={"message": "nope"})
    with pytest.raises(error) as caught:
        client.get_pull("octo", "play", 7)
    assert getattr(caught.value, "retryable", None) is retryable
    assert sleeps == []


def test_gives_up_after_max_retries(mock: respx.MockRouter, client: GitHubClient) -> None:
    mock.get(f"{API}/repos/octo/play/pulls/7").respond(503)
    with pytest.raises(ExternalServiceError) as caught:
        client.get_pull("octo", "play", 7)
    assert caught.value.retryable


def test_authorization_never_appears_in_logs(mock: respx.MockRouter, client: GitHubClient) -> None:
    stream = io.StringIO()
    configure_logging(settings(app_env="test", log_level="DEBUG"), stream=stream)
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    try:
        mock.get(f"{API}/repos/octo/play/pulls/7").mock(
            side_effect=[httpx.Response(502), httpx.Response(200, json=pull_json())]
        )
        client.get_pull("octo", "play", 7)
    finally:
        logging.getLogger().handlers[:] = [
            h for h in logging.getLogger().handlers if h.get_name() != "gatekeeper"
        ]
        structlog.reset_defaults()
    output = stream.getvalue()
    assert "github request will be retried" in output
    assert TOKEN not in output


def test_next_link_parsing() -> None:
    assert next_link('<https://x/a?page=2>; rel="next", <https://x/a?page=9>; rel="last"') == (
        "https://x/a?page=2"
    )
    assert next_link('<https://x/a?page=9>; rel="last"') is None
    assert next_link(None) is None


# -- auth ----------------------------------------------------------------------------------


@pytest.fixture
def private_key_file(tmp_path: Path) -> tuple[Path, Any]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = tmp_path / "app.pem"
    path.write_bytes(pem)
    return path, key.public_key()


def test_app_jwt_claims(private_key_file: tuple[Path, Any]) -> None:
    path, public_key = private_key_file
    auth = AppAuth(app_id=42, private_key_path=path, api_base_url=API, now=lambda: 10_000.0)
    claims = jwt.decode(
        auth.app_jwt(),
        public_key,
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims["iss"] == "42"
    assert claims["iat"] == 10_000 - 60
    assert claims["exp"] - 10_000 <= 600


def test_app_installation_token_is_cached_until_near_expiry(
    mock: respx.MockRouter, private_key_file: tuple[Path, Any]
) -> None:
    path, _ = private_key_file
    clock = [datetime(2026, 10, 25, 12, 0, tzinfo=UTC).timestamp()]
    installation = mock.get(f"{API}/repos/octo/play/installation").respond(json={"id": 99})
    tokens = mock.post(f"{API}/app/installations/99/access_tokens")
    tokens.side_effect = [
        httpx.Response(201, json={"token": "inst-1", "expires_at": "2026-10-25T13:00:00Z"}),
        httpx.Response(201, json={"token": "inst-2", "expires_at": "2026-10-25T14:00:00Z"}),
    ]
    auth = AppAuth(app_id=42, private_key_path=path, api_base_url=API, now=lambda: clock[0])
    assert auth.authorization("octo", "play") == "Bearer inst-1"
    clock[0] += 50 * 60  # 10 minutes before expiry: still cached
    assert auth.authorization("Octo", "Play") == "Bearer inst-1"
    clock[0] += 6 * 60  # within the 5-minute margin: refresh
    assert auth.authorization("octo", "play") == "Bearer inst-2"
    assert installation.call_count == 1
    assert tokens.call_count == 2
    assert tokens.calls.last.request.headers["Authorization"].startswith("Bearer ey")


def test_app_not_installed_is_not_found(
    mock: respx.MockRouter, private_key_file: tuple[Path, Any]
) -> None:
    path, _ = private_key_file
    mock.get(f"{API}/repos/octo/play/installation").respond(404, json={})
    with pytest.raises(NotFoundError):
        AppAuth(app_id=1, private_key_path=path, api_base_url=API).authorization("octo", "play")


def test_missing_private_key_file(tmp_path: Path) -> None:
    auth = AppAuth(app_id=1, private_key_path=tmp_path / "missing.pem", api_base_url=API)
    with pytest.raises(ConfigError, match="GITHUB_APP_PRIVATE_KEY_PATH") as caught:
        auth.app_jwt()
    assert str(tmp_path) not in str(caught.value)


def test_auth_mode_selection(tmp_path: Path) -> None:
    assert isinstance(auth_from_settings(settings()), TokenAuth)
    app = auth_from_settings(
        settings(github_app_id=5, github_app_private_key_path=str(tmp_path / "k.pem"))
    )
    assert app.mode == "app"
    with pytest.raises(ConfigError, match="GITHUB_TOKEN_DEV"):
        auth_from_settings(settings(github_token_dev=None))


def test_client_without_credentials_fails_on_first_use(mock: respx.MockRouter) -> None:
    client = GitHubClient(settings(github_token_dev=None))
    with pytest.raises(ConfigError):
        client.get_pull("octo", "play", 1)
