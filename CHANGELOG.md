# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- P04 review core:
  - identity scrub at the boundary, and a leak guard on model requests;
  - 10 deterministic signals;
  - a citable, budgeted diff;
  - one structured review call (`review.pr` v1);
  - a citation verifier;
  - heuristic confidence v1 with abstain rules;
  - a deterministic reply draft with a banned-word filter.
- Migration 0004: `analyses.head_sha`, `content_hash` and `verdict`.
- Skip-unchanged reprocessing.
- `gatekeeper show` and `fetch-pr --json`.
- REVIEW_* settings.
- ADR 0005.
- P03:
  - read-only GitHub client (token or GitHub App);
  - contributor-blind PR normalisation;
  - FastAPI webhook gateway (`make api`);
  - Postgres job queue and worker (`make worker`);
  - pipeline skeleton with stub steps and the `process_pr` job;
  - `gatekeeper` CLI (`fetch-pr`, `enqueue-pr`, `audit`, `jobs`);
  - `make relay`.
- Migration 0003: `webhook_deliveries` and `jobs`.
- Settings: GATEKEEPER_ALLOWED_REPOS, GITHUB_API_BASE_URL, WEBHOOK_MAX_BODY_BYTES, JOB_*, WORKER_POLL_INTERVAL_S, DIFF_MAX_BYTES, PR_MAX_FILES.
- `make model-check --require-vision`; a plain vision failure is now a warning.
- ADR 0004.
- P02 model adapter (`gatekeeper.models`):
  - Ollama backend and optional Gemini API backend (`--extra gemini`);
  - typed requests and responses, retries, structured output with one repair attempt, explicit context size, fail-open Redis cache;
  - audit events written by the adapter;
  - `FakeBackend` for tests.
- Prompt templates (`gatekeeper.prompts`) with escaped untrusted-content blocks.
- `make model-check` (`ARGS=--vision`).
- Model tuning settings: MODEL_TIMEOUT_S, MODEL_MAX_RETRIES, MODEL_MAX_CONCURRENCY, MODEL_CACHE_TTL_S, OLLAMA_NUM_CTX, OLLAMA_KEEP_ALIVE.
- Migration 0002: repo owner/name are unique case-insensitively, plus `get_or_create_repo`.
- ADR 0003.

### Changed

- The audit chain hash now also covers actor, parent_seq, item_id, schema_version and created_at. Reset development data with `make db-reset`.

### Added (P00–P01)

- P01 backend foundation:
  - typed settings with `require()`;
  - structlog logging with redaction and context binding;
  - the error model with public dicts;
  - `make env-check` and `make env-sync`.
- Database: SQLAlchemy 2.1 + psycopg 3 models, Alembic revision 0001 (repos, items, item_authors, analyses, events), and `make migrate`, `make migrate-down` and `make db-reset`.
- Audit trail: append-only events (trigger-enforced), redaction before hashing, a per-run SHA-256 chain with verification, and fail-closed recording.
- `make check`, plus a Postgres service in CI for integration tests.
- ADR 0002.
- P00 repository foundation: the uv-managed Python 3.13 project with ruff, mypy (strict), pytest and pytest-cov.
- pre-commit hooks: gitleaks, ruff and file hygiene.
- GitHub Actions CI (lint, typecheck, tests with coverage, secret scan) and Dependabot.
- docker-compose with PostgreSQL 16 and Redis 7, bound to localhost.
- `.env.example`, `make init-env` (generated secrets, never overwrites) and `make doctor`.
- `CLAUDE.md`, the architecture overview and ADR 0001 (thesis and non-goals).
