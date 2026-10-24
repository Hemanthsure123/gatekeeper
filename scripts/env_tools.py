"""Inspect and extend .env without ever printing a value.

check  List every key from .env.example as SET or MISSING, grouped by the prompt
       that first needs it, and flag optional keys.
sync   Append keys that exist in .env.example but not in .env, with empty values.
       Existing lines are never changed.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

_TEMPLATE_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=")
_ENV_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
_PROMPT = re.compile(r"First needed: (P\d+)")
_EMPTY_VALUES = frozenset({"", '""', "''"})
UNASSIGNED = "unassigned"


@dataclass(frozen=True)
class TemplateKey:
    name: str
    prompt: str
    optional: bool
    comment: str | None


def parse_template(text: str) -> list[TemplateKey]:
    keys: list[TemplateKey] = []
    previous = ""
    for line in text.splitlines():
        match = _TEMPLATE_ASSIGNMENT.match(line)
        if match:
            comment = previous if previous.startswith("#") else None
            prompt = _PROMPT.search(comment or "")
            optional = (comment or "").lstrip("# ").lower().startswith("optional")
            keys.append(
                TemplateKey(match[1], prompt[1] if prompt else UNASSIGNED, optional, comment)
            )
        previous = line
    return keys


def parse_env_status(text: str) -> dict[str, bool]:
    """Map each key in a .env file to whether it has a non-empty value. Values are discarded."""
    status: dict[str, bool] = {}
    for line in text.splitlines():
        match = _ENV_ASSIGNMENT.match(line)
        if match and not line.lstrip().startswith("#"):
            status[match[1]] = match[2].strip() not in _EMPTY_VALUES
    return status


def _prompt_order(prompt: str) -> tuple[int, str]:
    return (int(prompt[1:]), prompt) if prompt != UNASSIGNED else (10**6, prompt)


def check(template: list[TemplateKey], env: dict[str, bool]) -> str:
    lines = [".env status (values are never shown)"]
    for prompt in sorted({key.prompt for key in template}, key=_prompt_order):
        lines.append(f"\n{prompt}")
        for key in (k for k in template if k.prompt == prompt):
            state = "SET" if env.get(key.name) else "MISSING"
            suffix = "  (optional)" if key.optional else ""
            lines.append(f"  {state:<8} {key.name}{suffix}")
    extra = sorted(set(env) - {key.name for key in template})
    if extra:
        lines.append(f"\nIn .env but not in .env.example: {', '.join(extra)}")
    set_count = sum(1 for key in template if env.get(key.name))
    missing = [key for key in template if not env.get(key.name)]
    optional_missing = sum(1 for key in missing if key.optional)
    lines.append(
        f"\n{set_count} set, {len(missing)} missing ({optional_missing} of them optional)."
    )
    return "\n".join(lines)


def sync(env_path: Path, template: list[TemplateKey], env: dict[str, bool]) -> list[str]:
    """Append template keys absent from .env. Returns the appended key names."""
    missing = [key for key in template if key.name not in env]
    if not missing:
        return []
    existing = env_path.read_bytes()
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    block = ["", f"# Added by `make env-sync` on {stamp}."]
    for key in missing:
        if key.comment:
            block.append(key.comment)
        block.append(f"{key.name}=")
    prefix = "" if existing.endswith(b"\n") or not existing else "\n"
    with env_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(prefix + "\n".join(block) + "\n")
    return [key.name for key in missing]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check or extend .env without printing values.")
    parser.add_argument("command", choices=["check", "sync"])
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    example: Path = args.root / ".env.example"
    env_path: Path = args.root / ".env"

    try:
        template = parse_template(example.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"error: cannot read .env.example: {exc.strerror}", file=sys.stderr)
        return 2
    if not env_path.exists():
        print("No .env found. Run `make init-env` first.", file=sys.stderr)
        return 1
    env = parse_env_status(env_path.read_text(encoding="utf-8"))

    if args.command == "check":
        print(check(template, env))
        return 0
    added = sync(env_path, template, env)
    if added:
        print(f"Appended {len(added)} key(s) with empty values: {', '.join(added)}")
    else:
        print(".env already has every key from .env.example; nothing changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
