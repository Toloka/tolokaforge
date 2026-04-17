# Test Suite Baseline

> **Updated:** 2026-04-17
> **Branch:** `main`
> **Stage:** Post issue-71 cleanup (print debugging removed, baseline refreshed)
> **Previous baseline:** Phase 4 — 1127 tests, 971 passed, 14 skipped

---

## Summary

| Category    | Total | Passed | Failed | Skipped |
|-------------|-------|--------|--------|---------|
| Unit        | 1022  | 1008   | 0      | 14      |
| Canonical   | 85    | 85     | 0      | 0       |
| Integration | 80    | —      | 0      | —       |
| **Total**   | **1187** | **1093+** | **0** | **14+** |

> Integration pass/skip counts depend on available services (Docker, API keys).
> Unit + Canonical: **1093 passed, 14 skipped, 0 failed, 1 warning**.
> Coverage: **62.59%** (with `--cov-fail-under=60`)

**Warnings:** 1

---

## What Changed

### Phase 4 — Test Hardening (coverage 35.47% → 62.59%)

- **416 new unit tests** added across 9 new test files
- Coverage exclusions added for infrastructure code (gRPC protobuf, Docker layer,
  env services, gRPC services) — these are tested by integration tests, not unit tests
- Removed unused `suspicious` pytest marker from `pyproject.toml`
- Consolidated CI coverage runs: merged separate unit+canonical steps into one
- Removed redundant functional smoke step from `test-gate` CI job
- Fixed wasteful `uv add --dev pytest-cov` in `test-full` CI coverage step

New test files:
- `tests/unit/test_stuck_detector.py` — 18 tests for `StuckDetector` loop detection
- `tests/unit/test_rate_limiter.py` — 8 tests for `GlobalRateLimiter` thread safety
- `tests/unit/test_calculator_tool.py` — 25 tests for `CalculatorTool`
- `tests/unit/test_model_client.py` — 82 tests for LLM client helpers
- `tests/unit/test_orchestrator_logic.py` — 56 tests for orchestrator pure-logic
- `tests/unit/test_runner_logic.py` — 45 tests for trial runner logic
- `tests/unit/test_cli_commands.py` — 42 tests for CLI commands
- `tests/unit/test_tool_builtins.py` — 47 tests for tool builtins
- `tests/unit/grading/test_judge.py` — 78 tests for LLM judge grading

Bugs found:
- `CalculatorTool._eval_expr()` uses `ast.Num` removed in Python 3.14
- `TrialRunner._is_done()` lowercases text but compares uppercase markers — `###STOP###` never matches
- `model_client.py` has duplicate method definitions for `_tool_block_format()` and `_adapt_tool_content_blocks()`

### Stage A — Test Failure Triage (commit `e682af20a`)

- 31 failures → 0 failures
- 708 warnings → 1 warning
- Fixed import errors, path references, stale fixtures
- Added proper skip markers for environment-dependent tests
- Resolved async deprecation warnings and Pydantic V2 migration warnings

### Stages B + C — Test Consolidation (commits `9fbd1972a`, `ea38ac190`)

- 5 test categories → 3 (unit / canonical / integration)
- 688 collected tests → 508 tests
- Deleted `tests/functional/` directory
- Deleted `tests/e2e/` directory
- Old golden test infrastructure merged into `tests/canonical/`
- Task data refreshed: `user_simulator.script` → `scripted_flow`, descriptive task names
- food_delivery_2 project trimmed (~60 files removed including `tau_bench/`, `tau_tools/`)
- 2,275+ lines deleted

### Stages D + E — Repository Hygiene (commits `efa5a31c9`, `b8c5eadcc`)

- `.gitattributes` cleaned (removed blanket LFS, added targeted rules)
- `.gitignore` updated (added `converted/`)
- 39 orphaned test data files removed
- Ghost fixtures removed
- `tests/README.md` rewritten (733 → 155 lines)
- `with_env.sh` documentation contradiction fixed
- Module docstrings added to test modules
- 3 unused test util modules deleted
- 7 unused conftest fixtures removed

### Stage G — Delete Dead-Skip Tests & Fix Adapter Imports

- **103 → 40 skips** (63 fewer skips, 22 more passing tests)
- Fixed external adapter imports (later removed in open-source branch)
- Fixed `food_delivery_2_trajectory_051fa6cb` fixture path (UUID-based dir)
- Generated missing canonical snapshot for tau conversion
- Added graceful skip guards for TlkMcpCore when testcases data unavailable
- Deleted 3 telecom-dependent integration test files (27 dead tests):
  `test_docker_grading_native.py`, `test_docker_grading_tau.py`, `test_e2e_tau.py`
