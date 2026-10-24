"""Response cache for validated model outputs (Redis).

Fail open: any Redis problem logs a warning and the call continues uncached. Only
validated responses are stored; failures are never cached.
"""

from __future__ import annotations

from typing import Any, Final, Protocol

import redis
from redis.exceptions import RedisError

from gatekeeper.audit.hashing import canonical_json, sha256_hex
from gatekeeper.config import Settings
from gatekeeper.logging import get_logger
from gatekeeper.models.types import ImagePart, ModelRequest

KEY_PREFIX: Final = "gatekeeper:model-cache:v1:"

log = get_logger("gatekeeper.models.cache")


class ModelCache(Protocol):
    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str) -> None: ...


def cache_key(
    *, backend: str, model: str, request: ModelRequest[Any], json_schema: dict[str, Any]
) -> str:
    """SHA-256 over everything that can change the output. Images contribute only hashes."""
    parts = [
        {"image_sha256": part.sha256} if isinstance(part, ImagePart) else {"text": part.text}
        for part in request.parts
    ]
    material = {
        "backend": backend,
        "model": model,
        "system": request.system,
        "parts": parts,
        "json_schema": json_schema,
        "temperature": request.temperature,
        "seed": request.seed,
        "reasoning": request.reasoning.value,
        "prompt_id": request.prompt_id,
        "prompt_version": request.prompt_version,
    }
    return sha256_hex(canonical_json(material))


class RedisModelCache:
    def __init__(self, client: redis.Redis, *, ttl_s: int) -> None:
        self._client = client
        self._ttl_s = ttl_s

    @classmethod
    def from_settings(cls, settings: Settings) -> RedisModelCache | None:
        """Build from REDIS_URL, or return None (caching disabled) when it is not set."""
        if settings.redis_url is None:
            log.warning("model cache disabled: REDIS_URL is not set")
            return None
        client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=2,
            socket_connect_timeout=2,
        )
        return cls(client, ttl_s=settings.model_cache_ttl_s)

    def get(self, key: str) -> str | None:
        try:
            value = self._client.get(KEY_PREFIX + key)
        except (RedisError, OSError) as exc:
            log.warning("model cache read failed; continuing uncached", error=type(exc).__name__)
            return None
        return value if isinstance(value, str) else None

    def set(self, key: str, value: str) -> None:
        try:
            self._client.set(KEY_PREFIX + key, value, ex=self._ttl_s)
        except (RedisError, OSError) as exc:
            log.warning("model cache write failed; continuing", error=type(exc).__name__)
