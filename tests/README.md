# Tolokaforge Test Suite

## Overview

The test suite is organized into **3 categories**: unit, canonical, and integration.

| Category | Directory | Speed | External deps | Marker |
|----------|-----------|-------|---------------|--------|
| Unit | `tests/unit/` | Fast (< 1s each) | None | `@pytest.mark.unit` |
| Canonical | `tests/canonical/` | Fast (< 5s each) | None (uses golden snapshots) | `@pytest.mark.canonical` |
| Integration | `tests/integration/` | Slow (5-60s each) | Docker, API keys | `@pytest.mark.integration` |

Current baseline: see [BASELINE.md](BASELINE.md) for up-to-date numbers.

## Running Tests

Unit and canonical tests run without API keys or Docker:

```bash
# All non-integration tests
uv run pytest tests/unit/ tests/canonical/ -v

# Unit tests only
uv run pytest tests/ -v -m unit

# Canonical tests only
uv run pytest tests/ -v -m canonical

# Regenerate golden snapshots
uv run pytest tests/canonical/ --update-canon -v
```

Integration tests need `.env` variables (API keys, service URLs) — use `scripts/with_env.sh`:

```bash
# Integration tests (needs Docker + API keys in .env)
scripts/with_env.sh uv run pytest tests/ -v -m integration

# Full suite
scripts/with_env.sh uv run pytest tests/ -v
```

## Directory Structure

```
tests/
├── conftest.py              # Shared fixtures, auto-skip requires_api hook
├── unit/                    # Pure-logic tests, no I/O
│   ├── grading/             # Grading subsystem tests
│   └── adapters/            # Adapter unit tests
├── canonical/               # Golden snapshot tests
│   ├── conftest.py          # --update-canon flag, canon_snapshot fixture
│   └── snapshots/           # Committed golden JSON files
├── integration/             # Docker/API integration tests
│   └── docker/              # Docker foundation layer tests
├── data/                    # Test data
│   ├── tasks/               # Task fixtures (calc_basic, browser_basic, calc_custom_checks)
│   ├── projects/            # Full project snapshots (food_delivery_2, tau_retail_mini)
│   └── configs/             # Config fixtures
└── utils/                   # Shared test utilities
    ├── fixtures.py           # Common fixtures (mock_env_state, test_task_path, etc.)
    ├── validators.py         # Output validation helpers
    ├── mock_clients.py       # MockAsyncClient — canonical source
    ├── networks.py           # Docker network/volume fixtures
    ├── containers.py         # Docker container fixtures
    └── project_fixtures.py   # food_delivery_2 project data loaders
```

## Test Categories

### Unit Tests (`tests/unit/`)

Pure logic tests with no external dependencies. Mock everything.

- Grading checks: hash computation, JSONPath assertions, transcript rules
- Tool registry: schema conversion, tool invocation
- Adapter output: task bundle generation, conversion logic
- CLI commands: status, validation paths

### Canonical Tests (`tests/canonical/`)

Compare output against committed golden snapshots in `snapshots/`.

- Adapter conversion output
- Grading pipeline results
- Custom checks with real project data (food_delivery_2)
- Golden-set hash grading verification

Use `--update-canon` flag to regenerate snapshots after intentional changes.

### Integration Tests (`tests/integration/`)

Require Docker daemon, API keys, or both. Auto-skipped when prerequisites are missing.

- Docker container lifecycle and service health
- End-to-end runner pipeline (native, frozen_mcp_core)
- LLM-judged grading with real providers
- Security: container isolation, network segmentation

## Pytest Markers

Defined in `pyproject.toml` under `[tool.pytest.ini_options]`:

| Marker | Description |
|--------|-------------|
| `unit` | Fast, isolated unit tests |
| `integration` | Tests requiring external services |
| `canonical` | Canonization snapshot tests |
| `slow` | Tests taking > 5 seconds |
| `requires_api` | Needs LLM API key — auto-skipped if none set |
| `requires_docker` | Needs Docker daemon |
| `docker` | Real container tests |
| `requires_postgres` | Needs Postgres instance |
| `grading` | Grading system tests |
| `security` | Security validation tests |
| `performance` | Performance benchmarks |
| `llm` | Calls real LLM providers |

All markers are enforced via `--strict-markers`.

## Key Fixtures

| Fixture | Source | Used by |
|---------|--------|---------|
| `mock_env_state` | `utils/fixtures.py` | Unit tests for user tools |
| `test_task_path` | `utils/fixtures.py` | Integration Docker service tests |
| `temp_output_dir` | `utils/fixtures.py` | Integration Docker service tests |
| `canon_snapshot` | `canonical/conftest.py` | All canonical tests |
| `food_delivery_2_*` | `canonical/conftest.py` | Canonical golden-set tests |
| `json_db_container` | `utils/containers.py` | Integration security tests |
| `runner_container` | `utils/containers.py` | Integration security tests |

## Writing New Tests

1. **Choose the right category**: unit for logic, canonical for regression snapshots, integration for real services.
2. **Naming**: `test_<component>_<behavior>.py` for files, `test_<action>` for methods.
3. **Use shared fixtures** from `conftest.py` — don't duplicate.
4. **Add markers**: every test file should use the appropriate `@pytest.mark.*`.
5. **Import `MockAsyncClient`** from `tests.utils.mock_clients` for new tests (don't create local copies).

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `food_delivery_2` tests skip | Run `git lfs pull` to fetch project data |
| Integration tests skip | Set API keys in `.env`, ensure Docker is running |
| `--strict-markers` error | Add new markers to `pyproject.toml` |
| Snapshot mismatch | Re-run with `--update-canon` if change is intentional |

## Test Philosophy

- **Zero `xfail`**: every test either passes or gets deleted.
- **Zero bare `@skip`**: use conditional markers (`requires_api`, `requires_docker`).
- **Canonical golden data** for regression detection — diffs are reviewable in PRs.
- **Auto-skip** for missing prerequisites instead of hard failures.
