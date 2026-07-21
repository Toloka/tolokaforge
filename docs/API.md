# Python API Overview

Key classes and entry points for programmatic usage.

## run_trial

Run one trial in-process and get its `TrialResult` back, without
constructing the orchestrator or its batch lifecycle (queue, run-state,
worker pool, budget, resume).

```python
import tolokaforge

result = tolokaforge.run_trial(
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
