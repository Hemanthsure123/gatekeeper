"""Read-only GitHub account commands: ``whoami``, ``discover-repo`` and ``list-prs``.

They never print the token. Missing credentials produce an ACTION NEEDED block.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from gatekeeper.action_needed import action_needed
from gatekeeper.config import Settings
from gatekeeper.envfile import set_values
from gatekeeper.errors import ValidationFailed
from gatekeeper.github.auth import auth_from_settings
from gatekeeper.github.client import GitHubClient

MAX_LISTED_REPOS: Final = 20
_REPO = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)/([A-Za-z0-9._-]+)$")


def missing_token_block() -> str:
    return action_needed(
        "Add a read-only GitHub token for your playground repository",
        "Gatekeeper reads pull requests with it; no GitHub credentials are configured.",
        [
            "Open https://github.com/settings/personal-access-tokens/new (fine-grained token).",
            "Repository access: Only select repositories -> your playground repo.",
            "Permissions: Pull requests: Read-only, Issues: Read-only, Contents: Read-only "
            "(Metadata: Read-only is added automatically).",
            "Generate the token and paste it after GITHUB_TOKEN_DEV= in .env.",
        ],
        "GITHUB_TOKEN_DEV",
    )


def has_credentials(settings: Settings) -> bool:
    app = settings.github_app_id is not None and settings.github_app_private_key_path is not None
    return app or settings.github_token_dev is not None


def parse_repo(text: str) -> tuple[str, str]:
    match = _REPO.match(text.strip())
    if not match:
        raise ValidationFailed("expected a repository as owner/name")
    return match[1], match[2]


def cmd_whoami(settings: Settings, client: GitHubClient | None = None) -> int:
    if not has_credentials(settings):
        print(missing_token_block())
        return 2
    mode = auth_from_settings(settings).mode
    if mode == "app":
        print("Auth mode: GitHub App (installation tokens are per repository).")
        print("Account checks need a personal token; use fetch-pr to test App access.")
        return 0
    client = client or GitHubClient(settings)
    login = client.authenticated_login()
    limit = client.rate_limit()
    reset = datetime.fromtimestamp(limit.reset, UTC).strftime("%H:%M UTC")
    repos = client.accessible_repos()
    print(f"Account:    {login}")
    print("Auth mode:  token (GITHUB_TOKEN_DEV)")
    print(f"Rate limit: {limit.remaining}/{limit.limit} remaining (resets {reset})")
    print(f"Repositories this token can read ({len(repos)}):")
    for repo in repos[:MAX_LISTED_REPOS]:
        print(f"  - {repo.full_name}{' (private)' if repo.private else ''}")
    if len(repos) > MAX_LISTED_REPOS:
        print(f"  ... and {len(repos) - MAX_LISTED_REPOS} more")

    allowed = settings.allowed_repos
    target = next((r.full_name for r in repos if r.full_name.lower() in allowed), None)
    if target is None and len(repos) == 1:
        target = repos[0].full_name
    if target is None:
        print("Access check: cannot tell without a single target repository.")
        return 0
    owner, name = target.split("/", 1)
    for label, resource in (("Pull requests", "pulls"), ("Issues", "issues")):
        state = client.can_read(owner, name, resource)
        text = {True: "readable", False: "NOT readable", None: "cannot tell"}[state]
        print(f"{label} on {target}: {text}")
    return 0


def cmd_discover_repo(
    settings: Settings, client: GitHubClient | None = None, env_path: Path = Path(".env")
) -> int:
    if not has_credentials(settings):
        print(missing_token_block())
        return 2
    if settings.allowed_repos:
        print(
            f"GATEKEEPER_ALLOWED_REPOS is already set: {', '.join(sorted(settings.allowed_repos))}"
        )
        return 0
    repos = (client or GitHubClient(settings)).accessible_repos()
    if not repos:
        print(
            action_needed(
                "Give the token access to your playground repository",
                "The token cannot read any repository.",
                [
                    "Create the public repository gatekeeper-playground if it does not exist.",
                    "Edit the fine-grained token: Repository access -> Only select repositories "
                    "-> gatekeeper-playground.",
                    "Run uv run gatekeeper discover-repo again.",
                ],
            )
        )
        return 1
    if len(repos) == 1:
        set_values([("GATEKEEPER_ALLOWED_REPOS", repos[0].full_name)], env_path)
        print(f"set GATEKEEPER_ALLOWED_REPOS ({repos[0].full_name})")
        return 0
    print(f"The token can read {len(repos)} repositories; choose the one Gatekeeper may review:")
    for repo in repos[:MAX_LISTED_REPOS]:
        print(f'  make env-set ARGS="GATEKEEPER_ALLOWED_REPOS={repo.full_name}"')
    return 0


def cmd_list_prs(settings: Settings, repo: str, client: GitHubClient | None = None) -> int:
    if not has_credentials(settings):
        print(missing_token_block())
        return 2
    owner, name = parse_repo(repo)
    pulls = (client or GitHubClient(settings)).open_pulls(owner, name)
    if not pulls:
        print(f"No open pull requests in {owner}/{name}.")
        return 0
    for pull in pulls:
        print(f"#{pull.number:<5} {'[draft] ' if pull.draft else ''}{pull.title}")
    return 0
