"""Canonical JSON and the per-run hash chain.

- Canonical JSON: keys sorted, no insignificant whitespace, UTF-8, non-ASCII kept as is,
  NaN and Infinity rejected.
- ``payload_hash`` = SHA-256 hex of the canonical JSON of the *redacted* payload.
- ``chain_hash`` = SHA-256 hex of the canonical JSON array
  ``[previous_chain_hash or "", run_id, seq, type, payload_hash]``. Encoding the
  fields as a JSON array (rather than concatenating them) makes the preimage
  unambiguous.
"""

from __future__ import annotations

import hashlib
import json
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


def chain_hash(
    previous: str | None, run_id: UUID, seq: int, event_type: str, payload_digest: str
) -> str:
    return sha256_hex(
        canonical_json([previous or "", str(run_id), seq, event_type, payload_digest])
    )
