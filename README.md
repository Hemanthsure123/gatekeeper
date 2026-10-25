# Gatekeeper

**Contributor-blind pull request triage for open-source maintainers.** Gatekeeper reviews a pull request with an open-weight model (Gemma 4) and shows the result in a Chromium side panel next to the PR:

- a neutral verdict;
- evidence that code has checked line by line;
- a live view of what it is doing.

It never sees who wrote the change.

## Why

Maintainers of popular projects now get more pull requests than they can read. Many are low-effort: whitespace churn, "fixes" that do not match their description, generated noise. The common shortcuts judge the **person** instead: account age, follower count, "first-time contributor" badges. That is unfair to newcomers and easy to game.

Gatekeeper judges the **work**:

- **Contributor-blind by construction.** The author's login, mentions, profile links and e-mail addresses are scrubbed before anything is stored or reviewed. A leak guard blocks any model call that still contains the author's login, and an AST test keeps the review code from importing the identity module. Author names and avatars are never shown in the extension.
- **Evidence, not vibes.** The model must cite new-side diff lines with exact quotes. Code verifies every citation and drops the ones that do not hold. A "Likely low value" verdict needs verified evidence of a concern.
- **Unsure means abstain.** Low confidence, truncated input or invalid model output become "Needs a human look", never a guess.
- **People decide.** Nothing is posted automatically. The only write to GitHub is one comment, after a maintainer reviews and approves its exact text.
- **Every decision is replayable.** An append-only, hash-chained audit trail records every step, including exactly what the model saw.

## How it works

```text
 GitHub PR ──(read-only API)──► normalise ──► identity scrub ──► store (identity-free)
                                                                     │
        ┌────────────────────────── review core (contributor-blind) ─┘
        │  signals (code) ─► one Gemma 4 call ─► verify citations (code) ─► confidence (code)
        ▼
  verdict + verified evidence + reply draft
        │
        ├──► audit trail (append-only, hash-chained; the source of truth)
        ├──► activity feed ──(SSE)──► side panel: live tree, timeline, "Now: reading src/x.py"
        └──► reply draft ──► a maintainer approves the exact text ──► one GitHub comment
```

Reviews run on a Postgres-backed queue and worker. A review is started from the side panel, or by a GitHub webhook.

## Features

- **Side panel with three tabs:**
  - **Activity**:
    - the verdict card (verdict, confidence bar, abstain note, rationale and reply draft);
    - a live "Now:" line;
    - the repository tree, where each changed file shows its stage: listed, signalled, in context or omitted, cited, verified or dropped;
    - a timeline of real backend events.
  - **Chat**: grounded, read-only questions about the review ("Why this verdict?", "What should the contributor fix?"). Every line reference is checked against the diff.
  - **Insights**: KPIs, two small charts, recent analyses, and **what the model saw**: the redacted model input, straight from the audit trail.
- **Reveal in GitHub**: clicking a file or an evidence line range scrolls to it in the PR's Files changed page and highlights it.
- **Neutral wording**: "Looks valuable", "Needs a human look", "Likely low value". Never "spam"; a banned-word filter guards every contributor-facing string.
- **Hosted or local models**: Gemma 4 through the Gemini API with a rotating key pool, or a local model through Ollama.

## Run it

Prerequisites:

