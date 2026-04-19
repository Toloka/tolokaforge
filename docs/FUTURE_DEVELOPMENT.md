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

---

## Resolved Issues (verified 2026-04-19)

### ~~Issue 5 — `analyze_results` example crashes on successful runs~~ → RESOLVED

**Status:** Fixed. The code at `analyze_run.py:161-165` now correctly handles `None` coverage with an explicit `if coverage is None` check. Verified: `analyze_run.py` runs successfully on both zero-failure and successful run directories.

### ~~Issue 6 — `trajectory.yaml` excludes `tool_log`~~ → RESOLVED

**Status:** Fixed. `trajectory.yaml` now includes `tool_log` with full entries. Verified: all trial directories include `tool_log` in trajectory.yaml with tool name, success status, output, error, and duration fields.

### ~~Issue 7 — Docker network/container name collisions~~ → RESOLVED

**Status:** Fixed in `tolokaforge/docker/network.py:221-239`. The 409 Conflict race condition is handled: when `client.networks.create()` fails with 409, the code retries with `_find_existing_network()` lookup and reuses the existing network. Container stale-removal is also handled in `container.py:353-375`.

### ~~Issue 10 — BuiltinGenericToolWrapper masks tool failures~~ → RESOLVED

**Status:** Fixed in `tolokaforge/runner/tool_factory.py:712-723`. `BuiltinGenericToolWrapper.execute()` now raises `ToolExecutionError` when the underlying tool returns `result.success == False`. The runner service (`service.py:795-801`) catches this exception and correctly records `EXECUTION_STATUS_ERROR`. This ensures:
- `tool_success_rate` accurately reflects actual success/failure ratio
- `failure_attribution` detects tool argument and execution errors
- `tool_usage.error_count` correctly tracks failures

### ~~Issue 11 — Missing `.env.example`~~ → RESOLVED

**Status:** Fixed. `.env.example` exists with documented API key placeholders (OPENROUTER_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY). Matches README Quick Start instructions.

### ~~Issue 8 — Tool duration measurements meaningless in Docker mode~~ → RESOLVED

**Status:** Fixed. The Runner service measures actual tool execution time server-side via `time.time()` in `service.py:748-804`. The measured `latency_seconds` is reported through `response.metrics.latency_seconds` in the proto response. The `RunnerClient` in `docker_runtime.py:248-251` reads this server-side measurement (not gRPC round-trip). The orchestrator's `output_writer.py` correctly aggregates these durations in `tool_usage.total_duration_s`.

### ~~Issue 13 — `run_state.json` config_path inconsistency~~ → RESOLVED

**Status:** Fixed in `tolokaforge/core/resume.py`. Added `_normalize_to_relative()` static method to `RunStateManager` that converts absolute paths to CWD-relative paths when possible. Applied to both `config_path` and `output_dir` in `initialize_run()`. Now both CLI and programmatic API produce consistent relative paths in `run_state.json`.

---

## Open Issues

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

### Issue 3 — env.yaml does not capture agent-written files

After trial completion, `env.yaml` only shows initial filesystem state (files from `initial_state.filesystem.copy`). Files written by the agent during execution are absent. The grading works correctly (Runner has direct filesystem access) but post-hoc analysis tools see incomplete state.

**Root cause verified (2026-04-19):** `EnvironmentState.sync_filesystem_from_disk()` is defined at `tolokaforge/core/env_state.py:233-252` but **never called anywhere** in the codebase. This is dead code — no caller invokes it before `get_final_state()`.

In Docker mode, the orchestrator runs on the host and cannot access the Runner container's filesystem (files are at `/work/` inside the container). The `GetState` gRPC only returns DB state, not filesystem state.

**Fix options:**
1. **(Minimal)** For non-Docker mode: call `env_state.sync_filesystem_from_disk()` before `get_final_state()` in the orchestrator.
2. **(Full)** Extend `GetState` gRPC to include filesystem state from the Runner container, or add a dedicated filesystem sync RPC. This would also benefit `env.yaml` in Docker mode.

### Issue 9 — `mock_web_url` in env.yaml uses Docker-internal DNS

`env.yaml` shows `mock_web_url: http://mock-web:8080` — only useful inside Docker. Confusing for post-hoc analysis from the host.

### Issue 12 — Browser task state checks could be more robust (TASK QUALITY)

**File:** `examples/browser_task/dataset/tasks/browser/browser_public_example_01/grading.yaml:10`

