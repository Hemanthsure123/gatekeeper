"""``make model-check``: show the configured backend and make two small structured calls
through the full adapter (the second should be a cache hit). ``--vision`` also sends a
generated solid-red PNG and asks for its colour. Never prints secrets.

Exit codes: 0 ok, 1 backend/model problem, 2 configuration problem, 3 vision check failed
(only with ``--require-vision``; a plain ``--vision`` failure is a warning, exit 0).
"""

from __future__ import annotations

import argparse
import struct
import sys
import zlib
from collections.abc import Sequence

from gatekeeper.config import Settings, get_settings
from gatekeeper.errors import GatekeeperError
from gatekeeper.logging import configure_logging
from gatekeeper.models.adapter import ModelAdapter
from gatekeeper.models.factory import get_backend
from gatekeeper.models.types import CachePolicy, ImagePart, ModelResponse
from gatekeeper.prompts.model_check import GREETING, VISION, ColourAnswer, Greeting

MODEL_KEYS = {"ollama": "OLLAMA_MODEL", "gemini_api": "GEMMA_MODEL_PRIMARY"}


def solid_png(rgb: tuple[int, int, int] = (255, 0, 0), size: int = 32) -> bytes:
    """A tiny solid-colour PNG built with the standard library only."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    row = b"\x00" + bytes(rgb) * size  # filter byte 0, then RGB pixels
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * size))
        + chunk(b"IEND", b"")
    )


def _describe(label: str, response: ModelResponse[Greeting]) -> str:
    usage = response.usage
    return (
        f"{label}: latency {response.latency_ms:.0f} ms, tokens in/out "
        f"{usage.input_tokens}/{usage.output_tokens}, cache_hit={response.cache_hit}, "
        f"attempts={response.attempts}, key={response.meta.get('key_alias', '-')}, "
        f"failovers={response.meta.get('failovers', 0)}, parsed={response.parsed.model_dump()}"
    )


def _missing_model(settings: Settings) -> str | None:
    key = MODEL_KEYS.get(settings.model_backend)
    if key and getattr(settings, key.lower()) is None:
        example = "gemma4:e4b" if key == "OLLAMA_MODEL" else "<model id from the Gemma docs>"
        return (
            f"{key} is not set. Add this line to .env, then run make model-check again:\n"
            f"  {key}={example}"
        )
    return None


def run_vision(adapter: ModelAdapter) -> bool:
    image = ImagePart.from_bytes(solid_png(), "image/png")
    request = VISION.render().to_request(
        ColourAnswer, extra_parts=(image,), max_output_tokens=64, cache_policy=CachePolicy.BYPASS
    )
    try:
        response = adapter.generate(request, allow_unaudited=True)
    except GatekeeperError as exc:
        print(f"Vision: FAIL ({exc.code}: {exc.message})")
        return False
    answer = response.parsed.colour.strip().lower()
    passed = "red" in answer
    print(
        f"Vision: {'PASS' if passed else 'FAIL'} (model said {answer!r} for a solid red image; "
        f"key={response.meta.get('key_alias', '-')}, latency {response.latency_ms:.0f} ms)"
    )
    return passed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the configured model backend.")
    parser.add_argument("--vision", action="store_true", help="also test image input")
    parser.add_argument(
        "--require-vision",
        action="store_true",
        help="test image input and exit 3 if it fails",
    )
    args = parser.parse_args(argv)

    try:
        settings = get_settings()
    except GatekeeperError as exc:
        print(f"Configuration problem: {exc.message}")
        return 2
    configure_logging(settings)
    print(f"Backend: {settings.model_backend}")
    if problem := _missing_model(settings):
        print(problem)
        return 2
    try:
        adapter = get_backend(settings)
    except GatekeeperError as exc:
        print(f"Configuration problem: {exc.message}")
        return 2

    health = adapter.health()
    pool = getattr(adapter.driver, "pool", None)
    if pool is not None:
        print(f"Key pool: {pool.size} key(s), strategy {pool.strategy}")
    print(f"Health: {'ok' if health.ok else 'NOT OK'} - {health.message}")
    if not health.ok:
        return 1
    try:
        caps = adapter.capabilities()
        print(
            f"Capabilities: images={caps.supports_images}, "
            f"structured_output={caps.supports_structured_output}, "
            f"reasoning_control={caps.supports_reasoning_control}, "
            f"context_tokens={caps.max_context_tokens}"
        )
        request = GREETING.render(note="Gatekeeper model check").to_request(
            Greeting, max_output_tokens=256
        )
        print(f"Model: {adapter.model_name()}")
        print(_describe("Call 1", adapter.generate(request, allow_unaudited=True)))
        print(_describe("Call 2", adapter.generate(request, allow_unaudited=True)))
    except GatekeeperError as exc:
        print(f"Model call failed ({exc.code}): {exc.message}")
        return 1

    if (args.vision or args.require_vision) and not run_vision(adapter):
        if args.require_vision:
            return 3
        print("WARNING: vision check failed; image input may not work with this model/backend.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
