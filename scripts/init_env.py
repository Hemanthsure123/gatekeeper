"""Create .env from .env.example with local defaults and freshly generated secrets.

Safety rules (see CLAUDE.md):
- Never overwrites an existing .env; exits non-zero with a message instead.
- Never prints a value. Only key names are reported.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parent.parent

# Host side of the port mapping in docker-compose.yml.
POSTGRES_HOST = "127.0.0.1"
POSTGRES_PORT = 15432

DEFAULTS: dict[str, str] = {
    "APP_ENV": "development",
    "LOG_LEVEL": "INFO",
    "POSTGRES_USER": "gatekeeper",
    "POSTGRES_DB": "gatekeeper",
    "REDIS_URL": "redis://127.0.0.1:16379/0",
    "MODEL_BACKEND": "ollama",
    "OLLAMA_BASE_URL": "http://localhost:11434",
}
GENERATED_SECRETS: tuple[str, ...] = (
    "POSTGRES_PASSWORD",
    "GATEKEEPER_ADMIN_TOKEN",
    "SESSION_SECRET",
    "GITHUB_WEBHOOK_SECRET",
)
DERIVED: tuple[str, ...] = ("DATABASE_URL",)
SECRET_NBYTES = 32

_ASSIGNMENT = re.compile(r"^(?P<key>[A-Z][A-Z0-9_]*)=(?P<value>.*)$")


class TemplateError(Exception):
    """The .env.example template cannot be used safely."""


def parse_keys(template: str) -> list[str]:
    """Return the template's keys in order, rejecting duplicates and missing managed keys."""
    keys = [m["key"] for line in template.splitlines() if (m := _ASSIGNMENT.match(line))]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        raise TemplateError(f"duplicate keys in .env.example: {', '.join(duplicates)}")
    missing = sorted({*DEFAULTS, *GENERATED_SECRETS, *DERIVED} - set(keys))
    if missing:
        raise TemplateError(f".env.example is missing keys: {', '.join(missing)}")
    return keys


def build_database_url(user: str, password: str, database: str) -> str:
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{quote(database, safe='')}"
    )


def generate_values() -> dict[str, str]:
    """Return every value init-env sets; all other keys are written empty."""
    values = dict(DEFAULTS)
    for key in GENERATED_SECRETS:
        values[key] = secrets.token_urlsafe(SECRET_NBYTES)
    values["DATABASE_URL"] = build_database_url(
        values["POSTGRES_USER"], values["POSTGRES_PASSWORD"], values["POSTGRES_DB"]
    )
    return values


def render(template: str, values: Mapping[str, str]) -> str:
    """Return the template with each assignment replaced by its value, or emptied."""
    lines = []
    for line in template.splitlines():
        match = _ASSIGNMENT.match(line)
        lines.append(f"{match['key']}={values.get(match['key'], '')}" if match else line)
    return "\n".join(lines) + "\n"


def write_new_file(path: Path, content: str) -> None:
    """Create ``path`` exclusively (never overwrite) with owner-only permissions."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


def _refuse(target: Path) -> int:
    print(
        f"Refusing to overwrite existing {target.name}; nothing was changed.\n"
        "If you really want a fresh one, move or delete it yourself, then run "
        "`make init-env` again.",
        file=sys.stderr,
    )
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create .env from .env.example.")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    example: Path = args.root / ".env.example"
    target: Path = args.root / ".env"

    if os.path.lexists(target):
        return _refuse(target)
    try:
        template = example.read_text(encoding="utf-8")
        keys = parse_keys(template)
    except (OSError, TemplateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    values = generate_values()
    try:
        write_new_file(target, render(template, values))
    except FileExistsError:
        return _refuse(target)

    print("Created .env from .env.example. Values are never printed.")
    print("Set to local defaults:", ", ".join(k for k in keys if k in DEFAULTS or k in DERIVED))
    print("Generated random secrets:", ", ".join(k for k in keys if k in GENERATED_SECRETS))
    print("Left empty for you to fill in:", ", ".join(k for k in keys if k not in values))
    return 0


if __name__ == "__main__":
    sys.exit(main())
