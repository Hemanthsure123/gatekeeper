"""Run the API: ``python -m gatekeeper.api`` (``make api``). Binds to API_HOST:API_PORT
(default 127.0.0.1:8000); ``--host`` / ``--port`` override them."""

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
    parser = argparse.ArgumentParser(description="Run the Gatekeeper API.")
    parser.add_argument("--host", default=None, help="bind address (default API_HOST)")
    parser.add_argument("--port", type=int, default=None, help="port (default API_PORT)")
    args = parser.parse_args(argv)
    try:
        settings = get_settings()
        host = args.host or settings.api_host
        port = args.port or settings.api_port
        configure_logging(settings)
        app = create_app(settings)
    except GatekeeperError as exc:
        print(f"Cannot start the gateway: {exc.message}", file=sys.stderr)
        return 2
    # log_config=None keeps uvicorn's loggers on our structlog pipeline (with redaction).
    uvicorn.run(app, host=host, port=port, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
