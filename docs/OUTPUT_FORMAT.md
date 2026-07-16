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
└── trials/
    └── {task_id}/
        └── {trial_index}/
            ├── task.yaml
            ├── trajectory.yaml             ← message trace + status + metrics
            ├── env.yaml
            ├── metrics.yaml
            ├── grade.yaml
            ├── judge_trajectory.yaml       ← rubric-judge transcript (only when an LLM judge ran)
            ├── logs.yaml
            ├── prompts.yaml                ← agent + user-sim system prompts
            ├── tools_schemas.yaml          ← post-policy tool list
            └── services/                   ← per-service compose logs (on trial-body or graded failure)
                ├── {service}.log
                └── _capture.yaml           ← manifest (provision-failure path only)
```

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

The flat `tokens_input` / `tokens_output` pair was removed in Stage 5
(P7) — `usage` is now a nested block that carries the full
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
```

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

## `trials/{task_id}/{trial_index}/services/`

Per-service compose logs, written on a trial-body or graded failure and only on
the per-trial runtime backend (see
[`docs/architecture/RUNTIME_BACKENDS.md`](architecture/RUNTIME_BACKENDS.md:1)
§ "Per-service log capture on failure"). The shared-stack backend never writes
this directory.

* **`{service}.log`** — raw `docker compose logs --tail=N` bytes for one
  service (`N` = `compute.log_tail`, default 500). One file per compose
  service that produced output. No parsing, no format changes.
* **`_capture.yaml`** — manifest written **only on the provision-failure
  path** (compose-up or reset-recipe failure), where no `metrics.yaml` exists
  to amend:

  ```yaml
  tail: 500
  capture_reason: provision_error
  services:
    db:
      bytes: 4096
  ```

  On the trial-body path the durable record is the
  [`metrics.yaml` `captured_service_logs`](#captured_service_logs--on-trial-body-or-graded-failure)
  field instead, so the two surfaces never both write a record.

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
```

Score scale: `0.0` ≤ `score` ≤ `1.0`. `binary_pass` is the harness-level
pass/fail; `score` is a fractional pass rate (used for tasks with partial
credit).

### Rubric-judge fields

`criterion_results`, `judge_status`, and `judge_usage` are populated only
when an LLM rubric judge ran (`grading.llm_judge` configured). See
[`docs/GRADING.md`](GRADING.md) for the rubric mechanism and the two
weighting layers.

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
