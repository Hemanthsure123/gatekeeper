"""Minimal typed views of GitHub REST responses: only the fields Gatekeeper reads."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class _GitHubModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class GitHubUser(_GitHubModel):
    login: str
    id: int
    created_at: datetime | None = None


class Label(_GitHubModel):
    name: str


class RepoRef(_GitHubModel):
    id: int
    full_name: str
    #: Private repositories are never sent to a hosted model unless explicitly allowed.
    private: bool = False


class BranchRef(_GitHubModel):
    sha: str
    repo: RepoRef | None = None


class PullRequest(_GitHubModel):
    number: int
    state: str
    title: str
    body: str | None = None
    draft: bool = False
    merged_at: datetime | None = None
    user: GitHubUser
    author_association: str | None = None
    labels: list[Label] = []
    base: BranchRef
    head: BranchRef
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0


class PullFile(_GitHubModel):
    filename: str
    status: str
    additions: int = 0
    deletions: int = 0
    changes: int = 0
    #: Absent for binary files and for diffs too large for GitHub to include.
    patch: str | None = None


class CheckRun(_GitHubModel):
    name: str
    status: str
    conclusion: str | None = None


class Repository(_GitHubModel):
    full_name: str
    private: bool = False


class PullSummary(_GitHubModel):
    number: int
    title: str
    draft: bool = False


class RateLimit(_GitHubModel):
    limit: int
    remaining: int
    reset: int


class PullFiles(_GitHubModel):
    files: list[PullFile]
    #: True when we stopped paginating at PR_MAX_FILES (GitHub itself caps at 3000).
    truncated: bool
