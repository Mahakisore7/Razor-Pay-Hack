.DEFAULT_GOAL := help
.PHONY: help setup dev down test test-no-llm lint types migrate demo bench clean

COMPOSE := docker compose -f infra/docker-compose.yml --project-directory .
CORE := cd services/core &&
CONSOLE := cd apps/console &&

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## Install both toolchains, Playwright browsers, and git hooks
	$(CORE) uv sync
	$(CONSOLE) pnpm install --frozen-lockfile
	$(CONSOLE) pnpm exec playwright install chromium
	uvx pre-commit install --hook-type pre-commit --hook-type commit-msg

dev: ## Bring up the full compose stack (Postgres, Redis, api, worker, scheduler, console, Jaeger, Prometheus)
	$(COMPOSE) up --build

down: ## Tear down the compose stack
	$(COMPOSE) down

test: ## Run the full test suite (unit, property, integration -- needs Docker) and the console suite
	$(CORE) uv run pytest
	$(CONSOLE) pnpm test

test-no-llm: ## Run the test suite excluding anything that calls a real LLM (ROADMAP P5)
	$(CORE) uv run pytest -m "not llm"
	$(CONSOLE) pnpm test

lint: ## Static checks: ruff, import-linter, ESLint, Prettier
	$(CORE) uv run ruff check .
	$(CORE) uv run ruff format --check .
	$(CORE) uv run lint-imports
	$(CONSOLE) pnpm lint
	$(CONSOLE) pnpm format

types: ## Type checks: mypy --strict, tsc --strict
	$(CORE) uv run mypy
	$(CONSOLE) pnpm typecheck

migrate: ## Apply database migrations
	@if [ -f services/core/alembic.ini ]; then \
		cd services/core && uv run alembic upgrade head; \
	else \
		echo "No migrations yet (schema ships in Phase 1) -- nothing to do."; \
	fi

demo: ## Seed a cohort and run the benchmark (ARCHITECTURE §9)
	@echo "make demo: seeding and benchmarking ship in Phase 3 -- nothing to do yet."

bench: ## Run the three-arm benchmark (ROADMAP P3)
	@echo "make bench: the benchmark runner ships in Phase 3 -- nothing to do yet."

clean: ## Remove caches and build artefacts (not containers, volumes, or node_modules)
	find services/core -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf services/core/.pytest_cache services/core/.mypy_cache services/core/.ruff_cache
	rm -rf services/core/.coverage services/core/coverage.xml services/core/htmlcov
	rm -rf apps/console/.next apps/console/playwright-report apps/console/test-results
	rm -f apps/console/tsconfig.tsbuildinfo
