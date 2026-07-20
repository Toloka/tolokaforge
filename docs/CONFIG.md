# Configuration Guide

Tolokaforge uses YAML for three layers of configuration:
- Run configuration (`run.yaml`)
- Task specification (`task.yaml`)
- Grading specification (`grading.yaml`)

For full schemas, see `docs/REFERENCE.md`.

## Run Configuration (`run.yaml`)

```yaml
models:
  agent:
    provider: "openai"
    name: "gpt-4o-mini"
    temperature: 0.0
    max_tokens: 4096
    seed: 42
    # Reasoning / thinking configuration. Must be a struct — see `reasoning:` below.
    reasoning:
      mode: off
    # Optional: override auto-detected model capabilities
    capabilities:
      dict_map_prompt_hints: true
  user:
    provider: "openai"
    name: "gpt-4o-mini"
    temperature: 0.3
  # Optional: read-only rubric judge model. Required only when a selected task
  # grades with `llm_judge` — the run aborts up front if a rubric task is
  # selected but `judge` is absent (no default, no fallback to the agent model).
  # Keep it separate from `agent` to avoid self-grading bias; temperature 0.0
  # for determinism.
  judge:
    provider: "openrouter"
    name: "anthropic/claude-sonnet-4.6"
    temperature: 0.0

orchestrator:
  workers: 4
  repeats: 5
  max_budget_usd: 50.0        # optional hard stop
  max_requests_per_second: 2.0 # optional global throttle across workers
  max_attempt_retries: 1      # optional retries for transient infra failures
  queue_backend: "sqlite"     # "sqlite" (default) or "postgres"
  queue_postgres_dsn: null    # required when queue_backend="postgres"
  # max_turns: 60             # optional run-level cap; omit for none
  continue_prompt: "Please proceed to the next step."
  timeouts:
    turn_s: 60
    episode_s: 1200
  stuck_heuristics:
    max_repeated_tool_calls: 5
    max_idle_turns: 8
  runtime: "docker"       # Docker-based tool execution (only supported mode)

evaluation:
  # Optional: external task-pack roots (local paths)
  task_packs:
    - "/abs/path/private-pack-core"
    - "/abs/path/private-pack-mobile"
  # Resolved relative to each task pack root when task_packs is set
  tasks_glob: "**/task.yaml"
  output_dir: "results/run_001"
  cache_images: true
  harness_adapter:
    type: "native"
    params: {}
```

