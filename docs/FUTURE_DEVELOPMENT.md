# Future Development Plan

> **Updated:** 2026-04-19
> **Scope:** Dockerfile cleanup, feature verification, remaining validation gaps

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

**Fix requires:** Extending the GetState gRPC response to include filesystem state from the Runner container, or adding a dedicated filesystem sync RPC.

### Issue 4 — Feature verification gaps

**Verified:**
- ✅ LLM judge grading (custom_grading: score=0.92)
- ✅ Combined weighted grading (state + transcript + judge)
- ✅ JSONPath file assertions
- ✅ Transcript rules with `required_actions`
- ✅ Browser tool end-to-end (mock-web + Playwright)
- ✅ Multi-turn conversation (scripted and LLM user)
- ✅ Distributed execution (workers=2, repeats=2)

**Open:**
- Hash-based grading method
- Custom checks grading method
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

## Migration Checklist

### Stage 9 — Dockerfile Review + Runner Cleanup
- [ ] Strip domain directories from runner.Dockerfile, core.py, builder.py
- [ ] Audit all 8 Dockerfiles for necessity
- [ ] Verify minimal runner image works with frozen_mcp_core

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

### Open infrastructure
- [ ] env.yaml captures agent-written files (requires gRPC extension)
