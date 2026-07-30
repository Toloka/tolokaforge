# Output Format — Authoritative Contract

This document enumerates every file, every field, and every schema version
that lands on disk for a TolokaForge run. Analytics consumers
([`tools/eval-orchestrator`](../tools/eval-orchestrator),
[`tools/benchmark-analyzer`](../tools/benchmark-analyzer), downstream
dashboards, PR reviewers) MUST read only these fields. Fields not listed
here are implementation details and may change without notice.

When we revise a contract, the schema version in the relevant file is
bumped and this document is updated in the same commit.

## Directory Layout

```
{output_dir}/
├── LIMIT_HIT.json                          ← only when a budget hit cut the run short
├── services/                              ← run-level per-service compose logs (shared-stack materialise failure only)
│   ├── {service}.log
│   └── _capture.yaml                      ← manifest (capture_reason: materialise_error)
└── trials/
    └── {task_id}/
        └── {trial_index}/
            ├── task.yaml
            ├── trajectory.yaml             ← message trace + status + metrics
            ├── env.yaml
            ├── metrics.yaml
            ├── grade.yaml
            ├── judge_trajectory.yaml       ← rubric-judge transcript (only when an LLM judge ran)
            ├── judge_inputs.yaml           ← rubric-judge structured inputs for replay (only when an LLM judge ran)
            ├── logs.yaml
            ├── prompts.yaml                ← agent + user-sim system prompts
            ├── tools_schemas.yaml          ← post-policy tool list
            └── services/                   ← per-service compose logs (on trial-body or graded failure)
                ├── {service}.log
                └── _capture.yaml           ← manifest (provision-failure path only)
```

Byte counts from every `services/` bundle above — per-trial and run-level — are
rolled up run-wide in `aggregate.json` → `captured_service_logs` (see
[`docs/ANALYTICS.md`](ANALYTICS.md:1) § `aggregate.json` → `captured_service_logs`).

* `{output_dir}` = the orchestrator's run output root. The naming
  convention differs by entry point:
  * `tolokaforge run` derives `{output_dir} = {base}_{YYYYMMDD_HHMMSS}`
    where `{base}` is `config.evaluation.output_dir` (e.g.
    `results/coding_example_20260629_154233`). Successive runs land in
    sibling directories with distinct timestamps.
  * `tolokaforge prepare` + `tolokaforge worker` use the `--run-dir`
    value verbatim. Workers join an already-prepared directory by
    name; no timestamp is appended.

  The directory basename is the canonical `run_id` stamped into
  `engine_run_state.json` and every `TrialSpec.run_id`, so the two
  entry points produce different `run_id` formats by design.
* `{task_id}` and `{trial_index}` map 1:1 to `TaskConfig.task_id` and the
  per-task trial ordinal.
* Every trial bundle is **self-contained** — every artifact needed to
  audit the trial lives inside a single `trials/{task_id}/{trial_index}/`
  directory. There is no results-root sidecar tree.

## `LIMIT_HIT.json`

Written under `{output_dir}/` on the first budget crossing during a
`tolokaforge run` — cost, wall-time, or terminated-trial count — and
absent on natural completion. Records which budget fired first, the
configured threshold, the counter's value at the moment of the hit, and
the time the hit was detected. Read by the CLI after `Orchestrator.run()`
returns to shape the `⏸ Run stopped (<reason>)` end banner (see
[`docs/CLI.md`](CLI.md) § Cost, time, and sample limits).

```json
{
  "which": "cost",
  "threshold": 5.0,
  "value_at_hit": 5.03,
  "timestamp": "2026-07-15T12:34:56Z"
}
```

| Field | Type | Meaning |
|---|---|---|
| `which` | `"cost"` \| `"time"` \| `"sample"` | Which budget crossed its threshold first. Additional values are rejected by the writer. |
| `threshold` | float | The limit as configured — `--cost-limit` USD or `--time-limit` seconds. Numeric type is `float` for uniform on-disk shape; integer limits round-trip losslessly. |
| `value_at_hit` | float | The counter's value at the moment of the hit. May exceed `threshold` on the last increment (e.g. a $0.02 trial pushing spend from $4.99 to $5.01 records `value_at_hit=5.01`). |
| `timestamp` | ISO 8601 UTC string | When the hit was detected. Formatted `YYYY-MM-DDTHH:MM:SSZ` with an explicit `Z` suffix. |

Written via [`tolokaforge.core.budgets.write_limit_hit_marker`](../tolokaforge/core/budgets.py); the on-disk shape is locked by the `LimitHitMarker` Pydantic model (`extra="forbid"`). A resumed run that hits a fresh limit overwrites an existing marker — the file always reflects the current run state, not a history.

## `trials/{task_id}/{trial_index}/tools_schemas.yaml`

* **Shape**: YAML sequence of tool-schema mappings.
* **Content**: the agent's raw tool schemas **after**
  `capabilities.schema_sanitizer.sanitize(...)`. Reproduces exactly what
  the provider saw on the wire.
* **Per-trial**: one file per trial, no dedup. Repeats of the same task
  on the same model write identical bytes to independent files —
  audit-friendly trade for ~150 KB extra per trial.
* **Latest-write-wins**: the orchestrator creates the trial directory
  fresh, then writes once per trial. If a re-run targets the same
  directory, the latest payload replaces the earlier one.
* **Guardrail**: pairs a run's effective tool surface to its trial
  outputs so every investigation starts with the actual schemas — not
  a guess.

## `trials/{task_id}/{trial_index}/prompts.yaml`

* **Shape**: YAML mapping with two string-or-null keys.
* **Content**:
  ```yaml
  system_prompt: "You are a helpful assistant..."     # agent system prompt
  user_system_prompt: "You are a user interacting..."  # user simulator prompt
  ```
* **Why a separate file**: domain-rich evals carry 15–20 KB of
  agent-side system prompt. Embedding that in `trajectory.yaml` made
  every message-trace open scroll past kilobytes of unchanging policy
  text. `prompts.yaml` keeps them readable, re-greppable, and
  separable from the per-turn timeline they conditioned.
* **Field names**: identical to the historical
  `Trajectory.system_prompt` / `Trajectory.user_system_prompt`. Only the
  file moved; analytics tools that already read those names still work,
  they just open `prompts.yaml` instead of `trajectory.yaml`.
* **Null semantics**:
  - `system_prompt: null` — no agent system prompt was set (rare; some
    non-LLM agents).
  - `user_system_prompt: null` — scripted user simulator (no LLM-shaped
    prompt). Distinct from a missing-file case where the orchestrator
    didn't run to write_prompts at all.
