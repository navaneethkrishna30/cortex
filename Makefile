.PHONY: check lint types test fix

check: lint types test

lint:
	uv run --frozen ruff check .
	uv run --frozen ruff format --check .

types:
	uv run --frozen pyright

test:
	uv run --frozen python -m pytest

fix:
	uv run --frozen ruff check --fix .
	uv run --frozen ruff format .
