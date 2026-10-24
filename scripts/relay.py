"""``make relay``: forward GitHub webhooks from a smee.io channel to the local gateway.

Reads WEBHOOK_PROXY_URL through Settings and runs ``npx smee-client`` (pinned) with
``--url`` and ``--target``. The channel URL is never printed: anyone who has it can
read the webhook payloads sent to it.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Sequence
from typing import Final

from gatekeeper.config import get_settings
from gatekeeper.errors import GatekeeperError

SMEE_PACKAGE: Final = "smee-client@5.0.0"


def build_command(npx: str, url: str, target: str) -> list[str]:
    return [npx, "--yes", SMEE_PACKAGE, "--url", url, "--target", target]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Relay webhooks from WEBHOOK_PROXY_URL.")
    parser.add_argument("--port", type=int, default=8000, help="local gateway port (default 8000)")
    args = parser.parse_args(argv)
    try:
        settings = get_settings()
    except GatekeeperError as exc:
        print(f"Configuration problem: {exc.message}")
        return 2
    if not settings.webhook_proxy_url:
        print(
            "WEBHOOK_PROXY_URL is not set. Create a channel at https://smee.io/new, add\n"
            "  WEBHOOK_PROXY_URL=<your channel URL>\n"
            "to .env, and use the same URL as the GitHub webhook's Payload URL."
        )
        return 2
    npx = shutil.which("npx")
    if npx is None:
        print(
            "Node.js is required for the relay (npx not found). Install Node.js LTS, e.g.\n"
            "  winget install OpenJS.NodeJS.LTS"
        )
        return 2
    target = f"http://127.0.0.1:{args.port}/webhooks/github"
    print(f"Relaying webhooks from WEBHOOK_PROXY_URL to {target} (Ctrl+C to stop).")
    command = build_command(npx, settings.webhook_proxy_url, target)
    try:
        return subprocess.call(command)  # noqa: S603 - fixed argv, resolved executable
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
