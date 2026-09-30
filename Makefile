.PHONY: build consumer-smoke format lint sync test typecheck verify

sync:
	uv sync --all-extras --all-groups --locked

format:
	uv run --locked ruff format .
	uv run --locked ruff check --fix .

lint:
	uv run --locked ruff format --check .
	uv run --locked ruff check .

typecheck:
	uv run --locked mypy

test:
	uv run --locked pytest
	uv run --locked python scripts/check_coverage.py

build:
	uv run --locked python -m build
	uv run --locked twine check --strict dist/*

consumer-smoke:
	uv run --locked python scripts/verify_distribution.py

verify: lint typecheck test consumer-smoke