- Deleted 9 `TRIAL_DIR`-dependent tests from `test_custom_checks_canon.py`
  (no trial data exists for `order_modify_with_checks`)
- Deleted 3 dead-skip tests from `test_grading_pipeline_canon.py`
- Deleted `test_8_workers_no_deadlock` from `test_performance.py`
- Deleted `test_user_tool_executor` from `test_dual_control.py` (telecom reference)
- Cleaned up orphaned imports and dead code

### Stage H — Remove contrib-dependent tests (40 → 23 skips)

- **40 → 23 skips** (17 fewer skips) by deleting 18 tests with impossible dependencies
- Deleted `test_grading_hash.py` entirely (2 tests) — depended on
  `TlkMcpCoreAdapter` + `contrib/` paths + running DB service
- Trimmed `test_docker_grading.py` from 9 → 1 test — deleted 7 tests that referenced
  non-existent `domains/external_retail_v3/testcases/` path (would always skip)
- Trimmed `test_orchestrator_docker.py` from 6 → 1 test — same non-existent path issue
- Trimmed `test_e2e_tlk_mcp_core.py` from 10 → 6 tests — deleted 4 tests (c, e, f, g)
  that required `mcp_tools_library` for live tool reconstruction and execution
- Kept Docker health check tests (work when containers are running)
- Kept 6 synthetic-data E2E tests (a, b, d, h, i, j) that work without external deps

### Phase 3 — CI Hardening with Merge Queue + Coverage

- **449 → 711 total tests** (+262 tests from subtasks 1–3)
  - Unit: 303 → 496 (+193)
  - Canonical: 33 → 73 (+40)
  - Integration: 113 → 142 (+29)
- Added label-triggered `test-gate` job to CI
  (full suite + coverage enforcement when `ready-to-merge` label applied)
- Added `pytest-cov` dependency and `[tool.coverage.*]` config in `pyproject.toml`
  with `fail_under = 60` threshold
- Removed duplicate `MockAsyncClient` from `test_e2e_tlk_mcp_core.py` —
  now imports from shared `tests/utils/mock_clients.py`

---

## Test Categories

### Unit (`tests/unit/`)

Fast, isolated tests with no external dependencies. Covers:
- Model serialization and validation
- Grading logic (hash, transcript, state checks, fuzzy compare)
- Adapter contracts (native, tau, tlk_mcp_core)
- CLI parsing and configuration
- Core utilities (pricing, metrics, resume, logging)
- Search and Typesense integration stubs

### Canonical (`tests/canonical/`)

Snapshot-based golden tests using `canon_snapshot` fixture. Covers:
- Adapter conversion output stability
- Grading pipeline output determinism
- Task validation schema compliance

Update snapshots with: `uv run pytest tests/canonical/ --update-canon`

### Integration (`tests/integration/`)

Tests requiring services or Docker. Most are skip-gated by environment markers. Covers:
- Docker container lifecycle and health checks
- gRPC runner/executor/agent service communication
- End-to-end grading with live DB service
- Nova API integration
- Security and isolation checks

---

## Skipped Tests

| Group | Count | Reason |
|-------|-------|--------|
| Integration — Nova API | 21 | Require `NOVA_API_KEY` |
| Integration — Docker/network | 1 | Requires Docker images (`tolokaforge-runner`, etc.) |
| Integration — Typesense | 1 | Requires Docker daemon for Typesense server |

All skips are conditional (`skipif` / `skip`) with legitimate real-service reasons.
No dead-skip tests remain. No contrib-dependent tests remain.

---

## CI Strategy

| Event | Job | Scope |
|-------|-----|-------|
| `pull_request` | `test-smoke` | Unit + canonical (fast, ~2 min) |
| `pull_request` + label `ready-to-merge` | `test-gate` | Full suite + coverage (`--cov-fail-under=60`) |
| `push` to `main` / `schedule` | `test-full` | Everything + Docker builds (nightly) |

---

## Running Tests

```bash
# All tests (unit + canonical, integration skipped without services)
uv run pytest tests/ -v

# Unit tests only
uv run pytest tests/ -v -m unit

# Canonical tests only
uv run pytest tests/ -v -m canonical

# Integration tests (requires services + .env)
scripts/with_env.sh uv run pytest tests/ -v -m integration

# Update canonical snapshots
uv run pytest tests/canonical/ --update-canon
```
