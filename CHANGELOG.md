# Changelog

All notable changes to this project are documented in this file.

## [Unreleased]

### Fix

- **grading**: a golden replay whose per-action execution errored no longer
  composes a fabricated `hash_score: 0.0`; the runner reads
  `HashGradingResult.hash_unscorable`, leaves `components.hash_score` at the
  `-1.0` not-evaluated sentinel, and the declared-but-unscored fold refusal
  fires — the trial lands UNGRADEABLE, matching the judge-errored case.
- **grading**: a declared-but-unscored component (e.g. a judge that errored)
  now refuses the fold and returns a fail-loud verdict; the trial lands
  UNGRADEABLE rather than passing on a silently redistributed weighted mean.
  `resolve_uncounted_fold` marks the refusal with `FoldedGrade.refusal = True`;
  the runner's `GradeTrial` and the grader-service dispatch both surface it
  as `success = False` on the wire. `lot_ops_01` and `helpdesk_01` example
  packs are now UNGRADEABLE on the core substrate (their `state_checks`
  (RUNNER_ONLY) + `llm_judge` declarations require the runner substrate to
  score); runner-side grading unchanged.
- **loop**: empty completions (no text + no tool calls) now terminate with
  `TerminationReason.EMPTY_COMPLETION` (fail-loud) instead of silently
  appending an empty assistant message that Gemini and similar providers
  reject on the next request as an API error. The termination reason is
  additive on `Trajectory`; the failure-attribution table classifies it as
  `timeout_or_resource` (provider-side deterministic termination), and the
  runner-side graders auto-fail it without dispatching to `grade_trial`.
- **loop**: transient `TerminationReason.API_ERROR` classifications are
  retried once (bounded, hardcoded default via
  `LoopConfig.api_error_retries=1` + `api_error_backoff_s=1.0`) before
  terminating the trial. Rate limits, API timeouts, `TRIAL_LOST` and empty
  completions remain one-shot. The retry budget resets at every outer
  turn, and the `messages` list is unchanged on a failed attempt so the
  retry attempts against the same prefix.

## v0.22.1 (2026-09-02)

## v0.22.0 (2026-09-02)

### Feat

