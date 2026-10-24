"""Audit trail: append-only, redacted, hash-chained events for every pipeline step."""

from gatekeeper.audit.hashing import canonical_json, chain_hash, payload_hash, sha256_hex
from gatekeeper.audit.reader import AuditReader, ChainVerification, verify_events
from gatekeeper.audit.recorder import AuditRecorder, Run, StoredEvent
from gatekeeper.audit.types import SCHEMA_VERSION, Actor, EventType
from gatekeeper.redaction import Redactor

__all__ = [
    "SCHEMA_VERSION",
    "Actor",
    "AuditReader",
    "AuditRecorder",
    "ChainVerification",
    "EventType",
    "Redactor",
    "Run",
    "StoredEvent",
    "canonical_json",
    "chain_hash",
    "payload_hash",
    "sha256_hex",
    "verify_events",
]
