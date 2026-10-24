"""Choose the backend from MODEL_BACKEND and wrap it in the shared adapter."""

from __future__ import annotations

from gatekeeper.config import Settings, get_settings
from gatekeeper.errors import ConfigError
from gatekeeper.models.adapter import ModelAdapter
from gatekeeper.models.backend import BackendDriver
from gatekeeper.models.cache import ModelCache, RedisModelCache


def get_driver(settings: Settings) -> BackendDriver:
    if settings.model_backend == "ollama":
        from gatekeeper.models.ollama import OllamaBackend

        return OllamaBackend(settings)
    if settings.model_backend == "gemini_api":
        from gatekeeper.models.gemini import GeminiBackend

        return GeminiBackend(settings)
    raise ConfigError(f"{settings.model_backend} backend not implemented yet")


def get_backend(
    settings: Settings | None = None,
    *,
    cache: ModelCache | None = None,
    use_cache: bool = True,
) -> ModelAdapter:
    """The ModelBackend every feature should use. Redis caching unless ``use_cache=False``."""
    settings = settings or get_settings()
    driver = get_driver(settings)
    if cache is None and use_cache:
        cache = RedisModelCache.from_settings(settings)
    return ModelAdapter.from_settings(driver, settings, cache)
