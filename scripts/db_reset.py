"""Drop every Gatekeeper table and re-run all migrations. Development only.

Refuses unless APP_ENV is ``development``. Destroys local data, including the
append-only audit trail, so it must never run anywhere else.
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

from gatekeeper.config import Settings, get_settings
from gatekeeper.errors import GatekeeperError

REPO_ROOT = Path(__file__).resolve().parent.parent


def reset_allowed(settings: Settings) -> bool:
    return settings.app_env == "development"


def main(settings: Settings | None = None) -> int:
    try:
        settings = settings or get_settings()
    except GatekeeperError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not reset_allowed(settings):
        print(
            f"Refusing to reset the database: APP_ENV is {settings.app_env!r}; "
            "db-reset only runs in development.",
            file=sys.stderr,
        )
        return 1
    config = Config(toml_file=str(REPO_ROOT / "pyproject.toml"))
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    print("Database reset: downgraded to base and upgraded to head.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