* **Per-trial**: one file per trial, no dedup; same self-contained
  pattern as `tools_schemas.yaml`.

## `trials/{task_id}/{trial_index}/task.yaml`

Snapshot of every identity the run was parameterised by. Readers use this
to reproduce a trial, not the `task.yaml` file in the task source
directory (they differ — this one is **frozen** at trial-start-time and
carries resolved preset info).

```yaml
task_id: "051fa6cb-..."
trial_index: 0
category: "food_delivery"
description: "Task description text"
grading_config:
  state_checks: {...}
  transcript_rules: {...}
  combine: {...}
tools:
  agent: {enabled: [...]}
  user: {enabled: [...]}
policies: {...}
model_config:
  agent:
    provider: "anthropic"
    name: "claude-opus-4.7"
    temperature: 0.0
    reasoning:                              # ReasoningConfig struct (Stage 0)
      mode: "budget"
      budget_tokens: 8000
      effort_hint: null
      display: null
    capabilities: {...}
    resolved:                               # Stage 7 — preset fingerprint
      effective_preset: "anthropic_claude_4_7"
      schema_sanitizer: "passthrough"
      prompt_policy: "none"
      content_policy: "anthropic"
      response_policy: "standard"
      reasoning_codec: "anthropic"
      cache_policy: "anthropic_ephemeral"
  user:
    provider: "openai"
    name: "gpt-4o-mini"
    reasoning: null
    resolved:
      effective_preset: "default"
      schema_sanitizer: "passthrough"
      prompt_policy: "none"
      content_policy: "openai"
      response_policy: "standard"
      reasoning_codec: "none"
      cache_policy: "none"
  judge: null                               # run-level rubric judge (models.judge);
                                            # null when unconfigured, else a full
                                            # role block with its own resolved.*
```

### `model_config.<role>.resolved.*` (Stage 7, P6)

Computed by the orchestrator at trial-start via
[`tolokaforge.core.llm.presets.resolve_effective_preset`](../tolokaforge/core/llm/presets.py)
+ [`resolve_policy_names`](../tolokaforge/core/llm/presets.py). Shape:

| Field | Values | Source |
|---|---|---|
| `effective_preset` | preset name from [`model_presets.yaml`](../tolokaforge/core/data/model_presets.yaml) (e.g. `anthropic_claude_4_7`) or `"default"` on fallthrough | `resolve_effective_preset` |
| `schema_sanitizer` | `passthrough` \| `strict` | policy registry |
| `prompt_policy` | `none` \| `dict_map_hints` | policy registry |
| `content_policy` | `openai` \| `anthropic` | policy registry |
| `response_policy` | `standard` \| `unwrap_input` \| `array_dict_map` | policy registry |
| `reasoning_codec` | `none` \| `anthropic` \| `openai` | policy registry |
| `cache_policy` | `none` \| `anthropic_ephemeral` | policy registry |

`params_policy` is intentionally omitted from `resolved.*` — it is a
stateful [`GenerationParams`](../tolokaforge/core/llm/params_policy.py)
dataclass, not a single-named policy. Callers needing the full parameter
block read the `agent.capabilities` block directly.

The `judge` role (the run-level read-only rubric judge, `models.judge`) is
recorded symmetrically with `agent` / `user` — its own role block plus a
`resolved.*` fingerprint — so every grade bundle records which judge produced
it. It is `null` when the run configures no judge.

## `trials/{task_id}/{trial_index}/trajectory.yaml`

