"""Build helper for the side-panel extension (``make extension-install|build|dev``).

Make recipes may only call uv, python and docker, so npm runs from here. It also draws
the toolbar icons with the standard library (zlib + struct PNG) into
``extension/public/icons`` before each build; the icons are generated, not committed.
"""

from __future__ import annotations

import argparse
import math
import shutil
import struct
import subprocess
import sys
import zlib
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extension"
ICONS = EXTENSION / "public" / "icons"
SIZES = (16, 32, 48, 128)
MIN_NODE_MAJOR = 20
#: The single accent colour (also --accent in the panel CSS), and white.
ACCENT = (59, 91, 219)
WHITE = (255, 255, 255)
SUPERSAMPLE = 4


def _png(width: int, height: int, rgba: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    stride = width * 4
    raw = b"".join(b"\x00" + rgba[y * stride : (y + 1) * stride] for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def _shape(x: float, y: float) -> tuple[int, int, int] | None:
    """The icon in unit coordinates: an accent rounded square with a white gate
    (two posts and a crossbar). None means transparent."""
    radius = 0.22
    cx, cy = min(max(x, radius), 1 - radius), min(max(y, radius), 1 - radius)
    if math.hypot(x - cx, y - cy) > radius:
        return None
    post = (0.24 <= x <= 0.36 or 0.64 <= x <= 0.76) and 0.2 <= y <= 0.8
    bar = 0.36 <= x <= 0.64 and (0.34 <= y <= 0.44 or 0.56 <= y <= 0.66)
    return WHITE if post or bar else ACCENT


def draw_icon(size: int) -> bytes:
    pixels = bytearray()
    samples = SUPERSAMPLE * SUPERSAMPLE
    for py in range(size):
        for px in range(size):
            r = g = b = a = 0
            for sy in range(SUPERSAMPLE):
                for sx in range(SUPERSAMPLE):
                    colour = _shape(
                        (px + (sx + 0.5) / SUPERSAMPLE) / size,
                        (py + (sy + 0.5) / SUPERSAMPLE) / size,
                    )
                    if colour is not None:
                        r, g, b, a = r + colour[0], g + colour[1], b + colour[2], a + 1
            if a:
                pixels += bytes((r // a, g // a, b // a, round(255 * a / samples)))
            else:
                pixels += b"\x00\x00\x00\x00"
    return _png(size, size, bytes(pixels))


def write_icons() -> None:
    ICONS.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        (ICONS / f"icon{size}.png").write_bytes(draw_icon(size))


def _npm() -> str:
    node, npm = shutil.which("node"), shutil.which("npm")
    if node is None or npm is None:
        raise SystemExit(
            "Node.js and npm are required (Node 20 or newer). Install the LTS from "
            "https://nodejs.org, then reopen the terminal."
        )
    version = subprocess.run(  # noqa: S603 - fixed argv, resolved executable, no shell
        [node, "--version"], capture_output=True, text=True, check=True
    )
    major = int(version.stdout.strip().lstrip("v").split(".")[0])
    if major < MIN_NODE_MAJOR:
        raise SystemExit(f"Node {MIN_NODE_MAJOR} or newer is required (found {version.stdout})")
    return npm


def run_npm(*args: str) -> int:
    command = [_npm(), *args]
    return subprocess.run(command, cwd=EXTENSION, check=False).returncode  # noqa: S603 - fixed argv


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Gatekeeper extension.")
    parser.add_argument("command", choices=["install", "build", "dev", "icons"])
    args = parser.parse_args(argv)
    if args.command == "icons":
        write_icons()
        return 0
    if args.command == "install":
        lock = EXTENSION / "package-lock.json"
        return run_npm("ci" if lock.exists() else "install", "--no-audit", "--no-fund")
    if not (EXTENSION / "node_modules").exists():
        code = main(["install"])
        if code:
            return code
    write_icons()
    if args.command == "dev":
        return run_npm("run", "dev")
    code = run_npm("run", "build")
    if code:
        return code
    manifest = EXTENSION / "dist" / "manifest.json"
    if not manifest.exists():
        print("build finished but dist/manifest.json is missing", file=sys.stderr)
        return 1
    print(f"Extension built: {manifest.parent} (load it unpacked from chrome://extensions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
