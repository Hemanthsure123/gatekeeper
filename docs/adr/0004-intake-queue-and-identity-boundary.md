# ADR 0004: Postgres job queue, read-only GitHub client, delivery table, identity boundary

- **Status:** Accepted
- **Date:** 2026-10-25
- **Builds on:** [ADR 0002](0002-database-identity-and-audit.md), [ADR 0003](0003-model-adapter.md)

## Context

P03 connects GitHub to the pipeline. An event arrives by webhook or from the CLI. The pull request is fetched, normalised and queued, a worker processes it, and an audited analysis exists at the end. The platform is Windows, and every part must run there.

## Decisions

### 1. A Postgres-backed queue, not RQ, Celery or Redis

- **Why not RQ:** its documentation (Limitations) says RQ workers need `fork()`, so they don't run on Windows without WSL. The worker docs do mention `SimpleWorker` as a Windows option, but it is a non-isolating workaround.
- **Why Postgres:**
  - It is durable.
  - A job commits **in the same transaction** as related rows. For example, the webhook delivery is marked `queued` atomically with the job insert.
  - The future dashboard can see the queue with plain SQL.
  - It needs no new dependency.
- **Redis** stays, for the model cache only.
- **Claiming:**
  - First, expired leases are reaped: they are re-queued, or marked `dead` when out of attempts.
  - Then `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)` claims a job. Concurrent workers skip each other's locked rows and never double-claim.
- **Leases:** a running job has a lease (`JOB_LEASE_S`), extended by a heartbeat until the job finishes or `JOB_TIMEOUT_S` passes.
  - `complete` and `fail` only apply for the worker that still holds the lock.
  - A worker that lost its lease cannot overwrite the job's state.
- **Failures:**
  - Retryable failures back off exponentially (30 s, 60 s, 120 s, capped at 1 h), never sooner than a server's `retry_after`, up to `JOB_MAX_ATTEMPTS`. Then the job is `dead`.
  - Non-retryable failures go straight to `failed`.
  - Only an error **code** is stored, never a stack trace.
- **Dedupe:** a unique partial index on `dedupe_key` (where status is queued or running) means the same PR at the same head SHA is never queued twice.
- **Worker:** one process works one job at a time, because local inference is CPU-bound. Ctrl+C (and Ctrl+Break on Windows) stops gracefully after the current job. A second Ctrl+C aborts, and the lease then lets another worker reclaim the job.
- **Limit:** Python cannot pre-empt a running call. Handlers check the deadline between steps, and model calls have their own `MODEL_TIMEOUT_S`.

### 2. The GitHub client is read-only

- Only GET requests exist: the pull request, its files, check runs, and a user by id.
- It pins `X-GitHub-Api-Version: 2026-03-10`. None of that version's breaking changes touch these endpoints.
- It follows `Link` pagination and refuses next-page links to another host, so the token is never sent elsewhere.
- It retries connection errors and 5xx with backoff, and honours rate limits:
  - primary: `x-ratelimit-remaining: 0` means wait until `x-ratelimit-reset`;
  - secondary: wait for `retry-after`, or at least 60 s.
- Waits over 60 s are not slept inline. They surface as a retryable error carrying `retry_after`, so the queue backs off.
- **Credentials:**
  - Token mode uses `GITHUB_TOKEN_DEV`.
  - App mode is used when both App keys are set. It builds an RS256 JWT (iat −60 s, exp ≤ 10 min, iss = App ID), then exchanges it via `/repos/{o}/{r}/installation` and `/app/installations/{id}/access_tokens`. The token is cached until 5 minutes before it expires. The private key is read from disk at use time.
  - Credentials are resolved lazily, so nothing is demanded until the first request.
- Authorization values are never logged.
- **No write calls exist.** Posting comments or labels needs a separate, policy-gated executor (a later ADR).

### 3. Webhook deliveries are idempotent and recorded

- `webhook_deliveries` holds one row per `X-GitHub-Delivery`: received, queued, ignored or enqueue_failed.
- The gateway checks, in order:
  1. the body size;
  2. the HMAC-SHA256 signature, with a constant-time compare;
  3. JSON parsing and the required headers.
- Only `pull_request` events with actions opened, reopened, synchronize, ready_for_review or edited are queued.
- Everything else, including repos outside `GATEKEEPER_ALLOWED_REPOS`, is recorded as `ignored`. An empty allowlist allows every repo in development and none in production.
- A redelivery of a `queued` or `ignored` delivery returns "duplicate". `received` and `enqueue_failed` deliveries may be processed again.
- The gateway refuses to start without `GITHUB_WEBHOOK_SECRET`. Payload bodies are never logged.

### 4. The identity boundary is structural

- `github/normalize.py` splits a PR into two objects:
  - `ItemContent` for `items.content`: no logins, avatars, reviewers, commenters, commit authors, fork label or branch names;
  - `AuthorInfo` for `item_authors`.
- `gatekeeper.identity` is the only code that reads or writes `item_authors`.
- Pipeline steps receive an identity-free `ItemView`.
- An AST test fails if anything in `gatekeeper.review_core` or `gatekeeper.pipeline.steps` imports `gatekeeper.identity`, references `ItemAuthor`, or names the `item_authors` table.
- Not yet done: scrubbing the author's login from free text (title, body, patches). That belongs to the intake step in a later prompt.

## Consequences

- Positive: the queue is durable and visible, and it works on Windows. Duplicate deliveries and duplicate jobs are harmless.
- Positive: contributor-blindness has an automated check from the first pipeline commit.
- Negative: a Postgres queue is polled, not pushed. One-second polling is fine at local triage volumes.
- Negative: a job that overruns its timeout keeps running until its current step returns. Only the lease and retry handle it.
