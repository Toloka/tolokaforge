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

After trial completion, `env.yaml` only shows initial filesystem state (files from `initial_state.filesystem.copy`). Files written by the agent during execution are absent. The grading works (Runner has direct filesystem access) but post-hoc analysis tools see incomplete state.

**Confirmed by example runs (2026-04-19):**
- `custom_grading`: env.yaml has `prompt.txt` but not `submissions/knowledge_hypothesis_rationale.md`
- `package_api`: env.yaml has `problem.txt` but not `submissions/answer.md`
- `browser_task`: env.yaml has `policy_brief.txt` but not `submissions/browser_policy_response.md`

**Fix requires:** Extending the GetState gRPC response to include filesystem state from the Runner container, or adding a dedicated filesystem sync RPC.

### Issue 4 — Feature verification gaps

**Verified (2026-04-19 example analysis):**
- ✅ LLM judge grading (custom_grading: judge score=0.92, combined=0.984)
- ✅ Combined weighted grading (state 0.6 + transcript 0.2 + judge 0.2)
- ✅ JSONPath file assertions (contains_ci, path_glob)
- ✅ Transcript rules with `required_actions` and `disallow_regex`
- ✅ Browser tool end-to-end (mock-web + Playwright, score=0.825)
- ✅ Multi-turn conversation (scripted user in custom_grading, LLM user in browser_task)
- ✅ Distributed execution (workers=2, repeats=2, SQLite backend)
- ✅ Package API programmatic run (Orchestrator + RunConfig from Python)
- ✅ Docker auto-start lifecycle (build, start, health check, stop, destroy)
- ✅ Cost tracking (per-trial and aggregate)
- ✅ Grade component `None` sentinel (replaces old `-1.0`)
- ✅ `analyze_results` example loads trajectories, computes metrics, reports pass rates
- ✅ `trajectory.yaml` includes full `tool_log` data

**Open:**
- Hash-based grading method
- Custom checks grading method
- Unstable fields / unstable extra fields
- Initial state data patches
- TypeSense RAG search integration
- Multiple LLM providers (only OpenRouter/Anthropic tested)
- `tolokaforge docker build` / `tolokaforge docker up` CLI commands

### Issue 7 — Docker network/container name collisions prevent concurrent runs (BUG)

**Files:** `tolokaforge/docker/network.py:188-222`, `tolokaforge/docker/stack.py`

Fixed names (`runner-net`, `tolokaforge-runner`, `tolokaforge-db-service`) cause 409 Conflict errors when two `tolokaforge run` processes execute simultaneously. The `Network.create()` has a TOCTOU race between `_find_existing_network()` and `client.networks.create()`.

**Fix:** Add unique run-id suffix to Docker resource names, or catch 409 and retry.

### Issue 8 — Tool duration measurements meaningless in Docker mode

`DockerRunnerAdapter` measures only gRPC round-trip (0.001–0.002s), not actual tool execution time inside the container. Makes `tool_usage.total_duration_s` misleading.

### Issue 9 — `mock_web_url` in env.yaml uses Docker-internal DNS

`env.yaml` shows `mock_web_url: http://mock-web:8080` — only useful inside Docker. Confusing for post-hoc analysis from the host.

### Issue 10 — BuiltinGenericToolWrapper masks tool failures as successes (BUG — CRITICAL)

**Files:** `tolokaforge/runner/tool_factory.py:700-704`, `tolokaforge/runner/service.py:780-788`

When a builtin tool (browser, calculator, etc.) returns `ToolResult(success=False, error="...")`, `BuiltinGenericToolWrapper.execute()` converts it to the string `"Error: {error}"` and returns it. The runner service (`service.py:787`) then treats any string return as `EXECUTION_STATUS_SUCCESS` since no exception was raised.

**Impact chain confirmed via browser_task run (2026-04-19):**
1. BrowserTool.execute(`{}`) → `ToolResult(success=False, error="Missing required 'actions' parameter...")`
2. BuiltinGenericToolWrapper.execute() → returns string `"Error: Missing required 'actions' parameter..."`
3. runner/service.py → `EXECUTION_STATUS_SUCCESS` (line 787: no exception = success)
4. docker_adapter.py → logs `success: True` with error text in output field

**Corruption effects:**
- `tool_success_rate` artificially inflated (shows 100% when tool had validation errors)
- `failure_attribution` cannot detect tool argument errors (tool evidence is empty)
- `tool_usage.error_count` always 0 even when tools returned errors
- `metrics.yaml` records `tool_success_rate: 1.0` for browser task despite failed browser call

