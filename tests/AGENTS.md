# AGENTS.md — tests/

## Test Categories

| Category | Directory | Marker | Speed | External Deps |
|----------|-----------|--------|-------|---------------|
| Unit | `tests/unit/` | `@pytest.mark.unit` | < 1s each | None |
| Canonical | `tests/canonical/` | `@pytest.mark.canonical` | < 5s each | None (golden snapshots) |
| Integration | `tests/integration/` | `@pytest.mark.integration` | 5–60s each | Docker, API keys |

## Commands

```bash
# Unit tests
uv run pytest tests/ -v -m unit

# Canonical tests
uv run pytest tests/ -v -m canonical

# Regenerate golden snapshots (intentional changes only)
uv run pytest tests/canonical/ --update-canon -v

# Integration tests (requires .env with API keys + Docker)
scripts/with_env.sh uv run pytest tests/ -v -m integration

# Full suite
scripts/with_env.sh uv run pytest tests/ -v
```

Both marker-based (`pytest -m unit`) and directory-based (`pytest tests/unit/`) invocation produce identical results since all test files have `pytestmark` set.

## Directory Structure

```
tests/
├── conftest.py              # Shared fixtures, auto-skip hooks
├── unit/                    # Pure-logic tests, no I/O
│   ├── grading/             # Grading subsystem tests
│   └── adapters/            # Adapter unit tests
├── canonical/               # Golden snapshot tests
│   ├── conftest.py          # --update-canon flag, canon_snapshot fixture
│   └── snapshots/           # Committed golden JSON files
├── integration/             # Docker/API integration tests
│   └── docker/              # Docker foundation layer tests
├── data/                    # Test fixtures
│   ├── tasks/               # Task fixtures (calc_basic, browser_basic, etc.)
│   ├── projects/            # Full project snapshots (requires git lfs pull)
│   └── configs/             # Config fixtures
└── utils/                   # Shared test utilities
    ├── fixtures.py          # Common fixtures (mock_env_state, test_task_path)
    ├── validators.py        # Output validation helpers
    ├── mock_clients.py      # MockAsyncClient — canonical source
    ├── networks.py          # Docker network/volume fixtures
    ├── containers.py        # Docker container fixtures
    └── project_fixtures.py  # Project data loaders
```

## Rules

1. **Every test MUST have a marker**: `@pytest.mark.unit`, `@pytest.mark.canonical`, or `@pytest.mark.integration`. Strict markers are enforced.
2. **Test behavior, not code** — mocks hide problems. Prefer real objects where feasible.
3. **Unit tests**: pure logic, mock external deps, fast. No I/O.
4. **Canonical tests**: compare output against golden snapshots in `snapshots/`. No external deps.
5. **Integration tests**: real services, Docker, API keys. Auto-skipped when prerequisites missing.
6. **Import `MockAsyncClient`** from `tests.utils.mock_clients` — do not create local copies.
7. **Use shared fixtures** from `conftest.py` — do not duplicate.
8. **Zero `xfail`**, zero bare `@skip`. Use conditional markers (`requires_api`, `requires_docker`).

## Golden Snapshots

- Snapshots live in `tests/canonical/snapshots/`.
- Run `uv run pytest tests/canonical/ --update-canon -v` to regenerate after intentional changes.
- Snapshot diffs are reviewable in PRs — treat mismatches as regressions until proven otherwise.

## Known Issues

- `tests/data/projects/` requires `git lfs pull` — golden-set tests skip without LFS data.
- `--strict-markers` error → add new markers to `pyproject.toml` `[tool.pytest.ini_options]`.
- Integration tests skip silently when Docker or API keys are unavailable. This is by design.
