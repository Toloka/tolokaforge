"""Run one trial through the ``tolokaforge.runner.run_trial`` library API.

Obtains a ``TaskConfig`` from the bundled ``examples/native/tool_use`` pack,
runs a single trial in-process, and prints the resulting grade.

Needs an LLM key in ``.env`` (like every real run) and a live runner
(``make docker-up``). Then, from the repo root::

    uv run python examples/library/run_trial.py
"""

from pathlib import Path

from tolokaforge.runner import load_task, run_trial
from tolokaforge.secrets import init_default

_TASK_YAML = (
    Path(__file__).resolve().parents[1]
    / "native/tool_use/dataset/tasks/tool_use/tool_use_public_example_01/task.yaml"
)


def main() -> None:
    # Bootstrap the SecretManager (reads .env) so LLMClient can find the key —
    # the same bootstrap the CLI runs at startup.
    init_default()

    task = load_task(_TASK_YAML)

    result = run_trial(
        task=task,
        models={
            "agent": {
                "provider": "openrouter",
                "name": "anthropic/claude-sonnet-4.6",
                "temperature": 0.0,
            },
        },
    )

    print(result.trajectory.grade)


if __name__ == "__main__":
    main()