**Fix:** `BuiltinGenericToolWrapper.execute()` should raise an exception on `result.success == False`, letting the runner service mark it as `EXECUTION_STATUS_ERROR`. Alternatively, refactor the runner service to understand structured `ToolResult` returns.

### Issue 11 — Missing `.env.example` referenced in README (USABILITY)

**File:** `README.md:36`

README Quick Start says `cp .env.example .env` but `.env.example` does not exist in the repository. New users see a broken first step.

**Fix:** Create `.env.example` with documented placeholder variables.

### Issue 12 — Browser task `contains_ci` state check is brittle (TASK QUALITY)

**File:** `examples/browser_task/dataset/tasks/browser/browser_public_example_01/grading.yaml:11`

The state check `contains_ci: "not eligible for cancellation"` expects the exact phrase "not eligible for cancellation". The agent correctly identifies that cancellation is not allowed and uses semantically equivalent phrases ("cannot be cancelled", "Cancellation DENIED", "cancellation is not permitted"), but the literal substring match fails.

This reduces the task score from 1.0 to 0.825 (state_checks: 0.75 instead of 1.0) even though the agent's reasoning and conclusion are correct.

**Fix:** Broaden the check to accept common phrasings: `contains_any_ci: ["not eligible for cancellation", "cannot be cancelled", "cancellation is not permitted", "cancellation denied"]` or use a regex pattern.

### Issue 13 — `run_state.json` config_path inconsistency (MINOR)

**Files:** `tolokaforge/core/orchestrator.py`, `tolokaforge/core/resume.py`

When using the programmatic API (`Orchestrator(RunConfig(...))`), `run_state.json.config_path` is an absolute path (e.g., `/workspaces/tolokaforge_opensource/results/package_api`). When using CLI (`tolokaforge run --config ...`), it's a relative path (e.g., `results/custom_grading`). This inconsistency could break tooling that resolves paths from `run_state.json`.

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

## Stage 10 — Critical Bug Fixes

> **Goal:** Fix bugs discovered during 2026-04-19 example analysis.
> **Priority:** ASAP — these affect metric accuracy and analysis tooling.

### Bug fixes needed

1. **BuiltinGenericToolWrapper masks tool failures** (Issue 10 — CRITICAL)
   - Raise exception or return structured error from `BuiltinGenericToolWrapper.execute()`
   - Ensure runner service records correct `EXECUTION_STATUS_ERROR`
   - Verify tool_success_rate and failure_attribution reflect actual errors

2. **Docker network race condition** (Issue 7)
   - Catch 409 Conflict in `Network.create()` and retry with lookup
   - Consider adding run-id suffix to Docker resource names

3. **Missing `.env.example`** (Issue 11)
   - Create `.env.example` with documented API key placeholders
   - Ensure README Quick Start works for new users

4. **Browser task brittle state check** (Issue 12)
   - Broaden `contains_ci` to accept common paraphrases

### Verification

- [ ] Tool validation errors recorded as `success=false` in tool_log
- [ ] `tool_success_rate` reflects actual success/failure ratio
- [ ] `failure_attribution` detects tool argument errors
- [ ] `Network.create()` handles 409 Conflict gracefully
- [ ] `.env.example` exists and matches README instructions
- [ ] Browser task state check passes for semantically correct responses

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

- [ ] env.yaml captures agent-written files (Issue 3 — requires gRPC extension)
- [ ] Tool duration reflects actual execution time, not just gRPC round-trip (Issue 8)
- [ ] `mock_web_url` in env.yaml uses host-accessible URL (Issue 9)
- [ ] Add stale results directory cleanup mechanism
- [ ] Normalize `run_state.json` config_path to always be relative (Issue 13)

---

## Migration Checklist

### Stage 9 — Dockerfile Review + Runner Cleanup
- [ ] Strip domain directories from runner.Dockerfile, core.py, builder.py
- [ ] Audit all 8 Dockerfiles for necessity
- [ ] Verify minimal runner image works with frozen_mcp_core

### Stage 10 — Critical Bug Fixes (ASAP)
- [ ] Fix BuiltinGenericToolWrapper masking tool failures (Issue 10)
- [ ] Fix Docker network race condition (Issue 7)
- [ ] Create `.env.example` (Issue 11)
- [ ] Fix browser task brittle state check (Issue 12)

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
- [ ] env.yaml captures agent-written files (requires gRPC extension)
- [ ] Tool duration reflects actual execution time
- [ ] mock_web_url uses host-accessible URL
- [ ] Stale results directory cleanup
- [ ] Normalize run_state.json config_path
