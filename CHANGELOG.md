# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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