- **llm**: namespace-matched gateway wildcards, one provider-pin rule, route provenance (#1407)

### Fix

- **conductor**: skip Runner GetState RPC when task declares no json_db + demote no-target log (#1414)

## v0.21.4 (2026-08-28)

### Fix

- **ci**: switch claude-review model to claude-opus-4-7 (#1338)
- **ci**: bump Claude Code Action pin to v1.0.209 (#1337)

## v0.21.3 (2026-08-28)

### Feat

- **grading**: engine-repin-unblock — runner wire-model aliases, expected_hash refusal, trace_checks on_missing: withhold (#1315)

### Fix

- **grader**: route RunnerRPCTrialGrader through runtime_backend for per-trial runtimes (#1328)

## v0.21.2 (2026-08-26)

### Fix

- **deps**: bump grpcio floor to 1.83.0 to match generated runner stubs (#1310)

## v0.21.1 (2026-08-26)

### Fix

- **llm**: route moonshotai/kimi-k3 to empty-assistant filler-on + rename NovaMessageAssembly (#1284) (#1288)
- **tests**: sync pipe-listener from inside select() to stop Linux CI flake (#1289)

## v0.21.0 (2026-08-26)

### Fix

- **publish**: grader/rag build sibling coding-harnesses; runner subset ships seam entry-points (#1285)

## v0.20.0 (2026-08-25)

### Feat

- **grader**: standalone-extensible-grader — the deployed grader image grades the full surface (#1259) (#1276)
- **coding-harness**: lift agent_harness to a top-level, adapter-agnostic capability (#1279)
- **grader**: wire the queue trial grader end-to-end (#1254) (#1256)
- **grader**: Milestone 32 — grader-detachment seam foundation (#1202)
- **llm**: persist the OpenRouter generation id per request (#1242)

### Fix

- **tests**: main test-smoke regressions from milestone-36 + coding-harness lift (#1283)
- **orchestrator**: fail loud when a docker-CLI-needing run resolves to pull (#1267)

## v0.19.1 (2026-08-19)

### Feat

- **coding-harnesses**: RuntimeGateway + ContainerFileInjector + gateway_route (ADR-0037) (#1241)

## v0.19.0 (2026-08-18)

### BREAKING CHANGE

- the `unsupported_effort_levels` params key is removed. Preset
and provider blocks declaring it — in the bundled data or in an operator
overlay — must move to `param_value_rules`, which additionally requires an
`evidence` string. The bundled Gemini declaration is migrated in this PR; an
overlay still using the old key fails loud at overlay load, naming the file and
the legal keys. The models wheel now requires an engine that understands the
new key: see docs/RELEASING.md for the release ordering.

### Feat

- **refactor**: hoist coding-harness surface to top-level tolokaforge_coding_harnesses (#1236)
- **grading**: deterministic trace checks — the trace-checks tail (37 issues) (#1196)
- **tbench**: consolidated matrix harness fixes — Kimi K2.7 middleware, opencode routing/auth, Gemini via LiteLLM, supervisord, disk hygiene (#1228)
- **automation**: auto-integration commits the models wheel only, and releases it (#1067)
- **tbench**: TrialMode.HARNESS + 6 shipped coding-harness CLIs + YAML-driven registry (ADRs 0031/0032) (#1083)
- **docker**: pull-vs-build policy for tolokaforge run (docker.image_source) (#1082)
- **core**: user simulator fidelity — the simulated user says what the task author intended (#1109)
- **llm**: param_value_rules — declare a value a route will not take (#1110)

### Fix

- **runner**: _finalise preserves first_user_message_source + user_reply_guard_events (#1170)
- **tbench**: opencode ANTHROPIC_BASE_URL /v1 + kimi-code multi-turn docs (#1159)
- **config**: keep DockerConfig out of the orchestrator-only package (#1136)
- **automation**: score observe contamination as a rate, not a boolean (#1127)
- **runner**: assemble $.filesystem state at grading time (#1074) (#1096)

## v0.18.1 (2026-08-12)

### Feat

- **core**: a trial whose `TaskDescription.metadata` carries `agent_harness_command` runs that command once instead of driving the LLM turn loop — for tasks that ship their own agent (a coding-harness CLI that plans and edits inside the container), where the turn loop would stack a second agent on the first. `InProcessConductor._run_agent_loop` branches to the new `_run_harness_trial`, which calls the new `TrialRunner.run_harness`: one `execute_tool` call, no `ToolCallingLoop`, no LLM generation, no user turn. The trajectory records the task instruction as the user message, the command's output as the agent's single reply, and the invocation in `tool_log`. Grading is untouched — `_grade` reads the trajectory and the trial's env state, not how they were produced. The deadline is the target tool's own `timeout_s`, which governs both the RPC (`asyncio.wait_for`) and the `subprocess.run` behind the compose-exec wrapper — abandoning the former does not stop the latter, so a run whose effective episode budget (`min(task trial_seconds, orchestrator.timeouts.episode_s)`) is below the harness budget is refused naming both knobs, rather than silently shortened into grading a container the CLI is still writing to. A trial registering more than one agent tool is refused, since one exec is the whole trial, and a blank or non-string `agent_harness_command` is refused rather than silently falling back to the turn loop. The engine names no CLI — the command arrives fully formed from the adapter. Absent the key, the turn loop runs exactly as before.
- **adapters**: `terminal_bench` adapter param `agent_harness` (default `engine-loop`) selects the agent that drives a trial. `engine-loop` keeps tolokaforge's own turn loop and leaves the task image untouched — named for what runs, since this repo installs no Terminus-2 scaffold and `terminus-2` is deliberately not an accepted value: a trial labelled with it would claim a comparison it did not run. `claude-code` / `codex` / `gemini-cli` layer that vendor's CLI onto the task image at a version pinned in `HARNESSES`, and require the new `agent_model` param — the CLI would otherwise select its own default and the run config's model would not be the one measured. The model reaches the CLI with any `openrouter/` prefix stripped: a vendor CLI reaches OpenRouter through `*_BASE_URL`, not litellm, and the prefix would make it select its direct-vendor handler, read the blank vendor key, and 401. The engine loop keeps the prefix, which litellm needs to route. Layering splits the agent image in two — a build-only `{agent}-base` service carrying the task's own build (held out of `docker compose up` by a `tolokaforge-build` compose profile), and the agent service building `_harness/harness.Dockerfile` on top of it. `docker_stack_requirements()` declares both builds, base first. The layered image tag carries the harness name and the harness is part of the staging digest, so switching harnesses can never reuse a stale image. Install steps live in one place, `harness/install-harness.sh`; an unrecognised harness aborts the image build. Under harness mode, a task compose file declaring a service named `{agent}-base` is rejected with the same message as the `runner` / `db-service` collisions; without a harness no base service is injected, so such a task loads unchanged.
- **adapters**: `terminal_bench` adapter param `agent_provider_env: dict[str, str]` (default `{}`) forwards a harness CLI's provider credentials into the task container. Values resolve through `expand_secret_refs`, so a run config writes `${secret:ANTHROPIC_API_KEY}` rather than the credential itself; a value containing a newline or a `$` is refused, since each becomes one line of the per-trial `.env` where a newline splits the line and a `$` starts an interpolation; keys are checked against an allow-list (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `GOOGLE_API_KEY`) and anything else is refused naming the accepted set. The values flow `StackPatch.inputs` → `EnvironmentManifest.stack_inputs` → per-trial `.env`, so none enters the compose file, the staging digest, or the image. The compose variable carrying each value is namespaced (`ANTHROPIC_API_KEY=${TBENCH_PROVIDER_ANTHROPIC_API_KEY}`) rather than named on both sides: compose resolves `${VAR}` from the invoking shell before the per-trial `.env`, so an un-prefixed name would let whatever `ANTHROPIC_API_KEY` the operator's shell holds silently replace the declared value — putting a real production key inside a benchmark container and into its trial artifacts. `TaskDescription.metadata` gains `agent_harness` always and `agent_harness_command` under harness mode — the latter is the whole of what the engine core needs, so no CLI name or argv leaks out of the adapter. Under harness mode the `bash` tool's `timeout_s` carries the task's `[agent] timeout_sec` instead of the 120s per-call default, since one exec runs the entire trial.
- **tbench-adapter**: synthesise EnvironmentManifest from task compose; migrate compose lifecycle to PerTrialRuntimeBackend (#1060)
- **skills**: pre-flight decision extraction + educative PR/umbrella templates (#1034)
- **adapters**: `DockerStackRequirements.image_builds: list[ComposeImageBuild]` (default `[]`) declares task-side compose images the orchestrator builds once per run, immediately after the engine `:local` aliases are in place. Each entry runs `docker compose -f <compose_file> build <service>`, skipped when the service's pinned image already resolves locally. A build failure raises and aborts the run — instead of every trial of that task failing later with a `PROVISION_ERROR` naming compose, not the broken Dockerfile. The new field is deliberately absent from `to_core_stack_kwargs()` (same carve-out as `needs_rag_service`) — it is the orchestrator's declarative pre-build seam, not a stack kwarg. Every existing adapter keeps the empty default and needs no edit. (#1045)
- **core**: `OrchestratorConfig.strict_task_load` (default `false`) turns an adapter's `get_task()` failure into a startup refusal instead of the historical log-and-skip. Left `false`, `Orchestrator.load_tasks` behaves exactly as before — a broken task id is logged at error level and the run proceeds with the remaining tasks. Set to `true`, the exception propagates naming the offending task so the run refuses to start with a silently shorter task list; the bundled `examples/terminal_bench/*.yaml` opt in. `--dry-run` is strict regardless via its own loader (`load_tasks_for_dry_run`). (#1045)

### Changed

- **runtime**: `RuntimeBackendBuildContext.mount_docker_socket` is now derived from the same predicate that decides `enable_docker_cli` on the runner image build (`_run_needs_docker_cli`) — the terminal-bench adapter or any task routing a shipped tool through the compose variant (`tools.agent.<tool>.service`). An image with the CLI and no socket, or a socket and no CLI, are both useless — the two flags are one decision. Terminal-bench runs now reach the host daemon from the per-trial runner without the adapter having to declare `mount_docker_socket=True` on `DockerStackRequirements` itself. (#1045)
- **adapters**: `terminal_bench` adapter declares its environment as an `EnvironmentPatch(stack=StackPatch(compose_file=<staging path>, runner_service="runner"), network_policy=<param>)` on every `TaskConfig`, and the resolved `EnvironmentManifest` (with every compose service resolved to `ServiceSpec(isolation="ephemeral")` by `project_loader.resolve`) on every `TaskDescription`. Backend selection is task-driven, so terminal-bench runs now route through `PerTrialRuntimeBackend` — `TrialExecutor`'s bracket, per-trial network isolation, and `PROVISION_ERROR` attribution all apply — with no run-config change. The `bash` tool's `ToolSource.extra` shrinks to `{"service": <resolved agent service>, "compose_project_prefix": "tbench_"}`; `compose_file`, `task_dir`, `env_vars`, and `TaskDescription.tool_artifacts` are dropped. `docker_stack_requirements()` now carries only `image_builds` (one `ComposeImageBuild` per discovered task) — no task-pack mounts, no shared log bind, no socket flag. New adapter params: `network_policy` (default `full_internet`), `image_tag` (default `local`), `staging_root` (default: a `tolokaforge-tbench` directory under the system temp dir), `prebuild_images` (default `true`). `image_registry` now requires a companion `image_tag` — a floating tag is rejected up front with the adapter's own message. (#1045)

### Removed

- **adapters**: `terminal_bench` adapter params `runner_task_dir` and `logs_host_root` (plus the `LOGS_HOST_ROOT` class attribute). Task files are staged under `staging_root` and mounted into the agent service via relative volumes in the synthesised compose file, so no runner-side task path and no host-daemon log root are required. A config still passing either param raises in `TerminalBenchAdapter.__init__` naming the removed param and its replacement. (#1045)

### Fix

- **docker**: ship tolokaforge_models sources in the base wheel for wheel-install Docker builds (#1073)

## v0.18.0 (2026-08-12)

### Feat

- **core**: Milestone 29 — tolokaforge-models split (ADR-0030 delivery) (#1058)
- **automation**: let the Slack poller read a header-admission gateway (#1037)
- **llm**: address the gateway in its own dialect and by its own route name (#942)
- **ci**: auto-promote rc images to stable on green rc-smoke (#917) (#918)

### Fix

- **llm**: admit the parameters an operator declares, when litellm's map cannot (#1000)

## v0.16.1 (2026-08-07)

### Feat

- **grading**: composite primary keys in state_checks.id_fields (#924)

### Fix

- **core**: the TypeSense Docker rewrite drops the description cache (#928)

## v0.16.0 (2026-08-06)

### Feat

- **skills**: JSONL progress channel for orchestration subagents (#909)

### Fix

- **grading**: hash-source rule skips a pack whose adapter may supply the source (#911) (#914)
- **llm**: user simulator restarts the conversation after the agent answers (CBT-021) (#905)

## v0.15.0 (2026-08-05)

### Feat

- **grading**: deterministic trace checks, milestone 28 (#890)
- **tools**: optional docker exec --user for compose-variant bash_session + str_replace_editor (#894)
- **tools**: add build_check builtin — zero-arg peer-service HTTP probe (#892)
- **core**: multi-actor architecture — interaction_mode + Actor Protocol + TurnPolicy seam (#868) (#872)

### Fix

- **actors**: AgentOnlyTurnPolicy signals AGENT_DONE on text-only turn (#876) (#877)

## v0.14.2 (2026-08-04)

### Fix

- **docker**: take the runner build context from the builder in core_stack (v0.14.1 still broken) (#864)

## v0.14.1 (2026-08-04)

### Fix

- **docker**: resolve the runner build context on a wheel install (#858)

## v0.14.0 (2026-08-04)

### Feat

- **runtime**: runner wheel split — slim image via subset build target (M15) (#847)
- **secrets**: resolve ${secret:NAME} references in config values (#798)
- **runtime**: Service Readiness Contract — first-class host-invokability boundary (#803) (#817)

### Fix

- **orchestrator**: allow per-trial runs with heterogeneous compose files (#849)
- **runner+orchestrator**: substrate-native support for adapters using compose-variant tools + no DB service (#843)
- **runner-client**: accept degraded runner status + introduce HealthLevel/HealthReport pattern (#801) (#841)
- **grading**: decode wire tool calls in run_custom_checks instead of … (#804)
- **test**: add mkfir and write config before run orchestrator (#802)

## v0.13.1 (2026-08-03)

### Feat

- **slack**: custom message icons, one override parameter per icon role (#724)
- **automation**: report gateway availability and accept a route directive (#723)
- **llm**: route LLM calls through a gateway (LiteLLM proxy), env-configured (#718)
- **grading**: finish runner-side custom_checks as a Pattern-A extension (#704)

### Fix

- **grading**: make the two grading substrates agree — substrate parity, the trajectory record, hash composition, and the combine algebra (#748)
- **automation**: resolve a request against both catalogs, and route every reply icon through the registry (#728)
- **docker**: auto host ports for rag/mock-web; persist rag HF cache on volume (#703)

## v0.13.0 (2026-07-30)

### Feat

- rate-limit probe mode (fixed-interval 429 retry, hours-long budgets) (#665)

## v0.12.0 (2026-07-29)

### Feat

- **adapters**: make rag-service search_kb functional for native tasks (#107) (#666)
- **runtime**: Runner as a distributable service (M14 consolidation) (#642)
- **tools**: configurable working_root on str_replace_editor (#643)
- **adapters**: adapter-declared trial-grader name on orchestrator (#631)

### Fix

- **docker**: widen rag healthcheck start-period to cover model load (#661)
- **docker**: scope mock-web build context to its service files (#654)
- **deploy**: pin linux/amd64 in standalone compose for arm64 hosts (#647)
- **ci**: bind no environment for publish-images dry-run (#646)

## v0.11.2 (2026-07-27)

### Fix

- **runner**: preserve simulator text glued to ###STOP### (closes #611) (#619)

## v0.11.1 (2026-07-27)

### Feat

- **runtime**: runtime independence v1 — expose runner as an independently-usable component (#557)

### Fix

- **runtime**: repair two #557 regressions breaking unit + canonical tests (#615)
- **automation**: resolve-agent prompt - code-shape discipline + code-grounded data-scope (#562)
- **runner**: fail loud on id_fields typos + MCP diff-sync id resolution (#600 follow-ups) (#603)
- **runner**: resolve DB primary-key field from config, not model source (#600)

## v0.11.0 (2026-07-23)

### Feat

- **grading**: judge scoring integrity — verdict consistency, judge customization, offline replay (#528)

## v0.10.0 (2026-07-23)

### Feat

- **cli**: Improved Terminal DX (#460)
- **tools**: persistent agent shell + first-class editor tools (M25 consolidation) (#587)
- **runtime**: per-service network_access opt-out on ServiceSpec (untrusted-sibling partitioning) (#588)

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

## v0.17.0 (2026-08-11)

### Feat

- **core**: new public path accessors on [`tolokaforge.core.model_data`](tolokaforge/core/model_data.py) — `bundled_pricing_path()`, `bundled_presets_path()`, `bundled_providers_path()`. Each returns a `pathlib.Path` when the resource exists and raises `FileNotFoundError` when it does not. Stable within v0.17.x; removal or signature change requires a deprecation announcement. Downstream consumers should reach for these instead of raw `importlib.resources` — see [`docs/RELEASING.md § Downstream data-resource consumers`](docs/RELEASING.md#downstream-data-resource-consumers). The pre-cutover `_DATA_ROOT` constant points at `tolokaforge/core/data/`; the models-wheel cutover ([#938](https://github.com/Toloka/tolokaforge/issues/938)) will flip that one line to `tolokaforge_models/data/` with no consumer-side edits. `tolokaforge/core/model_data.py` split into a light seam module + orchestrator-only `model_data_fingerprint.py` sibling so the seam is safe to include in the runner subset. Runner-subset registration for `tolokaforge/core/data/providers.yaml` — a #935 bycatch fix so LLM-judge grading inside a runner-subset image resolves the provider bindings at first use. (#937)
- **llm**: engine-general helpers reached by per-model policy subclasses are now public API — [`coerce_json_strings`](tolokaforge/core/llm/response_policy.py), [`coerce_empty_containers`](tolokaforge/core/llm/response_policy.py), and [`find_additional_properties`](tolokaforge/core/llm/dict_maps.py) (re-exported from [`tolokaforge.core.llm`](tolokaforge/core/llm/__init__.py)). Enables per-model recovery classes to compose the shipped coercion helpers without importing `_`-prefixed engine internals. Stable within v0.17.x; removal or signature change requires a deprecation announcement. Ships alongside `StrictSchema.inline_refs_in_tool` (public overridable classmethod hook for per-tool `$ref` resolution — subclasses that need cycle tolerance override the hook rather than a private method) and six `ClassVar[…]` annotations on the pre-existing `StrictSchema` class-attribute hooks (`KEY_FIELD`, `VALUE_FIELD`, `carry_scalar_dict_map_value`, `flatten_oneof_discriminator`, `strip_parameters_root_description`, `strip_re2_incompatible_patterns`) — a subclass method that mis-writes `self.<hook> = ...` now surfaces as a type-checker error rather than a silent instance-attribute shadow. A canonical import-boundary test at [`tests/unit/llm/test_public_api_boundary.py`](tests/unit/llm/test_public_api_boundary.py) enumerates the eight currently-shipped per-model subclasses / composite classes and rejects any private-symbol reach into `tolokaforge.core.llm.*` — a regression that adds a `_`-prefixed import or a private-method override fires immediately at test-import time. (#936)
- **llm** (Bucket B per ADR-0030 § Docs flip taxonomy): `DictMapHints.build_hints` is now a public instance-method hook on [`tolokaforge/core/llm/prompt_policy.py`](tolokaforge/core/llm/prompt_policy.py) — signature widened from `@staticmethod _build_hints(tools)` to `build_hints(self, tools)`. Enables `RefResolvingDictMapHints` (and future subclasses that want to close over instance state) to override without `# type: ignore[override]`, and clears the base-class shape mismatch flagged in [ADR-0030 § Colleague review focus points, item 9](docs/adr/0030-tolokaforge-models-split.md). Stable within v0.17.x; removal or signature change requires a deprecation announcement. (#936)
- **llm**: provider transport bindings now live in `tolokaforge/core/data/providers.yaml` — Nova's three-site mapping (init `NOVA_API_BASE` `os.environ.setdefault`, `_format_model_name` bare-name return, `_call_with_key_rotation` per-attempt `api_base` / `api_key` / `custom_llm_provider` / slug rewrite), `UNROUTABLE_PROVIDERS` routability, OpenRouter rotation env vars (`OPENROUTER_API_KEYS` / `OPENROUTER_API_KEY`), `custom_llm_provider` litellm hints, and per-provider `rate_limit_patterns` are data-driven. Adding a new provider becomes a `providers.yaml` entry. New public seam: `LLMClient.classify_loop_error(exc)` — bound method that closes over compiled `binding.rate_limit_patterns` so the per-provider patterns thread to `ToolCallingLoop` without crossing the compiled tuple across module boundaries. Schema is [`tolokaforge.core.llm.providers.ProviderBinding`](tolokaforge/core/llm/providers.py) (frozen, `extra="forbid"`); the pre-cutover data file lives at `tolokaforge/core/data/providers.yaml`, and the models-wheel cutover ([#938](https://github.com/Toloka/tolokaforge/issues/938)) will move it to `tolokaforge_models/data/providers.yaml` while widening the `models_fingerprint` payload. (#935)
- **testing**: new public engine seam `tolokaforge.testing.certify` — `Capability`, `ModelCertificate` (widened with `excluded_capabilities` / `known_unsupported_reasons` / `probe_params` / `capability_extras`), `ALL_MODELS`, and the `@register_probe` / `get_probe` dispatch API for out-of-tree probe bodies (#931).
- **observability**: `engine_run_state.json` records the resolved model-data fingerprint — the `models_fingerprint` field carries `{package_version, content_sha256, api_version, minimum_engine_version}` computed from the post-overlay preset table, pricing table, and certificate registry, so a completed run identifies exactly which model-data snapshot it was scored against (#933).
- **llm**: three new policy slots (`assistant_text_policy`, `params_policy`, `message_assembly_policy`) bring `_POLICY_REGISTRIES` to nine. `assistant_text_policy` reshapes `message.content` between litellm parse and `GenerationResult.text` — unblocks the Cohere `<|START_TEXT|>…<|END_TEXT|>` marker case (#929) via an out-of-tree subclass. `params_policy` promotes `ParamsPolicy` to a public base class with a class-body `KNOWN_KEYS` declaration; the overlay validator reads the union across every registered subclass instead of introspecting `GenerationParams.__init__`. `message_assembly_policy` extracts the Nova filler string from `client.py` into a per-instance data field on `NovaMessageAssembly` — the string is now configurable at YAML level via `{name: nova, params: {empty_assistant_filler: "…"}}`. Preset slot values accept both bare `name` (legacy) and `{name, params}` (new — passed to the class constructor); the overlay validator rejects nested-key typos with a `difflib.get_close_matches` suggestion. Additive fields on the `resolve_policy_names` fingerprint: `message_assembly_policy` and `assistant_text_policy` land in `task.yaml.model_config.<role>.resolved.*`; `params_policy` stays intentionally omitted (`GenerationParams` constructor kwargs are already serialised via `model_config.<role>.capabilities`). (#934)

### Fix

- **core** — **Behaviour change**: `reload_pricing(path=<missing>)` and `_load_bundled_presets` now raise `FileNotFoundError` / `ValueError` instead of silently returning `{}` or falling back to defaults. Non-mapping JSON payloads in a pricing file now raise `ValueError` naming the observed type instead of surfacing as an `AttributeError` at the first `.get()` call. Consequence: a downstream caller passing `reload_pricing(path=<maybe-missing>)` will now surface an exception at engine startup — the previous shape silently produced a zero-cost pricing table, which read as `{}` in leaderboards. If the "maybe-missing" behaviour is genuinely wanted, callers must catch the raise themselves. See [ADR-0030 § "Downstream data-resource consumers"](docs/adr/0030-tolokaforge-models-split.md#downstream-data-resource-consumers-new--widening-revised-2026-08-07) for the rationale. (#937)
- **llm**: kill the Nova model-name conditional in `_format_model_name` (Blocker rule 3 antidote follow-up to #934's Gemini removal). Nova's `format_model_name_bare: true` binding field now drives the bare-name return path; no `self.config.provider.lower() == "nova"` string comparison remains in the client. (#935)
- **automation**: `run-probes` renamed the `--path <dir>` flag to `--pyargs <module>` for the moved certification suite (defaulting to `tolokaforge.testing.certify.suite`); `integrate-model.yml` uses the default so no operator-side changes are needed (#931).
- **testing**: `tolokaforge.testing.certify` no longer eagerly imports the pytest fixtures at the package level — runtime callers of the certify seam (e.g. `tolokaforge.core.model_data`) no longer need `pytest` installed. Suite authors continue to reach the fixtures via `pytest_plugins = ["tolokaforge.testing.certify.fixtures"]` or by importing the submodule directly (#931, exposed and fixed via #933).
- **llm**: kill the `if reasoning_name == "gemini"` model-name conditional in `build_capabilities` (AGENTS.md Blocker rule 3 antidote). Gemini's `drop_placeholder_signature` knob now flows through ordinary `{name, params}` dispatch on the `reasoning_codec` slot; the `capabilities: {gemini_drop_placeholder_signature: true}` wire-compat override is rerouted internally in `_apply_config_overrides` — modern preset overlays should declare `reasoning_codec: {name: gemini, params: {drop_placeholder_signature: true}}` instead. (#934)

### Deprecated

- **llm**: bare `name` slot values in preset YAML are deprecated in favour of the `{name, params}` shape. Both are accepted through the v0.17.x cycle and removed in v0.18.0. `_RECOGNISED_OVERRIDE_KEYS` (and the `capabilities:` bespoke override keys it backs — `gemini_drop_placeholder_signature`, `dict_map_prompt_hints`, `supports_typed_dict_maps`, `supports_schema_extras`, `fixed_temperature`, `supports_seed`, `unwrap_input_key`, `reasoning_via_extra_body`) follow the same window and are removed in v0.18.0 (#1017). (#934)

## v0.16.1 (2026-08-07)

### Feat

- **grading**: composite primary keys in state_checks.id_fields (#924)

### Fix

- **core**: the TypeSense Docker rewrite drops the description cache (#928)

## v0.16.0 (2026-08-06)

### Feat

- **skills**: JSONL progress channel for orchestration subagents (#909)

### Fix

- **grading**: hash-source rule skips a pack whose adapter may supply the source (#911) (#914)
- **llm**: user simulator restarts the conversation after the agent answers (CBT-021) (#905)

## v0.15.0 (2026-08-05)

### Feat

- **grading**: deterministic trace checks, milestone 28 (#890)
- **tools**: optional docker exec --user for compose-variant bash_session + str_replace_editor (#894)
- **tools**: add build_check builtin — zero-arg peer-service HTTP probe (#892)
- **core**: multi-actor architecture — interaction_mode + Actor Protocol + TurnPolicy seam (#868) (#872)

### Fix

- **actors**: AgentOnlyTurnPolicy signals AGENT_DONE on text-only turn (#876) (#877)

## v0.14.2 (2026-08-04)

### Fix

- **docker**: take the runner build context from the builder in core_stack (v0.14.1 still broken) (#864)

## v0.14.1 (2026-08-04)

### Fix

- **docker**: resolve the runner build context on a wheel install (#858)

## v0.14.0 (2026-08-04)

### Feat

- **runtime**: runner wheel split — slim image via subset build target (M15) (#847)
- **secrets**: resolve ${secret:NAME} references in config values (#798)
- **runtime**: Service Readiness Contract — first-class host-invokability boundary (#803) (#817)

### Fix

- **orchestrator**: allow per-trial runs with heterogeneous compose files (#849)
- **runner+orchestrator**: substrate-native support for adapters using compose-variant tools + no DB service (#843)
- **runner-client**: accept degraded runner status + introduce HealthLevel/HealthReport pattern (#801) (#841)
- **grading**: decode wire tool calls in run_custom_checks instead of … (#804)
- **test**: add mkfir and write config before run orchestrator (#802)

## v0.13.1 (2026-08-03)

### Feat

- **slack**: custom message icons, one override parameter per icon role (#724)
- **automation**: report gateway availability and accept a route directive (#723)
- **llm**: route LLM calls through a gateway (LiteLLM proxy), env-configured (#718)
- **grading**: finish runner-side custom_checks as a Pattern-A extension (#704)

### Fix

- **grading**: make the two grading substrates agree — substrate parity, the trajectory record, hash composition, and the combine algebra (#748)
- **automation**: resolve a request against both catalogs, and route every reply icon through the registry (#728)
- **docker**: auto host ports for rag/mock-web; persist rag HF cache on volume (#703)

## v0.13.0 (2026-07-30)

### Feat

- rate-limit probe mode (fixed-interval 429 retry, hours-long budgets) (#665)

## v0.12.0 (2026-07-29)

### Feat

- **adapters**: make rag-service search_kb functional for native tasks (#107) (#666)
- **runtime**: Runner as a distributable service (M14 consolidation) (#642)
- **tools**: configurable working_root on str_replace_editor (#643)
- **adapters**: adapter-declared trial-grader name on orchestrator (#631)

### Fix

- **docker**: widen rag healthcheck start-period to cover model load (#661)
- **docker**: scope mock-web build context to its service files (#654)
- **deploy**: pin linux/amd64 in standalone compose for arm64 hosts (#647)
- **ci**: bind no environment for publish-images dry-run (#646)

## v0.11.2 (2026-07-27)

### Feat

- **adapters**: adapter-declared trial-grader name — the orchestrator now loads `adapter.trial_grader_name` (default `"runner_rpc"`, additive, every existing adapter unchanged); adapters shipping a custom `TrialGrader` under the `tolokaforge.trial_graders` entry-point group override it (#620)

### Fix

- **runner**: preserve simulator text glued to ###STOP### (closes #611) (#619)

## v0.11.1 (2026-07-27)

### Feat

- **runtime**: runtime independence v1 — expose runner as an independently-usable component (#557)

### Fix

- **runtime**: repair two #557 regressions breaking unit + canonical tests (#615)
- **automation**: resolve-agent prompt - code-shape discipline + code-grounded data-scope (#562)
- **runner**: fail loud on id_fields typos + MCP diff-sync id resolution (#600 follow-ups) (#603)
- **runner**: resolve DB primary-key field from config, not model source (#600)

## v0.11.0 (2026-07-23)

### Feat

- **grading**: judge scoring integrity — verdict consistency, judge customization, offline replay (#528)

## v0.10.0 (2026-07-23)

### Feat

- **cli**: Improved Terminal DX (#460)
- **tools**: persistent agent shell + first-class editor tools (M25 consolidation) (#587)
- **runtime**: per-service network_access opt-out on ServiceSpec (untrusted-sibling partitioning) (#588)

## v0.9.3 (2026-07-22)

## v0.9.2 (2026-07-21)

### Feat

- **core**: `tolokaforge.core.run_display_events` publishes the `RunDisplayEvents` engine seam — a 9-method `@runtime_checkable` Protocol with `ServiceSnapshot` / `ContainerSnapshot` TypedDicts and a `_NULL_EVENTS` no-op default — that front-ends implement to consume per-trial lifecycle events without pulling any UI package into the engine dependency graph. The orchestrator, conductor, and trial executor emit every lifecycle event through the seam; `OrchestratorDeps.events` accepts a consumer sink (defaults to the null singleton, so runs that never attach a front-end are byte-identical to the pre-seam engine) (#416)
- **runtime**: `RuntimeBackend` widens with `get_infrastructure_snapshot(handle) -> list[ContainerSnapshot]` — the display's per-trial infrastructure hook. `PerTrialRuntimeBackend` reads its per-trial compose stack; `SharedStackRuntimeBackend` returns `[]` in built-in mode and reads the run-wide compose otherwise; `InMemoryRuntimeBackend` returns a synthetic single-container shape. Every in-repo backend implements the new method in the same commit, so out-of-tree implementers of `RuntimeBackend` (none on `main`) would need to add the method to keep `isinstance(impl, RuntimeBackend)` semantically complete (#416)
- **dx**: `--display=rich` panel gains a compact **Boot log** region during the Docker startup window. When `_total_trials == 0` and the ring buffer contains any `tolokaforge.docker.*` record, `LiveRunDisplay` renders a `Panel(title="Boot log")` between the services widget and `main` listing the last five docker milestones, most-recent-last, as `HH:MM:SS.mmm | short-name | message` (UTC-stable). The region steals rows from `main` under a stable-height clamp — `budget = total - services_h - bottom_h - 5`; below three rows it drops entirely — so the total renderable height stays `max(12, viewport - 1)` and Rich Live never re-anchors (regression guard for the #392 stacking fix). The region disappears the moment trials dispatch. See [docs/CLI.md](docs/CLI.md) § Live run panel. (#394)
- **dx**: Full TUI mode (`tolokaforge run --display=full`) — a Textual `App` (`tolokaforge.dx.tui.TextualRunApp`) that consumes the same `RunDisplayEvents` seam the Rich Live panel does and renders a keyboard-navigable, tabbed run view. Header + one-line status bar, left-pane scrollable trial list (`j`/`k` or `↑`/`↓`; `PgUp`/`PgDn` for ~20-row jumps; `Home`/`End` for first/last), right-pane focused-trial summary with per-trial infrastructure. Bottom `TabbedContent` — **Overview** (banner, phase, services summary), **Logs** (`RichLog` fed by the shared ring buffer), **Services** (`DataTable` of engine-stack services), **Infra** (per-focused-trial containers), **Errors** (WARNING+ filtered). Tab keys `1`–`5`; `l` jumps to Logs; `?` toggles a modal help screen; `q` exits the UI (Ctrl-C still kills the run). Requires `pip install 'tolokaforge[dx]'` — `textual>=0.85.0` is now in the `[dx]` extras. `LiveRunDisplay.for_mode(DisplayMode.FULL)` returns the Textual app when textual is importable and falls back to the Rich `LiveRunDisplay` with a WARNING log line otherwise. See [docs/CLI.md](docs/CLI.md) § Full TUI and [ADR-0019](docs/adr/0019-front-end-plugin-namespace.md).
- **dx**: `tolokaforge` invoked with no subcommand (and the explicit `tolokaforge repl` verb) drops into an interactive Click REPL. Free-form passthrough of every top-level command: tab-completion of subcommands, flag names, and file paths; command history at `~/.tolokaforge_history`; exit via `exit`, `quit`, or Ctrl-D. Root flags supplied at REPL entry (`-v`, `-q`, `--display`, `--log-format`) apply to every command until the session exits — they mutate global logging + console state once via the `cli()` callback and stay in effect. Dependencies added to the `[dx]` extras: `click-repl>=0.3.0`, `prompt-toolkit>=3.0.51`. Library-only installs (`pip install tolokaforge`) are unaffected; running `tolokaforge` without the `[dx]` extras prints the install hint served by `tolokaforge._entry:main`. See [docs/CLI.md](docs/CLI.md) § Interactive shell.
- **cli**: `tolokaforge run --resume --run-dir <path>` now works — the CLI resolves the existing run dir (fixing the pre-milestone bug where a fresh timestamped dir was always allocated), loads `run_state.json`, and re-runs only pending/infrastructure-failed trials. Idempotent on a fully-complete run (`Nothing to do; run already complete`). The A5 start banner shows `→ Resume: <run-id>` on the resume path. Worker restart on a populated queue (`tolokaforge worker --run-dir <existing>`) resumes automatically via the durable queue. See [docs/CLI.md](docs/CLI.md) § Resume. (#286)
- **cli**: `tolokaforge run --dry-run [--dry-run-samples N]` (default N=3) resolves config + tasks with full parity to a real run, renders the first N samples as Rich panels (system prompt, user prompt, tool spec, resolved model / judge / runtime), and exits 0 without any provider HTTP call. Silenced under `--display=none`. See [docs/CLI.md](docs/CLI.md) § Dry run. (#284)
- **cli**: `tolokaforge run` gains `--cost-limit`, `--time-limit`, `--sample-limit`, `--fallback-models`, `--model-cost-config`. Any budget hit triggers graceful shutdown: in-flight trials finish, `LIMIT_HIT.json` is written under the run dir, and the A5 end banner shows `⏸ Run stopped (<reason>)`. The B1 cost-meter turns amber at 80% and red at 100% of `--cost-limit`. `--fallback-models` implements an ordered per-generate cursor letting a batch survive provider outages. `--model-cost-config` overlays JSON/YAML onto the shipped pricing table. See [docs/CLI.md](docs/CLI.md) § Cost, time, and sample limits and § Fallback models. (#283)
- **cli**: `tolokaforge run` prints a two-line start banner (`→ Run: <run-id>` + `→ Report: file:///…/`) on stderr before the run, and a three-line end banner (`✓ Run complete in <duration>` or `✗ Run failed in <duration>` + `→ Report: file:///…/` + `→ Browse: tolokaforge browse <run-id>`) after — including on failure. URLs are OSC 8 hyperlinks. Silenced under `--display=none`. `Orchestrator.run` now accepts pre-resolved `run_id` / `output_dir` kwargs. See [docs/CLI.md](docs/CLI.md) § Run banner. (#281)
- **cli**: `tolokaforge --version` prints the installed package version. Root `tolokaforge --help` groups commands under **Runs / Tasks / Docker / Config / Assets / Adapters** headings, alphabetical within each. See [docs/CLI.md](docs/CLI.md) § Root help layout. (#278)
- **cli**: `--display=rich` now renders a Rich Live progress panel during `tolokaforge run` — left pane shows per-trial status (`⏳` running, `✓` completed, `✗` failed), right pane shows the focused (most-recently-transitioned) trial's cumulative summary (`turn N · in Xk / out Y tok · $Z.ZZ · last: <event_kind>`), bottom bar shows `{completed}/{total} · {running} running · ${cost} · in {prompt} / out {completion} tok · fail {failed} · eta {eta}`. Under `--display={plain,log,none}` (or non-TTY under `rich`) the display is a no-op context manager and the log-line stream is preserved. Consumers subscribe via the `tolokaforge.core.run_display_events.RunDisplayEvents` Protocol (`run_started`, `trial_started`, `trial_progress`, `trial_completed`, `trial_failed`, `judgment_scored`, `run_finished`) threaded through `OrchestratorDeps.events`; the orchestrator, conductor, and runner emit into it and `tolokaforge.dx.live_panel.LiveRunDisplay` is the reference terminal front-end consumer. See [docs/CLI.md](docs/CLI.md) § Display modes and [ADR-0019](docs/adr/0019-front-end-plugin-namespace.md). (#285)
- **cli**: root flag `--display={full,rich,plain,log,none}` and env var `TOLOKAFORGE_DISPLAY=…` pick the overall stderr UI. Auto-selects `plain` when `CI` is set or when `sys.stderr` is not a TTY; auto-selects `rich` on a TTY. `--display=none` silences stderr on success while preserving the stdout artifact-path emission. `--display=full` falls back to `rich` when textual is not installed. Orthogonal to `--log-format`. See [docs/CLI.md](docs/CLI.md) § Display modes. (#282)
- **cli**: `tolokaforge run` and `tolokaforge prepare` emit the absolute run-dir path as a single line on `sys.stdout` on success. Read-only commands (`status`, `validate`, `config validate`, `assets stamp`, `worker`, `adapter convert`, `analyze`, `docker *`) leave `sys.stdout` empty. Idiom: `RUN_DIR=$(tolokaforge run --config …)`. See [docs/CLI.md](docs/CLI.md) § stdout / stderr contract. (#280)
- **logging**: structured console format `HH:MM:SS.mmm | LEVEL | k=v | message` with root `--verbose` / `--quiet` / `--log-format={pretty,plain,json}` flags; auto-select `pretty` on TTY / `plain` on pipe; ANSI palette matches `_display.THEME`. See [docs/CLI.md](docs/CLI.md) § Structured logging. (#279)
- **schema**: task.yaml minimal shape is task_id + description; initial_state / tools / user_simulator / grading now optional with sane defaults (#366)
- **runtime**: `compute.log_tail` + `compute.capture_logs_on_success` config knobs and a per-service compose-log capture primitive for trial-failure diagnostics (#302)
- **runtime**: `PerTrialRuntimeBackend` captures per-service logs on provision-stage failure (compose-up / reset-recipe) before teardown, writing `services/<service>.log` + a `services/_capture.yaml` manifest; `RuntimeBackend` gains `capture_service_logs` (per-trial writes `.log` files; shared-stack is a documented no-op) (#302)
- **runtime**: on a trial-body failure (`ERROR` / `TIMEOUT`) the trial executor captures per-service logs before teardown, emits a `trial.service_logs_captured` summary line, and amends the trial's `metrics.yaml` with a `captured_service_logs` byte-count map (#302)
- **examples**: `multi_service_slow_start` pack + `test_startup_order_stress.py` stress-cover the `depends_on` + healthcheck + `--wait` start-order chain against a `pg_sleep`-driven ≥20 s slow dependency, proving the per-trial backend blocks on the full chain before the trial's first RPC (#303)

### Fix

- **dx**: `--display=rich` no longer stacks duplicate copies of the `LiveRunDisplay` panel during trial execution. `LiveRunDisplay.__enter__` now sweeps every non-root logger in `logging.root.manager.loggerDict` (skipping `PlaceHolder` entries) and removes any `StreamHandler` bound to the captured pre-Live terminal streams; loggers with `propagate=False` additionally receive a `_LogSink` so their records still surface. This closes the channel through which litellm's private `LiteLLM` / `LiteLLM Router` / `LiteLLM Proxy` `StreamHandler`s bypassed Rich Live's cursor coordination. `__exit__` restores every removed handler. See [docs/CLI.md](docs/CLI.md) § Live run panel. (#392)
- **grading**: a project's `task_defaults.grading_defaults.combine` now deep-merges under each task's own `grading.yaml.combine` (task fields win, `weights` merge key-by-key); a task that omits `combine` inherits the project block instead of an arbitrary `{state_checks: 1.0}` / `pass_threshold: 1.0` fallback, and `get_grading_config` no longer raises on tasks that ship no `combine` block (#376)
- **runtime**: `SharedStackRuntimeBackend` no longer advertises `reset_recipes:*` capabilities — a shared stack cannot honour them (reset tasks route to `PerTrialRuntimeBackend`, which still advertises them). A shared-selected run that requested a `reset_recipes:*` capability was admitting a capability it could not deliver; it is now refused at run start with the standard admission error (#310)
- **runtime**: `network_policy: limited_internet` enforcement via a squid forward-proxy sidecar. Declare `stack.limited_internet_allowlist: [host, ...]` (bare hostnames or `*.domain` wildcards); the provisioner injects a digest-pinned `ubuntu/squid` sidecar on a dual internal/edge network, points app services' `HTTP(S)_PROXY` at it, and default-denies non-allowlisted egress with HTTP 403. Runner retains direct edge egress for `llm_judge` grading (#323).
- **manifest**: five endpoint-resolution override fields on `stack:` — `runner_port`, `db_service`, `db_port`, `rag_service`, `rag_port` — let task-pack authors point the engine at non-convention service names and ports without touching the runtime backend. Defaults reproduce prior behaviour byte-identically; unknown service overrides fail loud at manifest load (#144).
- **observability**: `aggregate.json` gains an additive `captured_service_logs` roll-up on `RunAggregate` — a run-level view (`captures`, `total_bytes`, `per_service_bytes`, per-bundle `entries`) of the per-trial and run-level captured compose-log surfaces, with a closed `source` vocabulary (`provision_failure` / `trial_body` / `shared_stack_materialise`). Produced at report generation by scanning the on-disk capture tree (per-trial `services/_capture.yaml` and `metrics.yaml`, plus the run-level shared-stack `services/_capture.yaml`); fail-safe — a corrupt capture artifact is skipped, never breaking report generation. Always emitted (zero envelope on clean runs); no `schema_version` bump (#337).
- **runtime**: `SharedStackRuntimeBackend._materialise_manifest` now captures per-service compose logs to `<output_dir>/services/<name>.log` + `_capture.yaml` (with `capture_reason: "materialise_error"`) before cleanup on the failure path — mirroring #302's per-trial pattern for run-level materialise failures (#339).
- **observability**: per-trial `provisioning_duration_s` recorded in `metrics.yaml` — wall-clock seconds around the `provision → await_ready → endpoints` bracket, monotonic-clock-measured, additive to the existing metrics shape (#354).
- **runtime**: provision-failed trials now write a minimal trial bundle (`trajectory.yaml` + `metrics.yaml` with `error: "provision_error"` + `grade.yaml`) to `<output_dir>/trials/<task>/<idx>/`, making cost aggregation and post-mortem tooling see a consistent trial-directory shape whether the trial completed or failed to provision (#338).
- **project-layer**: M9 keystone — canonical Project-layer shape activated with **warn-only** compat. Every legacy shape a real task pack ships continues to load unchanged, with a `DeprecationWarning` naming the file, the offending key, and the concrete migration action. **No hard breaks in this release.** Post-M9 follow-up #533 tracks the future strict-rejection flip once a deprecation-window release cycle has closed.

  Canonical shapes activated (aliases still accepted with warning):
  - **`actors.user`** is the canonical author shape for the user simulator on `project.yaml` `task_defaults` and `task.yaml`; it now drives the simulator at runtime (previously parsed but inert). The top-level `user_simulator` block is a legacy alias — the loader lifts it into `actors.user` per config layer with a `DeprecationWarning`. Direct-Python callers using `TaskConfig(user_simulator=...)` continue to work via a `mode="before"` shim on `TaskConfig` and `TaskDefaults` that lifts to `actors["user"]` with the same warning (#213).
  - **`evaluation.projects`** replaces `evaluation.task_packs`. Legacy key accepted with warning.
  - **`network_policy` lowercase enum values** (`no_internet`, `limited_internet`, `full_internet`) replace the uppercase names (`NO_INTERNET`, ...). Uppercase accepted with warning; lowercased at parse time.
  - **`security_context_defaults.run_as_user` / `.run_as_group`** replace `.user` / `.group`. Legacy keys accepted with warning; disagreeing values (both a legacy and a canonical key set to different values) fail loud.
  - **`stack` sub-object** on `default_environment` is the canonical substrate shape; flat `compose_file` / `runner_service` at the top level of `EnvironmentPatch` are accepted with warning.

  Compat surfaces preserved:
  - **Missing `project.yaml`** — a pack without one still loads via a synthesised default. The synthesiser emits a `DeprecationWarning` naming the searched root and the exact fix (`Add a project.yaml at the pack root...`). Post-M9 #533 will flip this to a hard error.
  - **Unknown keys** in `project.yaml` / `run_configs/*.yaml` / `task.yaml` / `grading.yaml` emit a `DeprecationWarning` naming the file, the key, and the closest schema match (e.g. `unknown key 'mox_turns' in dev.yaml — did you mean 'max_turns'? Rename 'mox_turns' to 'max_turns'... (tracked in #533)`) — the key is silently dropped from the model instance so existing configs keep loading. Top-level scan only; nested unknown keys pass through unnoticed (documented limitation; the recursive scan will land alongside the strict flip in #533).
  - **`stack: null` / `stack.compose_file: null`** in a task's `environment_manifest` (and in a project's `default_environment`) now emit a `DeprecationWarning` naming the offending file and field, with the documented full-override rule — a task cannot unset the environment (or its substrate pointer) out from under a project that declares one. Omit the key entirely to inherit. Post-M9 #533 will re-flip to a hard error.

  In-tree canonicalisation:
  - Every pack under `examples/native/` now ships a `project.yaml` at pack root, uses the `stack` sub-object, `actors.user`, `run_configs/<name>.yaml`, and `evaluation.projects`. `example-microservices-pack` is the reference exemplar.

  Every deprecation message here follows a uniform actionable shape: **what** legacy shape triggered the warning, **where** it lives (file basename via `source_context` — never absolute paths), **why** it is deprecated, **how** to migrate (a concrete key rename or block move with a worked example), and **when** it goes away (`(tracked in #533)`). This lets external pack authors migrate incrementally without any hard breaks and gives them a concrete follow-up issue to subscribe to (#213).

- **docs/security**: rewrite `SECURITY.md`'s architecture overview, threat table, testing, and checklist to reflect the actual `runner-net` (non-internal, docker-py `EngineStack`) model. The doc previously described a vanished `env-net` (`internal: true`) network and `docker-compose.yaml`, and listed "executor reaching external internet — addressed by `env-net internal:true`" as an addressed threat that no longer holds (#324).
- **loader** (M9): project `task_defaults` again loads the shipped `example-microservices-pack`. Only `TaskConfig`-shaped keys of `task_defaults` are merged into each task dict before validation; project-scoped-only keys (`grading_defaults`, `continue_prompt`) are excluded from that merge and reach the engine through their own seams. The excluded set is derived from the schema (`TaskDefaults.model_fields - TaskConfig.model_fields`), so future project-only defaults are handled automatically (#277).
- **loader** (M9): `stack: null` and `stack: {compose_file: null}` in a task's `environment_manifest` (and in a project's `default_environment`) now emit a `DeprecationWarning` naming the offending file and field, with the documented full-override rule — a task cannot unset the environment (or its substrate pointer) out from under a project that declares one. Omit the key to inherit. Strict rejection deferred to a future release (#235, #533).

### Docs

- **guide**: task-pack image-layering guide covering the 3-tier base/environment/instance pattern from SWE-bench, with Dockerfile snippets and compose-file references (#146).
- **security/runtime**: document the built-in (Case A) `EngineStack` as `full_internet` by construction — the built-in `runner-net` is non-internal and the runner retains egress for in-container LLM-as-judge grading; task-declared stacks remain the only path with an enforceable `network_policy`. Recorded in ADR-0018 + `RUNTIME_BACKENDS.md`, and locks the `Network.internal` foundation primitive + the `EngineStack.create_networks` non-internal invariant with a unit test (#324).

### Compat / migration notes

Every soft-warning path M9 introduces is documented as a `DeprecationWarning` that names the file, the offending key/shape, the concrete migration action, and a `(tracked in #NNN)` suffix pointing at the follow-up issue that carries the retirement schedule:

- **#533** — post-M9 strict flip: re-flip Project-layer `extra="forbid"`, remove `synthesize_default_project`, re-flip `stack: null` / `stack.compose_file: null` to hard errors, add the recursive unknown-key scan. Fires one release cycle after M9 lands.
- **#534** — post-M9 `orchestrator.max_turns` default flip (redo #265): flip default from `int = 50` back to `int | None = None` (opt-in cap) after the deprecation window closes.
- **#214** — M5 legacy alias retirement (pre-existing): removes `evaluation.task_packs`, top-level `user_simulator`, uppercase `network_policy`, `security_context.user/group`, flat-stack aliases.
- **#489** — `orchestrator.timeouts` opt-in default (sibling of #265): bundled with M5's `turn_s`/`episode_s` → `trial_seconds`/`tool_call_seconds` rename so the field reshapes once.

External pack authors: run your suite; every warning message tells you exactly what to change. There are no schema errors introduced in this release — a pack that loads on `main` today continues to load, with warnings that point at the migration you'll need to make before the strict flips in #533 / #534 / #214 / #489 land.

Release-summary anchors (short form of items detailed above):

- **project-layer**: Project-layer v1 finalization — canonical shape with warn-only compat (M9) (#531)
- **runtime**: multi-container v1 completion (M8 consolidation) (#511)

Additional fixes landed this release:

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

### Notes for embedders

- **`Orchestrator.run()` now returns the resolved `Path` of the run dir it created** (previously `None`). Callers that ignore the return value are unaffected — Python drops it silently. Callers that assign the result now hold a `Path` instead of `None`; update `results = orchestrator.run()` to `run_dir = orchestrator.run()` (or discard it). See [docs/API.md](docs/API.md) § Orchestrator. (#280)

### Breaking Changes

1. **`tolokaforge.cli.*` modules renamed to `tolokaforge.dx.*`.** The Click command tree, Rich panels, banners, and dry-run renderer are now the reference terminal front-end and live under a namespace whose name signals their pluggability role — the same `RunDisplayEvents` Protocol (in `tolokaforge.core.run_display_events`) admits alternate front-ends. Module map: `tolokaforge.cli._display` → `tolokaforge.dx._display`; `tolokaforge.cli._run_display` → `tolokaforge.dx.live_panel`; `tolokaforge.cli._run_banner` → `tolokaforge.dx.banners`; `tolokaforge.cli._dry_run_render` → `tolokaforge.dx.dry_run_render`; `tolokaforge.cli.main` → `tolokaforge.dx.cli.main`; `tolokaforge.cli.docker_commands` / `adapter_commands` / `config_commands` / `assets_commands` → `tolokaforge.dx.cli.{docker,adapter,config,assets}`. Rich is now an optional dep behind `pip install 'tolokaforge[dx]'`; the `tolokaforge` console script is served by a stdlib-only shim (`tolokaforge._entry:main`) that prints an install hint if the extras are missing. Library-only imports (`from tolokaforge.core.orchestrator import Orchestrator`) are unaffected. See [ADR-0019](docs/adr/0019-front-end-plugin-namespace.md).
2. **`StructuredLogger` console output moves from `sys.stdout` to `sys.stderr`.** Every tolokaforge log record — including the `orchestrator`, `runner`, `output_writer`, and adapter records that previously wrote to `stdout` via `StructuredLogger`'s private handler — now propagates through the root handler installed by `configure_root_logging`, which writes to `sys.stderr`. Downstream consumers piping tolokaforge's stdout to capture log lines should switch to `2>&1 | …` or to `--log-format=json` (still on stderr). Aligned with the `stdout=artifact` carveout in #280.
3. **Console log line shape changed to `HH:MM:SS.mmm | LEVEL | k=v | message`.** The legacy `"%(asctime)s - %(name)s - %(levelname)s - %(message)s"` format (seconds resolution, inline `(k=v, k=v)` in the message string) is gone. Machine consumers grepping the old shape need to update to the new column layout — the pipe-separated columns, ANSI palette, and JSON schema are pinned by canonical goldens under `tests/canonical/golden/logging/`. (#279)
4. **`tolokaforge run` with zero tasks now exits with code `1`** (previously exited `0` with a red "No tasks found!" line on stderr). Callers relying on the silent-success behaviour should either pre-filter empty task sets or handle the non-zero exit. (#280)

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
