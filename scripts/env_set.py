"""``make env-set ARGS="KEY=VALUE ..."``: set NON-SECRET keys in .env.

Refuses secrets and anything not on the allowlist (see ``gatekeeper.envfile``), never
prints existing values, and never creates .env. Prints ``set KEY`` per key.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

from gatekeeper.envfile import parse_assignments, set_values
from gatekeeper.errors import GatekeeperError

REPO_ROOT = Path(__file__).resolve().parent.parent


def main(argv: Sequence[str] | None = None, env_path: Path | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print('usage: make env-set ARGS="KEY=VALUE [KEY=VALUE ...]"', file=sys.stderr)
        return 2
    try:
        updates = parse_assignments(args)
        changed = set_values(updates, env_path or REPO_ROOT / ".env")
    except GatekeeperError as exc:
        print(f"Nothing changed. {exc.message}", file=sys.stderr)
        return 2 if exc.code == "validation_failed" else 1
    for key in changed:
        print(f"set {key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
