# Gatekeeper developer commands. Run every target from the repository root.
# Recipes only call uv, python and docker so they behave the same under sh
# (Git Bash, macOS, Linux, CI) and cmd.exe (make started from PowerShell).

UV ?= uv
COMPOSE ?= docker compose

# Caches and build output removed by `make clean`. Never touches .venv, .env or Docker volumes.
CLEAN_PY := import pathlib, shutil; \
	pats = ('.pytest_cache', '.mypy_cache', '.ruff_cache', 'htmlcov', 'build', 'dist', \
	'*.egg-info', '.coverage', '.coverage.*', 'coverage.xml', '**/__pycache__'); \
	skip = {'.venv', 'node_modules', '.git'}; \
	hits = [p for pat in pats for p in pathlib.Path('.').glob(pat) if not skip & set(p.parts)]; \
	[shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True) for p in hits]; \
	print('clean: removed', len(hits), 'paths')

.DEFAULT_GOAL := help
.PHONY: help setup check lint format typecheck test up down clean init-env env-check env-sync \
	doctor migrate migrate-down db-reset model-check api worker relay env-set gemma-setup \
	extension-install extension-build extension-dev dev stack

help:
	$(info Usage: make <target>)
	$(info setup      install Python and dev tools with uv, install git hooks)
	$(info check      lint, typecheck and tests with coverage)
	$(info lint       ruff lint and format check)
	$(info format     apply ruff fixes and formatting)
	$(info typecheck  mypy in strict mode)
	$(info test       pytest with coverage)
	$(info up         start Postgres and Redis and wait until healthy)
	$(info down       stop local services, keeping data volumes)
	$(info clean      remove caches and build output)
	$(info init-env   create .env from .env.example, never overwrites)
	$(info env-check  show which .env keys are SET or MISSING, by prompt; never prints values)
	$(info env-sync   append keys missing from .env with empty values)
	$(info env-set    set NON-SECRET keys: make env-set ARGS="KEY=VALUE ...")
	$(info doctor     report tool versions and Ollama status)
	$(info migrate      apply all database migrations)
	$(info migrate-down roll back the latest migration)
	$(info db-reset     drop and re-create the schema, development only)
	$(info model-check  check the model backend; ARGS=--vision also tests image input)
	$(info gemma-setup  check Gemini API keys by alias and select the Gemma 4 models)
	$(info dev          run the API and one worker together (checks services and migrations))
	$(info stack        make up, make migrate, then make dev)
	$(info api          run the API (webhooks and the extension API) on API_HOST:API_PORT)
	$(info worker       run one queue worker; Ctrl+C finishes the current job, then stops)
	$(info relay        forward webhooks from WEBHOOK_PROXY_URL to the local gateway, needs Node)
	$(info extension-install  install the extension's dev tools (npm, needs Node 20+))
	$(info extension-build    build the side-panel extension into extension/dist)
	$(info extension-dev      rebuild the extension on every change)
	@exit 0

setup:
	$(UV) sync --all-extras
	$(UV) run pre-commit install

check: lint typecheck test

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format:
	$(UV) run ruff check --fix --exit-zero .
	$(UV) run ruff format .

typecheck:
	$(UV) run mypy

test:
	$(UV) run pytest

up:
	$(COMPOSE) up --detach --wait --wait-timeout 120

down:
	$(COMPOSE) down

clean:
	@$(UV) run --no-project python -c "$(CLEAN_PY)"

init-env:
	$(UV) run --no-project python scripts/init_env.py

env-check:
	$(UV) run --no-project python scripts/env_tools.py check

env-sync:
	$(UV) run --no-project python scripts/env_tools.py sync

env-set:
	$(UV) run python scripts/env_set.py $(ARGS)

doctor:
	$(UV) run --no-project python scripts/doctor.py

migrate:
	$(UV) run alembic upgrade head

migrate-down:
	$(UV) run alembic downgrade -1

db-reset:
	$(UV) run python scripts/db_reset.py

model-check:
	$(UV) run python -m gatekeeper.models.check $(ARGS)

api:
	$(UV) run python -m gatekeeper.api $(ARGS)

worker:
	$(UV) run python -m gatekeeper.worker

relay:
	$(UV) run python scripts/relay.py $(ARGS)

gemma-setup:
	$(UV) run python scripts/gemma_setup.py

extension-install:
	$(UV) run --no-project python scripts/extension.py install

extension-build:
	$(UV) run --no-project python scripts/extension.py build

extension-dev:
	$(UV) run --no-project python scripts/extension.py dev

dev:
	$(UV) run python scripts/dev.py

stack:
	$(MAKE) up
	$(MAKE) migrate
	$(MAKE) dev
