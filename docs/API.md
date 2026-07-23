# Python API Overview

Key classes and entry points for programmatic usage.

## Orchestrator

```python
from tolokaforge.core.orchestrator import Orchestrator

orchestrator = Orchestrator(config, output_dir="results")
run_dir = orchestrator.run()  # returns the resolved Path of the timestamped run dir
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
