# Python API Overview

Key classes and entry points for programmatic usage.

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

## CLI

```bash
uv run tolokaforge run --config examples/browser_task/run_config.yaml
uv run tolokaforge validate --tasks "examples/**/task.yaml"
uv run tolokaforge analyze --trajectory results/.../trajectory.yaml
```

See `docs/REFERENCE.md` for schemas and tool definitions.
