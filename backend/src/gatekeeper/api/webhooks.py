"""GitHub webhook handling, independent of the web framework.

Order of checks (the route enforces 1 and 2 before any parsing):
1. body size limit (413);
2. ``X-Hub-Signature-256`` HMAC over the raw body, constant-time compare (401);
3. JSON parsing and the required ``X-GitHub-Event`` / ``X-GitHub-Delivery`` headers;
4. ``ping`` -> 200; only selected ``pull_request`` actions on allowed repos are queued,
   everything else is recorded as ignored;
5. idempotency by delivery id; 6. enqueue, 202 (or 503 if enqueueing fails).

Payload bodies are never logged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Protocol

from gatekeeper.config import Settings
from gatekeeper.errors import ValidationFailed
from gatekeeper.logging import get_logger

PROCESSED_EVENT: Final = "pull_request"
PROCESSED_ACTIONS: Final = frozenset(
    {"opened", "reopened", "synchronize", "ready_for_review", "edited"}
)
SIGNATURE_PREFIX: Final = "sha256="

log = get_logger("gatekeeper.api.webhooks")


class DeliveryStatus(StrEnum):
    RECEIVED = "received"
    QUEUED = "queued"
    IGNORED = "ignored"
    ENQUEUE_FAILED = "enqueue_failed"


#: Deliveries in these states are finished; a redelivery is answered "duplicate".
FINAL_STATUSES: Final = frozenset({DeliveryStatus.QUEUED, DeliveryStatus.IGNORED})


@dataclass(frozen=True)
class DeliveryRecord:
    delivery_id: str
    event: str
    action: str | None
    repo_full_name: str | None


@dataclass(frozen=True)
class PrJob:
    owner: str
    name: str
    number: int
    head_sha: str | None
    delivery_id: str | None

    def payload(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "name": self.name,
            "number": self.number,
            "head_sha": self.head_sha,
            "delivery_id": self.delivery_id,
        }

    def dedupe_key(self) -> str:
        """Same PR at the same head commit is never queued twice."""
        head = self.head_sha or "latest"
        return f"pr:{self.owner}/{self.name}#{self.number}@{head}".lower()


class WebhookStore(Protocol):
    def register(self, record: DeliveryRecord) -> DeliveryStatus | None:
        """Insert the delivery as ``received``. Return its existing status if it was
        already known, else None."""
        ...

    def mark(self, delivery_id: str, status: DeliveryStatus) -> None: ...

    def enqueue_pr(self, job: PrJob) -> None:
        """Atomically enqueue the job and mark the delivery ``queued``."""
        ...


@dataclass(frozen=True)
class Outcome:
    status_code: int
    body: dict[str, Any]


def signature_valid(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith(SIGNATURE_PREFIX):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(SIGNATURE_PREFIX + expected, header.strip())


def ignore_reason(
    settings: Settings, event: str, action: str | None, repo: str | None
) -> str | None:
    if event != PROCESSED_EVENT:
        return f"event {event!r} is not processed"
    if action not in PROCESSED_ACTIONS:
        return f"action {action!r} is not processed"
    if repo is None or "/" not in repo:
        return "payload has no repository"
    owner, name = repo.split("/", 1)
    if not settings.repo_allowed(owner, name):
        return "repository is not in GATEKEEPER_ALLOWED_REPOS"
    return None


def _pr_job(payload: Mapping[str, Any], repo: str, delivery_id: str) -> PrJob:
    pull = payload.get("pull_request")
    if not isinstance(pull, Mapping) or not isinstance(pull.get("number"), int):
        raise ValidationFailed("pull_request payload has no number")
    head = pull.get("head")
    head_sha = head.get("sha") if isinstance(head, Mapping) else None
    owner, name = repo.split("/", 1)
    return PrJob(
        owner=owner,
        name=name,
        number=pull["number"],
        head_sha=head_sha if isinstance(head_sha, str) else None,
        delivery_id=delivery_id,
    )


def process_delivery(
    settings: Settings, store: WebhookStore, event: str, delivery_id: str, body: bytes
) -> Outcome:
    """Handle an already size-checked and signature-verified delivery."""
    try:
        payload = json.loads(body)
    except ValueError:
        return Outcome(400, {"error": ValidationFailed("body is not valid JSON").to_public_dict()})
    if not isinstance(payload, dict):
        return Outcome(
            400, {"error": ValidationFailed("body is not a JSON object").to_public_dict()}
        )
    if event == "ping":
        return Outcome(200, {"status": "pong"})

    action = payload.get("action") if isinstance(payload.get("action"), str) else None
    repository = payload.get("repository")
    repo = repository.get("full_name") if isinstance(repository, Mapping) else None
    repo = repo if isinstance(repo, str) else None
    record = DeliveryRecord(delivery_id, event, action, repo)

    existing = store.register(record)
    if existing in FINAL_STATUSES:
        log.info("duplicate webhook delivery", delivery_id=delivery_id, status=str(existing))
        return Outcome(200, {"status": "duplicate"})

    reason = ignore_reason(settings, event, action, repo)
    if reason is None and repo is not None:
        try:
            job = _pr_job(payload, repo, delivery_id)
        except ValidationFailed as exc:
            reason = exc.message
    if reason is not None:
        store.mark(delivery_id, DeliveryStatus.IGNORED)
        log.info("webhook delivery ignored", delivery_id=delivery_id, event=event, reason=reason)
        return Outcome(200, {"status": "ignored", "reason": reason})

    try:
        store.enqueue_pr(job)
    except Exception as exc:
        log.error("webhook enqueue failed", delivery_id=delivery_id, error=type(exc).__name__)
        try:
            store.mark(delivery_id, DeliveryStatus.ENQUEUE_FAILED)
        except Exception as mark_exc:
            log.error(
                "could not mark delivery", delivery_id=delivery_id, error=type(mark_exc).__name__
            )
        return Outcome(503, {"status": "enqueue_failed"})
    log.info("webhook delivery queued", delivery_id=delivery_id, number=job.number)
    return Outcome(202, {"status": "queued"})
