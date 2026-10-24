"""Postgres implementation of the webhook delivery store."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from gatekeeper.api.webhooks import DeliveryRecord, DeliveryStatus, PrJob
from gatekeeper.db.models import WebhookDelivery
from gatekeeper.db.session import session_scope
from gatekeeper.queue import PROCESS_PR, enqueue


class PostgresWebhookStore:
    def __init__(self, session_factory: sessionmaker[Session], *, max_attempts: int) -> None:
        self._sessions = session_factory
        self._max_attempts = max_attempts

    def register(self, record: DeliveryRecord) -> DeliveryStatus | None:
        with session_scope(self._sessions) as session:
            inserted = session.execute(
                insert(WebhookDelivery)
                .values(
                    delivery_id=record.delivery_id,
                    event=record.event,
                    action=record.action,
                    repo_full_name=record.repo_full_name,
                    status=DeliveryStatus.RECEIVED.value,
                )
                .on_conflict_do_nothing(index_elements=["delivery_id"])
                .returning(WebhookDelivery.delivery_id)
            ).scalar_one_or_none()
            if inserted is not None:
                return None
            status = session.execute(
                select(WebhookDelivery.status).where(
                    WebhookDelivery.delivery_id == record.delivery_id
                )
            ).scalar_one()
            return DeliveryStatus(status)

    def mark(self, delivery_id: str, status: DeliveryStatus) -> None:
        with session_scope(self._sessions) as session:
            session.execute(
                update(WebhookDelivery)
                .where(WebhookDelivery.delivery_id == delivery_id)
                .values(status=status.value)
            )

    def enqueue_pr(self, job: PrJob) -> None:
        """Job insert and delivery status change commit together, or not at all."""
        with session_scope(self._sessions) as session:
            enqueue(
                session,
                PROCESS_PR,
                job.payload(),
                max_attempts=self._max_attempts,
                dedupe_key=job.dedupe_key(),
            )
            if job.delivery_id is not None:
                session.execute(
                    update(WebhookDelivery)
                    .where(WebhookDelivery.delivery_id == job.delivery_id)
                    .values(status=DeliveryStatus.QUEUED.value)
                )
