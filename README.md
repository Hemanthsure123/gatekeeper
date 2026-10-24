# Gatekeeper

Gatekeeper is a local-first GitHub triage tool for pull requests, issues and comments. It uses open models through Ollama (Gemma 4). Its principles:

- **Judge the work, not the contributor.** Scoring never sees who wrote something.
- **Show verified evidence.** Every judgement points at things that can be checked.
- **Admit uncertainty.** "Not sure" is a valid, first-class answer.
- **Never take harmful automatic actions.** Destructive actions are off by default.
- **Record every decision** so it can be replayed and audited.

Gatekeeper runs only on your machine. Nothing is deployed.

## Status

**Foundation only (P00).** This repo has tooling, CI, local services, environment handling and docs, but no application logic yet. See [docs/architecture.md](docs/architecture.md) for the planned layers and [docs/adr/0001-thesis-and-non-goals.md](docs/adr/0001-thesis-and-non-goals.md) for the thesis.

## Prerequisites

| Tool | Version used here | Notes |
| --- | --- | --- |
| [uv](https://docs.astral.sh/uv/) | 0.12.5+ | Installs Python 3.13.15 (pinned in `.python-version`) for you |
| GNU make | 4.x | Windows: `winget install ezwinports.make` |
| Docker with Compose v2 | Docker 28, Compose 2.40 | Docker Desktop on Windows and macOS |
| [Ollama](https://ollama.com) | 0.40+ | Models are pulled in a later prompt |
| git | 2.40+ | |

## Quickstart

```sh
make setup      # install Python 3.13 + dev tools with uv, install pre-commit hooks
make init-env   # create .env with local defaults and generated secrets (never overwrites)
make up         # start PostgreSQL 16 and Redis 7, wait until healthy
make doctor     # check tool versions and whether Ollama responds
make test       # run the test suite with coverage
```

Run `make` with no target to list every target. `make down` stops the services and keeps their data.

### Local services

| Service | Address | Notes |
| --- | --- | --- |
| PostgreSQL 16 | `127.0.0.1:15432` | Credentials come from `.env` |
| Redis 7 | `127.0.0.1:16379` | Append-only persistence |
| Ollama | `http://localhost:11434` | Run it yourself; Gatekeeper only connects to it |

The non-default ports keep Gatekeeper's services from clashing with other local Postgres or Redis instances. Both are bound to localhost only.

### Secrets

`make init-env` fills in safe local defaults and generates random secrets. It prints key names only, never values. You add these keys to `.env` yourself when a later prompt needs them: `GITHUB_TOKEN_DEV`, `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_PATH`, and optionally `GEMINI_API_KEY` and `HF_TOKEN`. Keep private keys under `secrets/`, which git ignores.

## Repository layout

```text
backend/     Python package (src/gatekeeper) and its tests
scripts/     init_env.py, doctor.py and their tests
dashboard/   placeholder for the review dashboard
extension/   placeholder for the extension client
eval/        placeholder for the evaluation harness
docs/        architecture, ADRs and build logs
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). To report a security issue, see [SECURITY.md](SECURITY.md).

## License

Licensed under the [Apache License 2.0](LICENSE). Copyright `<YOUR NAME OR ORG>`.
