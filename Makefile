.PHONY: install collect synthesize test lint typecheck format check clean

install:
	uv sync

collect:
	uv run researcher collect

synthesize:
	uv run researcher synthesize

test:
	uv run pytest

test-golden:
	uv run pytest -m golden

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy researcher_agent

check: lint typecheck test

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache dist build *.egg-info
	find . -type d -name __pycache__ 