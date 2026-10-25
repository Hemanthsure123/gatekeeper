# Demo pack: a synthetic playground for Gatekeeper

Everything in this folder is synthetic: no real people, secrets or repositories.

- `base/` is the playground's `main` branch: a tiny `textkit` package with **one real, subtle bug** (`truncate` can exceed the requested width), tests, a README and CONTRIBUTING.md.
- `issue-1.md` is the issue describing that bug.
- `prs/` holds five pull requests. Each has `branch.txt`, `title.txt`, `body.md` and `changes/` (full replacement files).
- `expected.md` lists the expected verdict ranges, as an expectation rather than a test.
- `seed.ps1` (Windows) and `seed.sh` push the playground. **You run them yourself**: they use your git credentials and never read `.env`.

## Five steps

1. Create an empty **public** repository named `gatekeeper-playground` on GitHub, without a README. The seed script only writes `main` when the repository has no commits.
2. Run the seed script and type `yes` when asked:
   - Windows: `powershell -ExecutionPolicy Bypass -File demo\seed.ps1 <owner>/gatekeeper-playground`
   - macOS or Linux: `bash demo/seed.sh <owner>/gatekeeper-playground`
3. Create issue **#1** using the title and body from `demo/issue-1.md`, before opening the PRs.
4. Open the five PRs from the printed compare URLs. If the GitHub CLI is installed and logged in, the script offers to open them for you. To see how the review handles different authors, ask a teammate to open some of them from their own account.
5. Set the allowlist and review the PRs:

   ```sh
   uv run gatekeeper discover-repo   # or: make env-set ARGS="GATEKEEPER_ALLOWED_REPOS=<owner>/gatekeeper-playground"
   uv run gatekeeper list-prs <owner>/gatekeeper-playground
   uv run gatekeeper fetch-pr <owner>/gatekeeper-playground#2
   ```
