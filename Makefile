# Telegram-Finance-Bot V2 developer shortcuts (wrappers around uv). V1 is not affected.
.PHONY: sync lint format type imports test check staging-config

sync:
	uv sync --frozen

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

type:
	uv run mypy

imports:
	uv run lint-imports --no-cache

test:
	uv run pytest --cov --cov-report=term-missing

check: lint type imports test

# Validate the staging compose model offline (Docker CLI only: no daemon, no pull, starts nothing).
staging-config:
	docker compose -f deploy/staging/compose.yml config --quiet
