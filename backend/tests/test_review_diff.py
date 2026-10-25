"""Diff parsing, citable line numbers, rendering and the token budget."""

from __future__ import annotations

from factories import file_change, item_content
from gatekeeper.review_core.diff import build_diff_view, parse_patch, render_file, text_tokens

PATCH = (
    "@@ -10,4 +10,5 @@ def load():\n"
    " keep = 1\n"
    "-old = 2\n"
    "+new = 2\n"
    "+extra = 3\n"
    " tail = 4\n"
    "\\ No newline at end of file\n"
    "@@ -40 +41 @@\n"
    "-a\n"
    "+b"
)


def test_parse_assigns_new_side_line_numbers() -> None:
    first, second = parse_patch(PATCH)
    assert [(line.kind, line.new_line) for line in first.lines] == [
        ("ctx", 10),
        ("del", None),
        ("add", 11),
        ("add", 12),
        ("ctx", 13),
    ]
    assert [(line.kind, line.new_line, line.text) for line in second.lines] == [
        ("del", None, "a"),
        ("add", 41, "b"),
    ]


def test_render_marks_only_new_side_lines_as_citable() -> None:
    rendered = render_file(file_change("src/x.py", patch=PATCH), parse_patch(PATCH))
    lines = rendered.splitlines()
    assert lines[0] == "FILE: src/x.py (modified, +3 -2)"
    assert "L10|  keep = 1" in lines
    assert "    | -old = 2" in lines
    assert "L11| +new = 2" in lines
    assert "L41| +b" in lines


def test_view_orders_by_importance_and_exposes_citable_lines() -> None:
    files = [
        file_change("README.md", patch="@@ -1 +1 @@\n-a\n+b"),
        file_change("package-lock.json", patch="@@ -1 +1 @@\n-a\n+b"),
        file_change("src/x.py", patch=PATCH),
        file_change("logo.png", patch=None, binary=True),
    ]
    view = build_diff_view(item_content(files), budget_tokens=10_000)
    assert view.included == ("src/x.py", "README.md", "package-lock.json")
    assert (
        view.text.index("src/x.py") < view.text.index("README.md") < view.text.index("package-lock")
    )
    assert view.without_patch == ("logo.png",)
    assert "[no diff: binary or too large for GitHub]" in view.text
    assert view.citable["src/x.py"][12] == "extra = 3"
    assert 11 not in view.citable["README.md"]
    assert view.too_large is False
    assert view.estimated_tokens == text_tokens(view.text)


def test_budget_leaves_out_less_important_files() -> None:
    big_doc = "@@ -1 +1,200 @@\n" + "\n".join(f"+doc line {i}" for i in range(200))
    files = [file_change("docs/guide.md", patch=big_doc), file_change("src/x.py", patch=PATCH)]
    view = build_diff_view(item_content(files), budget_tokens=200)
    assert view.included == ("src/x.py",)
    assert view.omitted == ("docs/guide.md",)
    assert "FILES NOT SHOWN (review budget): docs/guide.md" in view.text
    assert view.too_large is False


def test_too_large_when_the_most_important_file_cannot_fit() -> None:
    huge = "@@ -1 +1,500 @@\n" + "\n".join(f"+code {i}" for i in range(500))
    files = [
        file_change("src/huge.py", patch=huge),
        file_change("README.md", patch="@@ -1 +1 @@\n+x"),
    ]
    view = build_diff_view(item_content(files), budget_tokens=100)
    assert view.too_large is True
    assert "src/huge.py" in view.omitted
