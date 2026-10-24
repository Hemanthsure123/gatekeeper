"""Canonical JSON and the per-run hash chain.

- Canonical JSON: keys sorted, no insignificant whitespace, UTF-8, non-ASCII kept as is,
  NaN and Infinity rejected.
- ``payload_hash`` = SHA-256 hex of the canonical JSON of the *redacted* payload.
- ``chain_hash`` = SHA-256 hex of the canonical JSON *object*::

      {"actor", "created_at", "item_id", "parent_seq", "payload_hash", "previous",
       "run_id", "schema_version", "seq", "type"}

  where ``previous`` is the previous event's chain hash (``""`` for seq 1),
  ``created_at`` uses :func:`format_timestamp` and absent values are JSON ``null``.
  Named keys make the preimage unambiguous and self-describing.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID


def canonical_json(value: Any) -> bytes:
    """Serialize ``value`` canonically. Raises TypeError/ValueError for non-JSON values."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def payload_hash(payload: Any) -> str:
    return sha256_hex(canonical_json(payload))


def format_timestamp(value: datetime) -> str:
    """Fixed ISO-8601 UTC form with microseconds, e.g. ``2026-10-25T09:30:00.000123Z``."""
    if value.tzinfo is None:
        raise ValueError("audit timestamps must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def chain_hash(
    previous: str | None,
    *,
    run_id: UUID,
    seq: int,
    event_type: str,
    payload_digest: str,
    actor: str,
    parent_seq: int | None,
    item_id: UUID | None,
    schema_version: str,
    created_at: datetime,
) -> str:
    return sha256_hex(
        canonical_json(
            {
                "actor": actor,
                "created_at": format_timestamp(created_at),
                "item_id": None if item_id is None else str(item_id),
                "parent_seq": parent_seq,
                "payload_hash": payload_digest,
                "previous": previous or "",
                "run_id": str(run_id),
                "schema_version": schema_version,
                "seq": seq,
                "type": event_type,
            }
        )
    )
