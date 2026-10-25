# ADR 0007: Extension-first UI, an activity feed beside the audit trail, and one approval-gated write

- **Status:** Accepted
- **Date:** 2026-10-25
- **Builds on:** [ADR 0002](0002-database-identity-and-audit.md), [ADR 0004](0004-intake-queue-and-identity-boundary.md), [ADR 0005](0005-review-core.md)

## Context

Maintainers triage pull requests on github.com, so a separate dashboard would pull them away from where they work. The product also needs to show, while a review runs, which file it is reading and which step it is on. Finally, a review is only useful once its result reaches the contributor, but automatic actions are against the thesis.

## Decisions

1. **The UI is a Chromium side panel.**
   - It is a Manifest V3 extension with three tabs: Activity, Chat and Insights.
   - It is plain TypeScript built by Vite, with no UI framework and no remote code.
   - The panel makes every API call from the extension origin, with a bearer token (`GATEKEEPER_ADMIN_TOKEN`). The content script only scrolls and highlights on github.com. It makes no network calls and sends no page data.
   - The planned standalone dashboard is dropped for now.
2. **The activity feed is separate from the audit trail.**
   - `activity_events` is a best-effort display feed. A failed write logs a warning and never fails a run, its events are redacted and identity-free, and it is read by polling and streamed to the panel over SSE.
   - The audit trail (`events`) stays the source of truth: fail-closed, append-only and hash-chained.
   - The two never substitute for each other. The panel's "What the model saw" view reads the audit trail, not the feed.
   - Per-file progress is derived by replaying the feed (`gatekeeper.activity.file_states`).
3. **There is exactly one GitHub write: an approved comment.**
   - `GitHubClient.create_issue_comment` refuses to run without `allow_write=True` and is never retried.
   - Only `POST /api/analyses/{id}/comment` calls it, and only after the maintainer confirms the exact (optionally edited) text. Edited text is length-limited and passes the banned-word filter, and a footer says a maintainer approved it.
   - The post is idempotent per analysis (`comment_posts` plus a row lock).
   - It is audited **before** sending (the approval) and after (the GitHub result). If the audit cannot be written, nothing is sent.
4. **There are no automatic actions.** Nothing is labelled, closed or approved, and nothing is posted without a person. Chat is read-only and cannot trigger actions.
5. **Chat is grounded and blind.**
   - Prompt `chat.pr` v1 answers from the stored analysis and the diff only.
   - Questions are scrubbed through `gatekeeper.identity`, and the leak guard gets the item's forbidden terms.
   - References are checked against the diff.

## Consequences

- **Positive:**
  - The review appears where maintainers already are.
  - The live tree and timeline make the pipeline legible, and "What the model saw" makes contributor-blindness visible.
  - The single write is narrow, audited and human-approved.
- **Negative:**
  - "Reveal in GitHub" depends on undocumented page markup and anchors. It uses several strategies, then falls back to opening the file.
  - The extension works only in Chromium.
  - The activity feed polls Postgres about every 500 ms per open stream, which is fine locally but not designed for many viewers.
  - Posting needs a token with Issues or Pull requests write permission, which is broader than the read-only token used before P06.
