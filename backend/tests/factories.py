"""Synthetic test data builders shared by the review-core tests. No real people or repos."""

from __future__ import annotations

from typing import Any

from gatekeeper.github.normalize import CheckSummary, FileChange, ItemContent, Totals


def file_change(path: str = "src/app.py", patch: str | None = None, **overrides: Any) -> FileChange:
    values: dict[str, Any] = {
        "path": path,
        "status": "modified",
        "additions": sum(1 for line in (patch or "").splitlines() if line.startswith("+")),
        "deletions": sum(1 for line in (patch or "").splitlines() if line.startswith("-")),
        "patch": patch,
        "patch_missing": patch is None,
        "patch_omitted": False,
        "binary": False,
    }
    values.update(overrides)
    return FileChange(**values)


def item_content(
    files: list[FileChange] | None = None,
    *,
    title: str = "Improve parser error messages",
    body: str = "Clearer errors when the config file is malformed. Fixes #12.",
    checks: list[CheckSummary] | None = None,
    referenced_issues: list[int] | None = None,
    truncation_reasons: list[str] | None = None,
) -> ItemContent:
    files = files if files is not None else [file_change(patch="@@ -1,2 +1,2 @@\n-old\n+new\n ctx")]
    reasons = truncation_reasons or []
    return ItemContent(
        title=title,
        body=body,
        base_sha="a" * 40,
        head_sha="b" * 40,
        draft=False,
        labels=[],
        files=files,
        totals=Totals(
            files=len(files),
            additions=sum(f.additions for f in files),
            deletions=sum(f.deletions for f in files),
        ),
        diff_truncated=bool(reasons),
        truncation_reasons=reasons,
        checks=checks or [],
        referenced_issues=referenced_issues if referenced_issues is not None else [12],
        image_urls=[],
    )
