"""Classify changed files by path only (no content, no identity)."""

from __future__ import annotations

from enum import IntEnum
from pathlib import PurePosixPath
from typing import Final

LOCKFILES: Final = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lockb",
        "poetry.lock",
        "uv.lock",
        "pipfile.lock",
        "cargo.lock",
        "go.sum",
        "gemfile.lock",
        "composer.lock",
        "packages.lock.json",
        "pubspec.lock",
        "mix.lock",
    }
)
GENERATED_DIRS: Final = frozenset(
    {"vendor", "node_modules", "dist", "build", "third_party", "__generated__", "generated"}
)
GENERATED_SUFFIXES: Final = (".min.js", ".min.css", "_pb2.py", "_pb2_grpc.py", ".pb.go", ".map")
DOC_SUFFIXES: Final = frozenset({".md", ".rst", ".txt", ".adoc", ".mdx"})
DOC_STEMS: Final = frozenset(
    {"readme", "changelog", "license", "contributing", "authors", "notice", "code_of_conduct"}
)
DOC_DIRS: Final = frozenset({"docs", "doc", "documentation"})
TEST_DIRS: Final = frozenset({"test", "tests", "__tests__", "spec", "specs", "testing"})
SOURCE_SUFFIXES: Final = frozenset(
    {
        ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt",
        ".kts", ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".scala",
        ".sh", ".ps1", ".sql", ".vue", ".svelte", ".dart", ".m", ".lua", ".ex", ".exs",
    }
)  # fmt: skip


class FileKind(IntEnum):
    """Also the review order: lower values are shown to the model first."""

    SOURCE = 0
    TEST = 1
    OTHER = 2
    DOCS = 3
    GENERATED = 4


def classify(path: str) -> FileKind:
    pure = PurePosixPath(path.lower())
    name, parts = pure.name, set(pure.parts[:-1])
    if name in LOCKFILES or parts & GENERATED_DIRS or name.endswith(GENERATED_SUFFIXES):
        return FileKind.GENERATED
    if ".generated." in name:
        return FileKind.GENERATED
    is_test_name = (
        name.startswith("test_")
        or pure.stem.endswith(("_test", "_spec"))
        or ".test." in name
        or ".spec." in name
    )
    if parts & TEST_DIRS or is_test_name:
        return FileKind.TEST
    if pure.suffix in DOC_SUFFIXES or pure.stem in DOC_STEMS or parts & DOC_DIRS:
        return FileKind.DOCS
    if pure.suffix in SOURCE_SUFFIXES:
        return FileKind.SOURCE
    return FileKind.OTHER
