# Python API Overview

Key classes and entry points for programmatic usage.

## run_trial

Run one trial in-process and get its `TrialResult` back, without
constructing the orchestrator or its batch lifecycle (queue, run-state,
worker pool, budget, resume).

```python
from tolokaforge.runner import run_trial

result = run_trial(
    task=task,                          # a TaskConfig
    models={"agent": {"provider": "openrouter", "name": "anthropic/claude-sonnet-4.6"}},
    runtime="auto",                     # or a registered backend name
    grader="runner_rpc",
    conductor="in_process",
    output_dir=None,                    # None → no disk artifacts
    trial_index=0,
)
print(result.trajectory.grade)
```

Keyword-only signature:

```python
def run_trial(
    *,
    task: TaskConfig,
    models: dict[str, ModelConfig | dict[str, Any]],
    runtime: str = "auto",
    grader: str = "runner_rpc",
    conductor: str = "in_process",
    output_dir: Path | str | None = None,
    trial_index: int = 0,
) -> TrialResult: ...
```

- **`models`** — a role → model map. `agent` is required; `user` and
  `judge` are optional (`user` defaults to the engine's default user
  model). Values are `ModelConfig` instances or plain dicts.
- **`runtime="auto"`** — resolves to `per_trial` when the task's
  environment manifest requires per-trial isolation, else `shared`,
  mirroring the CLI's task-driven default. `auto` is a reserved value;
  any other value is a registered runtime-backend name.
- **`runtime` / `grader` / `conductor`** — resolved by name through the
  entry-point registries (`tolokaforge.runtime_backends`,
  `tolokaforge.trial_graders`, `tolokaforge.conductors`).
- **`output_dir=None`** — writes no artifacts to disk; pass a directory
  to persist them.

Errors:

- **`pydantic.ValidationError`** — `models` is missing `agent` or carries
  a malformed / unexpected value, or the composed run config is invalid.
- **`UnknownImplementationError`** (`tolokaforge.core.plugin_registry`) —
  a `runtime` / `grader` / `conductor` name is not registered; the
  message lists the known names.
- **`ProvisionError`** (`tolokaforge.core.runtime`) — the substrate
  failed to provision. Raised, not swallowed.

## tolokaforge run-trial

`tolokaforge run-trial` runs a single trial as a subprocess a harness in any
language drives over a pipe — the subprocess wrapper of `run_trial`. It reads
one request on stdin, runs one trial, and writes one terminal message on
stdout.

**Framing.** UTF-8 JSON Lines: one JSON object per line, `\n`-delimited, on
both stdin and stdout. Every message carries `"v":1` — the wire-protocol
version, independent of the tolokaforge package version. stdout is flushed
after the message and carries *only* the wire; diagnostics and tracebacks go to
stderr.

**stdin — one message, then EOF:**

```json
{"v":1,"type":"start","task":{},"models":{"agent":{}},"runtime":"auto","grader":"runner_rpc","conductor":"in_process"}
{"v":1,"type":"cancel"}
```

- `task` and `models` mirror the `run_trial` arguments (`models.agent` is
  required).
- `runtime` / `grader` / `conductor` are registered implementation names
  (`tolokaforge.runtime_backends` / `tolokaforge.trial_graders` /
  `tolokaforge.conductors`); omitted fields default to
  `"auto"` / `"runner_rpc"` / `"in_process"`.
- A `cancel` sent as the first line acknowledges and exits without running a
  trial.

**stdout — exactly one terminal message:**

```json
{"v":1,"type":"result","result":{}}
{"v":1,"type":"error","error_type":"ProvisionError","message":"stack failed to become ready","fatal":true}
```

- `result` is `TrialResult.model_dump(mode="json")`.
- `error` carries the named `error_type`, a human-readable `message`, and
  `fatal:true` (a single-trial invocation has no non-terminal errors).

**Error types and exit codes.** Every error path exits `1`; the `error_type`
carries the discriminating detail.

| Outcome | `error_type` | exit |
|---|---|---|
| success (`result` emitted) | — | 0 |
| malformed JSON / bad `v` / unknown `type` | `ProtocolError` | 1 |
| `cancel` message / premature stdin EOF / SIGTERM / SIGINT | `cancelled` | 1 |
| invalid `task` or `models` | `ValidationError` | 1 |
| unknown `runtime` / `grader` / `conductor` name | `UnknownImplementationError` | 1 |
| substrate provisioning failure | `ProvisionError` | 1 |
| any other failure | `InternalError` | 1 |

**Signals (POSIX).** SIGTERM and SIGINT trigger clean teardown (via
`run_trial`'s provisioning `finally` brackets) followed by a `cancelled` error
and a non-zero exit.

**Working directory.** File assets on the task (`grading.yaml`,
`initial_state.json`, tools) resolve against the subprocess working directory,
because a task crossing the wire carries no source directory. Spawn the
subprocess with `cwd` at the task-pack root, or send a fully-inline task that
references no on-disk files.

See `docs/adr/0019-runtime-independence.md` § Surface 3 for the full contract.

## Orchestrator

```python
from tolokaforge.core.orchestrator import Orchestrator

orchestrator = Orchestrator(config, output_dir="results")
results = orchestrator.run()
```

## TrialRunner

```python
from tolokaforge.core.runner import TrialRunner

runner = TrialRunner(
    task_id="task_id",
    trial_index=0,
    agent_client=agent_client,
    user_simulator=user_simulator,
    tool_executor=tool_executor,
    tool_schemas=tool_schemas,
    max_turns=50,
    turn_timeout_s=60,
    episode_timeout_s=1200,
)
trajectory = runner.run(system_prompt, initial_message)
```

## LLMClient

```python
from tolokaforge.core.model_client import LLMClient

client = LLMClient(model_config)
result = client.generate(system="...", messages=[...])
```

## ModelCapabilities

```python
from tolokaforge.core.model_policies import ModelCapabilities

caps = ModelCapabilities.for_model(name="openai/gpt-5.4", provider="openai")
# Returns resolved capabilities with schema/prompt policies
```

`tolokaforge.core.model_policies` — Model capability policies (Strategy Pattern) and YAML preset loader. Presets are defined in `tolokaforge/core/data/model_presets.yaml`. Key public symbols:

- `ModelCapabilities` — resolved capability set for a model (schema/prompt policies, feature flags)
- `DictMapParam` — dataclass describing a detected dict-map parameter (tool name, param name, value schema)
- `detect_dict_maps(tools)` — shared utility that scans tool definitions for `additionalProperties`-based dict-map parameters; used by both `StrictSchema` and `DictMapHints` policies
- `StrictSchema` — schema policy that rewrites tool schemas for strict-mode models (GPT-5)
- `DictMapHints` — prompt policy that appends system-prompt hints for dict-map parameters

## CLI

```bash
uv run tolokaforge run --config examples/native/coding/run_configs/dev.yaml
uv run tolokaforge validate --tasks "tasks/**/task.yaml"
uv run tolokaforge analyze --trajectory results/.../trajectory.yaml
```

> **Note:** Task packs live outside the engine tree. Point `task_packs`
> in your config at any directory containing tasks, or place them in
> `tasks/`. See `examples/` for the expected layout.

See `docs/REFERENCE.md` for schemas and tool definitions.
