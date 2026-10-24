"""Audit event vocabulary. Both enums are additive-only: never rename or remove a value."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

SCHEMA_VERSION: Final = "gatekeeper.event/1"


class EventType(StrEnum):
    RUN_START = "run_start"
    RUN_END = "run_end"
    INTAKE = "intake"
    SIGNAL_RESULT = "signal_result"
    MODEL_REQUEST = "model_request"
    MODEL_RESPONSE = "model_response"
    VERIFIER_RESULT = "verifier_result"
    VERDICT = "verdict"
    DECISION = "decision"
    ACTION = "action"
    ERROR = "error"


class Actor(StrEnum):
    SYSTEM = "system"
    MODEL = "model"
    MAINTAINER = "maintainer"
    GITHUB = "github"
