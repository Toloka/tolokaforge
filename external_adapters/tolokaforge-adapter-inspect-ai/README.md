# tolokaforge-adapter-inspect-ai

A tolokaforge adapter for [Inspect AI](https://inspect.aisi.org.uk/) tasks.

## How it works

Inspect tasks have no portable, static format — an Inspect task is Python that runs
on Inspect's own runtime (its solvers, scorers and sandbox). So this adapter does
**not** reimplement Inspect; it *delegates* to it:

1. **Discover** — enumerate the `@task` functions in a task pack via Inspect's
   `list_tasks`, and translate each into a tolokaforge `TaskConfig`.
2. **Execute** — hand the task to Inspect (`inspect eval`), which runs its own solver
   and scorer against the model under test.
3. **Normalize** — read the resulting `.eval` log back into tolokaforge's `Grade` and
   `Trajectory` (`normalize.py`), so reporting and analysis are unchanged.

Model routing goes through an OpenAI-compatible / LiteLLM-proxy endpoint configured by
the run. Secrets (keys, base URLs) are resolved via `tolokaforge.secrets` when wiring
the eval environment — never read from the process environment by this package.

## Usage

Discover and translate Inspect tasks through the adapter, then execute a task with the
bridge and project the result:

```python
from tolokaforge_adapter_inspect_ai.adapter import InspectAiAdapter
from tolokaforge_adapter_inspect_ai.bridge import run_inspect_eval
from tolokaforge_adapter_inspect_ai import normalize
from inspect_ai.log import read_eval_log

adapter = InspectAiAdapter({"inspect_task_dir": "path/to/inspect/tasks"})
info = adapter._tasks[adapter.get_task_ids()[0]]

result = run_inspect_eval(
    task_file=info.file,
    task_name=info.name,
    model="litellm-proxy/<model>",   # OpenAI-compatible / LiteLLM-proxy endpoint
    log_dir="runs/inspect",
    env={...},                       # base URL + key, resolved via tolokaforge.secrets
)
grade = normalize.run_grade(read_eval_log(str(result.log_path)))
```

## Scope

The adapter discovers and translates Inspect tasks, and executes them through the
local subprocess bridge (`bridge.py` → `inspect eval` → `normalize`), exercised
end-to-end with the offline `mockllm` provider at $0.

Execution through the tolokaforge runner is not wired: `to_task_description` and
`grade` raise `NotImplementedError`. Running an Inspect task therefore goes through
`bridge.run_inspect_eval` + `normalize`, not `tolokaforge run`.

## Development

```bash
uv sync
uv run pytest external_adapters/tolokaforge-adapter-inspect-ai/tests -v
```
