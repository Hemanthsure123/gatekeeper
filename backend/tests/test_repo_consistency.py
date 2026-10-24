"""Guards against configuration drift between files that must agree."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
POSTGRES_IMAGE = re.compile(r"^\s*image:\s*[\"']?(postgres:[^\s\"']+)", re.M)


def postgres_images(path: Path) -> list[str]:
    return POSTGRES_IMAGE.findall(path.read_text(encoding="utf-8"))


def test_ci_postgres_image_matches_docker_compose() -> None:
    compose = postgres_images(REPO_ROOT / "docker-compose.yml")
    ci = postgres_images(REPO_ROOT / ".github" / "workflows" / "ci.yml")
    assert len(compose) == 1, compose
    assert ci, "CI workflow has no postgres service image"
    assert set(ci) == set(compose), f"CI uses {ci}, docker-compose.yml uses {compose}"
