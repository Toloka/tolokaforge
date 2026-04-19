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

**Open:**
- Hash-based grading method
- Custom checks grading method
- Unstable fields / unstable extra fields
- Initial state data patches
- TypeSense RAG search integration
- Multiple LLM providers (only OpenRouter/Anthropic tested)
- `tolokaforge docker build` / `tolokaforge docker up` CLI commands

### Issue 5 — `analyze_results` example crashes on successful runs (BUG)

**File:** `examples/analyze_results/analyze_run.py:162`

`summarize_failure_attributions([])` returns `{"deterministic_attribution_coverage": None}`. The `.get("deterministic_attribution_coverage", 0.0)` returns `None` (key exists with `None` value, not missing). Then `{coverage:.3f}` throws `TypeError`.

**Fix:** `coverage=failure_summary.get("deterministic_attribution_coverage") or 0.0`

### Issue 6 — `trajectory.yaml` excludes `tool_log` — breaks analysis round-trip (BUG)

**File:** `tolokaforge/core/output_writer.py:77-96`

`write_trajectory()` only writes messages metadata — no `tool_log`. When `analyze_run.py` loads trajectory.yaml to reconstruct `Trajectory`, `tool_log` defaults to `[]`. This breaks:
- Failure attribution (empty tool evidence)
- Per-tool analytics
- `failure_attribution.py:128` false positive on "missing_tool"

**Fix:** Save tool_log in trajectory.yaml or as a separate `tool_log.yaml` that `analyze_run.py` loads.

### Issue 7 — Docker network/container name collisions prevent concurrent runs (BUG)

**Files:** `tolokaforge/docker/network.py:188-222`, `tolokaforge/docker/stack.py`

Fixed names (`runner-net`, `tolokaforge-runner`, `tolokaforge-db-service`) cause 409 Conflict errors when two `tolokaforge run` processes execute simultaneously. The `Network.create()` has a TOCTOU race between `_find_existing_network()` and `client.networks.create()`.

**Fix:** Add unique run-id suffix to Docker resource names, or catch 409 and retry.

### Issue 8 — Tool duration measurements meaningless in Docker mode

`DockerRunnerAdapter` measures only gRPC round-trip (0.001–0.002s), not actual tool execution time inside the container. Makes `tool_usage.total_duration_s` misleading.

### Issue 9 — `mock_web_url` in env.yaml uses Docker-internal DNS

`env.yaml` shows `mock_web_url: http://mock-web:8080` — only useful inside Docker. Confusing for post-hoc analysis from the host.

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
> **Priority:** ASAP — these affect example usability and analysis tooling.

### Bug fixes needed

1. **`analyze_run.py` NoneType crash** (Issue 5)
   - Fix `.get()` fallback for `None` values
   - Test with zero-failure and non-zero-failure runs

2. **`trajectory.yaml` missing `tool_log`** (Issue 6)
   - Add `tool_log` to `write_trajectory()` output
   - Update `analyze_run.py` to load tool_log if present
   - Verify failure attribution works with loaded trajectories

3. **Docker network race condition** (Issue 7)
   - Catch 409 Conflict in `Network.create()` and retry with lookup
   - Consider adding run-id suffix to Docker resource names

### Verification

- [ ] `analyze_run.py` succeeds against zero-failure run directory
- [ ] `analyze_run.py` succeeds against runs with failures
- [ ] `trajectory.yaml` includes tool_log data
- [ ] Failure attribution works on loaded trajectories
- [ ] `Network.create()` handles 409 Conflict gracefully

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

---

## Migration Checklist

### Stage 9 — Dockerfile Review + Runner Cleanup
- [ ] Strip domain directories from runner.Dockerfile, core.py, builder.py
- [ ] Audit all 8 Dockerfiles for necessity
- [ ] Verify minimal runner image works with frozen_mcp_core

### Stage 10 — Critical Bug Fixes (ASAP)
- [ ] Fix `analyze_run.py` NoneType crash
- [ ] Fix `trajectory.yaml` missing tool_log
- [ ] Fix Docker network race condition (409 Conflict)

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
