.PHONY: install install-dev sync test test-coverage lint lint-fix format format-check clean build build-adapter build-all docker-build docker-build-core docker-up docker-down docker-status help

# =============================================================================
# Installation (using uv)
# =============================================================================
# uv is the recommended package manager for this project
# Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh

install:
	uv sync

install-dev: install
	uv run playwright install --with-deps chromium

# Alias for install (uv terminology)
sync: install

# =============================================================================
# Testing
# =============================================================================

test:
	uv run pytest tests/ -v

test-unit:
	uv run pytest tests/ -v -m unit

test-canonical:
	uv run pytest tests/ -v -m canonical

test-coverage:
	uv run pytest tests/ --cov=tolokaforge --cov-report=html --cov-report=term

# =============================================================================
# Code Quality
# =============================================================================

# Directories to lint/format (contrib/ excluded - external libraries)
LINT_DIRS = tolokaforge tests scripts tools

# Check linting (no fix) - CI ready, exits non-zero on issues
lint:
	uv run ruff check $(LINT_DIRS)

# Auto-fix linting issues
lint-fix:
	uv run ruff check --fix $(LINT_DIRS)

# Apply formatting (black + ruff format)
format:
	uv run black $(LINT_DIRS)
	uv run ruff format $(LINT_DIRS)

# Check formatting only (no changes) - CI ready, exits non-zero on issues
format-check:
	uv run black --check $(LINT_DIRS)
	uv run ruff format --check $(LINT_DIRS)

# =============================================================================
# Package Building
# =============================================================================

build:
	rm -rf dist/
	uv build

build-adapter:
	rm -rf dist/
	uv build --package tolokaforge-adapter-terminal-bench

build-all: build build-adapter

# =============================================================================
# Docker (via tolokaforge CLI — replaces docker-compose and bash scripts)
# =============================================================================

docker-build:
	uv run tolokaforge docker build

docker-build-core:
	uv run tolokaforge docker build --core

docker-up:
	uv run tolokaforge docker up --profile core

docker-down:
	uv run tolokaforge docker down --volumes

docker-status:
	uv run tolokaforge docker status

# =============================================================================
# Cleanup
# =============================================================================

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	rm -rf build/ dist/ .coverage htmlcov/ .pytest_cache/ .mypy_cache/

clean-all: clean
	rm -rf .venv/

# =============================================================================
# Help
# =============================================================================

help:
	@echo "TolokaForge - LLM Tool-Use Benchmarking Harness"
	@echo ""
	@echo "Prerequisites:"
	@echo "  uv             - Install with: curl -LsSf https://astral.sh/uv/install.sh | sh"
	@echo ""
	@echo "Installation:"
	@echo "  make install      - Install core + dev dependencies"
	@echo "  make install-dev  - Install + Playwright browser runtime"
	@echo "  make sync         - Alias for install"
	@echo ""
	@echo "Testing:"
	@echo "  make test         - Run all tests"
	@echo "  make test-unit    - Run unit tests only"
	@echo "  make test-canonical - Run canonical tests only"
	@echo "  make test-coverage - Run tests with coverage"
	@echo ""
	@echo "Code Quality:"
	@echo "  make lint         - Check linting (ruff, no fix) - CI ready"
	@echo "  make lint-fix     - Auto-fix linting issues (ruff --fix)"
	@echo "  make format       - Format code (black + ruff format)"
	@echo "  make format-check - Check formatting only - CI ready"
	@echo ""
	@echo "Packaging:"
	@echo "  make build         - Build tolokaforge sdist + wheel"
	@echo "  make build-adapter - Build tolokaforge-adapter-terminal-bench"
	@echo "  make build-all     - Build all packages"
	@echo ""
	@echo "Docker:"
	@echo "  make docker-build       - Build all Docker images"
	@echo "  make docker-build-core  - Build core images only (db-service + runner)"
	@echo "  make docker-up          - Start Docker services (core stack)"
	@echo "  make docker-down        - Stop and remove Docker services"
	@echo "  make docker-status      - Show Docker service status"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean        - Clean build artifacts"
	@echo "  make clean-all    - Clean everything including .venv"
