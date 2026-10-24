"""Model adapter: one typed, validated, cached and audited interface over model backends.

Use :func:`get_backend` to obtain the adapter; tests use
:func:`gatekeeper.models.testing.fake_adapter`.
"""

from gatekeeper.models.adapter import ModelAdapter, RequestValidator
from gatekeeper.models.backend import BackendCall, BackendDriver, Completion, ModelBackend, Turn
from gatekeeper.models.factory import get_backend, get_driver
from gatekeeper.models.types import (
    DEFAULT_SEED,
    BackendHealth,
    CachePolicy,
    Capabilities,
    ImagePart,
    ModelRequest,
    ModelResponse,
    ModelRole,
    Reasoning,
    TextPart,
    Usage,
)

__all__ = [
    "DEFAULT_SEED",
    "BackendCall",
    "BackendDriver",
    "BackendHealth",
    "CachePolicy",
    "Capabilities",
    "Completion",
    "ImagePart",
    "ModelAdapter",
    "ModelBackend",
    "ModelRequest",
    "ModelResponse",
    "ModelRole",
    "Reasoning",
    "RequestValidator",
    "TextPart",
    "Turn",
    "Usage",
    "get_backend",
    "get_driver",
]