Notes:
- `models.judge` is the optional run-level read-only rubric judge model (no default); the run fails loud up front if a selected task grades with `llm_judge` but `models.judge` is absent.
- `models.agent.capabilities` overrides auto-detected model capabilities. Auto-detection (via `ModelCapabilities.for_model()`) covers most models; use overrides for A/B comparisons or to fix edge cases. Available fields: `dict_map_prompt_hints` (inject system prompt hints for dict-map parameters), `supports_typed_dict_maps`, `supports_schema_extras`, `fixed_temperature`, `supports_seed`, `unwrap_input_key`, `reasoning_via_extra_body`. See [Model Capability Presets](#model-capability-presets) below.
- PyPI wheels exclude `tasks/**`; configure benchmark content via `evaluation.task_packs`.
- `runtime: docker` is the only supported runtime; it uses the runner gRPC service and environment containers.
- `max_budget_usd` pauses scheduling new trials when cumulative spend reaches the budget.
- `max_requests_per_second` applies a global limiter across worker threads.
- `max_attempt_retries` retries transient failures (`rate_limit`, `api_error`, `timeout`) before marking a trial failed.
- `compute.log_tail` (default `500`, must be `>= 1`) bounds the `docker compose logs --tail` line count captured per service when a multi-service trial fails or grades red on the per-trial backend.
- `compute.capture_logs_on_success` (default `false`) is a debug escape hatch: when `true`, per-service logs are captured for successful trials too, not only failures.
- `queue_backend: postgres` enables distributed queue/state using Postgres; set `queue_postgres_dsn`.
- If `evaluation.task_packs` is empty, `tasks_glob` is resolved relative to the working directory.
- If `evaluation.task_packs` is set, relative `tasks_glob` patterns are resolved under each task-pack root and merged.
- If `evaluation.task_packs` is set, `tasks_glob` must be relative (absolute patterns fail fast).
- For Docker runs with external task packs, mount packs into the orchestrator/mock-web containers and set:
  - `TASK_PACKS_DIRS` for orchestrator-visible pack roots
  - `TASKS_DIRS` for mock-web task roots (category directories)
- Recommended: generate compose override from config via
  `uv run python scripts/generate_task_pack_compose_override.py --config examples/native/coding/run_configs/dev.yaml --output docker-compose.taskpacks.override.yaml`
- For long runs, inspect progress with:
  `tolokaforge status --run-dir <output_dir_timestamped>`
- For Postgres queue status (no local `run_queue.sqlite`):
  `tolokaforge status --run-dir <any_existing_dir> --config examples/native/coding/run_configs/dev.yaml`
- For distributed worker mode:
  `tolokaforge prepare --config examples/native/coding/run_configs/dev.yaml --run-dir <run_dir> --reset-queue`
  `tolokaforge worker --config examples/native/coding/run_configs/dev.yaml --run-dir <run_dir>`
- For multi-runner distributed execution (e.g., GitHub Actions matrix), use
  `queue_backend: postgres` with a shared `queue_postgres_dsn`.

### `reasoning:` — declarative thinking configuration

`reasoning:` is a **struct**, not a bare string. Bare strings (`reasoning: medium`) are rejected at load time with a migration pointer.

Schema:

```yaml
reasoning:
  mode: off | adaptive | budget       # default: off
  budget_tokens: <int>                 # honoured when mode=budget (Anthropic thinking)
  effort_hint: low | medium | high     # provider-native effort string
  display: visible | summary | omitted # default: visible
```

Examples:

```yaml
# No reasoning requested (default).
reasoning:
  mode: off

# Adaptive — forward the native effort hint (Claude 4.5/4.6, OpenRouter
# `reasoning.effort` dict, OpenAI `reasoning_effort`). NOT accepted on
# Claude 4.7 — raises ValueError on the `anthropic_claude_4_7` preset.
reasoning:
  mode: adaptive
  effort_hint: medium

# Budget (Anthropic-native) — send the canonical litellm `thinking` kwarg
# with a concrete token budget. REQUIRED for Claude 4.7 (it ignores the
# adaptive effort dict). `budget_tokens` is mandatory unless the preset
# declares `reasoning_budget_default`; the `anthropic_claude_4_7` preset
# ships `reasoning_budget_default: 8000`, so the bare form is valid for
# Claude 4.7:
reasoning:
  mode: budget
  budget_tokens: 8000   # explicit — wins over preset default

# Claude 4.7 shortcut — uses the preset-level default (8000 tokens):
reasoning:
  mode: budget

# Budget on non-Anthropic presets (OpenAI GPT-5, Grok, Qwen) falls back to
# the effort-kwarg path — `budget_tokens` is silently unused because no
# cross-provider canonical budget kwarg exists. Provide `effort_hint` when
# going through these presets:
reasoning:
  mode: budget
  budget_tokens: 8000
  effort_hint: high
```

Preset-driven routing summary (see `docs/LLM_LAYER.md` § `params_policy`
for the full matrix):

| Preset family | `mode=adaptive` emits | `mode=budget` emits | Sampling dropped when thinking |
|---|---|---|---|
| `anthropic_claude_4_7` (Opus/Sonnet 4.7) | **`ValueError`** | top-level `thinking={type, budget_tokens}` | ✅ (`temperature` / `top_p` / `top_k`) |
| `anthropic` (Claude ≤ 4.6) | `extra_body.reasoning.effort` | effort fallback | ❌ |
| `openai_gpt5` / `xai_grok` / `qwen` | `reasoning_effort` or `extra_body.reasoning.effort` | effort fallback | ❌ |
| `default` / `aws_nova` | *(nothing)* | *(nothing)* | ❌ |

Semantics are mapped per-provider by
[`tolokaforge/core/llm/params_policy.py`](../tolokaforge/core/llm/params_policy.py);
see [`docs/LLM_LAYER.md`](LLM_LAYER.md) for the full translation table.

### Model Capability Presets

Model capabilities are auto-detected from model name/provider using preset definitions in `tolokaforge/core/data/model_presets.yaml`. Override auto-detected capabilities via the `capabilities` field in model config:

```yaml
models:
  agent:
    name: openai/gpt-5.4
    capabilities:
      dict_map_prompt_hints: true  # Inject hints for typed dict-map parameters
```

Available overrides:
- `dict_map_prompt_hints` (bool) — enables the `DictMapHints` prompt policy which appends explicit hints to the system prompt about dict-map parameters (`additionalProperties: {schema}`). When enabled together with `StrictSchema` (auto-enabled for GPT-5 models), both schema-level enriched descriptions AND system prompt hints are applied. Dict-map detection uses the shared `detect_dict_maps()` utility in `model_policies.py`.
- `supports_typed_dict_maps` (bool) — whether model handles typed dict-map schemas natively (without `StrictSchema` rewriting)
- `supports_schema_extras` (bool) — whether model accepts `title`, `examples`, `minProperties`
- `fixed_temperature` (float | null) — force specific temperature
- `supports_seed` (bool) — whether model accepts seed parameter
- `unwrap_input_key` (bool) — unwrap Nova/Bedrock `{input: args}` wrapper
- `reasoning_via_extra_body` (bool) — send reasoning via `extra_body` (OpenRouter)

#### Prompt caching (preset-driven only)

Prompt caching (Anthropic ephemeral `cache_control`) is preset-driven, **not**
a `capabilities:` override in Stage 6. Anthropic-family presets
(`anthropic`, `anthropic_claude_4_7`) default to
`cache_policy: anthropic_ephemeral`; every other preset carries `cache_policy: none`.
To disable caching for an ablation study, edit
[`tolokaforge/core/data/model_presets.yaml`](../tolokaforge/core/data/model_presets.yaml:22)
and set `cache_policy: none` on the preset. Observe cache-hit rates via
`Metrics.usage.cache_read_input_tokens` + `cache_creation_input_tokens` in
`metrics.yaml`.

To add support for a new model, add an entry to `tolokaforge/core/data/model_presets.yaml`:

```yaml
presets:
  my_new_model:
    match: ["my-provider/my-model*"]
    schema_sanitizer: strict       # passthrough | strict
    prompt_policy: dict_map_hints  # none | dict_map_hints
    cache_policy: none             # none | anthropic_ephemeral (Anthropic only)
```

### Preset overlay file (no engine release required)

For models that reuse existing policy classes, you don't need to release the
engine to add or adjust a preset. Point TolokaForge at a second YAML file that
gets merged onto the bundled `model_presets.yaml` at startup. See
[ADR 0002 — External model registry](adr/0002-external-model-registry.md)
for the rationale and [`docs/ADD_NEW_MODEL.md`](ADD_NEW_MODEL.md) for a
walkthrough.

Set the overlay path two ways; precedence is **CLI flag > config field**:

1. CLI flag: `--presets-file overlay.yaml` on `run`, `prepare`, `worker`, and
   `config validate`. Use this for one-off overlays — smoke-eval iterations,
   ablations, anything you don't want to commit alongside the run config.
2. Run-config field — commit the overlay path next to the run definition
   when an overlay is part of *what this benchmark is*:

   ```yaml
   engine:
     presets_file: ./overlay.yaml   # relative to the working directory
   ```

The overlay file uses the same schema as `model_presets.yaml`. Overlay
presets are prepended to the iteration order so first-match-wins lets you
shadow a bundled preset. Same-named overlay presets *replace* the bundled
entry (logged at INFO so the replacement is visible). Policy-name strings
(`schema_sanitizer`, `prompt_policy`, etc.) must resolve to a class already
shipped in the engine — overlays can compose existing policies but cannot
introduce new classes. Unknown names raise `ValueError` at startup naming
both the overlay file and the offending key.

#### Distributed-worker propagation

`tolokaforge prepare --presets-file overlay.yaml ...` persists the resolved
overlay path into `engine_run_state.json` alongside the run queue. Worker
subprocesses launched later from the same `--run-dir` pick it up
automatically — you don't have to thread the flag through every
`tolokaforge worker` invocation. A worker-side `--presets-file` flag still
wins over the persisted value when both are set.

## Task Specification (`task.yaml`)

```yaml
task_id: "browser_simple_navigation"
name: "Simple Browser Navigation"
category: "browser"
description: "Navigate to the mock Example Domain page"

initial_state:
  json_db: "initial_state.json"          # optional
  filesystem:
    copy:
      - from: "fixtures/file.txt"
        to: "/env/fs/agent-visible/file.txt"
  mock_web:
    base_url: "http://mock-web:8080"
  rag:
    corpus_dir: "rag/corpus"

system_prompt: null

tools:
  agent:
    enabled: ["browser", "read_file", "write_file", "db_query", "search_kb"]
  user:
    enabled: []

user_simulator:
  mode: "scripted"   # "scripted" or "llm"
  persona: "cooperative"
  backstory: ""
  scripted_flow:
    - if_assistant_contains: "done"
      user: "Thanks!"
    - default: "Please continue."

policies:
  guidance:
    - "Use the browser tool to navigate"
  disallowed_actions: []

metadata:
  complexity: "medium"                # optional analytics slice
  expected_failure_modes: ["tool_arguments", "timeout_or_resource"]
  tags: ["onboarding", "browser-basics"]

grading: "grading.yaml"
```

## Grading Specification (`grading.yaml`)

```yaml
combine:
  method: "weighted"
  weights:
    state_checks: 0.6
    transcript_rules: 0.2
    llm_judge: 0.2
  pass_threshold: 0.8

state_checks:
  jsonpaths:
    - path: "$.db.orders[0].status"
      equals: "completed"

transcript_rules:
  must_contain: ["confirmed"]
  disallow_regex: ["(?i)password"]
  max_turns: 40
  tool_expectations:
    required_tools: ["browser"]
    disallowed_tools: []

llm_judge:                                 # the judge MODEL is set once per run
                                           # under models.judge — NOT here
  rubric:                                  # structured Rubric (NOT free text)
    reference: |                           # optional author-written ground truth
      The correct order total is $42.50 with apple_pay.
    criteria:
      - id: task_complete
        description: "Order was placed with the correct total and payment method"
        kind: binary
        required: true
        weight: 1.0
      - id: clarity
        description: "Confirmation message is clear and complete"
        kind: graded
        weight: 0.5
```

The rubric is a structured `Rubric` (per-criterion scoring + a required gate),
not a free-text blob; the old `rubric: "<text>"` shape, the `output_schema`
field, and the per-task judge-model field were all removed. The judge **model**
is now a run-level role (`models.judge`, see above) — separate from the agent
under test, with no default and no fallback. See
[GRADING.md](GRADING.md#llm-judge-rubric-grading) for the judge mechanism, the
two weighting layers, and the fail-loud ERRORED status.

## Environment Variables

Common keys:
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `OPENROUTER_API_KEY`
- `GOOGLE_API_KEY`
- `AZURE_API_KEY`, `AZURE_API_BASE`
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`
- `OLLAMA_API_BASE`

## Output Structure

```
output_dir/
├── aggregate.json
├── failure_attribution.json
├── per_task_metrics.json
├── metadata_slices.json
├── run_queue.sqlite
└── trials/
    └── <task_id>/<trial_index>/
        ├── task.yaml
        ├── trajectory.yaml
        ├── env.yaml
        ├── metrics.yaml
        ├── grade.yaml
        └── logs.yaml
```

See `docs/OUTPUT_FORMAT.md` for details.
For runner operations and queue workflows, see `docs/RUNNER.md`.
For metrics and attribution interpretation, see `docs/ANALYTICS.md`.
