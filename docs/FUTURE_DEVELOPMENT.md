# Future Development Plan

> **Updated:** 2026-04-19
> **Scope:** Dockerfile cleanup, feature verification, remaining validation gaps, critical bug fixes

---

## Completed Work

| Stage | Achievement |
|-------|------------|
| 0–6 | Task data separation, Docker Python layer, adapter plugins, native conversion layer, canonical test infrastructure |
| 7 | Test failures fixed: 31→0, dead-skip tests deleted. Final: 427 passed, 23 skipped |
| 10 | Test consolidation: 5→3 categories (unit/canonical/integration). 688→449 tests |
| 13 | `.gitattributes` cleaned, 39 orphaned files removed |
| FrozenMcpCoreAdapter | Self-contained converted tasks with `_domain/` bundle, `tool_artifacts` delivery, stable hash grading |
| SecretManager | Universal secret provider via `init_default`/`get_default`. Serialization for Runner (`TOLOKAFORGE_SECRETS_JSON`) |
| LLM Judge in Runner | Runner evaluates `llm_judge` via litellm. Cost tracking via `judge_cost_usd` proto field. Robust JSON extraction |
| SQLite resilience (14) | Thread-local connections, retry with backoff, WAL checkpointing, orchestrator exception safety net |
| Judge cost (15) | Full pipeline from `evaluate_llm_judge()` → proto → orchestrator metrics |
| Browser tool (16) | Tool schema documented, `execute()` API fixed, mock-web service auto-started, initial_url injected into system prompt, Docker DNS resolution fixed |
| Health noise (17) | Health check polling failures downgraded to DEBUG |
| Container reuse | Fixed `attrs` bug in `ServiceStack._start_service` |
| Pydantic fix (9) | `CommandHealthProbe.command` renamed to `cmd` with alias |
| Tool duration (10) | `output_writer.py` sums `duration_s` from tool logs |
| Browser infrastructure | Mock-web auto-starts via `core_stack(enable_mock_web=True)`. Task packs bind-mounted. `_resolve_url()` maps short Docker names to container names. System prompt injects browser URL + task guidance |
| Grade components | `-1.0` sentinel replaced with `None` for unconfigured components |
| Failure attribution | Infrastructure errors detected (connection refused, missing tools). Coverage returns `None` for 0/0 |
| Test suite (current) | 1000 unit + 72 canonical tests passing. 3/3 example task YAMLs valid |
| E2E runs (2026-04-19) | All 4 examples run with real LLM (OpenRouter/claude-sonnet-4-6). All pass. See [E2E Run Results](#e2e-run-results-2026-04-19) |

---

## E2E Run Results (2026-04-19)

All examples executed with real LLM providers (OpenRouter, `anthropic/claude-sonnet-4-6`).

| Example | Tasks | Trials | Pass Rate | Score | Cost | Latency | Notes |
|---------|-------|--------|-----------|-------|------|---------|-------|
| `custom_grading` | 1 | 1 | 100% | 0.984 | $0.068 | 47.9s | Weighted: state=1.0, transcript=1.0, judge=0.92 |
| `package_api` | 1 | 1 | 100% | 1.000 | $0.012 | 8.9s | Programmatic API run |
| `distributed_run` | 1 | 2 | 100% | 1.000 | $0.026 | 7.7s avg | 2 workers, 2 repeats, parallel execution |
| `browser_task` | 1 | 1 | 100% | 1.000 | $0.063 | 37.5s | Mock-web + Playwright, 1 tool error recovered |
| `analyze_results` | — | — | — | — | — | — | Ran on all 4 runs, no crashes |

**Key observations:**
- All grading methods work: state_checks, transcript_rules, llm_judge, combined weighted
- Browser tool recovered from initial error (agent sent `{}` instead of `{actions: [...]}`)
- Docker lifecycle clean: build→start→health→run→stop→destroy with no leaks
- `tool_success_rate` correctly reflects the browser tool error (0.857 = 6/7)
- `analyze_results` handles zero-failure runs correctly

---

## Resolved Issues (verified 2026-04-19)

### ~~Issue 5 — `analyze_results` example crashes on successful runs~~ → RESOLVED

**Status:** Fixed. The code at `analyze_run.py:161-165` now correctly handles `None` coverage with an explicit `if coverage is None` check. **E2E verified:** `analyze_run.py` ran successfully on all 4 run directories with zero failures.

### ~~Issue 6 — `trajectory.yaml` excludes `tool_log`~~ → RESOLVED

**Status:** Fixed. **E2E verified:** All trial directories include `tool_log` in trajectory.yaml with tool name, success status, output, error, and duration fields. Confirmed in custom_grading (4 tool calls), package_api (2 tool calls), browser_task (7 tool calls).

### ~~Issue 7 — Docker network/container name collisions~~ → RESOLVED

**Status:** Fixed in `tolokaforge/docker/network.py:221-239`. The 409 Conflict race condition is handled. Container stale-removal handled in `container.py:353-375`.

### ~~Issue 10 — BuiltinGenericToolWrapper masks tool failures~~ → RESOLVED

**Status:** Fixed in `tolokaforge/runner/tool_factory.py:712-723`. **E2E verified:** Browser task tool_success_rate=0.857 correctly reflects 1 error out of 7 calls. `tool_usage.error_count=1` for browser tool is accurate.

### ~~Issue 11 — Missing `.env.example`~~ → RESOLVED

**Status:** `.env.example` exists with documented API key placeholders.

### ~~Issue 8 — Tool duration measurements meaningless in Docker mode~~ → RESOLVED

**Status:** Fixed. Server-side measurement via `time.time()` in Runner service. **E2E verified:** tool durations in metrics.yaml are sub-ms for file I/O (read_file: ~0.3ms, write_file: ~0.5ms) and ~100ms for browser actions — consistent with Docker-local execution.

### ~~Issue 13 — `run_state.json` config_path inconsistency~~ → RESOLVED

**Status:** Fixed. **E2E verified:** `run_state.json` shows relative path `results/browser_task` (CLI) and absolute path for programmatic API — both correct.

### ~~Issue 14 — `_grade_via_runner_rpc` incorrect return type annotation~~ → RESOLVED

**Status:** Fixed. At `orchestrator.py:1425`, `_grade_via_runner_rpc` is now annotated `-> tuple[Grade, float]`, matching the actual return type.

### ~~Issue 15 — Invalid `# noqa` directive~~ → RESOLVED

**Status:** Fixed. The `# noqa: WPS433` directive no longer exists in `frozen_mcp_core.py`. The late imports in `adapters/__init__.py` correctly use `# noqa: E402`.

---

## Open Issues

### Issue 16 — `mock_web_url` leaks into ALL env.yaml files (CRITICAL — abstraction leak)

**File:** `tolokaforge/core/env_state.py:59`

**Root cause:** `self.mock_web_url: str = "http://mock-web:8080"` is a **hardcoded non-empty default**. In `get_final_state()` at line 174, `if self.mock_web_url:` is always true because the default is never falsy. This causes `mock_web_url: http://mock-web:8080` to appear in **every** env.yaml, including tasks that never use mock_web.

**E2E evidence:** All 4 runs show `mock_web_url: http://mock-web:8080` in env.yaml — including `custom_grading` and `package_api` which are pure knowledge_reasoning tasks with no browser component.

**Impact:**
1. Leaks Docker-internal URLs into non-Docker task results
2. Confuses analysis tools and humans reading results
3. Violates "don't leak abstractions" principle

**Fix:** Change default from `"http://mock-web:8080"` to `""`. The conditional in `get_final_state()` will then correctly omit the field for tasks that don't configure mock_web. Same fix for `json_db_url` and `rag_service_url` defaults (lines 57-58) — while these don't currently leak into env.yaml, their always-truthy defaults are a latent bug that would bite any code using `if env_state.json_db_url:`.

### Issue 3 — env.yaml does not capture agent-written files (CONFIRMED)

**E2E evidence:** All 4 runs show only initial-state files in env.yaml filesystem:
- `custom_grading`: only `prompt.txt`, missing `submissions/knowledge_hypothesis_rationale.md`
- `package_api`: only `problem.txt`, missing `submissions/answer.md`
- `distributed_run`: only `problem.txt`, missing `submissions/answer.md`
- `browser_task`: only `policy_brief.txt`, missing `submissions/browser_policy_response.md`

**Root cause:** `sync_filesystem_from_disk()` at `env_state.py:233-252` is dead code — never called. In Docker mode, the orchestrator can't access the Runner container's filesystem (`/work/`). The `GetState` gRPC returns only DB state, not filesystem state.

**Fix options:**
1. **(Minimal)** For non-Docker mode: call `env_state.sync_filesystem_from_disk()` before `get_final_state()` in the orchestrator.
2. **(Full)** Extend `GetState` gRPC to include filesystem state from the Runner container, or add a dedicated filesystem sync RPC.

### Issue 1 — Runner Docker image includes unnecessary domain files

The Runner image bakes in domain-specific directories causing unnecessary rebuilds, slow context assembly, and bloated images. Three synchronized locations need cleanup:

| Location | What it contains |
|----------|-----------------|
| `docker/runner.Dockerfile` | COPY commands + PYTHONPATH for domain dirs |
| `tolokaforge/docker/stacks/core.py` | `context_files` list with domain dirs |
| `tolokaforge/docker/builder.py` | `IMAGE_DEFINITIONS` with domain dirs |

### Issue 2 — `docker/` directory audit needed

8 Dockerfiles exist. Questions:
- `json_db.Dockerfile` and `db_service.Dockerfile` may overlap
- `orchestrator.Dockerfile` and `agent.Dockerfile` may be obsolete
- Should Dockerfiles move inside `tolokaforge/docker/dockerfiles/`?

### Issue 12 — Browser task state checks could be more robust (TASK QUALITY)

**File:** `examples/browser_task/dataset/tasks/browser/browser_public_example_01/grading.yaml:10`

The state checks use literal substring matching (`contains_ci: "not permitted"`, `contains_ci: "7 business days"`, etc.). Agents using different wording will fail. Consider regex or `contains_any_ci` for checks where multiple valid phrasings exist.

### Issue 4 — Feature verification gaps

**Verified (2026-04-19 — E2E with real LLM via OpenRouter):**
- ✅ LLM judge grading (custom_grading: llm_judge.model_ref + rubric + output_schema, score=0.92)
- ✅ Combined weighted grading (state 0.6 + transcript 0.2 + judge 0.2 → 0.984)
- ✅ JSONPath file assertions (contains_ci, path_glob — all 4 checks passed)
- ✅ Transcript rules with `required_actions` and `disallow_regex`
- ✅ Browser tool support (mock-web + Playwright, grading via state_checks + transcript_rules)
- ✅ Multi-turn conversation (scripted user in custom_grading, LLM user in browser_task)
- ✅ Distributed execution (workers=2, repeats=2, SQLite backend, parallel)
- ✅ Package API programmatic run (Orchestrator + RunConfig from Python)
- ✅ Docker auto-start lifecycle (build, start, health check, stop, destroy)
- ✅ Cost tracking (per-trial and aggregate, including judge cost)
- ✅ Grade component `None` sentinel (replaces old `-1.0` at proto boundary)
- ✅ `analyze_results` loads trajectories, computes metrics (pass@k, cost, latency)
- ✅ `trajectory.yaml` includes full `tool_log` data
- ✅ BuiltinGenericToolWrapper properly raises on tool failure
- ✅ Docker network 409 race condition handled
- ✅ Tool duration uses server-side measurement in Docker mode
- ✅ Failure attribution coverage returns `None` for 0/0 (no failures)
- ✅ `tolokaforge status --run-dir` CLI works correctly
- ✅ metadata_slices.json (by_benchmark_type, by_complexity) generated
- ✅ run_state.json tracks trial status with timestamps
- ✅ Unit tests: 1000 passed, 6 skipped
- ✅ Canonical tests: 72 passed, 1 skipped
- ✅ Lint: all checks passed
- ✅ Task validation: 3/3 example tasks valid

**Open:**
- Hash-based grading method (end-to-end with real LLM)
- Custom checks grading method (end-to-end with real LLM)
- Unstable fields / unstable extra fields
- Initial state data patches
- TypeSense RAG search integration
- Multiple LLM providers (only OpenRouter tested; ANTHROPIC_API_KEY present but not exercised)
- `tolokaforge docker build` / `tolokaforge docker up` CLI commands

---

## Stage 9 — Dockerfile Review and Runner Image Cleanup

> **Goal:** Make the Runner Docker image domain-agnostic.

### Approach

Convert all tasks to frozen format, use `frozen_mcp_core` exclusively for Docker runs. Runner image only contains `tolokaforge/` + `pyproject.toml` + `README.md`.

### Steps

1. Strip domain directories from `runner.Dockerfile` COPY commands and PYTHONPATH
2. Strip domain directories from `core.py` `context_files`
3. Strip domain directories from `builder.py` `IMAGE_DEFINITIONS`
4. Audit all 8 Dockerfiles for necessity
5. Verify `frozen_mcp_core` tasks still work end-to-end with minimal image

### Verification

- [ ] Runner image builds with only `tolokaforge/` + `pyproject.toml` + `README.md`
- [ ] `frozen_mcp_core` tasks execute correctly with minimal image
- [ ] No orphaned Dockerfiles
- [ ] `tolokaforge docker build --core` succeeds

---

## Stage 10 — Bug Fixes

> **Goal:** Fix bugs discovered during E2E validation.

### Resolved (2026-04-19)

1. **`_grade_via_runner_rpc` return type** (Issue 14) → already correct: `-> tuple[Grade, float]`
2. **Invalid `# noqa` directive** (Issue 15) → already removed from `frozen_mcp_core.py`

### Bug fixes needed (ASAP)

1. **`EnvironmentState` service URL defaults leak Docker internals** (Issue 16)
   - Change `mock_web_url` default from `"http://mock-web:8080"` to `""`
   - Change `json_db_url` default from `"http://json-db:8000"` to `""`
   - Change `rag_service_url` default from `"http://rag-service:8001"` to `""`
   - Callers that need these URLs must set them explicitly (already done in `executor/service.py`)

2. **`sync_filesystem_from_disk` dead code** (Issue 3 — partial fix)
   - Call `sync_filesystem_from_disk()` before `get_final_state()` in orchestrator — non-Docker mode only
   - Document that Docker mode requires gRPC extension for full fix

### Verification

- [x] `_grade_via_runner_rpc` type annotation matches actual return
- [x] No ruff warnings about invalid noqa directives
- [ ] Service URL defaults are empty strings, not Docker-internal URLs
- [ ] `mock_web_url` absent from env.yaml for non-mock-web tasks
- [ ] Non-Docker mode env.yaml includes agent-written files
- [ ] All 1000 unit + 72 canonical tests still pass

---

## Stage 11 — End-to-End Adapter and Provider Validation

> **Goal:** Validate full pipeline for each adapter and LLM provider.
> **Depends on:** Stage 9

### Remaining work

- [ ] FrozenMcpCoreAdapter extended validation (TypeSense, user LLM, data patches)
- [ ] Other LLM providers tested (OpenAI direct, Anthropic direct, Google)
- [ ] `tolokaforge docker build` / `tolokaforge docker up` CLI commands tested

---

## Stage 12 — Feature Verification Matrix

> **Goal:** Systematically verify every grading/task feature works correctly.
> **Depends on:** Stage 11

### Remaining features to verify

| Feature | How to verify |
|---------|--------------|
| Hash-based grading | Run frozen retail task, compare final DB state against golden hash |
| Custom checks | Run task with `custom_checks.script` Python grading logic |
| Unstable fields | Create test with `unstable_fields`, verify hash exclusion |
| Data patches | Verify `data_patch` overrides merge with base state |
| User simulator context | Verify backstory/context injection across turns |

---

## Stage 13 — Analysis Tooling and Observability

> **Goal:** Fix agent state capture and analysis tooling.
> **Depends on:** Stage 10

### Work items

- [ ] env.yaml captures agent-written files in Docker mode (Issue 3 — requires gRPC extension)
- [ ] Add stale results directory cleanup mechanism

---

## Migration Checklist

### Stage 9 — Dockerfile Review + Runner Cleanup
- [ ] Strip domain directories from runner.Dockerfile, core.py, builder.py
- [ ] Audit all 8 Dockerfiles for necessity
- [ ] Verify minimal runner image works with frozen_mcp_core

### Stage 10 — Bug Fixes (ASAP)
- [x] Fix `_grade_via_runner_rpc` return type annotation (Issue 14) — already correct
- [x] Fix invalid `# noqa: WPS433` directive (Issue 15) — already removed
- [ ] Fix `EnvironmentState` service URL defaults (Issue 16) — hardcoded Docker URLs
- [ ] Call `sync_filesystem_from_disk()` in non-Docker orchestrator path (Issue 3 partial)

### Stage 11 — E2E Validation (remaining)
- [ ] FrozenMcpCoreAdapter extended validation (TypeSense, user LLM, data patches)
- [ ] Other LLM providers tested
- [ ] Docker CLI commands tested

### Stage 12 — Feature Verification (remaining)
- [ ] Hash-based grading verified
- [ ] Custom checks grading verified
- [ ] Unstable fields work correctly
- [ ] Data patches work
- [ ] User simulator context maintenance

### Stage 13 — Analysis Tooling
- [ ] env.yaml captures agent-written files in Docker mode (requires gRPC extension)
- [ ] Stale results directory cleanup
