"""Report local tool versions and whether Ollama responds.

Never reads .env: OLLAMA_BASE_URL comes from the process environment only and
falls back to the local default. Problems are reported as warnings; the exit
code is always 0 so a missing tool never looks like a crash.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
MIN_PYTHON = (3, 13)
TIMEOUT_SECONDS = 5.0

TOOLS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("git", ("git", "--version")),
    ("uv", ("uv", "--version")),
    ("docker", ("docker", "--version")),
    ("docker compose", ("docker", "compose", "version")),
    ("ollama", ("ollama", "--version")),
)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    hints: tuple[str, ...] = field(default=())


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv, resolved executable, no shell
        list(command), capture_output=True, text=True, timeout=TIMEOUT_SECONDS, check=False
    )


def tool_version(name: str, command: Sequence[str]) -> Check:
    executable = shutil.which(command[0])
    if executable is None:
        return Check(name, ok=False, detail="not found on PATH")
    try:
        result = _run([executable, *command[1:]])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check(name, ok=False, detail=f"could not run: {exc}")
    lines = [line.strip() for line in (result.stdout + result.stderr).splitlines() if line.strip()]
    version_line = next(
        (line for line in lines if "version" in line.lower()), lines[0] if lines else ""
    )
    if result.returncode != 0:
        return Check(name, ok=False, detail=f"exited with code {result.returncode}: {version_line}")
    return Check(name, ok=True, detail=version_line or "(no version output)")


def python_check() -> Check:
    detail = f"Python {platform.python_version()} ({sys.executable})"
    if sys.version_info < MIN_PYTHON:
        wanted = ".".join(map(str, MIN_PYTHON))
        return Check("python", ok=False, detail=detail, hints=(f"Gatekeeper needs {wanted}+",))
    return Check("python", ok=True, detail=detail)


def docker_daemon_check() -> Check:
    executable = shutil.which("docker")
    if executable is None:
        return Check("docker daemon", ok=False, detail="docker CLI not found")
    try:
        result = _run([executable, "info", "--format", "{{.ServerVersion}}"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check("docker daemon", ok=False, detail=f"could not run: {exc}")
    if result.returncode != 0 or not result.stdout.strip():
        return Check(
            "docker daemon",
            ok=False,
            detail="not reachable",
            hints=("start Docker Desktop (or the docker service) before `make up`",),
        )
    return Check("docker daemon", ok=True, detail=f"server {result.stdout.strip()}")


def display_url(url: str) -> str:
    """Drop any user:password@ part so credentials are never printed."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def ollama_check(base_url: str) -> Check:
    name = "ollama api"
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return Check(name, ok=False, detail="OLLAMA_BASE_URL must be an http(s) URL")
    url = base_url.rstrip("/") + "/api/tags"
    shown = display_url(url)
    try:
        # Scheme is restricted to http(s) above.
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return Check(
            name,
            ok=False,
            detail=f"no response from {shown}: {reason}",
            hints=("start Ollama (`ollama serve` or the desktop app)",),
        )
    except ValueError:
        return Check(name, ok=False, detail=f"{shown} did not return JSON")

    raw_models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        return Check(name, ok=False, detail=f"{shown} returned an unexpected payload")
    models = sorted(
        str(m["name"]) for m in raw_models if isinstance(m, dict) and isinstance(m.get("name"), str)
    )
    if not models:
        return Check(
            name,
            ok=False,
            detail=f"reachable at {shown}, but no models are installed",
            hints=("pull a model, e.g. `ollama pull <model>`",),
        )
    return Check(name, ok=True, detail=f"reachable at {shown}", hints=tuple(models))


def run_checks(base_url: str) -> list[Check]:
    checks = [tool_version(name, command) for name, command in TOOLS]
    checks.insert(1, python_check())
    checks.append(docker_daemon_check())
    checks.append(ollama_check(base_url))
    return checks


def report(checks: Sequence[Check]) -> str:
    lines = ["Gatekeeper doctor"]
    for check in checks:
        status = "ok  " if check.ok else "WARN"
        lines.append(f"  [{status}] {check.name:<15} {check.detail}")
        prefix = "model:" if check.name == "ollama api" and check.ok else "hint:"
        lines.extend(f"         {'':<15} {prefix} {hint}" for hint in check.hints)
    warnings = sum(not check.ok for check in checks)
    lines.append(f"{warnings} warning(s)." if warnings else "All checks passed.")
    return "\n".join(lines)


def main() -> int:
    base_url = os.environ.get("OLLAMA_BASE_URL", "").strip() or DEFAULT_OLLAMA_BASE_URL
    print(report(run_checks(base_url)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
