"""A per-process pool of Gemini API keys with rotation, cooldowns and dead-key tracking.

Keys are only ever referred to by position alias (``key-1``, ``key-2`` ...). A key value
never appears in logs, errors, audit events or the cache.

- Strategy ``round_robin`` rotates through healthy keys from a random start per process;
  ``primary_first`` always prefers the lowest-numbered healthy key.
- A rate-limited key cools down for the server's retry delay when given, else
  ``base * 2**(n-1)`` capped at ``max`` (n = consecutive rate limits; reset on success).
- An auth/permission failure marks the key dead for the rest of the process.
- ``acquire`` waits for the shortest cooldown when every live key is cooling and the wait
  is at most ``max_wait_s``; otherwise it raises a retryable ``ModelUnavailable`` so the
  adapter/queue back off. With every key dead it raises ``GeminiKeysInvalid``.
- Cooldown state is per process (not shared through Redis; see the P05 build log).
"""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import SecretStr

from gatekeeper.config import Settings
from gatekeeper.errors import ConfigError, GeminiKeysInvalid, ModelUnavailable
from gatekeeper.logging import get_logger

STRATEGIES: Final = ("round_robin", "primary_first")

log = get_logger("gatekeeper.models.key_pool")


@dataclass
class _Key:
    alias: str
    secret: SecretStr
    cooldown_until: float = 0.0
    rate_limits: int = 0
    dead: bool = False


@dataclass(frozen=True)
class KeyLease:
    alias: str
    secret: SecretStr


class KeyPool:
    def __init__(
        self,
        keys: Sequence[SecretStr],
        *,
        strategy: str = "round_robin",
        cooldown_base_s: float = 30.0,
        cooldown_max_s: float = 3600.0,
        max_wait_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rand: Callable[[], float] = random.random,
    ) -> None:
        if not keys:
            raise ConfigError("missing required setting(s): GEMINI_API_KEYS (or GEMINI_API_KEY)")
        if strategy not in STRATEGIES:
            raise ConfigError(f"GEMINI_KEY_STRATEGY must be one of {', '.join(STRATEGIES)}")
        self._keys = [_Key(f"key-{i}", key) for i, key in enumerate(keys, start=1)]
        self.strategy = strategy
        self._base = cooldown_base_s
        self._max = cooldown_max_s
        self._max_wait = max_wait_s
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = int(rand() * len(self._keys)) % len(self._keys)
        self._avoid: str | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> KeyPool:
        pool = cls(
            settings.gemini_keys(),
            strategy=settings.gemini_key_strategy,
            cooldown_base_s=settings.gemini_key_cooldown_base_s,
            cooldown_max_s=settings.gemini_key_cooldown_max_s,
            max_wait_s=settings.gemini_max_wait_for_key_s,
        )
        log.info("gemini key pool ready", pool_size=pool.size, strategy=pool.strategy)
        return pool

    @property
    def size(self) -> int:
        return len(self._keys)

    @property
    def aliases(self) -> list[str]:
        return [key.alias for key in self._keys]

    def status(self) -> dict[str, str]:
        """alias -> healthy | cooling | dead (for model-check and tests)."""
        now = self._clock()
        with self._lock:
            return {
                k.alias: "dead" if k.dead else ("cooling" if k.cooldown_until > now else "healthy")
                for k in self._keys
            }

    # -- selection ------------------------------------------------------------------------

    def acquire(self) -> KeyLease:
        while True:
            with self._lock:
                now = self._clock()
                live = [k for k in self._keys if not k.dead]
                if not live:
                    raise GeminiKeysInvalid(
                        "every Gemini API key in the pool was rejected (invalid or no permission)"
                    )
                healthy = [k for k in live if k.cooldown_until <= now]
                if healthy:
                    chosen = self._choose(healthy)
                    return KeyLease(chosen.alias, chosen.secret)
                wait = min(k.cooldown_until for k in live) - now
            if wait > self._max_wait:
                raise ModelUnavailable(
                    "all Gemini API keys are rate limited; the next one is free in about "
                    f"{wait:.0f} s",
                    retry_after=wait,
                )
            log.info("all gemini keys cooling; waiting", wait_s=round(wait, 1))
            self._sleep(wait)

    def _choose(self, healthy: list[_Key]) -> _Key:
        candidates = healthy
        if self._avoid is not None and len(healthy) > 1:
            candidates = [k for k in healthy if k.alias != self._avoid] or healthy
        self._avoid = None
        if self.strategy == "primary_first":
            return candidates[0]
        order = {k.alias: i for i, k in enumerate(self._keys)}
        n = len(self._keys)
        chosen = min(candidates, key=lambda k: (order[k.alias] - self._next) % n)
        self._next = (order[chosen.alias] + 1) % n
        return chosen

    # -- feedback -------------------------------------------------------------------------

    def _get(self, alias: str) -> _Key:
        return next(k for k in self._keys if k.alias == alias)

    def rate_limited(self, alias: str, retry_after: float | None = None) -> float:
        """Put ``alias`` in cooldown; returns the cooldown in seconds."""
        with self._lock:
            key = self._get(alias)
            key.rate_limits += 1
            if retry_after is not None and retry_after > 0:
                delay = min(retry_after, self._max)
            else:
                delay = min(self._max, self._base * 2.0 ** (key.rate_limits - 1))
            key.cooldown_until = self._clock() + delay
        log.warning("gemini key rate limited; cooling down", key=alias, cooldown_s=round(delay, 1))
        return delay

    def invalid(self, alias: str) -> None:
        with self._lock:
            self._get(alias).dead = True
        log.error("gemini key rejected (invalid or no permission); not used again", key=alias)

    def transient(self, alias: str) -> None:
        """A timeout/5xx on ``alias``: prefer a different key on the next attempt."""
        with self._lock:
            self._avoid = alias

    def success(self, alias: str) -> None:
        with self._lock:
            self._get(alias).rate_limits = 0
