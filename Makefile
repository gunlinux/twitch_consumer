.PHONY: dev
dev: ## Install dev dependencies
	uv sync --dev

check: lint types test
	echo "check"

types: ## Type-check with mypy
	uv run mypy src/

.PHONY: lint
lint: ## Run linters
	uv run ruff check .
	uv run flake8 src/ test/

.PHONY: fix
fix: ## Fix lint errors
	uv run ruff check --fix
	uv run ruff format

.PHONY: test
test: ## Run tests
	uv run pytest
