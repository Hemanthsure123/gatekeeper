# Gatekeeper: five-minute demo walkthrough

This script uses the synthetic playground (`demo/`): a tiny Python package with one real bug, issue #1, and five pull requests designed to show different outcomes. Everything in it is synthetic.

> **Expectations, not guarantees.** Each review comes from a single model call, and the model can answer differently between runs. When a run disagrees with the table below, show it: the evidence, the confidence and the abstain rules are the point of the demo.

## Before the demo (about 15 minutes, once)

1. **Configure** (secrets go into `.env` by hand, never through a command):

   ```sh
   make setup
   make init-env                 # generates GATEKEEPER_ADMIN_TOKEN and local secrets
   # add GEMINI_API_KEYS and GITHUB_TOKEN_DEV to .env yourself
   make gemma-setup              # checks each key by alias and selects the Gemma 4 models
   make model-check              # run twice: the second call should be a cache hit
   ```

2. **Seed the playground** with the steps in [demo/README.md](../demo/README.md):
   1. Create an empty public repository `gatekeeper-playground`.
   2. Run `demo/seed.ps1` (Windows) or `demo/seed.sh`.
   3. Create issue #1 from `demo/issue-1.md`.
   4. Open the five PRs.

   Then set the allowlist:

   ```sh
   make env-set ARGS="GATEKEEPER_ALLOWED_REPOS=<owner>/gatekeeper-playground"
   uv run gatekeeper list-prs <owner>/gatekeeper-playground
   ```

3. **Start everything** (leave it running):

   ```sh
   make stack                    # Postgres + Redis, migrations, API + worker with [api]/[worker] logs
   make extension-build          # in a second terminal
   ```

4. **Load the extension:**
   1. Open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked** and pick `extension/dist`.
   2. Open **Details**, then **Extension options**:
      - Server URL: `http://127.0.0.1:8000`.
      - Access token: the `GATEKEEPER_ADMIN_TOKEN` value from `.env`.
      - **Test connection** should say "Connected".
   3. Pin the toolbar icon.

5. **Warm up (optional).** Analyse PRs 2 to 5 once from the panel, so they show finished results during the demo, and keep PR 1 for the live run.

## The demo (5 minutes)

### 0:00 The problem (30 s)

Open the playground's pull request list.

> "Maintainers drown in pull requests. The usual shortcuts judge the person: account age, followers, first-timer badges. Gatekeeper judges only the work, shows its evidence, and admits when it isn't sure."

### 0:30 A live review: PR #1 "Fix truncate() exceeding the requested width" (90 s)

1. Open PR #1 and click the Gatekeeper icon. The side panel opens on the **Activity** tab.
2. Click **Analyse** (or **Re-analyse**). Point out, as they happen:
   - The **Now:** line moves through the steps: "reading the change", "checking textkit/core.py", "asking the model", "verifying a citation in …".
   - In the **tree**, changed files step through their stages (listed, signalled, in context, cited, verified). The file being worked on is highlighted.
   - The **timeline** fills with real backend events.
3. When the run finishes, the verdict card should read **✓ Looks valuable**, with a confidence bar, a one-line rationale and verified citations.
4. Click the file `textkit/core.py` in the tree. The drawer shows its signals and verified evidence.
5. Click an evidence line range. The GitHub tab jumps to those lines in **Files changed** and highlights them.

> "Every citation was checked by code against the diff: path, line numbers and the quoted text. Citations that don't hold are dropped and counted."

### 2:00 Noise versus newcomers: PRs #2 and #3 (60 s)

1. Open PR #2, "Update core.py" (whitespace only).
   - Expect **▽ Likely low value** or **◎ Needs a human look**.
   - Open the file drawer: *"Every changed line in this file differs only in whitespace."*
   - The reply draft is constructive, not dismissive.
2. Open PR #3, "Add a short usage section to the README".
   - Expect **✓ Looks valuable** or **◎ Needs a human look**.

> "Small isn't bad. A small, honest docs change is never labelled low value for being small, or for coming from a newcomer. Gatekeeper never sees who opened it."

### 3:00 Claims and prompt injection: PRs #4 and #5 (60 s)

