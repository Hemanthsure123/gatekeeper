"""Parse unified patches and render a citable, budgeted diff for the review prompt.

Rendering: added and context lines carry their NEW-side line number (``L42| +code``,
``L43|  code``); removed lines carry none (``    | -code``). Only new-side lines are
citable, and the verifier checks citations against exactly these lines.

Budget: files are ordered source, tests, other, docs, generated (then by their order in
the PR) and included whole until the token budget is used; the rest are listed as left
out. If even the most important file does not fit, the view is ``too_large`` and the
review abstains without a model call.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from gatekeeper.github.normalize import FileChange, ItemContent
from gatekeeper.models.adapter import BYTES_PER_TOKEN
from gatekeeper.review_core.paths import classify

HUNK_HEADER: Final = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
REMOVED_PREFIX: Final = "    | -"


@dataclass(frozen=True)
class DiffLine:
    kind: Literal["add", "del", "ctx"]
    text: str
    new_line: int | None


@dataclass(frozen=True)
class Hunk:
    header: str
    lines: tuple[DiffLine, ...]


@dataclass(frozen=True)
class DiffView:
    text: str
    included: tuple[str, ...]
    omitted: tuple[str, ...]
    without_patch: tuple[str, ...]
    #: path -> {new-side line number -> line text} for every included file.
    citable: Mapping[str, Mapping[int, str]] = field(default_factory=dict)
    estimated_tokens: int = 0
    too_large: bool = False


def text_tokens(text: str) -> int:
    """The adapter's conservative estimate (UTF-8 bytes / 3), without call overhead."""
    return math.ceil(len(text.encode("utf-8")) / BYTES_PER_TOKEN)


def parse_patch(patch: str) -> list[Hunk]:
    hunks: list[Hunk] = []
    header: str | None = None
    lines: list[DiffLine] = []
    new_line = 0
    for raw in patch.splitlines():
        match = HUNK_HEADER.match(raw)
        if match:
            if header is not None:
                hunks.append(Hunk(header, tuple(lines)))
            header, lines, new_line = raw, [], int(match[3])
            continue
        if header is None or raw.startswith("\\"):  # preamble, or "\ No newline at end of file"
            continue
        if raw.startswith("+"):
            lines.append(DiffLine("add", raw[1:], new_line))
            new_line += 1
        elif raw.startswith("-"):
            lines.append(DiffLine("del", raw[1:], None))
        else:
            lines.append(DiffLine("ctx", raw[1:] if raw.startswith(" ") else raw, new_line))
            new_line += 1
    if header is not None:
        hunks.append(Hunk(header, tuple(lines)))
    return hunks


def citable_lines(hunks: list[Hunk]) -> dict[int, str]:
    return {
        line.new_line: line.text
        for hunk in hunks
        for line in hunk.lines
        if line.new_line is not None
    }


def render_file(file: FileChange, hunks: list[Hunk]) -> str:
    out = [f"FILE: {file.path} ({file.status}, +{file.additions} -{file.deletions})"]
    for hunk in hunks:
        out.append(hunk.header)
        for line in hunk.lines:
            if line.kind == "add":
                out.append(f"L{line.new_line}| +{line.text}")
            elif line.kind == "ctx":
                out.append(f"L{line.new_line}|  {line.text}")
            else:
                out.append(f"{REMOVED_PREFIX}{line.text}")
    return "\n".join(out)


def _no_patch_line(file: FileChange) -> str:
    why = "omitted (diff size limit)" if file.patch_omitted else "binary or too large for GitHub"
    return (
        f"FILE: {file.path} ({file.status}, +{file.additions} -{file.deletions}) [no diff: {why}]"
    )


def build_diff_view(content: ItemContent, budget_tokens: int) -> DiffView:
    ordered = sorted(enumerate(content.files), key=lambda pair: (classify(pair[1].path), pair[0]))
    with_patch = [(f, patch) for _, f in ordered if (patch := f.patch) is not None]
    without = [f for _, f in ordered if f.patch is None]

    sections: list[str] = []
    citable: dict[str, Mapping[int, str]] = {}
    included: list[str] = []
    omitted: list[str] = []
    used = 0
    too_large = False
    for index, (file, patch) in enumerate(with_patch):
        hunks = parse_patch(patch)
        rendered = render_file(file, hunks)
        cost = text_tokens(rendered)
        if used + cost <= budget_tokens:
            sections.append(rendered)
            citable[file.path] = citable_lines(hunks)
            included.append(file.path)
            used += cost
        else:
            omitted.append(file.path)
            too_large = too_large or index == 0

    sections.extend(_no_patch_line(f) for f in without)
    if omitted:
        sections.append("FILES NOT SHOWN (review budget): " + ", ".join(omitted))
    text = "\n\n".join(sections)
    return DiffView(
        text=text,
        included=tuple(included),
        omitted=tuple(omitted),
        without_patch=tuple(f.path for f in without),
        citable=citable,
        estimated_tokens=text_tokens(text),
        too_large=too_large,
    )
