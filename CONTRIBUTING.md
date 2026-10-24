# Contributing to Gatekeeper

Thanks for helping. Gatekeeper judges the work and not the contributor, and we hold our own contributions to the same standard.

## Set up

```sh
make setup      # Python 3.13 + dev tools via uv, pre-commit hooks
make init-env   # local .env with generated secrets
make up         # Postgres + Redis
make doctor     # sanity check
```

## Before you open a pull request

1. `make lint typecheck test` passes locally.
2. The pre-commit hooks pass: gitleaks, ruff, whitespace and YAML checks. Never skip them with `--no-verify`.
3. New code ships with tests. Behaviour tied to the safety rules gets an explicit test. Those rules are contributor-blind scoring, no auto-close of first PRs, schema-validated model output, and untrusted input handled as data.
4. New environment keys go in `.env.example` with a one-line comment, and secret keys stay empty there.
5. An architecture change comes with an ADR in `docs/adr/`.

## Commits

Use small [Conventional Commits](https://www.conventionalcommits.org/) such as `feat: ...`, `fix: ...`, `docs: ...`, `chore: ...`, `ci: ...`, `test: ...` and `refactor: ...`. Each commit should cover one concern.

## Secrets

Never commit secrets, tokens, private keys or `.env`. Reference configuration by key name only. If you commit a secret by accident, rotate it first, then tell the maintainers (see [SECURITY.md](SECURITY.md)).

## AI assistants

[CLAUDE.md](CLAUDE.md) holds the project rules for AI coding assistants. Human contributors should follow them as well.