The trajectory carries only the message trace + per-trial metadata. The
agent and user-simulator system prompts live in
[`prompts.yaml`](#trialstask_idtrial_indexpromptsyaml); tool schemas in
[`tools_schemas.yaml`](#trialstask_idtrial_indextools_schemasyaml).

```yaml
task_id: "051fa6cb-..."
trial_index: 0
simulator_schema_version: 1
start_ts: "2026-01-01T12:00:00+00:00"
end_ts: "2026-01-01T12:05:00+00:00"
status: "completed"                                   # TrialStatus enum
termination_reason: "agent_done"                      # TerminationReason enum or null
messages:
  - role: "user"
    content: "..."
    ts: "2026-01-01T12:00:00Z"
  - role: "assistant"
    content: "..."
    tool_calls: [...]
    reasoning:                                        # StructuredReasoning
      blocks:
        - type: "thinking"
          text: "Considering the best tool to call."
          signature: "EqoBCkgIARABGAIiQAK+..."
          encrypted_data: null
        - type: "redacted_thinking"
          text: ""
          signature: null
          encrypted_data: "EvwBCkgIARABGAIi..."
      summary: null   # null when blocks already cover the content (Plan B)
      budget_used: 512
    ts: "2026-01-01T12:00:01Z"
```

### Top-level fields

| Field | Type | When populated | Purpose |
|---|---|---|---|
| `simulator_schema_version` | `int` | always `1` today | Monotonic; bump whenever the simulator prompt shape changes. Analytics consumers gate cross-run comparisons on this stamp. |

### `messages[*].reasoning.summary` — when populated

`StructuredReasoning.summary` is the **server-shipped abstract** — a
string distinct from the verbatim `blocks[*].text`. In production
transports (OpenAI / OpenRouter-routed Anthropic) the provider populates
the abstract from the same source as the blocks, so the codec drops it
to avoid doubling the on-disk reasoning footprint. Result:

* `summary == null` **when** `"\n\n".join(b.text for b in blocks if b.text) == server_summary` — the typical case for our routes today.
* `summary != null` **when** the provider ships a strictly distinct
  abstract (rare; reserved for direct-API paths). Consumers should fall
  back to `as_plain_text()` for a routing-agnostic projection.

### `messages[*].reasoning` — [`StructuredReasoning`](../tolokaforge/core/llm/reasoning.py)

| Block `type` | Source | `text` | `signature` | `encrypted_data` |
|---|---|---|---|---|
| `thinking` | Anthropic `message.thinking_blocks` with `type: "thinking"` | visible chain-of-thought (may be empty when `display="omitted"`) | base64-ish signature; MUST be echoed back verbatim to sustain interleaved thinking | `null` |
| `redacted_thinking` | Anthropic `message.thinking_blocks` with `type: "redacted_thinking"` | always `""` | `null` | opaque payload from `data` field |
| `summary_text` | OpenAI / Qwen / Grok `message.reasoning_content` | summary text | `null` | `null` |

The `reasoning` block is extracted by the provider-specific `ReasoningCodec`
registered on the preset (see
[`docs/LLM_LAYER.md`](LLM_LAYER.md) § `reasoning_codec`). Non-reasoning
models emit `reasoning: null`.

## `trials/{task_id}/{trial_index}/env.yaml`

```yaml
agent: {...}       # Agent-side database state
user:
  device: {...}    # User device state
db: {...}          # Full database state
filesystem: {...}  # File system state
mock_web_url: "..."
agent_visible_dir: /work
environment:       # present only for manifest-driven trials (see below)
  network_policy: no_internet
  runner_service: runner
  services:
    app-db:
      image: postgres:16
      pinned: true
      isolation: reset
      reset_seed: baseline
      dsns: []
      mounts:
        - /docker-entrypoint-initdb.d/init.sql:ro
    app-service:
      image: tolokaforge-runner:0.5.0
      pinned: true
      isolation: shared
      reset_seed: null
      dsns:
        - postgresql://app:***@app-db:5432/mfg
      mounts:
        - /srv/app/main.py:ro
```

Free-form snapshot of the environment state at trial end. Adapters /
tasks control the shape; no schema version attached.

### `environment` — resolved environment identity

Manifest-driven trials (a task carrying an `environment_manifest`, i.e.
Project-layer / multi-container substrates) record the resolved
environment identity under `environment`. The block is a pure function of
the trial's `EnvironmentManifest`, so it is available for post-mortems
even after a per-trial stack is torn down. Trials without a manifest
(run.yaml-only / JSON-DB tasks) omit the key entirely.

Top-level keys:

| Key | Meaning |
|---|---|
| `network_policy` | Run-level network posture (`no_internet` / `limited_internet` / `full_internet`) |
| `runner_service` | Compose service the runner executes inside |
| `services` | Per-service identity, keyed by compose service name |

Each `services.<name>` entry:

| Field | Meaning |
|---|---|
| `image` | Resolved image reference after `${VAR}` / `${VAR:-default}` substitution from `stack_inputs` |
| `pinned` | `true` when the resolved image carries a digest or a non-floating tag |
| `isolation` | `shared` / `reset` / `ephemeral`; services absent from the manifest default to `ephemeral` |
| `reset_seed` | Seed name for a `reset` service, else `null` |
| `dsns` | Connection strings from the service's compose `environment`, each with any embedded password replaced by `***` |
| `mounts` | Container-side mount targets (`<target>:<mode>`); host source paths are omitted |

DSN passwords are redacted and host mount sources are never recorded, so
the block is safe to share and stable across hosts.

## `trials/{task_id}/{trial_index}/metrics.yaml`

`usage` is a nested block that carries the full
[`tolokaforge.core.llm.usage.Usage`](../tolokaforge/core/llm/usage.py:1)
dataclass. Anthropic cache counters + reasoning-budget spend are
first-class fields; a `provider_raw` dump of the litellm usage block is
included for forensics. Each LLM API call is also recorded in
`usage.calls[]` as a `ProviderRawCall` carrying its per-call tokens,
`cost_usd`, `cost_source` (`"litellm"` / `"local"` / `"unknown"`), and
`latency_s` — the trial-level `cost_usd` is the sum of those entries.

To help analytics consumers detect schema evolution, every trial-level
metrics file includes a root-level `schema_version: 1` marker.

```yaml
latency_total_s: 174.14
turns: 14
api_calls: 14
usage:
  prompt_tokens: 2006
  completion_tokens: 300
  reasoning_tokens: 250
  cached_tokens: 1920
  cache_creation_input_tokens: 0
  cache_read_input_tokens: 1920
  provider_raw: {...}      # raw litellm usage block for the last call
  calls:                   # one ProviderRawCall per LLM API call
    - prompt_tokens: 256
      completion_tokens: 18
      cached_tokens: 0
      reasoning_tokens: 0
      cache_creation_input_tokens: 0
      cache_read_input_tokens: 0
      cost_usd: 0.00912
      cost_source: litellm
      latency_s: 1.23
cost_usd: 0.127055
tool_calls: 7
tool_success_rate: 1.0
stuck_detected: false
tool_usage:
  - tool: "get_user_details"
    count: 2
    success: 2
    fail: 0
  - tool: "create_order"
    count: 2
    success: 2
    fail: 0
rate_limit_retries: 0
rate_limit_wait_s: 0.0
rate_limit_first_ts: null
rate_limit_last_ts: null
rate_limit_by_role_model: []
probe_successful_calls: 0
probe_success_duration_s: 0.0
probe_prompt_tokens: 0
probe_completion_tokens: 0
probe_bucket_width_s: 0
probe_dropped_buckets: 0
probe_buckets: []
  - tool_name: "create_order"
    call_count: 2
    success_count: 2
    error_count: 0
    total_duration_s: 1.84
  - tool_name: "get_user_details"
    call_count: 2
    success_count: 2
    error_count: 0
    total_duration_s: 0.42
```

`tool_usage` rolls up the trial's recorded tool calls by tool name, sorted by
name, and its field names match the
[`ToolUsage`](../tolokaforge/core/models.py) model so a consumer can
`model_validate()` the round-trip. `success_count` counts calls whose recorded
status is `success`; every other status — including a call the executor refused
before it reached the tool — counts as an error. `total_duration_s` sums the wall
time measured around each call, failures included.

`tool_log`, the trial's ordered `RecordedToolCall` list, is **not** written to any
artifact. It lives on the in-process `Trajectory` and feeds grading; a bundle read
back from disk carries the message trace but no per-call `output`, `status`,
`latency_seconds` or `executor`. Persisting it is a stated prerequisite of #682
(replay).

Semantics per `usage` field (see
[`docs/LLM_LAYER.md`](LLM_LAYER.md:1) § `usage` for the provider-routing
table):

| Field | Populated by | Meaning |
|---|---|---|
| `prompt_tokens` | every provider | Total input tokens counted toward billing |
| `completion_tokens` | every provider | Total generated tokens |
| `reasoning_tokens` | providers with thinking | `completion_tokens_details.reasoning_tokens` (thinking-budget spend) |
| `cached_tokens` | OpenAI + Anthropic | `prompt_tokens_details.cached_tokens` (generic cache hit) |
| `cache_creation_input_tokens` | Anthropic | Tokens written to the ephemeral cache this call |
| `cache_read_input_tokens` | Anthropic | Tokens re-used from the ephemeral cache this call |
| `provider_raw` | — | Best-effort dump of the *last* call's raw usage block |

### `rate_limit_*` / `probe_*` — rate-limit probe accounting

Populated only by runs with
[`orchestrator.rate_limit_probe.enabled: true`](CONFIG.md:1); zero / `null` /
empty on every other run.

Two prefixes, two censuses of the same mode. `rate_limit_*` is the **failure**
side (429 retries and the sleep they cost); `probe_*` is the **success** side
(goodput, served concurrency, tokens). Both are needed — see
[Why both censuses](#why-both-censuses) below.

| Field | Meaning |
|---|---|
| `rate_limit_retries` | 429 retries the probe absorbed across every LLM call in the trial (agent + user simulator) |
| `rate_limit_wait_s` | Summed retry sleep scheduled for those retries |
| `rate_limit_first_ts` / `rate_limit_last_ts` | UTC timestamps bracketing the window the trial spent blocked on 429s. The **429** window — successful calls do not move it |
| `rate_limit_by_role_model` | Both censuses split per `(role, model)`; every flat field above and below is the sum across these rows |
| `probe_successful_calls` | LLM calls that returned a result. `>=` the number of `usage.calls` rows: a call whose provider returned no usage block still succeeded but adds no usage row |
| `probe_success_duration_s` | Summed client-observed duration of those successful calls |
| `probe_prompt_tokens` / `probe_completion_tokens` | Tokens the provider reported for those successful calls |
| `probe_bucket_width_s` | Width of a `probe_buckets` window, seconds — the denominator of every per-window rate |
| `probe_dropped_buckets` | `(role, model, window)` **rows** refused by `max_buckets` — the same unit as the cap, so one window lost on a two-role trial counts 2. Non-zero means `probe_buckets` is a truncated *prefix* and only the flat / per-`(role, model)` totals are complete |
| `probe_buckets` | The same counters per `(role, model, absolute window)`, sorted by window then role then model |

```yaml
rate_limit_retries: 4
rate_limit_wait_s: 60.0
rate_limit_by_role_model:
  - role: agent
    model: openrouter/deepseek/deepseek-v3.2-exp
    retries: 3
    wait_s: 45.0
    first_ts: "2026-07-29T10:00:00Z"
    last_ts: "2026-07-29T10:00:30Z"
    successful_calls: 18
    success_duration_s: 214.5
    prompt_tokens: 369857
    completion_tokens: 4120
  - role: user
    model: openrouter/anthropic/claude-sonnet-4.6
    retries: 1
    wait_s: 15.0
    first_ts: "2026-07-29T10:01:00Z"
    last_ts: "2026-07-29T10:01:00Z"
    successful_calls: 9
    success_duration_s: 11.2
    prompt_tokens: 89984
    completion_tokens: 380
probe_successful_calls: 27
probe_success_duration_s: 225.7
probe_prompt_tokens: 459841
probe_completion_tokens: 4500
probe_bucket_width_s: 30
probe_dropped_buckets: 0
probe_buckets:
  - bucket_start_ts: "2026-07-29T10:00:00Z"
    role: agent
    model: openrouter/deepseek/deepseek-v3.2-exp
    successful_calls: 12
    success_duration_s: 138.0
    prompt_tokens: 240110
    completion_tokens: 2700
    retries: 3
    wait_s: 45.0
  - bucket_start_ts: "2026-07-29T10:00:30Z"
    role: agent
    model: openrouter/deepseek/deepseek-v3.2-exp
    successful_calls: 6
    success_duration_s: 76.5
    prompt_tokens: 129747
    completion_tokens: 1420
    retries: 0
    wait_s: 0.0
```

The `(role, model)` breakdown exists because the roles are different models: in
an arena config the agent is the model under test and the user simulator is a
fixed, unrelated one, so a single flat counter blends a measured model's numbers
with an unmeasured one's. `Metrics.usage` cannot substitute — `usage.calls` holds
agent calls only and carries **no role field**, so per-model goodput and latency
are not computable from it at all. Rows are sorted by `(role, model)`.

`model` is the raw provider-qualified slug the client called. Grouping slugs into
an upstream-provider taxonomy is the consumer's job. Attributing a 429 to the
specific OpenRouter upstream that served the request is **not** available here —
`provider` is `openrouter` for every role in an OpenRouter-routed config, and the
engine does not capture the upstream identity.

`latency_total_s` is trial wall time and therefore *includes*
`rate_limit_wait_s`. A non-zero `rate_limit_wait_s` is the mechanical marker
that this trial's latency figures are not comparable with a normal run's, and
that the run must not produce a leaderboard number. The zero case proves nothing
about the *mode*: a probe that found headroom looks exactly like a normal run on
the 429 census.

#### Why both censuses

The 429 count alone is not a measurement:

- It is **schedule-dependent**. It counts how often *your* clients chose to poll,
  which is why the retry interval is fixed — `retries / (1 / retry_interval_s)`
  recovers blocked client-time only because the interval is constant.
- It is **silent for some providers** — a provider can throttle by slowing calls
  down instead of rejecting them, and then the 429 census shows no ceiling at all
  while goodput and latency both do.

`probe_success_duration_s / wall_seconds` is the Little's-law in-flight
concurrency the provider actually served. It is computed on **successful calls
only**, which is what makes it schedule-independent.

#### Field observations

The single record of the measurements the design above is justified by. The code
and the rest of these docs state the *invariants* and point here rather than
restating figures that a second probe run will change.

| Observation | Measured |
|---|---|
| Silent throttling — a provider can hold the 429 count at zero and pay for it in latency instead | A model with no provider pin produced **zero** 429s across four probe runs up to **33k input tokens/s**, while per-call latency inflated **41 %**. Only goodput and latency showed the ceiling. |
| Non-stationarity — a cumulative average reports neither end of a decay, which is why `probe_buckets` exists | At a **constant** 70-way offered concurrency, goodput fell **1.70 → 0.43** successful calls/s over ~12 minutes while the rejection rate climbed **66 % → 86 %**. The blended average is ~1.07. |
| 429 volume at the cap | **3,176** absorbed 429s on one 70-way probe leg. |
| Token profiles differ ~4x between domains, so tokens/s — not calls/s — is what sums across legs | **369,857** vs **89,984** input tokens per trial. |

Measured on OpenRouter, 2026-07. Re-measure before quoting: none of these is a
property of the harness.

#### Computing goodput, tokens/s and served concurrency

Every emitted field is an **additive count over an absolute-time window**, never
a rate. A single trial's goodput ratio is meaningless — goodput is a run-level
quantity — so sum the counts first and form the ratio last.

For one window `W` (one `probe_buckets` row, or the sum of the rows sharing a
`bucket_start_ts`), with `width = probe_bucket_width_s`:

```
goodput (successful calls/s) = W.successful_calls   / width
input tokens/s               = W.prompt_tokens      / width
output tokens/s              = W.completion_tokens  / width
served concurrency           = W.success_duration_s / width      # Little's law
mean served latency (s)      = W.success_duration_s / W.successful_calls
rejection rate               = W.retries / (W.retries + W.successful_calls)
```

Whole-run figures replace `width` with the run's wall seconds and use the flat
`probe_*` totals summed across trials:

```
run goodput = sum(trial.probe_successful_calls) / run_wall_seconds
```

**A whole-run average understates the peak.** A batch of `N` trials at `N`
workers has a spike-then-drain profile: every worker starts at once, so offered
concurrency is `N` at the beginning and decays to 1 as the last trials finish.
Averaging across the whole run divides peak work by a wall time that includes the
drain. Read the per-window series for the sustained plateau and take the whole-run
average only as a lower bound. For the same reason a probe sample set needs at
least as many trials as the offered concurrency, or it measures the sample size
instead of the provider.

#### Summing across simultaneous run legs

One runner process serves one domain, so a full measurement runs all legs
**simultaneously**, one process each, and sums their throughput into a global
number. The rules:

1. **Launch the legs simultaneously.** Sequential legs never load the provider at
   the intended total concurrency, so their sum is not a measurement of anything.
2. **Align on `bucket_start_ts`, not on run start.** The boundary is
   `floor(epoch_seconds / bucket_width_s) * bucket_width_s`, so two processes on
   two machines with synchronised clocks emit the *identical* value for the same
   instant. Group every leg's rows by that timestamp and add the counts
   window-by-window. A run-start-relative boundary would give every leg its own
   grid and make the sum meaningless — which is the whole reason the field is
   epoch-anchored.
3. **Use the same `bucket_width_s` in every leg.** Different widths do not align.
4. **Sum tokens/s, not calls/s.** Measured token profiles differ ~4x between
   domains (see [Field observations](#field-observations)), so a call in one leg is
   not the same unit of work as a call in another. Tokens are the additive
   quantity; calls/s across mixed domains is not.
5. **Filter to the roles you are measuring.** Keep `role: agent` rows if the model
   under test is the agent; the simulator is a different, unmeasured model sharing
   the same quota.
6. **Drop the first and last window of the merged series.** Legs do not start and
   stop on a window boundary, so the edge windows are partially covered and read
   low.
7. **Check `probe_dropped_buckets`.** Non-zero on any leg means that leg's series
   is a truncated prefix; the flat totals are still complete, the series is not.
   It counts refused `(role, model, window)` **rows**, so divide by the number of
   roles to get windows. `max_buckets` is a global cap on rows rather than a
   per-series one, so a high-volume role can consume the whole budget and a
   low-volume role's series can be absent entirely while its
   `rate_limit_by_role_model` row is non-zero — the counter does not say which
   series truncated. Both are unreachable at the 4096 default within any episode
   budget the invariant permits.

```python
# Merge one leg's trials into a per-window series, then add legs together.
from collections import defaultdict
import yaml, glob

series = defaultdict(lambda: {"calls": 0, "prompt_tokens": 0, "duration_s": 0.0})
for path in glob.glob("output/<leg>/trials/*/metrics.yaml"):
    metrics = yaml.safe_load(open(path))
    for row in metrics["probe_buckets"]:
        if row["role"] != "agent":
            continue
        window = series[row["bucket_start_ts"]]
        window["calls"] += row["successful_calls"]
        window["prompt_tokens"] += row["prompt_tokens"]
        window["duration_s"] += row["success_duration_s"]
```

`yaml.safe_load` is deliberate: parsing with a *pinned older* `tolokaforge`
`Metrics` model raises on the new keys (`extra="forbid"`).

### `provisioning_duration_s` — wall-clock provisioning latency

```yaml
provisioning_duration_s: 5.5
```

Wall-clock seconds (monotonic-clock-measured, rounded to milliseconds) from
`provision` start through `endpoints()` return — the full
`provision → await_ready → endpoints` bracket, excluding the trial body and
teardown. Recorded on every trial that provisions successfully and runs a
conductor body. Omitted from the provision-failure `metrics.yaml` (see
[Provision-failure bundle](#provision-failure-bundle)) because no
`provision → await_ready → endpoints` bracket completed.

### Provision-failure bundle

When a trial fails during substrate provisioning (`provision` / `reset_recipe`
raises, or `await_ready` times out), the conductor body never runs, so the
executor writes the trial directory itself. Only three files land —
`trajectory.yaml`, `metrics.yaml`, and `grade.yaml` — plus the
[`services/`](#trialstask_idtrial_indexservices) bundle when per-service capture
fired inside `provision`. `task.yaml`, `env.yaml`, and `logs.yaml` are **not**
written (no resolved model config, no environment state, no per-trial logger for
a run that never happened).

* `trajectory.yaml` — `status: error`, `termination_reason: provision_error`,
  empty `messages`.
* `metrics.yaml` — the default-`Metrics` shape (`cost_usd: null`,
  `schema_version: 1`, empty `tool_usage`) plus two top-level failure-signal
  keys:

  ```yaml
  error: provision_error
  error_reason: "partial startup — failed after service 'db'"
  ```

  `error` is the `TerminationReason.PROVISION_ERROR` value, so the failure
  vocabulary matches `trajectory.yaml`'s `termination_reason`; `error_reason`
  carries the underlying `ProvisionError` reason string. `provisioning_duration_s`
  and `captured_service_logs` are absent on this path.
* `grade.yaml` — `binary_pass: false`, `score: 0.0`, `reasons` carrying the
  provisioning failure stage and reason.

Writing this bundle is best-effort: an I/O failure while writing it is logged
and does not change the trial's failed result.

### `captured_service_logs` — on trial-body or graded failure

When a trial is diagnostics-worthy — its body fails (`trajectory.status` is
`error` or `timeout`) **or** it runs to completion but grades red
(`trajectory.status` is `completed` with `grade.binary_pass: false`) — on the
per-trial runtime backend, the executor captures each compose service's
`docker compose logs` output to
[`services/{service}.log`](#trialstask_idtrial_indexservices) **before**
teardown and records the byte counts as a top-level mapping on this file:

```yaml
captured_service_logs:
  db: 4096
  runner: 512
```

The key is present only when at least one service produced output. A
`completed` trial that passes, or one with no grade, does not trigger capture.
Capture on a successful trial is off by default; enable it with
`compute.capture_logs_on_success: true` (see [`docs/CONFIG.md`](CONFIG.md:1)).

These byte counts are rolled up across the run in `aggregate.json` →
`captured_service_logs` (see [`docs/ANALYTICS.md`](ANALYTICS.md:1)).

## `trials/{task_id}/{trial_index}/services/`

Per-service compose logs, written on a trial-body or graded failure and only on
the per-trial runtime backend (see
[`docs/RUNTIME_BACKENDS.md`](RUNTIME_BACKENDS.md:1)
§ "Per-service log capture on failure"). The shared-stack backend never writes
this directory.

* **`{service}.log`** — raw `docker compose logs --tail=N` bytes for one
  service (`N` = `compute.log_tail`, default 500). One file per compose
  service that produced output. No parsing, no format changes.
* **`_capture.yaml`** — manifest written **only on the provision-failure
  path** (compose-up or reset-recipe failure). The backend captures per-service
  logs inside `provision` and writes this manifest before it raises, so it holds
  the byte counts on this path:

  ```yaml
  tail: 500
  capture_reason: provision_error
  services:
    db:
      bytes: 4096
  ```

  On the trial-body path the durable record is the
  [`metrics.yaml` `captured_service_logs`](#captured_service_logs--on-trial-body-or-graded-failure)
  field instead. The provision-failure
  [`metrics.yaml`](#provision-failure-bundle) the executor writes does **not**
  carry `captured_service_logs`, so the two surfaces never both hold the byte
  counts.

  Byte counts from this manifest are rolled up across the run in `aggregate.json`
  → `captured_service_logs` (see [`docs/ANALYTICS.md`](ANALYTICS.md:1)).

## `trials/{task_id}/{trial_index}/grade.yaml`

```yaml
binary_pass: false
score: 0.0
components:
  state_checks: 0.0
  transcript_rules: null
  llm_judge: null
  custom_checks: null
reasons: "State: State hash mismatch. Diff: ..."
state_diff:  # Present when state check fails
  diff: |
    --- expected_state
    +++ actual_state
    @@ -1,10 +1,10 @@
    ...
  diff_lines: 200
  has_diff: true
custom_checks_details: null     # list[CustomCheckDetail] or null
criterion_results:              # per-criterion rubric breakdown; null unless an LLM judge ran
  - id: refund_amount
    met: true
    score: 1.0
    justification: "Reply quotes the correct $328.50 refund."
  - id: tone
    met: false
    score: 0.4
    justification: "Polite but terse; missed the apology the policy asks for."
judge_status: completed         # unspecified | completed | errored
judge_usage:                    # the judge's OWN token spend; null unless an LLM judge ran
  calls: 3
  prompt_tokens: 4120
  completion_tokens: 318
  reasoning_tokens: 0
  cost_usd: 0.0142
  tool_calls: 4
  consistency_rejections: 0    # submit_report attempts rejected for a verdict/justification mismatch
judge_kb_gating:                # the judge's knowledge-search gating; null unless an LLM judge ran
  knowledge_search_disabled: false  # config withheld the judge's KB tools (authoritative replay signal)
  offered: [search_kb]         # KB-tagged tools the judge was offered (audit detail)
  withheld: []                 # KB-tagged tools withheld by config (audit detail)
judge_custom_prompt: false      # null (no judge) | false (default prompt) | true (custom prompt)
judge_agent_prompt_included: true  # null (no judge) | false (agent policy gated out) | true (included)
```

Score scale: `0.0` ≤ `score` ≤ `1.0`. `binary_pass` is the harness-level
pass/fail; `score` is a fractional pass rate (used for tasks with partial
credit).

### Rubric-judge fields

`criterion_results`, `judge_status`, `judge_usage`, `judge_kb_gating`,
`judge_custom_prompt`, and `judge_agent_prompt_included` are populated only when an
LLM rubric judge ran (`grading.llm_judge` configured). See
[`docs/GRADING.md`](GRADING.md) for the rubric mechanism and the two weighting
layers.

* `criterion_results` — one entry per rubric criterion: `id`, `met`
  (binary verdict; for graded criteria, whether it cleared the author's
  0.5 threshold), `score` (`0`/`1` for binary, `0`–`1` for graded), and
  the judge's `justification`. `null` when no judge ran; `[]` is distinct
  (judge ran, rubric had no scorable criteria).
* `judge_status` — `unspecified` (no judge configured), `completed`
  (per-criterion results produced), or `errored`. **`errored` is the
  fail-loud marker**: the judge malfunctioned (retry / wall-time
  exhaustion or a crash). The `components.llm_judge` score is then
  **incomplete and MUST NOT be read as `0.0`** — it is left unscored and
  excluded from the weighted combine.
* `judge_usage` — the judge's own token usage / cost, separate from the
  agent's `metrics.yaml` `usage`. The judge runs a separate LLM inside
  the Runner; this records what *grading* cost. Populated for both
  `completed` and `errored` runs (an errored judge still spent tokens).
  `consistency_rejections` counts how many times the judge's `submit_report`
  was rejected for a verdict/justification mismatch (marker missing or
  marker/verdict conflict) on this trial — distinct from generic schema
  rejections, and `0` when every verdict matched its justification.
* `judge_kb_gating` — the judge's knowledge-search gating for this trial,
  kept separate from `judge_usage` (which stays strictly token/cost).
  `knowledge_search_disabled` is the **authoritative signal**:
  `true` means `grading.llm_judge.customization.disable_knowledge_search`
  withheld the judge's KB tools, regardless of whether the agent had any KB
  tool. `offered` and `withheld` are supporting audit detail — the KB-tagged
  tools the judge actually got, and those config withheld. An empty
  `withheld` on a disabled judge means the agent had no KB tool to gate. See
  [`docs/GRADING.md`](GRADING.md#judge-kb-faithfulness).
* `judge_custom_prompt` — whether the judge ran with a custom system-prompt
  body (`grading.llm_judge.customization.system_prompt`). Tri-state: `null`
  when no judge ran, `false` when the judge used the default prompt, `true`
  when a custom prompt replaced the default body (the marker contract is always
  appended regardless). The full custom text is not copied here — it lives in
  `task.yaml.grading_config.llm_judge.customization`, one file over in the same
  bundle. See [`docs/GRADING.md`](GRADING.md#customizing-the-judges-system-prompt).
* `judge_agent_prompt_included` — whether the harness embedded the agent's policy /
  system prompt in the judge's opening-message evidence
  (`grading.llm_judge.customization.include_agent_system_prompt`). Tri-state: `null`
  when no judge ran, `false` when the agent policy was gated out of the judge's
  evidence, `true` when it was included. Records the effective *setting*, not
  whether a block physically appeared — a trial with an empty agent prompt still
  reads `true` under the default. See
  [`docs/GRADING.md`](GRADING.md#gating-the-agents-policy-out-of-the-judges-evidence).

### Custom-checks fields

`components.custom_checks` and `custom_checks_details` are populated only when
the pack sets `grading.custom_checks.enabled: true` and delivers a `checks.py`
alongside the task. See [`docs/GRADING.md`](GRADING.md#custom-checks) for how
the aggregate combines with the other three components and
[`docs/custom_checks.md`](custom_checks.md) for the `@init` + `@check`
authoring API.

* `components.custom_checks` — aggregate score over every `@check` the pack
  emitted, in `[0.0, 1.0]`. `null` when the pack has no `custom_checks` block
  (or set `enabled: false`); when the executor errored under `fail_on_error:
  false` the component is also excluded from the weighted combine. See
  [`docs/custom_checks.md`](custom_checks.md#grade-output) for the scoring
  rules.

  **Field-overload note**: tasks graded via the alternate test-execution
  reward path (a `test.sh` grader, not `custom_checks.enabled: true`) also
  write to `components.custom_checks` — carrying the reward score in the
  same float. Unambiguous within a single grading mode: only one of the two
  paths writes the field per grade, and `custom_checks_details` is `null`
  under the reward path.
* `custom_checks_details` — one `CustomCheckDetail` entry per `@check` the
  pack emitted. `null` when no custom checks ran; `[]` is distinct (the
  executor ran but produced no per-check results). Each entry carries:
    * `check_name` — the `@check` function name; the reserved sentinel
      `__executor__` marks a top-level executor failure (module load error,
      timeout, crash) so the audit survives even when no per-check ran.
    * `status` — `"passed"` | `"failed"` | `"skipped"` | `"error"`.
    * `score` — `[0.0, 1.0]`; `skipped` checks are excluded from the
      aggregate.
    * `message` — human-readable one-liner from the check's return value.
    * `details` — optional dict of arbitrary structured detail the check
      attached (decoded from the wire `details_json`); `null` when empty.

Populated sample:

```yaml
components:
  state_checks: 1.0
  transcript_rules: null
  llm_judge: null
  custom_checks: 1.0
custom_checks_details:
  - check_name: balance_matches_transaction_net
    status: passed
    score: 1.0
    message: "balance 700 == opening 500 + credits 260 - debits 60"
    details:
      actual: 700
      expected: 700
      opening_balance: 500
      credits: 260
      debits: 60
  - check_name: transcript_enumerates_credit_transactions
    status: passed
    score: 1.0
    message: "all credit transaction ids enumerated: ['T-1', 'T-3', 'T-5']"
    details: null
```

## `trials/{task_id}/{trial_index}/judge_trajectory.yaml`

The rubric judge's own message transcript — written **only** when an LLM
judge ran and captured one (absent file ⇒ no judge transcript for this
trial). Kept out of `grade.yaml` for the same reason agent prompts live
in `prompts.yaml`: the transcript is often kilobytes of read-tool calls
and inspection, and a reviewer opening `grade.yaml` wants the verdict,
not the judge's working.

```yaml
messages:
  - role: system
    content: "You are a strict, evidence-based grading judge..."
  - role: user
    content: "The agent under evaluation operated under this policy..."
  - role: assistant
    content: ""
    tool_calls:
      - id: call_1
        name: get_db_state
        arguments: {tables: ["orders"]}
  - role: tool
    content: "{...}"
    tool_call_id: call_1
  - role: assistant
    content: ""
    tool_calls:
      - id: call_2
        name: submit_report
        arguments: {refund_amount: true, ...}
```

This is the **audit / reproducibility channel** for the judge. The judge
loop's tool-call ordering is non-deterministic even at `temperature=0`
(plan open question #2), so the recorded transcript — not a re-run — is
the source of truth for *what the judge saw and did*. For an `errored`
judge it carries the partial transcript up to the failure, which is the
most useful debugging artifact.

## `trials/{task_id}/{trial_index}/judge_inputs.yaml`

The rubric judge's non-derivable `run()` inputs — written **only** when an
LLM judge ran (absent file ⇒ no judge inputs for this trial). This is the
record an offline **judge replay** reads to re-execute the judge over the
recorded trajectory without live services: everything else the judge
consumed is already structured elsewhere (the transcript in
`trajectory.yaml`, the agent policy in `prompts.yaml`, the rubric + judge
model in `task.yaml`), so this file carries only what a replay cannot
otherwise reconstruct. Kept out of `grade.yaml` — the state-diff string can
be large — for the same reason the transcript lives in its own sidecar.

```yaml
state_diff_text: |
  orders[o1]: status open -> shipped
  order_items[i1]: qty 1 -> 2
read_tools_offered:
  - get_db_state
  - query_db
```

* `state_diff_text` — the exact `initial → final` state-delta string the
  judge was handed as its primary outcome view (the agent's own edits, not
  the trial-vs-golden diff, so it reveals nothing about the expected
  answer). `null` when no diff was built (a non-DB task, or a DB read hiccup
  degraded grading to no diff). A replay rebuilds the judge's opening
  message from this exact string.
* `read_tools_offered` — the non-KB read-only tools the judge was actually
  offered this trial: `get_db_state` / `query_db` (a DB reader was supplied)
  and `read_file` (a workspace existed). The KB read surface lives in
  `grade.yaml` `judge_kb_gating`. A replay declares which of these live
  backends it must shim offline.

## `trials/{task_id}/{trial_index}/logs.yaml`

```yaml
trial_id: "051fa6cb-...:0"
total_logs: 45
logs:
  - timestamp: "2026-01-01T12:00:00Z"
    level: "INFO"
    module: "runner"
    message: "Starting trial execution"
    context:
      task_id: "..."
      trial_index: 0
  - timestamp: "2026-01-01T12:00:01Z"
    level: "ERROR"
    module: "grading"
    message: "State hash mismatch in golden set grading"
    context:
      expected: "1d57efd98..."
      actual: "7d10f0521..."
```

Structured trial-level logs emitted by
[`StructuredLogger`](../tolokaforge/core/logging.py). One object per
log call; `context` carries arbitrary key/value pairs the call site
attached.

## `replays/{replay_id}/`

Written by `tolokaforge rejudge` (see [`docs/JUDGE_REPLAY.md`](JUDGE_REPLAY.md)) —
an **additive** subtree under the source run dir. Originals are never modified;
each replay lands in its own `replays/{replay_id}/` directory. Per re-judged
trial, mirroring the source path, replay writes a `grade.yaml`,
`judge_trajectory.yaml`, and `judge_inputs.yaml` (the same formats documented
above, so a replay bundle is itself replayable) plus a `replay_provenance.yaml`.
The batch also writes one `replays/{replay_id}/replay_report.yaml`.

### `replay_provenance.yaml`

How one trial's replay inputs were resolved:

```yaml
judge_model: openrouter/openai/gpt-4.1-mini
judge_model_source: override        # or "recorded"
rubric_source: recorded             # or "override"
knowledge_search_mode: recorded     # recorded | on | off
knowledge_search_disabled: false
custom_system_prompt: false         # whether a custom judge prompt was in effect
custom_prompt_source: null          # "recorded" | "override" | null (default prompt)
include_agent_system_prompt: true   # whether the agent policy was embedded in the judge's evidence
agent_prompt_source: null           # "recorded" | "override" | null (defaulted to include)
fidelity_mode: full                 # "full" (state_diff rebuilt) or "fallback" (old bundle, no state_diff)
```

`custom_system_prompt` / `custom_prompt_source` are resolved independently of the
rubric: a `--grading` override replaces the prompt only when it carries its own
`llm_judge.customization.system_prompt`, so a rubric-only override over a
custom-prompted bundle reads `rubric_source: override` while
`custom_prompt_source: recorded`. See [`docs/JUDGE_REPLAY.md`](JUDGE_REPLAY.md#custom-judge-system-prompt).

`include_agent_system_prompt` / `agent_prompt_source` follow the same independent
resolution for the agent-policy gating: a `--grading` override flips the gating only
when it carries its own `llm_judge.customization.include_agent_system_prompt`, and
`agent_prompt_source` is `null` exactly when the gating defaulted to include (no
recorded value, no override). See
[`docs/JUDGE_REPLAY.md`](JUDGE_REPLAY.md#agent-policy-evidence-gating).

### `replay_report.yaml`

The per-run comparison of the replay against the recorded originals:

```yaml
replay_id: replay_20260718_010425
judge_model: openrouter/openai/gpt-4.1-mini
criteria_compared: 2            # denominator: criteria in COMPARABLE trials only
criteria_agreed: 1
agreement_rate: 0.5             # null when nothing was comparable
aggregate_original_llm_judge: 1.0
aggregate_replay_llm_judge: 0.5
aggregate_llm_judge_delta: -0.5
replay_usage:                   # judge-only spend, summed across replayed trials
  calls: 5
  prompt_tokens: 500
  completion_tokens: 100
  reasoning_tokens: 0
  cost_usd: 0.05
carried_components: "Non-judge grade components ... are carried ..., not recomputed by replay."
trials:
  - bundle: trials/refund_task/0
    bucket: comparable          # comparable | original_errored | original_no_verdict | replay_errored
    original_llm_judge: 1.0
    replay_llm_judge: 1.0
    llm_judge_delta: 0.0
    criteria:
      - id: refund_amount
        original_met: true
        original_score: 1.0
        replay_met: true
        replay_score: 1.0
        met_agrees: true
        score_delta: 0.0
```

* **`agreement_rate`** is the fraction of criteria whose `met` matches, computed
  **only** over `comparable` trials (both the recorded and the replay judge
  produced per-criterion verdicts). Trials in the `original_errored`,
  `original_no_verdict`, or `replay_errored` buckets carry the available side's
  result but are **excluded from the denominator** — a broken grader is never
  counted as a disagreement, and a replay error is never a fabricated `0`.
* **Non-judge components are carried, not recomputed** — the deterministic
  state/transcript/db-probe components stay as recorded; replay only re-runs the
  `llm_judge` component (the aggregate deltas are over that component).
* Not-applicable (non-judge) trials never enter the report.

## Reading Output Files

```python
from pathlib import Path
import json
import yaml


def load_trial(trial_dir: Path) -> dict:
    """Load every per-trial artifact in a single bundle. The bundle is
    self-contained — no cross-trial sidecar lookup required."""
    data = {}
    for name in (
        "task",
        "trajectory",
        "env",
        "metrics",
        "grade",
        "judge_trajectory",
        "judge_inputs",
        "logs",
        "prompts",
        "tools_schemas",
    ):
        path = trial_dir / f"{name}.yaml"
        if path.exists():
            data[name] = yaml.safe_load(path.read_text())
    return data


def analyze_failures(output_dir: Path):
    """Find and analyze failed trials."""
    for grade_file in output_dir.glob("trials/*/*/grade.yaml"):
        grade = yaml.safe_load(grade_file.read_text())
        if grade["binary_pass"]:
            continue
        task_id = grade_file.parent.parent.name
        trial_idx = grade_file.parent.name
        print(f"Failed: {task_id} trial={trial_idx} score={grade['score']:.2f}")
        if (diff := grade.get("state_diff")) and diff["has_diff"]:
            print(diff["diff"])
```

## CLI Flags

### Verbose Mode
Enable DEBUG level logging:

```bash
tolokaforge run --config config.yaml --verbose
```

### Strict Mode
Raise errors immediately on ERROR level logs:

```bash
tolokaforge run --config config.yaml --strict
```

First ERROR log raises `RuntimeError` and stops execution.

## Schema Version Stamps

| File | Field | Current value | Bumped on |
|---|---|---|---|
| `trajectory.yaml` | `simulator_schema_version` | `1` | Any revision to the LLM user-simulator prompt body |
| `metrics.yaml` (`usage` block) | — (struct-typed) | n/a | Usage fields grow; removal breaks downstream analytics |
| `task.yaml.model_config.*.resolved` | — (struct-typed) | n/a | Policy registry grows; removing a slot is a breaking change |
| `prompts.yaml` | — | n/a | Two-key mapping; field names match the legacy `Trajectory.system_prompt` / `Trajectory.user_system_prompt` |
| `tools_schemas.yaml` | — | n/a | Format is the litellm tool-schema dict list, post-`schema_sanitizer` |

There is no global version stamp — each subsystem stamps independently so
changes localise.

## See Also

- [LLM_LAYER.md](LLM_LAYER.md) — LLM capability policy layer
- [LOGGING.md](LOGGING.md) — Structured logging system
- [tests/README.md](../tests/README.md) — Test suite documentation
- [REFERENCE.md](REFERENCE.md) — Technical reference