- [uv](https://docs.astral.sh/uv/) (it installs Python 3.13);
- GNU make (Windows: `winget install ezwinports.make`);
- Docker with Compose v2;
- Node.js 20 or newer (for the extension build);
- a Chromium browser (Chrome or Edge);
- a GitHub token. A fine-grained token is recommended. It needs Contents, Pull requests and Issues **read**, plus Issues **or** Pull requests **write** if you want to post approved comments.

```sh
make setup          # Python and dev tools
make init-env       # creates .env with generated local secrets (never overwrites)
#   then add your secrets to .env yourself: GEMINI_API_KEYS, GITHUB_TOKEN_DEV
make gemma-setup    # checks each Gemini key by alias and selects the Gemma 4 models
make env-set ARGS="GATEKEEPER_ALLOWED_REPOS=<owner>/<repo>"
make stack          # starts Postgres and Redis, migrates, runs the API and one worker
```

In a second terminal:

```sh
make extension-build   # builds extension/dist
```

Then load and connect the extension:

1. In Chrome, open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked** and choose `extension/dist`.
2. Open the extension's **Options**:
   - paste the value of `GATEKEEPER_ADMIN_TOKEN` from your `.env` (`make init-env` generated it);
   - click **Test connection**.
3. Open a pull request in an allowlisted repository, click the Gatekeeper toolbar icon, then **Analyse**.

A full five-minute demo script, using a synthetic playground with five seeded PRs, is in [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md). The playground pack itself is in [demo/README.md](demo/README.md).

Useful commands:

| Command | What it does |
| --- | --- |
| `make dev` | API plus one worker. It checks Postgres, Redis and migrations first |
| `uv run gatekeeper fetch-pr <owner>/<repo>#<n>` | Review a PR from the terminal |
| `uv run gatekeeper show <analysis-id>` | Print a stored review |
| `uv run gatekeeper audit <run-id>` | Print a run's audit events and verify the hash chain |
| `make model-check` | Check the model backend with two structured calls |

## Models and dependencies

- **Default model:** Gemma 4 (`gemma-4-26b-a4b-it` primary, `gemma-4-31b-it` secondary), hosted on Google's Gemini API (`MODEL_BACKEND=gemini_api`).
  - Several API keys can be pooled; they rotate with cooldowns, and a rate-limited request moves to the next key.
- **Local option:** any Gemma model through [Ollama](https://ollama.com) (`MODEL_BACKEND=ollama`). Nothing leaves your machine in this mode.
- **Gemma licensing:**
  - Google releases Gemma 4 under the [Apache 2.0 license](https://ai.google.dev/gemma/apache_2).
  - Earlier Gemma models are covered by the [Gemma Terms of Use](https://ai.google.dev/gemma/terms) and the [Gemma Prohibited Use Policy](https://ai.google.dev/gemma/prohibited_use_policy).
  - The Gemini API service has its own terms from Google.
- **Backend:** Python 3.13, FastAPI, SQLAlchemy and Alembic on PostgreSQL 16, Redis 7 (model cache), httpx and google-genai.
- **Extension:** TypeScript and Manifest V3, built with Vite. It uses no UI framework and loads no remote code.

## Data handling

- **Hosted mode** (`gemini_api`) sends the scrubbed pull request content to Google's Gemini API: title, description, diff and computed signals, with the author's login, mentions, profile links and e-mail addresses removed. Chat questions are scrubbed the same way.
- **Private repositories are refused** in hosted mode. When a review is blocked for that reason, the side panel offers **Allow hosted review for this private repository**:
  - you tick a box that states what is sent, for that repository only;
  - the choice is recorded in the audit trail, and you can revoke it;
  - `ALLOW_PRIVATE_REPOS_ON_HOSTED_MODEL=true` allows all private repositories at once.
- **Local mode** (`ollama`) keeps everything on your machine.
- **Secrets** live only in `.env`. The extension's access token is stored only in your browser profile.
- **API keys** are referred to only as `key-1`, `key-2`, and so on. They are masked in logs and audit events.
- **Network:** the API binds to `127.0.0.1` by default. The extension calls it only from its side panel, never from GitHub pages.

## Honest limitations

- **Confidence is a heuristic.** It combines model self-confidence, citation validity, signal agreement and completeness, and it has not been calibrated.
- **There is no benchmark yet.** The only data so far is a five-PR synthetic demo pack; results on real repositories are untested at scale.
- **Model answers vary.** The verifier and the abstain rules limit the damage, but a run can still disagree with a human.
- **The scrub is not perfect.** It removes the author's login, mentions, profile URLs and e-mails. It cannot catch real names or other handles written in free text.
- **GitHub's page markup is undocumented.** "Reveal in GitHub" relies on page structure and anchors that GitHub can change. It falls back to the file's own page.
- **Chromium only.** There is no Firefox build. The extension is loaded unpacked; it is not on the Chrome Web Store.
- **Local and single-user.** Only one worker has been run so far.

## Roadmap

- A labelled benchmark of real pull requests and a calibrated confidence score.
- Issue and comment triage with the same contributor-blind core.
- A policy-gated action executor (labels) with a dry run by default.
- Key-pool cooldowns shared through Redis, for several workers.
- A packaged extension and a hosted demo.

## Repository layout

```text
backend/     Python package (src/gatekeeper): API, pipeline, review core, models, audit
extension/   Chromium side panel (TypeScript, Manifest V3, Vite)
demo/        synthetic playground: a tiny package, one issue, five PRs, seed scripts
scripts/     developer tooling (env, doctor, dev runner, extension build)
docs/        architecture, ADRs, build logs and the walkthrough
```

Architecture: [docs/architecture.md](docs/architecture.md). Decisions: [docs/adr/](docs/adr/). Rules for contributors (human or AI): [CLAUDE.md](CLAUDE.md) and [CONTRIBUTING.md](CONTRIBUTING.md). Security: [SECURITY.md](SECURITY.md).

## License

[MIT License](LICENSE). Copyright (c) 2026 Hemanth Sure.
