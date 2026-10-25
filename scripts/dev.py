"""Local dev runner (``make dev``): the API and one queue worker, with prefixed logs.

1. Checks that Postgres and Redis are reachable and that migrations are at head
   (runs ``alembic upgrade head`` if they are not).
2. Starts ``python -m gatekeeper.api`` (API_HOST:API_PORT) and ``python -m
   gatekeeper.worker`` as child processes in this console, and prefixes their output
   with ``[api]`` and ``[worker]``.
3. Ctrl+C reaches both children (they share the console): uvicorn shuts down and the
   worker finishes its current job. Children still running after ``STOP_GRACE_S`` are
   terminated; an unfinished job is then reclaimed when its lease expires.

Never prints secrets: connection errors name the service, not the URL.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Final

ROOT: Final = Path(__file__).resolve().parents[1]
STOP_GRACE_S: Final = 20.0
CHILDREN: Final = (
    ("api", [sys.executable, "-m", "gatekeeper.api"]),
    ("worker", [sys.executable, "-m", "gatekeeper.worker"]),
)


def say(text: str) -> None:
    print(f"[dev] {text}", flush=True)


def preflight() -> bool:
    """Postgres, Redis and migrations. Returns False (after explaining) on a problem."""
    import redis
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    from gatekeeper.config import get_settings
    from gatekeeper.db.session import create_db_engine
    from gatekeeper.errors import GatekeeperError

    try:
        settings = get_settings()
        engine = create_db_engine(settings, connect_args={"connect_timeout": 3})
    except GatekeeperError as exc:
        say(f"configuration problem: {exc.message}")
        return False
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            current = set(MigrationContext.configure(connection).get_current_heads())
    except SQLAlchemyError:
        say("Postgres is not reachable. Start it with `make up` (or use `make stack`).")
        return False
    finally:
        engine.dispose()
    say("Postgres: ok")

    if settings.redis_url:
        try:
            redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2).ping()
            say("Redis: ok")
        except (redis.RedisError, OSError):
            say("Redis is not reachable. Start it with `make up` (or use `make stack`).")
            return False
    else:
        say("Redis: not configured (the model cache is off)")

    heads = set(ScriptDirectory(str(ROOT / "backend" / "alembic")).get_heads())
    if current != heads:
        say("migrations are not at head; running `alembic upgrade head`")
        code = subprocess.call([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT)
        if code != 0:
            say("migration failed; see the output above")
            return False
    say("migrations: at head")
    if not settings.gatekeeper_admin_token:
        say("note: GATEKEEPER_ADMIN_TOKEN is empty, so /api answers 503 until it is set")
    say(f"API: http://{settings.api_host}:{settings.api_port}  (Ctrl+C stops everything)")
    return True


def pump(name: str, stream: IO[str]) -> None:
    for line in stream:
        print(f"[{name}] {line.rstrip()}", flush=True)


def start(name: str, argv: Sequence[str]) -> subprocess.Popen[str]:
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    child = subprocess.Popen(  # noqa: S603 - fixed argv, this interpreter
        list(argv),
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    if child.stdout is not None:
        threading.Thread(target=pump, args=(name, child.stdout), daemon=True).start()
    return child


def stop(children: dict[str, subprocess.Popen[str]]) -> None:
    deadline = time.monotonic() + STOP_GRACE_S
    for name, child in children.items():
        remaining = max(0.1, deadline - time.monotonic())
        try:
            child.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            say(f"{name} did not stop within {STOP_GRACE_S:.0f} s; terminating it")
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    if not preflight():
        return 1
    children = {name: start(name, command) for name, command in CHILDREN}
    try:
        while True:
            for name, child in children.items():
                code = child.poll()
                if code is not None:
                    say(f"{name} exited with code {code}; stopping the rest")
                    for other in children.values():
                        if other.poll() is None:
                            other.terminate()
                    stop(children)
                    return code or 1
            time.sleep(0.5)
    except KeyboardInterrupt:
        say("stopping (the worker finishes its current job first)…")
        try:
            stop(children)
        except KeyboardInterrupt:
            for child in children.values():
                child.kill()
        say("stopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