The state checks use literal substring matching (`contains_ci: "not permitted"`, `contains_ci: "7 business days"`, etc.). While improved from the original "not eligible for cancellation" phrasing, agents that correctly identify the answer but use different wording (synonyms, paraphrases) will still fail. Consider regex or `contains_any_ci` for checks where multiple valid phrasings exist.

### Issue 14 — `_grade_via_runner_rpc` incorrect return type annotation (BUG)

**File:** `tolokaforge/core/orchestrator.py:1425`

The method is annotated as `-> Grade` but actually returns `tuple[Grade, float]` (line 1498: `return grade, grade_result.get("judge_cost_usd", 0.0)`; line 1514: `return Grade(...), 0.0`). The caller at line 1366 correctly destructures: `grade, judge_cost = self._grade_via_runner_rpc(...)`.

This doesn't cause runtime errors but violates type safety and will confuse type checkers (mypy/pyright) and IDEs.

**Fix:** Change annotation to `-> tuple[Grade, float]`.

### Issue 15 — Invalid `# noqa` directive (MINOR)

**File:** `tolokaforge/adapters/frozen_mcp_core.py:154`

Line has `# noqa: WPS433` (wemake-python-styleguide rule) but the project uses `ruff` for linting. Ruff warns: "Invalid rule code provided to `# noqa`". Should be changed to a standard `# noqa: E402` or just a comment explaining the late import.

### Issue 4 — Feature verification gaps

**Verified (2026-04-19 code analysis + test runs):**
- ✅ LLM judge grading (custom_grading grading config: llm_judge.model_ref + rubric + output_schema)
- ✅ Combined weighted grading (state 0.6 + transcript 0.2 + judge 0.2)
- ✅ JSONPath file assertions (contains_ci, path_glob)
- ✅ Transcript rules with `required_actions` and `disallow_regex`
- ✅ Browser tool support (mock-web + Playwright, grading via state_checks + transcript_rules)
- ✅ Multi-turn conversation (scripted user in custom_grading, LLM user in browser_task)
- ✅ Distributed execution (workers=2, repeats=2, SQLite backend)
- ✅ Package API programmatic run (Orchestrator + RunConfig from Python)
- ✅ Docker auto-start lifecycle (build, start, health check, stop, destroy)
- ✅ Cost tracking (per-trial and aggregate)
- ✅ Grade component `None` sentinel (replaces old `-1.0` at proto boundary via `_proto_score_to_optional`)
- ✅ `analyze_results` example loads trajectories, computes metrics, reports pass rates
- ✅ `trajectory.yaml` includes full `tool_log` data
- ✅ BuiltinGenericToolWrapper properly raises on tool failure
- ✅ Docker network 409 race condition handled
- ✅ Tool duration uses server-side measurement in Docker mode
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
- Multiple LLM providers (only OpenRouter/Anthropic tested)
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

> **Goal:** Fix bugs discovered during 2026-04-19 code analysis.

### Bug fixes needed

1. **`_grade_via_runner_rpc` return type** (Issue 14)
   - Fix type annotation from `-> Grade` to `-> tuple[Grade, float]`

2. **Invalid `# noqa` directive** (Issue 15)
   - Fix `frozen_mcp_core.py:154` to use valid ruff code or plain comment

3. **`sync_filesystem_from_disk` dead code** (Issue 3 — partial fix)
   - Call `sync_filesystem_from_disk()` in non-Docker mode orchestrator path
   - Document that Docker mode requires gRPC extension for full fix

### Verification

- [ ] `_grade_via_runner_rpc` type annotation matches actual return
- [ ] No ruff warnings about invalid noqa directives
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
- [ ] `mock_web_url` in env.yaml uses host-accessible URL (Issue 9)
- [ ] Add stale results directory cleanup mechanism

---

## Migration Checklist

### Stage 9 — Dockerfile Review + Runner Cleanup
- [ ] Strip domain directories from runner.Dockerfile, core.py, builder.py
- [ ] Audit all 8 Dockerfiles for necessity
- [ ] Verify minimal runner image works with frozen_mcp_core

### Stage 10 — Bug Fixes (ASAP)
- [ ] Fix `_grade_via_runner_rpc` return type annotation (Issue 14)
- [ ] Fix invalid `# noqa: WPS433` directive (Issue 15)
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
- [ ] mock_web_url uses host-accessible URL
- [ ] Stale results directory cleanup
