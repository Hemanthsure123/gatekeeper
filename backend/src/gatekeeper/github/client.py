"""Read-only GitHub REST client (httpx).

Follows the GitHub REST docs: ``X-GitHub-Api-Version: 2026-03-10``; pagination via
the ``Link`` header (``rel="next"``); rate limits via ``x-ratelimit-remaining`` /
``x-ratelimit-reset`` (primary) and ``retry-after`` or a "secondary rate limit"
message (secondary; otherwise wait at least one minute). Short waits are slept
inline; longer ones surface as a retryable ``ExternalServiceError`` carrying
``retry_after`` so the job queue backs off instead of blocking a worker.

Only GET requests exist here. Authorization values are never logged.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any, Final, Protocol
from urllib.parse import urlsplit

import httpx

from gatekeeper.config import Settings
from gatekeeper.errors import ExternalServiceError, NotFoundError
from gatekeeper.github.auth import API_VERSION, GitHubAuth, LazyAuth
from gatekeeper.github.models import CheckRun, GitHubUser, PullFile, PullFiles, PullRequest
from gatekeeper.logging import get_logger

PER_PAGE: Final = 100
MAX_RETRIES: Final = 3
MAX_INLINE_WAIT_S: Final = 60.0
SECONDARY_DEFAULT_WAIT_S: Final = 60.0
MAX_CHECK_RUNS: Final = 300
_NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="next"')

log = get_logger("gatekeeper.github")


class GitHubReader(Protocol):
    """What the pipeline needs from GitHub. Implemented by GitHubClient and test fakes."""

    def get_pull(self, owner: str, name: str, number: int) -> PullRequest: ...

    def list_pull_files(
        self, owner: str, name: str, number: int, *, max_files: int
    ) -> PullFiles: ...

    def list_check_runs(self, owner: str, name: str, ref: str) -> list[CheckRun]: ...

    def get_user_created_at(self, owner: str, name: str, user_id: int) -> datetime | None: ...


class _RetryLaterError(Exception):
    """Retryable failure; ``wait_s`` None means "use exponential backoff"."""

    def __init__(self, wait_s: float | None, reason: str, status: int | None) -> None:
        super().__init__(reason)
        self.wait_s = wait_s
        self.reason = reason
        self.status = status


def next_link(header: str | None) -> str | None:
    if not header:
        return None
    match = _NEXT_LINK.search(header)
    return match[1] if match else None


class GitHubClient:
    def __init__(
        self,
        settings: Settings,
        auth: GitHubAuth | None = None,
        *,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self._base = settings.github_api_base_url.rstrip("/")
        self._host = urlsplit(self._base).netloc
        self._auth = auth or LazyAuth(settings)
        self._http = http or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        self._sleep = sleep
        self._now = now
        self._rand = rand

    # -- public, read-only --------------------------------------------------------------

    def get_pull(self, owner: str, name: str, number: int) -> PullRequest:
        data = self._get_json(owner, name, f"/repos/{owner}/{name}/pulls/{number}")
        return PullRequest.model_validate(data)

    def list_pull_files(self, owner: str, name: str, number: int, *, max_files: int) -> PullFiles:
        files: list[PullFile] = []
        path = f"/repos/{owner}/{name}/pulls/{number}/files"
        for page in self._pages(owner, name, path):
            for entry in page if isinstance(page, list) else []:
                if len(files) >= max_files:
                    return PullFiles(files=files, truncated=True)
                files.append(PullFile.model_validate(entry))
        return PullFiles(files=files, truncated=False)

    def list_check_runs(self, owner: str, name: str, ref: str) -> list[CheckRun]:
        runs: list[CheckRun] = []
        path = f"/repos/{owner}/{name}/commits/{ref}/check-runs"
        for page in self._pages(owner, name, path):
            entries = page.get("check_runs", []) if isinstance(page, dict) else []
            runs.extend(CheckRun.model_validate(entry) for entry in entries)
            if len(runs) >= MAX_CHECK_RUNS:
                return runs[:MAX_CHECK_RUNS]
        return runs

    def get_user_created_at(self, owner: str, name: str, user_id: int) -> datetime | None:
        """Account creation date via ``GET /user/{account_id}`` (ids are stable, logins
        are not). ``owner``/``name`` choose the credentials in App mode."""
        data = self._get_json(owner, name, f"/user/{user_id}")
        return GitHubUser.model_validate(data).created_at

    # -- plumbing -----------------------------------------------------------------------

    def _pages(self, owner: str, name: str, path: str) -> Iterator[Any]:
        url: str | None = f"{self._base}{path}?per_page={PER_PAGE}"
        while url is not None:
            response = self._request(owner, name, url)
            yield response.json()
            url = next_link(response.headers.get("link"))
            if url is not None and urlsplit(url).netloc != self._host:
                raise ExternalServiceError(
                    "GitHub pagination link points to another host", retryable=False
                )

    def _get_json(self, owner: str, name: str, path: str) -> Any:
        return self._request(owner, name, f"{self._base}{path}").json()

    def _request(self, owner: str, name: str, url: str) -> httpx.Response:
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._attempt(owner, name, url)
            except _RetryLaterError as later:
                wait = later.wait_s if later.wait_s is not None else self._backoff(attempt)
                if attempt > MAX_RETRIES or wait > MAX_INLINE_WAIT_S:
                    raise ExternalServiceError(
                        f"GitHub request failed: {later.reason}",
                        retryable=True,
                        details={"status": later.status, "retry_after": round(wait, 1)},
                    ) from None
                log.warning(
                    "github request will be retried",
                    path=urlsplit(url).path,
                    status=later.status,
                    attempt=attempt,
                    wait_s=round(wait, 1),
                )
                self._sleep(wait)

    def _attempt(self, owner: str, name: str, url: str) -> httpx.Response:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": self._auth.authorization(owner, name),
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "gatekeeper",
        }
        try:
            response = self._http.get(url, headers=headers)
        except httpx.TransportError as exc:
            raise _RetryLaterError(None, type(exc).__name__, None) from exc

        status = response.status_code
        if status < 400:
            return response
        if status in (403, 429):
            wait = self._rate_limit_wait(response)
            if wait is not None:
                raise _RetryLaterError(wait, "rate limited", status)
        if status >= 500:
            raise _RetryLaterError(None, f"HTTP {status}", status)
        if status == 404:
            raise NotFoundError("GitHub resource not found", details={"path": urlsplit(url).path})
        raise ExternalServiceError(
            f"GitHub refused the request (HTTP {status})",
            retryable=False,
            details={"status": status, "path": urlsplit(url).path},
        )

    def _rate_limit_wait(self, response: httpx.Response) -> float | None:
        """Seconds to wait if this 403/429 is a rate limit, else None (a real refusal)."""
        headers = response.headers
        retry_after = headers.get("retry-after")
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                return SECONDARY_DEFAULT_WAIT_S
        if headers.get("x-ratelimit-remaining") == "0":
            try:
                return max(0.0, float(headers.get("x-ratelimit-reset", "0")) - self._now())
            except ValueError:
                return SECONDARY_DEFAULT_WAIT_S
        try:
            message = str(response.json().get("message", ""))
        except (ValueError, AttributeError):
            message = ""
        if "secondary rate limit" in message.lower() or response.status_code == 429:
            return SECONDARY_DEFAULT_WAIT_S
        return None

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with jitter: about 1, 2, 4 ... seconds, capped at 30."""
        ceiling = min(30.0, 2.0 ** (attempt - 1))
        return ceiling / 2 + self._rand() * ceiling / 2
