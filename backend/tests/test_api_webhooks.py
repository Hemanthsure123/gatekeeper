"""Webhook gateway tests with an in-memory delivery store (no database)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from gatekeeper.api.app import create_app
from gatekeeper.api.webhooks import (
    DeliveryRecord,
    DeliveryStatus,
    PrJob,
    signature_valid,
)
from gatekeeper.config import Settings
from gatekeeper.errors import ConfigError

SECRET = "synthetic-webhook-secret"


class AppClient:
    """In-process ASGI client via httpx.ASGITransport (Starlette's AppClient now wants
    the separate httpx2 package, which is not a project dependency)."""

    def __init__(self, app: FastAPI) -> None:
        self.app = app

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def send() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                return await client.request(method, url, **kwargs)

        return asyncio.run(send())

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)


class MemoryStore:
    def __init__(self, *, fail_enqueue: bool = False) -> None:
        self.deliveries: dict[str, DeliveryStatus] = {}
        self.records: dict[str, DeliveryRecord] = {}
        self.jobs: list[PrJob] = []
        self.fail_enqueue = fail_enqueue

    def register(self, record: DeliveryRecord) -> DeliveryStatus | None:
        if record.delivery_id in self.deliveries:
            return self.deliveries[record.delivery_id]
        self.deliveries[record.delivery_id] = DeliveryStatus.RECEIVED
        self.records[record.delivery_id] = record
        return None

    def mark(self, delivery_id: str, status: DeliveryStatus) -> None:
        self.deliveries[delivery_id] = status

    def enqueue_pr(self, job: PrJob) -> None:
        if self.fail_enqueue:
            raise RuntimeError("database down")
        self.jobs.append(job)
        assert job.delivery_id is not None
        self.deliveries[job.delivery_id] = DeliveryStatus.QUEUED


def settings(**values: Any) -> Settings:
    base: dict[str, Any] = {
        "github_webhook_secret": SECRET,
        "gatekeeper_allowed_repos": "octo/play",
        "webhook_max_body_bytes": 2048,
    }
    base.update(values)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


def client_for(store: MemoryStore, **values: Any) -> AppClient:
    app = create_app(settings(**values), store=store, readiness=lambda: {"database": "ok"})
    return AppClient(app)


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def pr_event(action: str = "opened", repo: str = "octo/play", number: int = 5) -> bytes:
    return json.dumps(
        {
            "action": action,
            "repository": {"full_name": repo},
            "pull_request": {"number": number, "head": {"sha": "c" * 40}},
        }
    ).encode()


def post(
    client: AppClient,
    body: bytes,
    *,
    event: str = "pull_request",
    delivery: str = "d-1",
    signature: str | None = None,
) -> Any:
    headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery}
    headers["X-Hub-Signature-256"] = signature if signature is not None else sign(body)
    return client.post("/webhooks/github", content=body, headers=headers)


def test_signature_matches_github_reference_vector() -> None:
    expected = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
    assert signature_valid("It's a Secret to Everybody", b"Hello, World!", expected)
    assert not signature_valid("It's a Secret to Everybody", b"Hello, World?", expected)
    assert not signature_valid("x", b"y", None)
    assert not signature_valid("x", b"y", "sha1=abc")


def test_valid_pull_request_is_queued() -> None:
    store = MemoryStore()
    response = post(client_for(store), pr_event())
    assert response.status_code == 202
    assert response.json() == {"status": "queued"}
    (job,) = store.jobs
    assert (job.owner, job.name, job.number, job.head_sha) == ("octo", "play", 5, "c" * 40)
    assert job.dedupe_key() == f"pr:octo/play#5@{'c' * 40}"
    assert store.deliveries["d-1"] is DeliveryStatus.QUEUED
    assert response.headers["x-request-id"]


@pytest.mark.parametrize("signature", ["", "sha256=" + "0" * 64, sign(b"other body")])
def test_bad_signature_is_rejected_and_nothing_stored(signature: str) -> None:
    store = MemoryStore()
    response = post(client_for(store), pr_event(), signature=signature)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_failed"
    assert store.deliveries == {}
    assert store.jobs == []


def test_oversize_body_is_rejected_before_processing() -> None:
    store = MemoryStore()
    body = b'{"padding": "' + b"x" * 4096 + b'"}'
    response = post(client_for(store), body)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert store.deliveries == {}


def test_missing_headers_and_bad_json() -> None:
    client = client_for(MemoryStore())
    body = pr_event()
    response = client.post(
        "/webhooks/github", content=body, headers={"X-Hub-Signature-256": sign(body)}
    )
    assert response.status_code == 400
    assert post(client, b"not json").status_code == 400


def test_ping() -> None:
    store = MemoryStore()
    response = post(client_for(store), b'{"zen": "x"}', event="ping")
    assert (response.status_code, response.json()) == (200, {"status": "pong"})
    assert store.deliveries == {}


@pytest.mark.parametrize(
    ("event", "body", "reason"),
    [
        ("issues", pr_event(), "event 'issues' is not processed"),
        ("pull_request", pr_event(action="closed"), "action 'closed' is not processed"),
        ("pull_request", pr_event(repo="someone/else"), "GATEKEEPER_ALLOWED_REPOS"),
    ],
)
def test_ignored_events_are_recorded(event: str, body: bytes, reason: str) -> None:
    store = MemoryStore()
    response = post(client_for(store), body, event=event)
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert reason in response.json()["reason"]
    assert store.deliveries["d-1"] is DeliveryStatus.IGNORED
    assert store.jobs == []


def test_allowlist_is_case_insensitive() -> None:
    store = MemoryStore()
    assert post(client_for(store), pr_event(repo="Octo/PLAY")).status_code == 202


def test_production_with_empty_allowlist_ignores_everything() -> None:
    store = MemoryStore()
    client = client_for(store, app_env="production", gatekeeper_allowed_repos=None)
    assert post(client, pr_event()).json()["status"] == "ignored"


def test_duplicate_deliveries_are_not_enqueued_twice() -> None:
    store = MemoryStore()
    client = client_for(store)
    assert post(client, pr_event()).status_code == 202
    again = post(client, pr_event())
    assert (again.status_code, again.json()) == (200, {"status": "duplicate"})
    assert len(store.jobs) == 1


def test_enqueue_failure_returns_503_and_can_be_retried() -> None:
    store = MemoryStore(fail_enqueue=True)
    client = client_for(store)
    response = post(client, pr_event())
    assert response.status_code == 503
    assert store.deliveries["d-1"] is DeliveryStatus.ENQUEUE_FAILED
    store.fail_enqueue = False
    assert post(client, pr_event()).status_code == 202
    assert len(store.jobs) == 1


def test_malformed_pull_request_payload_is_ignored() -> None:
    store = MemoryStore()
    body = json.dumps({"action": "opened", "repository": {"full_name": "octo/play"}}).encode()
    response = post(client_for(store), body)
    assert response.json()["status"] == "ignored"


def test_health_and_readiness() -> None:
    client = AppClient(
        create_app(settings(), store=MemoryStore(), readiness=lambda: {"database": "error"})
    )
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz")
    assert ready.status_code == 503
    assert ready.json()["components"] == {"database": "error"}


def test_request_id_is_echoed_when_sane() -> None:
    client = client_for(MemoryStore())
    assert (
        client.get("/healthz", headers={"X-Request-ID": "abc-123"}).headers["x-request-id"]
        == "abc-123"
    )
    generated = client.get("/healthz", headers={"X-Request-ID": "bad id with spaces"})
    assert generated.headers["x-request-id"] != "bad id with spaces"


def test_refuses_to_start_without_webhook_secret() -> None:
    with pytest.raises(ConfigError, match="GITHUB_WEBHOOK_SECRET"):
        create_app(settings(github_webhook_secret=None), store=MemoryStore(), readiness=lambda: {})


def test_payload_bodies_are_never_logged(capsys: pytest.CaptureFixture[str]) -> None:
    store = MemoryStore()
    marker = "very-private-body-content"
    body = json.dumps(
        {
            "action": "opened",
            "repository": {"full_name": "octo/play"},
            "pull_request": {"number": 1, "head": {"sha": "d" * 40}, "body": marker},
        }
    ).encode()
    post(client_for(store), body)
    captured = capsys.readouterr()
    assert marker not in captured.out + captured.err
