"""The contributor-blind rule, checked statically: review code never touches identity.

Scans every module in ``gatekeeper.review_core`` and ``gatekeeper.pipeline.steps`` with
``ast`` and fails on any import of ``gatekeeper.identity``, any reference to the
``ItemAuthor`` model, or the ``item_authors`` table name.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import gatekeeper.pipeline.steps
import gatekeeper.review_core

FORBIDDEN_MODULE = "gatekeeper.identity"
FORBIDDEN_NAMES = frozenset({"ItemAuthor"})
FORBIDDEN_TABLE = "item_authors"


def guarded_files() -> list[Path]:
    package = Path(gatekeeper.review_core.__file__).parent
    return [*sorted(package.rglob("*.py")), Path(gatekeeper.pipeline.steps.__file__)]


def _is_identity_module(name: str) -> bool:
    return name == FORBIDDEN_MODULE or name.startswith(FORBIDDEN_MODULE + ".")


def violations(source: str, filename: str = "<src>") -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source, filename)):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if _is_identity_module(alias.name)]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level == 0 and _is_identity_module(module):
                found.append(module)
            if module == "gatekeeper" and any(a.name == "identity" for a in node.names):
                found.append(FORBIDDEN_MODULE)
            found += [a.name for a in node.names if a.name in FORBIDDEN_NAMES or a.name == "*"]
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES | {"identity"}:
            found.append(node.attr)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and FORBIDDEN_TABLE in node.value
        ):
            found.append(FORBIDDEN_TABLE)
    return found


def test_guarded_modules_exist() -> None:
    files = guarded_files()
    assert any(f.name == "steps.py" for f in files)
    assert any(f.parent.name == "review_core" for f in files)


@pytest.mark.parametrize("path", guarded_files(), ids=lambda p: p.name)
def test_review_code_never_touches_identity(path: Path) -> None:
    assert violations(path.read_text(encoding="utf-8"), str(path)) == []


@pytest.mark.parametrize(
    "snippet",
    [
        "import gatekeeper.identity",
        "from gatekeeper.identity import get_author",
        "from gatekeeper import identity",
        "from gatekeeper.db.models import ItemAuthor",
        "from gatekeeper.db import *",
        "import gatekeeper.db.models as m\nm.ItemAuthor",
        "q = 'SELECT login FROM item_authors'",
    ],
)
def test_checker_catches_violations(snippet: str) -> None:
    assert violations(snippet)


def test_checker_allows_ordinary_code() -> None:
    assert violations("from gatekeeper.audit import EventType\nx = 'authors of the change'") == []