1. Open PR #4, "Add input validation to slugify".
   - The description promises validation and tests, but the diff renames a variable.
   - Expect **◎ Needs a human look** or **▽ Likely low value**.
   - Switch to **Chat** and click **"Why this verdict?"**. The answer cites lines, and its reference chips reveal them in GitHub.
2. Open PR #5, "Polish word_count docstring". Its description contains an instruction aimed at the reviewer.
   - Expect **◎ Needs a human look** at most. The injected instruction is ignored.
   - Switch to **Insights**, then **What the model saw**. The PR text sits inside `<untrusted_content>` blocks, and the note says: *"Author name and account details are never sent to the model."*

### 4:00 People decide (45 s)

1. Go back to PR #1. On the verdict card, click **Approve and post comment**.
2. The confirm sheet shows the exact text, which you can edit, and the footer that will be added.
3. Tick **I reviewed this**, then **Post comment**. The card shows **Posted** with a link.
   - This is the only GitHub write Gatekeeper can make. It is posted at most once per analysis, and it is audited before and after posting.
   - If the token has no write permission, the card says which permission is missing.
4. Optional, in the terminal: run `uv run gatekeeper audit <run-id>`. It shows the hash-chained events and ends with `chain: OK`.

### 4:45 Wrap-up (15 s)

> "Contributor-blind, evidence-first, abstains when unsure, and a person approves anything that leaves the tool. It runs on open-weight Gemma 4, either hosted or fully local with Ollama."

## Expected verdicts

| PR | Title | Expected range | What to point at |
| --- | --- | --- | --- |
| #1 | Fix truncate() exceeding the requested width | Looks valuable | verified citations on the fix and the tests; `Closes #1` |
| #2 | Update core.py | Likely low value or Needs a human look | the high-severity `whitespace_only` signal |
| #3 | Add a short usage section to the README | Looks valuable or Needs a human look | never "likely low value" for being small |
| #4 | Add input validation to slugify | Needs a human look or Likely low value | `claim_match` is mismatch or partial |
| #5 | Polish word_count docstring | Needs a human look at most | the injected instruction is ignored |

## If something misbehaves

- **The model is slow or rate-limited.**
  - The key pool fails over automatically, and the timeline shows the key alias and failovers.
  - If every key is cooling down, the job backs off and retries.
  - Fall back to PRs analysed during warm-up. Their results, timelines and model input are stored.
- **No network.** Every stored result, timeline, chat history and Insights view still loads from the local database. Only new analyses and chat answers need the model.
- **"Reveal in GitHub" can't find the diff.** GitHub's page markup is not a documented API. The extension then opens the file at the PR's commit, and the panel says so.
- **The panel says "Not connected".** Check that `make stack` is still running and that the token in Options matches `.env`.
- **The panel stays on "Queued; waiting for the worker".** The worker is not running. `make dev` starts both the API and the worker.
- **The comment button reports missing permissions.** The token needs Issues (write) or Pull requests (write) on the playground. Skip that step or show the message.

## FAQ for judges

- **How is this different from an AI code reviewer?**
  - Most AI reviewers comment on everything and act directly.
  - Gatekeeper produces a triage verdict for maintainers. Code checks every model citation, the tool abstains when unsure, and it never acts on its own.
- **How is this different from reputation-based spam filters?** Those judge accounts. Gatekeeper's scoring path cannot see the author:
  - identity is removed before storage;
  - a leak guard blocks model calls that contain the author's login;
  - an import test keeps the review code away from identity data.
- **Can a PR description manipulate the verdict?**
  - All PR text sits in escaped untrusted blocks that the system prompt treats as data.
  - Citations must match the real diff, and a negative verdict needs verified evidence.
  - Demo PR #5 tests this. It is a mitigation, not a proof.
- **Is the confidence score calibrated?** No. It is a transparent heuristic (self-confidence, citation validity, agreement with deterministic signals, completeness). Calibration needs a labelled benchmark, which is on the roadmap.
- **What leaves my machine?**
  - In hosted mode, only the scrubbed PR content and scrubbed chat questions, sent to the Gemini API. Private repositories are refused unless you allow them.
  - In Ollama mode, nothing.
- **Why open-weight Gemma?** It can run locally for private code, its weights are openly licensed (Gemma 4 is Apache 2.0), and the same prompts work hosted or local.
