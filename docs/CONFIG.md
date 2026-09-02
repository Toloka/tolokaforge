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
  # Required when the run dispatches a user simulator. The orchestrator
  # fails loud if `models.user` is absent — there is no hardcoded provider
  # default, so a run always names the provider it ships user turns to.
  # Project-wide fallbacks live under `project.run_defaults.models.user`
  # (see docs/PROJECTS.md); the loader merges them into each run config.
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
  # max_turns: 60             # run-level cap (default 50); raise to let
                              # task-authored max_turns above 50 stand
  continue_prompt: "Please proceed to the next step."
  timeouts:
    turn_s: 60                 # declared, not yet enforced — #1147
    episode_s: 1200
  rate_limit_probe:            # off by default — see below
    enabled: false
  stuck_heuristics:
    max_repeated_tool_calls: 5
  runtime: "shared"       # deprecated override; any registered backend name
  strict_task_load: false   # true = refuse to start on an adapter task-load failure

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
  # Optional: how strictly the pre-run gate reads each selected pack's grading
  # block. Omit the whole block for the default below.
  grading_validation:
    fail_on: "advisory"       # "advisory" | "error"
```

Notes:
- `models.judge` is the optional run-level read-only rubric judge model (no default); the run fails loud up front if a selected task grades with `llm_judge` but `models.judge` is absent.
- `evaluation.grading_validation.fail_on` (default `advisory`) names the least severe finding class the pre-run gate refuses the run over. `advisory` fails on both classes; `error` fails on errors alone. Before it schedules anything, a run puts every selected task's grading block through the same predicate `tolokaforge validate` applies and aborts naming **every** offending task; the rules and their three classes are in [GRADING.md § What is validated before a run](GRADING.md#what-is-validated-before-a-run). `unchecked` is not a value here: it is a channel rather than a severity, and is logged rather than enforced so a gate that could check nothing does not read as a clean bill of health.
- **A misspelled `grading_validation` block name is silently dropped.** `evaluation` is `extra="ignore"`, so `grading_validaton:` leaves the defaults in place without a word. The block's own fields are `extra="forbid"`, so a misspelled *field* inside a correctly-spelled block does fail loud.
- `models.agent.capabilities` overrides auto-detected model capabilities. Auto-detection (via `ModelCapabilities.for_model()`) covers most models; use overrides for A/B comparisons or to fix edge cases. Available fields: `dict_map_prompt_hints` (inject system prompt hints for dict-map parameters), `supports_typed_dict_maps`, `supports_schema_extras`, `fixed_temperature`, `supports_seed`, `unwrap_input_key`, `reasoning_via_extra_body`. See [Model Capability Presets](#model-capability-presets) below.
- PyPI wheels exclude `tasks/**`; configure benchmark content via `evaluation.task_packs`.
- `orchestrator.runtime` is a deprecated plan-shape coercion knob. Backend selection is composer-driven — the orchestrator always constructs `SharedStackRuntimeBackend` and the composer sequences the resolved plan's per-scope substrate. `shared` coerces every task's plan to run-scope, `per_trial` to trial-scope; multi-stack packs are refused under either coercion (declare stack-scope explicitly instead). Any other name registered in the `tolokaforge.runtime_backends` entry-point group (only `in_memory` in-tree today) is a legit backend swap, resolved at run start with an actionable error listing the known names on a typo. Legacy `docker` is a retained alias for `shared`. See [RUNTIME_BACKENDS.md](RUNTIME_BACKENDS.md).
- `orchestrator.strict_task_load` (default `false`) controls how the orchestrator handles an adapter exception raised from `get_task()` while `load_tasks` iterates the discovered task ids. Left `false`, the failure is logged at error level and that task is skipped — the run proceeds with the remaining tasks. Set to `true`, the exception propagates with the task id in the message so the run refuses to start rather than proceeding with a silently shorter task list; the bundled `examples/terminal_bench/*.yaml` opt in because a task-pack that fails to materialise is a config error the operator must see. **`--dry-run` is strict regardless of this flag** — it has its own loader (`load_tasks_for_dry_run`) with no exception handling, since surfacing config errors is the whole point of that entry point.
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

### `rate_limit_probe:` — measure a provider's served throughput

Off by default. When enabled, 429s retry at a **fixed** interval until a
generous per-call wall-clock budget is spent, instead of riding the standard
five-attempt exponential backoff. Everything that is not a 429 keeps the
standard bounded path, so a dead upstream cannot inherit the long budget.

The mode also records the **goodput** telemetry the measurement is actually made
from — successful calls, their duration and their tokens, per `(role, model)` and
per fixed-width absolute-time window. See
[OUTPUT_FORMAT.md](OUTPUT_FORMAT.md:1) § `probe_*` for the arithmetic and the
cross-leg summing rule.

```yaml
orchestrator:
  timeouts:
    episode_s: 14400              # 4 h — a probe absorbs 429s by sleeping
  rate_limit_probe:
    enabled: true
    retry_interval_s: 15          # the mean poll interval
    jitter_fraction: 0.2          # +/- 20 % so clients don't poll in lockstep
    per_call_budget_s: 3600       # "effectively infinite" per agent call
    simulator_per_call_budget_s: 600   # shorter: the simulator isn't measured
    bucket_width_s: 30            # goodput window; MUST match across run legs
    max_buckets: 4096             # per-trial window cap (memory bound)
```

| Field | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Arms the mode. Nothing changes while it is `false`. |
| `retry_interval_s` | `15.0` | Mean wait between 429 retries. Constant by design: a blocked client polls `1 / retry_interval_s` times per second, so blocked client-time is recoverable from the 429 count. |
| `jitter_fraction` | `0.2` | Symmetric jitter as a fraction of the interval (`interval x (1 +/- f)`). Without it, every client blocked at the cap retries in lockstep — burst, all rejected, wait, burst — which biases the measurement and is harsher on the provider. The mean interval is unchanged, so the poll-rate inversion still holds in expectation. `0.0` restores the exact fixed interval. |
| `per_call_budget_s` | `3600.0` | Wall-clock budget for one **agent** call's 429 retries. Exhausting it reraises the last 429, which surfaces as `termination_reason: rate_limit`. A *floor*, not an exact ceiling: `stop` is evaluated on an attempt's outcome, so a call overshoots by up to one retry interval plus one attempt's own timeout budget. |
| `simulator_per_call_budget_s` | `600.0` | Same, for the user-simulator client. Deliberately shorter: the simulator shares the agent's quota so it must absorb 429s (otherwise a simulator 429 kills the trial the agent-side probe kept alive), but its throughput is not what the probe measures, so agent-sized wall time here only eats lease headroom. One turn can spend it three times over — the reply guard regenerates a user reply a detector flags instead of editing it — so the per-turn ceiling below multiplies it. |
| `bucket_width_s` | `30` | Width of one goodput window, **whole seconds**. Windows are anchored on the Unix epoch, not on run start, so simultaneous run legs emit the same boundaries and can be summed window by window. **Every leg of one measurement must use the same width** or the series do not align. Whole seconds keep every boundary an exact integer epoch, so the timestamps match byte-for-byte across legs. |
| `max_buckets` | `4096` | Per-trial cap on how many `(role, model, window)` **rows** may be opened, so memory is bounded. A two-role trial consumes two rows per window, so at 30 s this is ~34 h for a single `(role, model)` series and ~17 h for the two-role default. Past the cap a recording still lands in the flat and per-`(role, model)` totals but cannot open a new row; `Metrics.probe_dropped_buckets` counts the refused rows (also rows, not windows), so truncation is never silent. Refusing *new* rows rather than evicting old ones keeps the retained series a contiguous prefix — a series with a hole would let a cross-leg sum silently undercount. The cap is global rather than per series, so a high-volume role can consume all of it. |

Cumulative totals alone are not sufficient, which is why the windows exist:
measured goodput decays at a **constant** offered concurrency while the rejection
rate climbs, and a single average reports neither end. Figures in
[OUTPUT_FORMAT.md](OUTPUT_FORMAT.md:1) § Field observations.

Two invariants are enforced by raising — at config load against
`orchestrator.timeouts.episode_s`, and again per task against the *effective*
episode budget after the `min(task trial_seconds, run episode_s)` clamp:

- `episode_s > 3600` — a probe on a minutes-long episode budget dies on the
  episode timeout instead of measuring anything.
- the whole **per-turn 429 ceiling** must be strictly below `episode_s`:

  ```
  turn_wall_ceiling_s = per_call_budget_s + 3 x simulator_per_call_budget_s
                      + 4 x (retry_interval_s x (1 + jitter_fraction) + 737)
  ```

  The episode timeout is only evaluated *between* turns, and one turn issues the
  agent's `generate` plus up to **three** user-simulator `reply` calls: the reply
  guard discards a reply a detector flags and regenerates it, up to
  `USER_REPLY_MAX_ATTEMPTS` times, and a 429 does not consume one of those
  attempts. `stop` is evaluated on an attempt's *outcome*, so each of those four
  calls can overrun its budget by one jitter-maximum retry interval plus one
  attempt's own ceiling (`737 s` = the client's `6 x 120 s` timeout budget plus
  its inner backoff). Worst-case trial wall time is therefore
  `episode_s + turn_wall_ceiling_s`, and holding the ceiling below `episode_s`
  bounds it under the `max(300, episode_s * 2)` queue lease. At the defaults the
  ceiling is `5400 + 3020 = 8420 s` against a 14400 s budget.

  The attempt count is a module constant rather than a config field
  (`tolokaforge/core/models/run_config.py`): it is a multiplier this invariant
  reads, so a run config able to raise it could defeat the invariant from the
  config file.

  `retry_interval_s` and `jitter_fraction` are part of the invariant on purpose:
  the defaults plus a large `retry_interval_s` alone can blow the lease.

This bounds what the *probe* adds. It does not bound tool execution, grading, or
a runaway upstream stream — the loop has no per-turn timeout, and an attempt's
`timeout` is a per-read httpx timeout unless a model preset sets
`api_call_wall_timeout_s`. Those components behave identically on a probe-off
run, so the guarantee is "enabling the mode cannot be what pushes a trial past
its lease", not "a trial can never outlive its lease".

`episode_s` is the only ceiling that has to move: the queue lease is derived
from it.

The mode reaches the agent client, the user-simulator client, and the
per-trial counters (both censuses). It deliberately does **not** reach the rubric
judge (grading must not probe) or a `--fallback-models` chain — a chain plus
`enabled: true` is rejected, because switching models mid-probe attributes one
model's 429s to another. There is no env override: the config block is the only
activation channel, so a client built without one never probes regardless of the
environment.

**A probe run must never produce a leaderboard number.**
`Metrics.latency_total_s` is trial wall time, so every latency figure on a
probe run is inflated by 429 sleep; `metrics.yaml` records
`rate_limit_retries` / `rate_limit_wait_s` and the per-`(role, model)`
`rate_limit_by_role_model` breakdown as the mechanical marker (see
[OUTPUT_FORMAT.md](OUTPUT_FORMAT.md:1) § `metrics.yaml`).

**What the artifacts do and do not prove.** A non-zero `rate_limit_wait_s`
proves the mode was on. The converse does not hold: a probe run that found
headroom and hit no 429s leaves every 429 counter at zero, which is
indistinguishable from a normal run *on the 429 census alone* — the `probe_*`
goodput census is populated either way and does distinguish them, but it is not a
gate. The engine does not archive the resolved run config into the output bundle,
so a mode-consistency gate has to compare against the config it dispatched — that
gate belongs to whatever dispatches the run, not here.

**Do not trust the 429 census on its own.** It is schedule-dependent (it counts
how often *your* clients chose to poll) and, for some providers, silent — a
provider can throttle by slowing calls down instead of rejecting them, and then
only goodput and latency show the ceiling. That is why the success census is
recorded. Measured figures: [OUTPUT_FORMAT.md](OUTPUT_FORMAT.md:1) § Field
observations.

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

Model capabilities are auto-detected from model name/provider using preset definitions in `tolokaforge_models/data/model_presets.yaml`. Override auto-detected capabilities via the `capabilities` field in model config:

```yaml
models:
  agent:
    name: openai/gpt-5.4
    capabilities:
      dict_map_prompt_hints: true  # Inject hints for typed dict-map parameters
```

Available overrides:
- `dict_map_prompt_hints` (bool) — enables the `DictMapHints` prompt policy which appends explicit hints to the system prompt about dict-map parameters (`additionalProperties: {schema}`). When enabled together with `StrictSchema` (auto-enabled for GPT-5 models), both schema-level enriched descriptions AND system prompt hints are applied. Dict-map detection uses the shared `detect_dict_maps()` utility in [`tolokaforge/core/llm/dict_maps.py`](../tolokaforge/core/llm/dict_maps.py).
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
[`tolokaforge_models/data/model_presets.yaml`](../tolokaforge_models/src/tolokaforge_models/data/model_presets.yaml:22)
and set `cache_policy: none` on the preset. Observe cache-hit rates via
`Metrics.usage.cache_read_input_tokens` + `cache_creation_input_tokens` in
`metrics.yaml`.

To add support for a new model, add an entry to
`tolokaforge_models/data/model_presets.yaml`. Nine slots exist today —
`schema_sanitizer`, `prompt_policy`, `content_policy`, `response_policy`,
`reasoning_codec`, `cache_policy`, `params_policy`,
`message_assembly_policy`, `assistant_text_policy` — each mapping 1:1 to
a class registry in
[`tolokaforge/core/llm/presets.py`](../tolokaforge/core/llm/presets.py).
The overlay validator rejects unknown slot names (`ValueError` with a
`difflib.get_close_matches` suggestion) and unknown nested `params:`
keys inside a slot block. See
[ADR-0030 § Extension-point surface](adr/0030-tolokaforge-models-split.md#extension-point-surface-new--widening).

Each slot value takes one of two shapes:

1. **Bare `name` (legacy).** Deprecated since v0.17.0; removed in
   v0.18.0. Works for slots whose class takes no constructor kwargs.
2. **`{name, params}` mapping (recommended).** The `params` dict is
   forwarded to the class constructor, so a policy configurable at YAML
   level (e.g. Gemini's placeholder-signature filter, Nova's alternate
   filler string) needs no engine code change.

```yaml
presets:
  my_new_model:
    match: ["my-provider/my-model*"]
    schema_sanitizer: strict                     # bare name — no ctor kwargs needed
    prompt_policy: dict_map_hints
    cache_policy: none
    reasoning_codec: {name: gemini, params: {drop_placeholder_signature: true}}
    message_assembly_policy: {name: nova, params: {empty_assistant_filler: "Sure."}}
    # Top-level `params:` is the `GenerationParams` shorthand — its keys must
    # match `GenerationParams.KNOWN_KEYS`:
    params:
      supports_seed: true
      reasoning_via_extra_body: true
```

**Deprecation window.** `_RECOGNISED_OVERRIDE_KEYS` (the
`capabilities:` bespoke override keys landing on
`ModelConfig.capabilities`) follows the same v0.17.x → v0.18.0 window
as bare `name` slot values and is removed together in v0.18.0 — see
[#1017](https://github.com/Toloka/tolokaforge/issues/1017).

**Mutex on the `params_policy` slot.** A preset may declare **either** a
top-level `params: {…}` shorthand (the historical form; treated as
`params_policy: {name: generation_params, params: {…}}`) **or** a
slot-nested `params_policy: {name: …, params: {…}}` — never both.
Overlay load raises `ValueError` naming both source keys and the preset;
merging or picking one silently would produce a fingerprint that
disagrees with the actual constructor kwargs.

**Legacy `capabilities:` overrides — modern equivalents.** The
per-`ModelConfig` `capabilities:` block still accepts the historical
bespoke keys (`gemini_drop_placeholder_signature`,
`dict_map_prompt_hints`, `supports_typed_dict_maps`, …). The modern
equivalent lives in the preset overlay under the appropriate slot: e.g.
`capabilities: {gemini_drop_placeholder_signature: true}` is equivalent
to a preset overlay that declares
`reasoning_codec: {name: gemini, params: {drop_placeholder_signature: true}}`.
The legacy keys stay accepted through v0.17.x and go away in v0.18.0
alongside `_RECOGNISED_OVERRIDE_KEYS`.

### Provider bindings (`providers.yaml`)

Provider transport knobs — endpoint URL, credential env-var names, routability
under an `LLM_PROXY_BASE_URL` gateway, rotation env-var, rate-limit text
patterns, and Nova-shaped slug rewrite / per-attempt transport pinning —
ship as data in
[`tolokaforge_models/data/providers.yaml`](../tolokaforge_models/src/tolokaforge_models/data/providers.yaml).
Six entries today: `openrouter`, `openai`, `anthropic`, `gemini`, `nova`,
`mock`. Loaded once at `LLMClient` construction via
[`get_provider_binding`](../tolokaforge/core/llm/providers.py). Onboarding a
new provider or adjusting a shipped provider's transport is one YAML edit —
no `client.py` / `proxy.py` change.

Schema fields (see
[`ProviderBinding`](../tolokaforge/core/llm/providers.py) and
[`docs/LLM_LAYER.md`](LLM_LAYER.md#provider-bindings) for the full contract):

| Field | Meaning |
|---|---|
| `endpoint` | Provider base URL (informational; may be pinned per-attempt when `kwargs_pin_transport=true`). |
| `api_base_env` | Env-var name a deployment may set to override `endpoint` — the client `os.environ.setdefault(api_base_env, endpoint)` at init. Nova's `NOVA_API_BASE`. |
| `api_key_env` | Primary credential env-var name; read via `SecretManager`. The env var `_rotate_key` republishes into `os.environ` after picking the next key on the direct-provider path (OpenRouter's `OPENROUTER_API_KEY`). |
| `api_keys_env` | Rotation-list env-var name (comma-separated). `None` disables rotation. OpenRouter's `OPENROUTER_API_KEYS`. |
| `unroutable` | The `LLM_PROXY_*` gateway rejects the provider at config time even when named explicitly in `LLM_PROXY_PROVIDERS`. `mock` and `nova`. |
| `custom_llm_provider` | Value pinned into `kwargs["custom_llm_provider"]` — Nova: `"openai"`; OpenRouter: `"openrouter"`. When `None`, compound providers (`openrouter/google`) fall back to `provider.split("/")[0]`. |
| `rate_limit_patterns` | Regex strings compiled once at construction and consulted by `LLMClient._is_rate_limit_exception`'s tier-3 text fallback and by `LLMClient.classify_loop_error`. Every shipped non-mock provider carries the same `DEFAULT_RATE_LIMIT_PATTERNS` list; onboarding a provider whose rate-limit prose differs is a YAML edit. |
| `slug_rewrite` | Two-step per-attempt rewrite of `kwargs["model"]`: `strip_prefix` then `ensure_prefix`. Nova: `nova/` → `openai/`. |
| `format_model_name_bare` | When `true`, `LLMClient._format_model_name` returns `config.name` as-is. Nova only. |
| `kwargs_pin_transport` | When `true`, `endpoint` and `api_key_env` are read fresh per attempt and pinned into `kwargs["api_base"]` / `kwargs["api_key"]` (fails loud when `api_key_env` resolves empty). Nova only. |

`providers.yaml` ships inside the `tolokaforge-models` wheel at
`tolokaforge_models/data/providers.yaml`. Its bytes contribute to
`engine_run_state.json`'s `models_fingerprint.content_sha256` alongside
`pricing.json` and `model_presets.yaml` — see [`OUTPUT_FORMAT.md`](OUTPUT_FORMAT.md)
and [ADR-0030 § "Fingerprinting for auditability"](adr/0030-tolokaforge-models-split.md#fingerprinting-for-auditability).

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

The overlay file uses the schema of `model_presets.yaml` (`default:`,
`presets:`, `providers:`) plus one block the bundled file has no use for:

```yaml
litellm_models:                    # models litellm's own map does not carry
  meta/muse-spark-1.2:
    supports_function_calling: true
    supports_reasoning: true       # needed when the config sets models.agent.reasoning
    evidence: "2026-08-10, litellm 1.96.0: no entry, so meta refused tools
      before sending; admitting them returns a correct tool call."
```

Each entry declares what a model accepts on the wire, with the observation
behind it, and admits exactly the parameters its flags name for that model's
calls. Nothing else changes, and nothing is written into litellm's global
map. See [`docs/LLM_LAYER.md`](LLM_LAYER.md#when-litellm-has-never-heard-of-the-model).

`config validate` treats a model absent from litellm's map as an *unknown*,
not a refusal — it emits an INFO with the exact `litellm_models:` entry to
declare, and does not exit non-zero. A `False` from a *mapped* entry stays an
ERROR (WARNING for `openrouter/` providers).

Overlay
presets are prepended to the iteration order so first-match-wins lets you
shadow a bundled preset. Same-named overlay presets *replace* the bundled
entry (logged at INFO so the replacement is visible). Policy-name strings
(`schema_sanitizer`, `prompt_policy`, etc.) must resolve to a class already
shipped in the engine — overlays can compose existing policies but cannot
introduce new classes. Unknown names raise `ValueError` at startup naming
both the overlay file and the offending key.

#### Distributed-worker propagation

`tolokaforge prepare --presets-file overlay.yaml ...` persists the resolved
overlay path into `engine_run_state.json` alongside the run queue, together
with the `models_fingerprint` of the fully-resolved preset + pricing +
certificate state (see [`docs/OUTPUT_FORMAT.md`](OUTPUT_FORMAT.md#engine_run_statejson)
for the on-disk schema). The overlay is folded into the fingerprint's
`content_sha256`, so two runs that resolve to different preset tables record
different digests — a completed run identifies both the overlay path and the
exact model-data snapshot it was scored against. Worker subprocesses launched
later from the same `--run-dir` pick up the overlay path automatically — you
don't have to thread the flag through every `tolokaforge worker` invocation.
A worker-side `--presets-file` flag still wins over the persisted value when
both are set.

## Task Specification (`task.yaml`)

```yaml
task_id: "browser_simple_navigation"
name: "Simple Browser Navigation"
category: "browser"
description: "Navigate to the mock Example Domain page"
initial_user_message: "Open example.com and tell me the page title."   # optional — pinned opener, delivered verbatim as turn 1

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

interaction_mode: "conversational"   # or "agent_only" — see below

tools:
  agent:
    enabled: ["browser", "read_file", "write_file", "db_query", "search_kb"]
  user:
    enabled: []

actors:
  user:
    mode: "scripted" # "scripted" or "llm"
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

### `interaction_mode:` — turn-loop shape

Selects whether the trial dispatches a user simulator alongside the agent.
Two values today, room for future values (e.g. `multi_actor`) as new
`TurnPolicy` implementations register in the `tolokaforge.turn_policies`
entry-point group. See [ADR-0028](adr/0028-multi-actor-turn-policy.md).

- **`conversational`** (default) — user simulator dispatched every turn.
  τ-bench and every existing pack expects this shape; leaving the field
  unset keeps that behavior byte-for-byte.
- **`agent_only`** — no user turn dispatched after the initial message.
  The agent runs until it takes a turn without calling a tool (routed to
  `TerminationReason.AGENT_DONE`), or to `max_turns` or `episode_timeout_s`.
  The user simulator is never constructed. Matches agent-driven eval shapes
  (code migration, autonomous tool-use) where the task lives entirely in the
  system prompt.

Both shapes read the same field for turn 0. `initial_user_message` is the
task's pinned opener: its text is delivered verbatim as the first user
message, whitespace included, and no simulator dispatch produces that turn.
`agent_only` **requires** it — that shape has no simulator to synthesize a
bootstrap message, so a task declaring it without an opener fails loud at
run-start. `conversational` treats it as optional: unset, the user simulator
writes turn 1. Under either mode, declaring the key with an empty or
whitespace-only value is refused at load.

**A non-empty `tools.user.enabled` needs a user turn that can call it.** The
declaration reaches the runner like the agent's — the tools are registered for
the trial — so a pack whose user turn can never make a call fails nothing at run
time and grades a `requestor: user` action, or an `executor: user` matcher,
against a call that could not have happened. Two shapes reach that state and each
is refused at load naming which one it is and the fix: `interaction_mode:
agent_only`, which dispatches no user turn at all, and a user simulator resolving
to `mode: scripted`, whose reply is text and never a tool call. `tools.user.enabled: []`
loads under both — the declaration is what is refused, not the key.

## Grading Specification (`grading.yaml`)

```yaml
combine:
  method: "weighted"
  weights:                                 # one entry per component the pack configures
    state_checks: 0.4
    transcript_rules: 0.2
    trace_checks: 0.2
    llm_judge: 0.1
    custom_checks: 0.1
  pass_threshold: 0.8

state_checks:
  jsonpaths:
    - path: "$.db.orders[0].status"
      equals: "completed"

transcript_rules:
  must_contain: ["confirmed"]
  disallow_regex: ["(?i)password"]
  max_turns: 40
  tool_expectations:                       # one sub-check per declared tool,
    required_tools: ["browser"]             # graded on both substrates —
    disallowed_tools: []                    # see docs/GRADING.md § Transcript Rules

trace_checks:                              # ordering / absence / counting over the timeline
  constraints:                             # see docs/GRADING.md § Trace Checks
    - id: searched_before_answering
      description: "the policy corpus is searched before the reply is sent"
      require:
        before:
          left:  { quantifier: any,  match: { kind: tool_call, tool: { equals: search } } }
          right: { quantifier: last, match: { kind: assistant_message } }

custom_checks:                             # author-written Python, see docs/custom_checks.md
  enabled: true                            # required; false (default) disables the block

llm_judge:                                 # the judge MODEL is set once per run
                                           # under models.judge — NOT here
  customization:                           # optional; sibling of rubric
    disable_knowledge_search: true         # tri-state (unset | true | false):
                                           # true withholds every knowledge-search
                                           # tool from the JUDGE (agent untouched)
                                           # system_prompt (optional str | null):
                                           # replace the judge's default grading-stance
                                           # body; the marker contract is always
                                           # appended by the harness
    system_prompt: |
      Grade strictly against the policy.
    include_agent_system_prompt: false     # tri-state (unset | true | false):
                                           # false omits the agent's policy from the
                                           # judge's opening-message evidence
                                           # (evidence gating; agent untouched)
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

A whole-state hash is read only where the flag turns it on: `state_checks.hash`
carrying a `golden_actions` or `expect_initial_state` under an `enabled` that is not
truthy is rejected at load wherever the adapter grading the task answers the
hash-source question at all — both substrates test the flag before reading any
source, so a source the block declares and nothing reads is the author's defect
whatever the adapter supplies. The refusal is addressed at the source the pack
declared; where no adapter answers — the declared `adapter_type` names an adapter
this environment has not installed, or one that has not implemented the hook — the
same shape is reported unchecked at the same address. Every other authoring rule this file's
grading block is checked against — tool names, argument names, `regex` compilation —
is in [GRADING.md](GRADING.md#what-is-validated-before-a-run).

The rubric is a structured `Rubric` (per-criterion scoring + a required gate),
not a free-text blob; a free-text `rubric: "<text>"`, an `output_schema` field,
or a per-task judge-model field is rejected at load with a migration message.
The judge **model** is a run-level role (`models.judge`, see above) — separate
from the agent under test, with no default and no fallback.

`customization` is an optional block, sibling of `rubric`, holding judge-side
settings. `disable_knowledge_search` is tri-state (`unset` | `true` | `false`):
`true` removes every knowledge-search tool from the judge's schema (rag
`search_kb`, the `search_policy` passthrough, any future KB backend) — the
*agent's* tools are untouched. `system_prompt` (`str | None`) replaces the judge's
default grading-stance body; the harness always appends the marker contract, so a
custom prompt can never break `submit_report` validation.
`include_agent_system_prompt` (`bool | None`) controls whether the agent's policy is
embedded in the judge's opening-message evidence: unset/`true` include it (the
default), `false` omits it (evidence gating, distinct from `system_prompt`'s
wording). Omitting the block leaves the judge at the faithful default. All fields
layer project→task (a project default under
`grading_defaults.llm_judge.customization`, task wins; `system_prompt: null`
resets a project prompt; `include_agent_system_prompt: true` (explicit
re-include) or `null` (reset) both override a project `false`) — see
[PROJECTS.md](PROJECTS.md#task-override-semantics). A malformed value,
an empty/whitespace-only `system_prompt`, or an unknown key under `customization`
is rejected loudly at load. See
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

Routing calls through an LLM gateway (a LiteLLM proxy or equivalent) instead of
calling providers directly (`LLM_PROXY_BASE_URL`, `LLM_PROXY_API_KEY`,
`LLM_PROXY_HEADERS`, `LLM_PROXY_REQUEST_ID_HEADER`, `LLM_PROXY_PROVIDERS`) is
documented in [`docs/LLM_LAYER.md` § proxy](LLM_LAYER.md#proxy--routing-calls-through-an-llm-gateway),
including how a value may reference a secret as `${secret:NAME}`.

### Writing `.env` by hand or from a script

`DotEnvProvider` accepts `KEY=value`, `KEY="value"` and `KEY='value'`. The
unquoted form is `[^\s#]*`, so **an unquoted value containing whitespace or `#`
does not parse and the whole line is dropped.** Anything that depended on that
key then reads as unset. The provider logs a warning naming the key (never the
value) when it drops a line, so check the log if a variable you are sure you set
appears missing. Quote the value, or keep it free of whitespace.

This bites hardest with JSON values such as `LLM_PROXY_HEADERS`: pretty-printed
JSON is dropped, compact JSON is fine. A workflow that writes one should pipe it
through `jq -ce` and refuse outright if a name or value still contains whitespace,
since a silently header-less gateway call bills to nobody or fails admission far
from the cause. `.github/workflows/integrate-model.yml` does both.

Two shell notes for the same case. A `${secret:NAME}` reference survives being
written through a shell **variable**, because bash does not rescan the result of a
parameter expansion. Inlined into script text instead, bash reads `${secret:X}` as
substring syntax and silently yields an empty string, so the engine never sees the
reference and cannot raise on it.

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
