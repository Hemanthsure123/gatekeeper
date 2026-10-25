"""Structural checks for the synthetic demo pack, plus proof that the base has exactly the
documented bug and that the valuable-fix PR fixes it."""

from __future__ import annotations

import importlib.util
import re
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

DEMO = Path(__file__).resolve().parents[2] / "demo"
PRS = sorted(p for p in (DEMO / "prs").iterdir() if p.is_dir())
REQUIRED = ("branch.txt", "title.txt", "body.md")


def test_five_prs_with_all_required_files() -> None:
    assert [p.name for p in PRS] == [
        "1-valuable-fix",
        "2-whitespace-noise",
        "3-honest-newcomer-docs",
        "4-claim-mismatch",
        "5-injection-attempt",
    ]
    for pr in PRS:
        for name in REQUIRED:
            assert (pr / name).read_text(encoding="utf-8").strip(), f"{pr.name}/{name} is empty"
        changes = [f for f in (pr / "changes").rglob("*") if f.is_file()]
        assert changes, f"{pr.name} has no changes"
        for change in changes:
            assert (
                DEMO / "base" / change.relative_to(pr / "changes")
            ).exists() or pr.name.startswith("3")


def test_branch_names_are_unique_and_safe() -> None:
    branches = [(p / "branch.txt").read_text(encoding="utf-8").strip() for p in PRS]
    assert len(set(branches)) == len(branches)
    assert all(re.fullmatch(r"[a-z0-9-]+", b) for b in branches)


def test_every_closing_reference_has_an_issue_file() -> None:
    for pr in PRS:
        for number in re.findall(r"Closes #(\d+)", (pr / "body.md").read_text(encoding="utf-8")):
            assert (DEMO / f"issue-{number}.md").exists()


def test_scripts_exist_confirm_and_never_force_or_read_env() -> None:
    for name in ("seed.ps1", "seed.sh"):
        text = (DEMO / name).read_text(encoding="utf-8")
        code = "\n".join(
            line
            for line in re.sub(r"<#.*?#>", "", text, flags=re.S).splitlines()
            if not line.lstrip().startswith("#")
        )
        assert ".env" not in code
        assert '"yes"' in text
        assert "--force" not in text
        assert "push -f" not in text
        assert "branch -D" not in text
    assert (DEMO / "README.md").exists()
    assert (DEMO / "expected.md").exists()
    for name in ("README.md", "CONTRIBUTING.md", "textkit/core.py", "tests/test_textkit.py"):
        assert (DEMO / "base" / name).exists()


def load_textkit(root: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, root / "textkit" / "core.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_base_has_the_bug_and_the_fix_fixes_it(tmp_path: Path) -> None:
    base = load_textkit(DEMO / "base", "demo_textkit_base")
    assert len(base.truncate("abcdefghij", 5)) > 5  # the documented bug

    fixed_root = tmp_path / "fixed"
    shutil.copytree(DEMO / "base", fixed_root)
    shutil.copytree(PRS[0] / "changes", fixed_root, dirs_exist_ok=True)
    fixed = load_textkit(fixed_root, "demo_textkit_fixed")
    assert fixed.truncate("abcdefghij", 5) == "ab..."
    assert fixed.truncate("abcdefghij", 2) == ".."
    assert fixed.truncate("short", 10) == "short"


@pytest.mark.parametrize("index", [1, 3, 4])
def test_other_prs_do_not_change_behaviour(index: int, tmp_path: Path) -> None:
    root = tmp_path / str(index)
    shutil.copytree(DEMO / "base", root)
    shutil.copytree(PRS[index] / "changes", root, dirs_exist_ok=True)
    module = load_textkit(root, f"demo_textkit_{index}")
    assert module.slugify("Hello, World!") == "hello-world"
    assert len(module.truncate("abcdefghij", 5)) > 5  # still buggy: not a fix
