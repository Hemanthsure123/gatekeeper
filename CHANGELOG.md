# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- P00 repository foundation: the uv-managed Python 3.13 project with ruff, mypy (strict), pytest and pytest-cov.
- pre-commit hooks: gitleaks, ruff and file hygiene.
- GitHub Actions CI (lint, typecheck, tests with coverage, secret scan) and Dependabot.
- docker-compose with PostgreSQL 16 and Redis 7, bound to localhost.
- `.env.example`, `make init-env` (generated secrets, never overwrites) and `make doctor`.
- `CLAUDE.md`, the architecture overview and ADR 0001 (thesis and non-goals).
