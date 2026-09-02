# Changelog

All notable changes to `tolokaforge-models` are documented in this file.
The wheel follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html);
its release cadence is orthogonal to the `tolokaforge` engine wheel's own
`vX.Y.Z` tag axis. See
[`docs/RELEASING.md`](https://github.com/Toloka/tolokaforge/blob/main/docs/RELEASING.md#pypi-package--tolokaforge-models-models-vxyz-automated).

## models-v1.3.0 (2026-08-25)

### Feat

- **grader**: standalone-extensible-grader — the deployed grader image grades the full surface (#1259) (#1276)
- **coding-harness**: lift agent_harness to a top-level, adapter-agnostic capability (#1279)
- **grader**: wire the queue trial grader end-to-end (#1254) (#1256)
- **grader**: Milestone 32 — grader-detachment seam foundation (#1202)
- **llm**: persist the OpenRouter generation id per request (#1242)
- **coding-harnesses**: RuntimeGateway + ContainerFileInjector + gateway_route (ADR-0037) (#1241)
- **refactor**: hoist coding-harness surface to top-level tolokaforge_coding_harnesses (#1236)
- **grading**: deterministic trace checks — the trace-checks tail (37 issues) (#1196)
- **tbench**: consolidated matrix harness fixes — Kimi K2.7 middleware, opencode routing/auth, Gemini via LiteLLM, supervisord, disk hygiene (#1228)

### Fix

- **orchestrator**: fail loud when a docker-CLI-needing run resolves to pull (#1267)

## models-v1.2.0 (2026-08-17)

## models-v1.1.0 (2026-08-17)

### BREAKING CHANGE

- the `unsupported_effort_levels` params key is removed. Preset
and provider blocks declaring it — in the bundled data or in an operator
overlay — must move to `param_value_rules`, which additionally requires an
`evidence` string. The bundled Gemini declaration is migrated in this PR; an
overlay still using the old key fails loud at overlay load, naming the file and
the legal keys. The models wheel now requires an engine that understands the
new key: see docs/RELEASING.md for the release ordering.

### Feat

- **automation**: auto-integration commits the models wheel only, and releases it (#1067)
- **tbench**: TrialMode.HARNESS + 6 shipped coding-harness CLIs + YAML-driven registry (ADRs 0031/0032) (#1083)
- **docker**: pull-vs-build policy for tolokaforge run (docker.image_source) (#1082)
- **core**: user simulator fidelity — the simulated user says what the task author intended (#1109)
- **llm**: param_value_rules — declare a value a route will not take (#1110)
- **tbench-adapter**: synthesise EnvironmentManifest from task compose; migrate compose lifecycle to PerTrialRuntimeBackend (#1060)
- **skills**: pre-flight decision extraction + educative PR/umbrella templates (#1034)

### Fix

- **runner**: _finalise preserves first_user_message_source + user_reply_guard_events (#1170)
- **tbench**: opencode ANTHROPIC_BASE_URL /v1 + kimi-code multi-turn docs (#1159)
- **config**: keep DockerConfig out of the orchestrator-only package (#1136)
- **automation**: score observe contamination as a rate, not a boolean (#1127)
- **runner**: assemble $.filesystem state at grading time (#1074) (#1096)
- **docker**: ship tolokaforge_models sources in the base wheel for wheel-install Docker builds (#1073)

## models-v1.0.0 (2026-08-12)

### Feat

- **core**: Milestone 29 — tolokaforge-models split (ADR-0030 delivery) (#1058)
- **automation**: let the Slack poller read a header-admission gateway (#1037)
- **llm**: address the gateway in its own dialect and by its own route name (#942)
- **ci**: auto-promote rc images to stable on green rc-smoke (#917) (#918)
- **grading**: composite primary keys in state_checks.id_fields (#924)
- **skills**: JSONL progress channel for orchestration subagents (#909)
- **grading**: deterministic trace checks, milestone 28 (#890)
- **tools**: optional docker exec --user for compose-variant bash_session + str_replace_editor (#894)
- **tools**: add build_check builtin — zero-arg peer-service HTTP probe (#892)
- **core**: multi-actor architecture — interaction_mode + Actor Protocol + TurnPolicy seam (#868) (#872)
- **runtime**: runner wheel split — slim image via subset build target (M15) (#847)
- **secrets**: resolve ${secret:NAME} references in config values (#798)
- **runtime**: Service Readiness Contract — first-class host-invokability boundary (#803) (#817)
- **slack**: custom message icons, one override parameter per icon role (#724)
- **automation**: report gateway availability and accept a route directive (#723)
- **llm**: route LLM calls through a gateway (LiteLLM proxy), env-configured (#718)
- **grading**: finish runner-side custom_checks as a Pattern-A extension (#704)
- rate-limit probe mode (fixed-interval 429 retry, hours-long budgets) (#665)
- **adapters**: make rag-service search_kb functional for native tasks (#107) (#666)
- **runtime**: Runner as a distributable service (M14 consolidation) (#642)
- **tools**: configurable working_root on str_replace_editor (#643)
- **adapters**: adapter-declared trial-grader name on orchestrator (#631)
- **runtime**: runtime independence v1 — expose runner as an independently-usable component (#557)
- **grading**: judge scoring integrity — verdict consistency, judge customization, offline replay (#528)
- **cli**: Improved Terminal DX (#460)
- **tools**: persistent agent shell + first-class editor tools (M25 consolidation) (#587)
- **runtime**: per-service network_access opt-out on ServiceSpec (untrusted-sibling partitioning) (#588)
- **project-layer**: Project-layer v1 finalization — canonical shape with warn-only compat (M9) (#531)
- **runtime**: multi-container v1 completion (M8 consolidation) (#511)
- **examples,runtime,assets**: multi-container example depth (Milestone 18) (#469)
- **core**: observability seam extension — llm_call trio + model identity (#389) (#450)
- **automation**: model auto-integration pipeline (observe/resolve/finalize + Slack-triggered poller) (#154)
- **project-layer**: make Project schema end-to-end runnable — task-schema relaxation, grading_defaults merge, dead-seam cleanup, docs residue (#375) (#390)
- **skills**: milestone integration-branch workflow with rich consolidation PR (#372)
- **examples**: swap example-microservices-pack backend-api from fictional to postgrest (real image) (#367)
- **runtime**: per-service log capture on trial failure (#302) (#347)
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
- **loader**: resolve project.yaml + run_configs base+delta merge (#219)
- **schema**: add ProjectConfig, TaskDefaults, RunDefaults + compute/storage/observability blocks (#215)
- **models**: add tencent/hy3 (Hunyuan 3 GA) (#204)
- **models**: add openai/gpt-5.6-terra and openai/gpt-5.6-sol (#203)
- **models**: add x-ai/grok-4.5 (pricing + capability certificate) (#196)
- **runtime**: SharedStackRuntimeBackend consumes environment_manifest (#167)
- **runtime**: :local engine-image alias + wire environment_manifest through TaskConfig (#163)
- **core**: TrialExecutor Protocol + wire per-trial substrate bracket (#162)
- **metrics**: roll up judge cost at task and run level (#159)
- **core**: PerTrialRuntimeBackend + trial-isolation enforcement + --runtime CLI (#148)
- **grading**: diff-first default state view for the rubric judge (#151)
- **core**: RuntimeBackend provisioning contract (ADR-0010) (#133)
- **core**: add EnvironmentManifest typed schema for multicontainer environments (#121)
- **llm**: register anthropic/claude-sonnet-5 (cert + pricing) (#129)
- **orchestrator**: make TrialArtifactWriter injectable (#112)
- **grading**: structured rubric grading via a runner-side read-only agentic judge (#94)
- **output**: formalize RunAggregateWriter as the run-level data-plane seam (#85)
- **output**: formalize TrialArtifactWriter as the typed data-plane seam (#79)
- **core**: define TrialSpec / TrialResult as the typed control↔trial seam (#74)
- **devcontainer**: add Dev Container config for reproducible dev env (#81)
- **adapters**: register migration_bench constant in AdapterType (#71)
- **llm**: register z-ai/glm-5.2 and moonshotai/kimi-k2.7-code (cert + pricing) (#72)
- **presets**: operator-overridable preset overlay file (#69)
- **llm**: add OpenRouter provider routing to ModelConfig (#68)
- **llm**: register nemotron-3-ultra-550b-a55b (cert + pricing) (#65)
- **llm**: recover MiniMax-M3 tag-conversion corruption (#55)
- **llm**: register anthropic/claude-fable-5 (#52)
- **llm**: register minimax/minimax-m3 with codec-only preset (#51)
- **llm**: register 7 arena-lineup models with preset routing (#46)
- **release**: automate releases with commitizen (cz bump) (#41)
- **llm**: register deepseek/deepseek-v3.2-exp (#36)
- **llm**: register anthropic claude-opus-4.8 (#10)
- Major engine cleanup for open-source release (#6)
- add stub mode to publish workflows for PyPI name reservation
- SecretManager as universal source + LLM Judge in Runner
- add food_delivery_2 test project data and canonical tests
- add PyPI publishing workflows and package metadata

### Fix

- **llm**: admit the parameters an operator declares, when litellm's map cannot (#1000)
- **core**: the TypeSense Docker rewrite drops the description cache (#928)
- **grading**: hash-source rule skips a pack whose adapter may supply the source (#911) (#914)
- **llm**: user simulator restarts the conversation after the agent answers (CBT-021) (#905)
- **actors**: AgentOnlyTurnPolicy signals AGENT_DONE on text-only turn (#876) (#877)
- **docker**: take the runner build context from the builder in core_stack (v0.14.1 still broken) (#864)
- **docker**: resolve the runner build context on a wheel install (#858)
- **orchestrator**: allow per-trial runs with heterogeneous compose files (#849)
- **runner+orchestrator**: substrate-native support for adapters using compose-variant tools + no DB service (#843)
- **runner-client**: accept degraded runner status + introduce HealthLevel/HealthReport pattern (#801) (#841)
- **grading**: decode wire tool calls in run_custom_checks instead of … (#804)
- **test**: add mkfir and write config before run orchestrator (#802)
- **grading**: make the two grading substrates agree — substrate parity, the trajectory record, hash composition, and the combine algebra (#748)
- **automation**: resolve a request against both catalogs, and route every reply icon through the registry (#728)
- **docker**: auto host ports for rag/mock-web; persist rag HF cache on volume (#703)
- **docker**: widen rag healthcheck start-period to cover model load (#661)
- **docker**: scope mock-web build context to its service files (#654)
- **deploy**: pin linux/amd64 in standalone compose for arm64 hosts (#647)
- **ci**: bind no environment for publish-images dry-run (#646)
- **runner**: preserve simulator text glued to ###STOP### (closes #611) (#619)
- **runtime**: repair two #557 regressions breaking unit + canonical tests (#615)
- **automation**: resolve-agent prompt - code-shape discipline + code-grounded data-scope (#562)
- **runner**: fail loud on id_fields typos + MCP diff-sync id resolution (#600 follow-ups) (#603)
- **runner**: resolve DB primary-key field from config, not model source (#600)
- **grading**: compare numerically-equal state values as equal (#532)
- **adapter**: fail conversion on invalid output (#494)
- **tools**: advertise PATCH requests (#463)
- **loader**: preserve storage discriminator tag under run_defaults merge (#312) (#365)
- **deps**: exclude litellm 1.92.0 due to fastapi import regression (#231)
- **docker**: materialize engine wheel via reinstall provider (closes #29, #13) (#176)
- **db-service**: support JSONPath filter expressions in /query (#157)
- **orchestrator**: select full_stack when the adapter declares rag-service need (#140)
- **pricing**: refresh GLM 5.1/5.2 rates to current OpenRouter list (#123)
- **core**: decouple TrialSpec.run_id from output_dir.name (#111)
- **grading**: faithful judge KB search — judge reads the same KB the agent did (#95) (#102)
- **docker**: unblock clean runner/rag-service builds and integration tests (#88)
- **ci**: pin Claude review action to claude-opus-4-8 (#78)
- **grading**: make unknown jsonpath operators fail loud + deterministic reasons (#66)
- **adapters**: restore bundle_writer so `adapter convert` works (#48)
- **presets**: enable empty-assistant filler for OpenRouter Amazon Nova (#35)
- **docker**: resolve the engine wheel under a relocated uv cache (#28)
- **adapters**: drop stale FrozenMcpCoreAdapter export and docstring (#14)
- stop leaking Docker-internal service URLs into env.yaml
- resolve Issues 13-15, update FUTURE_DEVELOPMENT.md with audit findings
- tool failure masking, brittle browser check, missing .env.example
- add file debugging and explicit dist path to publish jobs
- bump stub version to 0.0.2 (0.0.1 filename was used on TestPyPI)
- build wheel-only in stub mode to avoid leaking repo source in sdist
- use minimal stub README instead of full repo README in stub mode
- 3 critical bugs from example analysis
- browser task infrastructure + grading + failure attribution
- resolve FUTURE_DEVELOPMENT.md issues (Stages 14-17, Issues 5-10)
- container image mismatch detection + remove legacy context_files
- grading checks, scripted user case sensitivity, task-level Docker features detector
- examples pipeline — filesystem provisioning, Runner-side grading, tool factory, pricing
- refusal task grading — empty golden_actions no longer silently pass
- resolve 25 test failures and 14 false skips

### Refactor

- **core**: extract RunDisplayEvents engine seam to main (#416) (#433)
- **output**: pin schema_version + int/float wire invariants (closes #152, #153) (#174)
- **output**: typed models for run-level aggregate payloads (stage 1) (#149)
- **orchestrator**: collapse injection kwargs into OrchestratorDeps (#134)
- **docker**: rename ServiceStack → EngineStack; document docker-only + non-Protocol (#169)
- **core**: extract compose-materialisation primitives into shared module (#166)
- **core**: decompose Conductor + extract TrialGrader Protocol (#161)
- **runtime**: move per-trial RPC methods onto RuntimeBackend (ADR-0013) (#141)
- **runtime**: promote RunnerClient to a Protocol; rename concrete to GrpcRunnerClient (#135)
- **core**: EnvironmentManifest as compose-as-source-of-truth (#139)
- **core**: lift _run_trial behind a typed Conductor Protocol (#101)
- **core**: lift DockerRuntime behind a typed RuntimeBackend Protocol (#96)
- **grading**: relocate LLM-judge model from rubric to run config (#98)
- **trial**: type env_endpoints with EnvEndpoints Pydantic model (#92)
- **runner**: drop private-package prefix from MCP_ASYNC import path (#73)
- **adapters**: make the runner adapter-agnostic (plugin-first) (#61)

### Perf

- **orchestration**: reclaim wall-clock in /implement-milestone via overlap, review sharding, and stack warmup (#426)
