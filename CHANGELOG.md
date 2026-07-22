# Changelog

All notable changes to this project are documented in this file.

## Unreleased

### Feat

- **tools**: session-lifetime bash tool matching Anthropic `bash_20250124` — new `bash_session` schema (input: `command` string + `restart` bool) with two providers selectable purely by tool config: a local subprocess and a docker-compose `docker exec` into a running service (`tool_config: {service, compose_project_prefix}`). Identical wire schema either way. Additive; existing `bash` builtin unchanged.

## v0.9.3 (2026-07-22)

## v0.9.2 (2026-07-21)

### Feat

- **project-layer**: Project-layer v1 finalization — canonical shape with warn-only compat (M9) (#531)
- **runtime**: multi-container v1 completion (M8 consolidation) (#511)

### Fix

- **grading**: compare numerically-equal state values as equal (#532)
- **adapter**: fail conversion on invalid output (#494)
- **tools**: advertise PATCH requests (#463)

## v0.9.1 (2026-07-17)

## v0.9.0 (2026-07-17)

### Feat

- **examples,runtime,assets**: multi-container example depth (Milestone 18) (#469)
- **core**: observability seam extension — llm_call trio + model identity (#389) (#450)
- **automation**: model auto-integration pipeline (observe/resolve/finalize + Slack-triggered poller) (#154)
- **project-layer**: make Project schema end-to-end runnable — task-schema relaxation, grading_defaults merge, dead-seam cleanup, docs residue (#375) (#390)
- **skills**: milestone integration-branch workflow with rich consolidation PR (#372)
- **examples**: swap example-microservices-pack backend-api from fictional to postgrest (real image) (#367)
- **runtime**: per-service log capture on trial failure (#302) (#347)

### Fix

- **loader**: preserve storage discriminator tag under run_defaults merge (#312) (#365)

### Refactor

- **core**: extract RunDisplayEvents engine seam to main (#416) (#433)

### Perf

- **orchestration**: reclaim wall-clock in /implement-milestone via overlap, review sharding, and stack warmup (#426)

## v0.8.4 (2026-07-15)

### Feat

- **llm**: configurable hard wall-clock timeout for upstream calls (#327)
- **runtime**: enforce network_policy in docker provisioner + tests (#301) (#336)
- **examples**: runnable reset-recipe pack + end-to-end integration test (#299) (#314)
- **runtime**: Project layer runtime — isolation, reset recipes, capabilities, env identity (#298)
- **dev**: add cbm-onboard / cbm-offboard for codebase-memory-mcp (#266)
- **cli**: tolokaforge assets stamp verb (#263)
- **loader**: ${VAR} interpolation in run configs + --workers CLI flag (#262)
- **schema**: dual-home compute/storage.queue resolution (#241)
- **schema**: actor/seed/capability reservations + task-schema relaxation (#240)
- **schema**: EnvironmentPatch + resolve() + stack sub-object (#232)

## v0.8.3 (2026-07-13)

### Feat

- **loader**: resolve project.yaml + run_configs base+delta merge (#219)
- **schema**: add ProjectConfig, TaskDefaults, RunDefaults + compute/storage/observability blocks (#215)

### Fix

- **deps**: exclude litellm 1.92.0 due to fastapi import regression (#231)

## v0.8.2 (2026-07-10)

### Feat

- **models**: add tencent/hy3 (Hunyuan 3 GA) (#204)
- **models**: add openai/gpt-5.6-terra and openai/gpt-5.6-sol (#203)

## v0.8.1 (2026-07-09)

### Feat

- **models**: add x-ai/grok-4.5 (pricing + capability certificate) (#196)

## v0.8.0 (2026-07-06)

### Feat

- **runtime**: SharedStackRuntimeBackend consumes environment_manifest (#167)
- **runtime**: :local engine-image alias + wire environment_manifest through TaskConfig (#163)
- **core**: TrialExecutor Protocol + wire per-trial substrate bracket (#162)
- **metrics**: roll up judge cost at task and run level (#159)

### Fix

- **docker**: materialize engine wheel via reinstall provider (closes #29, #13) (#176)

### Refactor

- **output**: pin schema_version + int/float wire invariants (closes #152, #153) (#174)
- **output**: typed models for run-level aggregate payloads (stage 1) (#149)
- **orchestrator**: collapse injection kwargs into OrchestratorDeps (#134)
- **docker**: rename ServiceStack → EngineStack; document docker-only + non-Protocol (#169)
- **core**: extract compose-materialisation primitives into shared module (#166)
- **core**: decompose Conductor + extract TrialGrader Protocol (#161)

## v0.7.0 (2026-07-02)

### Feat

- **core**: PerTrialRuntimeBackend + trial-isolation enforcement + --runtime CLI (#148)

### Fix

- **db-service**: support JSONPath filter expressions in /query (#157)

## v0.6.0 (2026-07-02)

### Feat

- **grading**: diff-first default state view for the rubric judge (#151)

## v0.5.0 (2026-07-02)

### Feat

- **core**: RuntimeBackend provisioning contract (ADR-0010) (#133)
- **core**: add EnvironmentManifest typed schema for multicontainer environments (#121)

### Fix

- **orchestrator**: select full_stack when the adapter declares rag-service need (#140)

### Refactor

- **runtime**: move per-trial RPC methods onto RuntimeBackend (ADR-0013) (#141)
- **runtime**: promote RunnerClient to a Protocol; rename concrete to GrpcRunnerClient (#135)
- **core**: EnvironmentManifest as compose-as-source-of-truth (#139)

## v0.4.1 (2026-07-01)

### Feat

- **llm**: register anthropic/claude-sonnet-5 (cert + pricing) (#129)

### Fix

- **pricing**: refresh GLM 5.1/5.2 rates to current OpenRouter list (#123)

## v0.4.0 (2026-06-30)

### Feat

- **orchestrator**: make TrialArtifactWriter injectable (#112)

### Fix

- **core**: decouple TrialSpec.run_id from output_dir.name (#111)

### Refactor

- **core**: lift _run_trial behind a typed Conductor Protocol (#101)

## v0.3.1 (2026-06-26)

### Fix

- **grading**: faithful judge KB search — judge reads the same KB the agent did (#95) (#102)

### Refactor

- **core**: lift DockerRuntime behind a typed RuntimeBackend Protocol (#96)
- **grading**: relocate LLM-judge model from rubric to run config (#98)
- **trial**: type env_endpoints with EnvEndpoints Pydantic model (#92)

## v0.3.0 (2026-06-26)

### Feat

- **grading**: structured rubric grading via a runner-side read-only agentic judge (#94)
- **output**: formalize RunAggregateWriter as the run-level data-plane seam (#85)
- **output**: formalize TrialArtifactWriter as the typed data-plane seam (#79)
- **core**: define TrialSpec / TrialResult as the typed control↔trial seam (#74)
- **devcontainer**: add Dev Container config for reproducible dev env (#81)

### Fix

- **docker**: unblock clean runner/rag-service builds and integration tests (#88)
- **ci**: pin Claude review action to claude-opus-4-8 (#78)

### Refactor

- **runner**: drop private-package prefix from MCP_ASYNC import path (#73)

## v0.2.11 (2026-06-18)

### Feat

- **adapters**: register migration_bench constant in AdapterType (#71)
- **llm**: register z-ai/glm-5.2 and moonshotai/kimi-k2.7-code (cert + pricing) (#72)

## v0.2.10 (2026-06-17)

### Feat

- **presets**: operator-overridable preset overlay file (#69)
- **llm**: add OpenRouter provider routing to ModelConfig (#68)

### Fix

- **grading**: make unknown jsonpath operators fail loud + deterministic reasons (#66)

### Refactor

- **adapters**: make the runner adapter-agnostic (plugin-first) (#61)

## v0.2.9 (2026-06-16)

### Feat

- **llm**: register nemotron-3-ultra-550b-a55b (cert + pricing) (#65)

## v0.2.8 (2026-06-16)

### Feat

- **llm**: recover MiniMax-M3 tag-conversion corruption (#55)

## v0.2.7 (2026-06-10)

### Feat

- **llm**: register anthropic/claude-fable-5 (#52)

## v0.2.6 (2026-06-08)

### Feat

- **llm**: register minimax/minimax-m3 with codec-only preset (#51)

## v0.2.5 (2026-06-08)

### Fix

- **adapters**: restore bundle_writer so `adapter convert` works (#48)

## v0.2.4 (2026-06-05)

### Feat

- **llm**: register 7 arena-lineup models with preset routing (#46)
- **release**: automate releases with commitizen (cz bump) (#41)

## v0.2.3 (2026-06-04)

### Added

1. **`deepseek/deepseek-v3.2-exp` support.** New `ModelCertificate` (14 required / 6 known_unsupported, live-certified 2026-06-03) plus a dedicated `deepseek_v32` preset routing the experimental V3.2 line through the OpenAI reasoning codec. Unlike the V4 line it round-trips dict-map and discriminated-union tool calls on the standard response policy, so it needs neither `json_coerce` nor `dict_map_hints`; pricing was already present in `pricing.json`. (#36)

### Fixed

1. **`tolokaforge.__version__` reconciled** to match `pyproject.toml` (it had lagged at `0.2.1` through the 0.2.2 release).

## v0.2.2 (2026-06-03)

### Fixed

1. **Wheel resolver — relocated uv cache.** The Docker runner is provisioned from a host-resolved `tolokaforge` wheel; for a git-source install the `pip-cache` provider recovers the wheel `uv` built during `uv sync`. `_walk_pip_wheel_caches()` hard-coded `~/.cache/uv`, so when the cache was relocated (e.g. `astral-sh/setup-uv` sets `UV_CACHE_DIR` in CI) the wheel was missed and `tolokaforge run` failed at service start-up with `NoWheelError`. Cache *location* is now discovered via `uv cache dir` / `UV_CACHE_DIR` / `PIP_CACHE_DIR` (with the `~/.cache` defaults preserved); the uv-internal layout scan is unchanged, and `NoWheelError` now reports the caches it searched. (#27, #28)
2. **Adapters package export.** Removed the stale `FrozenMcpCoreAdapter` entry from `tolokaforge.adapters.__all__` (it is no longer importable from the engine), which had broken `from tolokaforge.adapters import *`. (#14)

## v0.2.1 (2026-05-29) — LLM Reasoning & Observability Overhaul

### Breaking Changes

1. **`ModelConfig.reasoning`** migrated from bare string to `ReasoningConfig` struct. YAML configs using `reasoning: "medium"` now raise `ValidationError` at load time — migrate to `reasoning: {mode: adaptive, effort_hint: medium}` or equivalent. See [`docs/CONFIG.md`](docs/CONFIG.md) § `reasoning:`.
2. **`GenerationResult.token_usage: dict`** removed — replaced by `GenerationResult.usage: Usage` (full Anthropic + OpenAI accounting incl. cache + reasoning tokens).
3. **`Metrics.tokens_input`** / **`Metrics.tokens_output`** removed — replaced by `Metrics.usage: Usage`. Aggregators expose `avg_<field>` / `total_<field>` per `Usage` field (e.g. `avg_reasoning_tokens`, `total_cache_read_input_tokens`).
4. **`Message.reasoning: str`** migrated to `Message.reasoning: StructuredReasoning | None` — preserves provider signatures + block types for replay.
5. **`tolokaforge.core.model_client`** / **`tolokaforge.core.model_policies`** modules deleted — every concept moved into `tolokaforge.core.llm.*`. Update imports accordingly.
6. **`in-process` runtime mode removed.** Docker is now the only supported runtime (`runtime: "docker"`). All tool execution is routed through the containerised executor service. Existing configs that specify `runtime: "in-process"` must be updated to `runtime: "docker"`.

### Fixed

1. **P1 / GPT-5.5 Decimal tool-call 500s** — `StrictSchema` now strips RE2-incompatible `pattern` + `format` keys and collapses Pydantic's `Decimal` `anyOf{number, string+pattern}` idiom to `{type: number}`. Four OTS domains that scored 0.000 on gpt55 now return valid tool calls.
2. **P2 / Qwen dict-map stringification** — new `qwen` preset wires `schema_sanitizer: strict` + `response_policy: array_dict_map` + `prompt_policy: dict_map_hints`. `qwen/*` and `qwen3*` now handle `Dict[str, T]` parameters correctly.
3. **P3 / Claude 4.7 ignores `reasoning: medium`** — new `anthropic_claude_4_7` preset emits canonical litellm `thinking={"type":"enabled","budget_tokens":N}` kwarg + drops `temperature` / `top_p` / `top_k` when thinking is active.
4. **P4 / Anthropic thinking blocks dropped across turns** — new `ReasoningCodec` abstraction captures full `{type, thinking, signature}` blocks on extraction and splices them back via `thinking_blocks` on assistant message dicts for interleaved-thinking replay.
5. **P5 / user-simulator prompt never persisted** — new `Trajectory.user_system_prompt` field captures the full simulator system prompt on first turn.
6. **P6 / effective tool schemas never persisted** — new `results/tools_schemas/<task_id>__<model_id>.json` sidecar dedup'd per `(task, model)` via filename.
7. **P7 / cache + reasoning token counters lost** — new `Usage` dataclass + `UsageExtractor` reads every normalised litellm field: `prompt_tokens`, `completion_tokens`, `reasoning_tokens`, `cached_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, plus `provider_raw` for forensics. OpenRouter-routed Anthropic caching now surfaces correctly — reads `prompt_tokens_details.cache_write_tokens` / `cache_read_tokens` as a fallback when top-level Anthropic fields are zero.
8. **P8 / no `cache_control` markers on Anthropic calls** — new `AnthropicEphemeralCache` automatically marks the last system-prompt content block + last tools entry with `cache_control: {type: ephemeral}` (5-minute TTL, Anthropic default).
9. **P9 / `reasoning: medium` abstraction leak** — new `ReasoningConfig(mode, budget_tokens, effort_hint, display)` provides explicit per-provider routing. Single in-repo legacy config migrated; external configs rebase in lockstep.
10. **P10 / no shared per-provider integration scaffolding** — new `tests/integration/llm/` with `Capability` enum + `ModelCertificate` registry; per-capability tests auto-skip with explanatory messages based on each model's declared certificate. See [`docs/ADD_NEW_MODEL.md`](docs/ADD_NEW_MODEL.md).

### Added

1. `tolokaforge/core/llm/` package with seven Protocol-driven policy modules:
   - `reasoning.py` / `reasoning_codec.py` — structured thinking-block extraction + replay
   - `schema_sanitizer.py` — `ToolSchemaSanitizer` with RE2 post-condition
   - `cache_policy.py` — `CachePolicy` with ephemeral-cache implementation
   - `usage.py` — `Usage` dataclass + `UsageExtractor` + field-wise `__add__`
   - `params_policy.py` / `content_policy.py` / `response_policy.py` — pre-existing classes ported
   - `capabilities.py` — `ModelCapabilities` with all seven policy slots
   - `presets.py` — preset registry with reverse-lookup + fingerprint helpers
2. `tolokaforge/core/output/artifacts.py` with `TrialArtifactWriter` Protocol + `FileArtifactWriter` + `model_id_slug`.
3. [`docs/LLM_LAYER.md`](docs/LLM_LAYER.md) — single authoritative reference for the new package.
4. [`docs/ADD_NEW_MODEL.md`](docs/ADD_NEW_MODEL.md) — six-step contributor guide for adding a new model / provider.
5. `anthropic/claude-opus-4.8` registered in pricing catalog, model presets (version-specific `anthropic_claude_4_8` preset ordered before generic `anthropic` for first-match-wins routing), and integration `ModelCertificate` (live-certified 2026-05-29; promotes `DICT_MAP_TOOL_CALL` + `DECIMAL_FIELD_TOOL_CALL` to `required` versus 4.6/4.7's `known_unsupported`).

### Changed

1. `litellm` version range set to `>=1.83.14,<2.0.0` (was `>=1.0.0`). Minimum version required for the canonical `thinking={}` kwarg and `thinking_blocks` first-class assistant-message field.
2. `task.yaml.model_config.<role>.resolved.*` block now records `{effective_preset, schema_sanitizer, prompt_policy, content_policy, response_policy, reasoning_codec, cache_policy}` for analytics-level config-drift detection. See [`docs/OUTPUT_FORMAT.md`](docs/OUTPUT_FORMAT.md) § `task.yaml`.

### Traceability

Every P# in [`plans/llm_reasoning_and_observability_fix.md`](plans/llm_reasoning_and_observability_fix.md) maps to a closed fix. Integration evals (Stage 10) require live API keys and are run manually — see [`docs/ADD_NEW_MODEL.md`](docs/ADD_NEW_MODEL.md) for the capability suite.

## v0.2.0 (2026-02-25)

### Added

1. `evaluation.task_packs` support across Docker runtime.
2. Multi-root mock-web routing via `TASKS_DIRS`.
3. Docker task-pack mount planning and smoke validation scripts.
4. Public benchmark examples across all OSS v1 benchmark types.
5. Tiered CI pipeline (PR smoke, nightly/full, release gate).
6. Public export verification tooling:
   - `scripts/release/prepare_public_export.sh`
   - Public export verification scripts
   - `scripts/tests/public_export_smoke.sh`

### Changed

1. Public examples were upgraded to non-placeholder structure with stronger grading and fixtures.
2. CI summary thresholds now enforce completion-rate in mock smoke runs and configurable pass-rate in release gating.
3. Mock-web static path resolution now supports multi-page task-local `www/` layouts.

### Security

1. Public export flow now strips internal-only integrations and scans for forbidden internal URL patterns.
