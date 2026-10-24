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
.PHONY: help setup lint format typecheck test up down clean init-env env-check env-sync doctor

help:
	$(info Usage: make <target>)
	$(info setup      install Python and dev tools with uv, install git hooks)
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
	$(info doctor     report tool versions and Ollama status)
	@exit 0

setup:
	$(UV) sync
	$(UV) run pre-commit install

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

doctor:
	$(UV) run --no-project python scripts/doctor.py
