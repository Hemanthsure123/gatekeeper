"""Each deterministic signal, on synthetic changes."""

from __future__ import annotations

import pytest

from factories import file_change, item_content
from gatekeeper.github.normalize import CheckSummary
from gatekeeper.review_core.paths import FileKind, classify
from gatekeeper.review_core.signals import (
    REGISTRY,
    SignalInput,
    SignalResult,
    compute_signals,
    is_truncated,
    signal_summary,
)


def signal(id_: str, inp: SignalInput) -> SignalResult:
    (result,) = [s for s in compute_signals(inp) if s.id == id_]
    return result


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("src/app.py", FileKind.SOURCE),
        ("tests/test_app.py", FileKind.TEST),
        ("web/app.spec.ts", FileKind.TEST),
        ("README.md", FileKind.DOCS),
        ("docs/guide/setup.html", FileKind.DOCS),
        ("package-lock.json", FileKind.GENERATED),
        ("vendor/lib/x.go", FileKind.GENERATED),
        ("static/app.min.js", FileKind.GENERATED),
        ("pyproject.toml", FileKind.OTHER),
    ],
)
def test_path_classification(path: str, kind: FileKind) -> None:
    assert classify(path) is kind


def test_tiny_change_with_long_description() -> None:
    content = item_content(body="x" * 600, files=[file_change(patch="@@ -1 +1 @@\n-a\n+b")])
    result = signal("tiny_change", SignalInput(content))
    assert (result.direction, result.value) == ("negative", 2)
    assert signal("tiny_change", SignalInput(item_content())).direction == "neutral"


def test_whitespace_only() -> None:
    patch = "@@ -1,3 +1,3 @@\n-def f():\n-    return 1\n+def f():  \n+\treturn 1\n-\n+"
    result = signal("whitespace_only", SignalInput(item_content([file_change(patch=patch)])))
    assert (result.value, result.direction, result.severity) == (1.0, "negative", "high")
    real = signal("whitespace_only", SignalInput(item_content()))
    assert real.direction == "neutral"


def test_docs_only_and_typo_scale() -> None:
    typo = file_change("README.md", patch="@@ -1 +1 @@\n-Teh install guide\n+The install guide")
    inp = SignalInput(item_content([typo]))
    assert signal("docs_only", inp).value is True
    result = signal("typo_scale", inp)
    assert (result.direction, result.severity) == ("neutral", "low")
    assert result.explanation.startswith("Only about")
    assert int(result.value) <= 4
    assert signal("typo_scale", SignalInput(item_content())).direction == "neutral"


@pytest.mark.parametrize(
    ("paths", "direction"),
    [
        (["src/a.py", "tests/test_a.py"], "positive"),
        (["src/a.py"], "negative"),
        (["README.md"], "neutral"),
    ],
)
def test_tests_touched(paths: list[str], direction: str) -> None:
    files = [file_change(p, patch="@@ -1 +1 @@\n-a\n+b") for p in paths]
    assert signal("tests_touched", SignalInput(item_content(files))).direction == direction


@pytest.mark.parametrize(
    ("checks", "value", "direction"),
    [
        ([], 0.0, "neutral"),
        ([CheckSummary(name="ci", status="completed", conclusion="success")], 1.0, "positive"),
        ([CheckSummary(name="ci", status="completed", conclusion="failure")], -1.0, "negative"),
        ([CheckSummary(name="ci", status="in_progress", conclusion=None)], 0.0, "neutral"),
    ],
)
def test_ci_status(checks: list[CheckSummary], value: float, direction: str) -> None:
    result = signal("ci_status", SignalInput(item_content(checks=checks)))
    assert (result.value, result.direction) == (value, direction)


def test_linked_issue_and_description() -> None:
    assert signal("linked_issue", SignalInput(item_content())).direction == "positive"
    unlinked = SignalInput(item_content(referenced_issues=[]))
    assert signal("linked_issue", unlinked).direction == "neutral"
    assert signal("description_present", SignalInput(item_content(body=""))).severity == "medium"
    assert (
        signal("description_present", SignalInput(item_content(body="fix"))).direction == "negative"
    )
    assert signal("description_present", SignalInput(item_content())).direction == "positive"


def test_lockfile_or_generated_only() -> None:
    files = [file_change("package-lock.json", patch="@@ -1 +1 @@\n-a\n+b")]
    assert signal("lockfile_or_generated_only", SignalInput(item_content(files))).value is True
    assert signal("lockfile_or_generated_only", SignalInput(item_content())).value is False


def test_truncated_input_from_content_or_renderer() -> None:
    truncated = SignalInput(item_content(truncation_reasons=["diff_max_bytes"]))
    assert signal("truncated_input", truncated).value is True
    omitted = SignalInput(item_content(), omitted_paths=("src/big.py",))
    result = signal("truncated_input", omitted)
    assert (result.value, result.severity) == (True, "high")
    assert result.evidence[0].path == "src/big.py"
    assert is_truncated([result])
    assert not is_truncated(compute_signals(SignalInput(item_content())))


def test_registry_and_summary() -> None:
    signals = compute_signals(SignalInput(item_content()))
    assert len(signals) == len(REGISTRY) == 10
    summary = signal_summary(signals)
    assert summary.count("\n") == 9
    assert "- linked_issue [positive, low]" in summary
