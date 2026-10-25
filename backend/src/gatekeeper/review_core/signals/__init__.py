"""Deterministic signals: small, explainable facts about the change.

Inputs are identity-free only (the scrubbed ``ItemContent`` and what the diff renderer
left out). No rule may use the author, account age, reputation or any identity data.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from gatekeeper.github.normalize import ItemContent
from gatekeeper.review_core.paths import FileKind, classify
from gatekeeper.review_core.signals import thresholds as t

Direction = Literal["positive", "negative", "neutral"]
Severity = Literal["low", "medium", "high"]
SEVERITY_WEIGHT: Final = {"low": 1, "medium": 2, "high": 3}


class EvidenceRef(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    line_start: int | None = None
    line_end: int | None = None


class SignalResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    value: bool | int | float
    direction: Direction
    severity: Severity
    explanation: str
    evidence: tuple[EvidenceRef, ...] = ()


@dataclass(frozen=True)
class SignalInput:
    content: ItemContent
    #: Files the diff renderer had to leave out of the model prompt.
    omitted_paths: tuple[str, ...] = ()


Rule = Callable[[SignalInput], SignalResult]

_WHITESPACE = re.compile(r"\s+")


def changed_lines(patch: str) -> tuple[list[str], list[str]]:
    """(removed, added) line texts from a unified patch, without the +/- markers."""
    removed: list[str] = []
    added: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:])
    return removed, added


def _files_with_patches(content: ItemContent) -> list[tuple[str, list[str], list[str]]]:
    return [(f.path, *changed_lines(f.patch)) for f in content.files if f.patch is not None]


def _result(
    id_: str,
    value: bool | float,
    direction: Direction,
    severity: Severity,
    explanation: str,
    evidence: Sequence[EvidenceRef] = (),
) -> SignalResult:
    return SignalResult(
        id=id_,
        value=value,
        direction=direction,
        severity=severity,
        explanation=explanation,
        evidence=tuple(evidence),
    )


# -- rules ---------------------------------------------------------------------------------


def tiny_change(inp: SignalInput) -> SignalResult:
    totals = inp.content.totals
    changed = totals.additions + totals.deletions
    description = len(inp.content.body.strip())
    if changed <= t.TINY_CHANGE_MAX_LINES and description >= t.TINY_CHANGE_LONG_DESCRIPTION:
        return _result(
            "tiny_change",
            changed,
            "negative",
            "low",
            f"The change touches {changed} line(s) but the description is {description} "
            "characters long.",
        )
    return _result(
        "tiny_change", changed, "neutral", "low", f"The change touches {changed} line(s)."
    )


def whitespace_only(inp: SignalInput) -> SignalResult:
    total = whitespace = 0
    for _path, removed, added in _files_with_patches(inp.content):
        total += len(removed) + len(added)
        old = Counter(_WHITESPACE.sub("", line) for line in removed)
        new = Counter(_WHITESPACE.sub("", line) for line in added)
        whitespace += old.get("", 0) + new.get("", 0)
        old.pop("", None)
        new.pop("", None)
        whitespace += 2 * sum((old & new).values())
    share = round(whitespace / total, 2) if total else 0.0
    if total and share >= 1.0:
        return _result(
            "whitespace_only",
            share,
            "negative",
            "high",
            "Every changed line differs only in whitespace.",
        )
    if total and share >= t.WHITESPACE_WARN_SHARE:
        return _result(
            "whitespace_only",
            share,
            "negative",
            "medium",
            f"About {round(share * 100)}% of the changed lines differ only in whitespace.",
        )
    return _result(
        "whitespace_only", share, "neutral", "low", "Most changes are not whitespace-only."
    )


def _docs_only(content: ItemContent) -> bool:
    return bool(content.files) and all(classify(f.path) is FileKind.DOCS for f in content.files)


def docs_only(inp: SignalInput) -> SignalResult:
    if _docs_only(inp.content):
        return _result("docs_only", True, "neutral", "low", "Only documentation files changed.")
    return _result("docs_only", False, "neutral", "low", "Not only documentation changed.")


def typo_scale(inp: SignalInput) -> SignalResult:
    if not _docs_only(inp.content):
        return _result("typo_scale", 0, "neutral", "low", "Not a documentation-only change.")
    chars = 0
    for _path, removed, added in _files_with_patches(inp.content):
        for old, new in zip(removed, added, strict=False):
            matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
            same = sum(block.size for block in matcher.get_matching_blocks())
            chars += len(old) + len(new) - 2 * same
        for extra in removed[len(added) :] + added[len(removed) :]:
            chars += len(extra)
    if chars <= t.TYPO_MAX_CHARS:
        return _result(
            "typo_scale",
            chars,
            "negative",
            "low",
            f"Only about {chars} character(s) of documentation changed.",
        )
    return _result(
        "typo_scale", chars, "neutral", "low", f"About {chars} characters of documentation changed."
    )


def tests_touched(inp: SignalInput) -> SignalResult:
    kinds = [classify(f.path) for f in inp.content.files]
    source, tests = FileKind.SOURCE in kinds, FileKind.TEST in kinds
    if source and tests:
        return _result(
            "tests_touched", True, "positive", "medium", "Source changes come with test changes."
        )
    if source:
        return _result(
            "tests_touched", False, "negative", "low", "Source files changed without test changes."
        )
    return _result("tests_touched", tests, "neutral", "low", "No source files changed.")


def ci_status(inp: SignalInput) -> SignalResult:
    checks = inp.content.checks
    if not checks:
        return _result("ci_status", 0.0, "neutral", "low", "No CI checks were reported.")
    failing = [c for c in checks if (c.conclusion or "") in t.FAILING_CONCLUSIONS]
    if failing:
        names = ", ".join(sorted({c.name for c in failing})[:3])
        return _result(
            "ci_status", -1.0, "negative", "medium", f"Some CI checks are failing ({names})."
        )
    if any(c.status != "completed" for c in checks):
        return _result("ci_status", 0.0, "neutral", "low", "CI checks are still running.")
    if all((c.conclusion or "") in t.PASSING_CONCLUSIONS for c in checks):
        return _result("ci_status", 1.0, "positive", "low", "All CI checks passed.")
    return _result("ci_status", 0.0, "neutral", "low", "CI checks finished with mixed results.")


def linked_issue(inp: SignalInput) -> SignalResult:
    issues = inp.content.referenced_issues
    if issues:
        refs = ", ".join(f"#{n}" for n in issues[:5])
        return _result(
            "linked_issue", len(issues), "positive", "low", f"The description links {refs}."
        )
    return _result(
        "linked_issue", 0, "neutral", "low", "No issue is linked with a closing keyword."
    )


def description_present(inp: SignalInput) -> SignalResult:
    length = len(inp.content.body.strip())
    if length == 0:
        return _result("description_present", 0, "negative", "medium", "The description is empty.")
    if length < t.SHORT_DESCRIPTION_CHARS:
        return _result(
            "description_present", length, "negative", "low", "The description is very short."
        )
    return _result(
        "description_present", length, "positive", "low", "The change has a description."
    )


def lockfile_or_generated_only(inp: SignalInput) -> SignalResult:
    files = inp.content.files
    if files and all(classify(f.path) is FileKind.GENERATED for f in files):
        return _result(
            "lockfile_or_generated_only",
            True,
            "negative",
            "medium",
            "Only lockfiles, generated or vendored files changed.",
        )
    return _result(
        "lockfile_or_generated_only", False, "neutral", "low", "Hand-written files changed."
    )


def truncated_input(inp: SignalInput) -> SignalResult:
    reasons = list(inp.content.truncation_reasons)
    if inp.omitted_paths:
        reasons.append(f"{len(inp.omitted_paths)} file(s) left out of the review prompt")
    if reasons:
        return _result(
            "truncated_input",
            True,
            "neutral",
            "high",
            f"Part of the change could not be reviewed ({'; '.join(reasons)}).",
            [EvidenceRef(path=path) for path in inp.omitted_paths[:10]],
        )
    return _result("truncated_input", False, "neutral", "low", "The whole change was available.")


REGISTRY: Final[tuple[Rule, ...]] = (
    tiny_change,
    whitespace_only,
    docs_only,
    typo_scale,
    tests_touched,
    ci_status,
    linked_issue,
    description_present,
    lockfile_or_generated_only,
    truncated_input,
)


def compute_signals(inp: SignalInput) -> list[SignalResult]:
    return [rule(inp) for rule in REGISTRY]


def signal_summary(signals: Sequence[SignalResult]) -> str:
    """Compact text for the prompt; one line per signal."""
    return "\n".join(f"- {s.id} [{s.direction}, {s.severity}]: {s.explanation}" for s in signals)


def is_truncated(signals: Sequence[SignalResult]) -> bool:
    return any(s.id == "truncated_input" and s.value is True for s in signals)
