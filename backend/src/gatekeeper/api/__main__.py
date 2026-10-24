"""Run the gateway: ``python -m gatekeeper.api`` (``make api``). Binds to 127.0.0.1."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

import uvicorn

from gatekeeper.api.app import create_app
from gatekeeper.config import get_settings
from gatekeeper.errors import GatekeeperError
from gatekeeper.logging import configure_logging


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Gatekeeper webhook gateway.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="port (default 8000)")
    args = parser.parse_args(argv)
    try:
        settings = get_settings()
        configure_logging(settings)
        app = create_app(settings)
    except GatekeeperError as exc:
        print(f"Cannot start the gateway: {exc.message}", file=sys.stderr)
        return 2
    # log_config=None keeps uvicorn's loggers on our structlog pipeline (with redaction).
    uvicorn.run(app, host=args.host, port=args.port, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
