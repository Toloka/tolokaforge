"""Drive one trial through the ``tolokaforge run-trial`` subprocess contract.

Spawns ``tolokaforge run-trial``, sends one JSON-Lines ``start`` message built
from a task in the bundled ``examples/native/tool_use`` pack, reads the single
``result`` / ``error`` line, and prints the grade — the language-agnostic
subprocess counterpart to ``examples/library/run_trial.py``.

Needs an LLM key in ``.env`` (like every real run) and a live runner
(``make docker-up``). Then, from the repo root::

    uv run python examples/run-trial/drive_run_trial.py
"""

import json
import os
import subprocess
import sys
from pathlib import Path

# The public task-file loader is deferred to #547; until it lands, obtaining a
# TaskConfig from disk goes through the adapter's loader — the honest current
# path a downstream harness would use.
from tolokaforge.adapters._task_loader import load_task_yaml
from tolokaforge.secrets import init_default

# LLM provider keys the spawned trial may need. The subprocess reads .env
# relative to its own working directory (the task-pack root, below), so we
# export the parent's resolved keys into the environment we hand it.
_LLM_KEYS = [
    "OPENROUTER_API_KEY",
    "OPENROUTER_API_KEYS",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
]

_TASK_YAML = (
    Path(__file__).resolve().parents[1]
    / "native/tool_use/dataset/tasks/tool_use/tool_use_public_example_01/task.yaml"
)


def main() -> None:
    # Bootstrap the SecretManager from the repo-root .env and export the keys so
    # the spawned subprocess (whose cwd is the task-pack root) inherits them.
    init_default().export_to_environ(_LLM_KEYS)

    task, task_dir = load_task_yaml(_TASK_YAML)

    start = {
        "v": 1,
        "type": "start",
        "task": task.model_dump(mode="json"),
        "models": {
            "agent": {
                "provider": "openrouter",
                "name": "anthropic/claude-sonnet-4.6",
                "temperature": 0.0,
            },
        },
        "runtime": "shared",
        "conductor": "in_process",
    }

    # A wire task carries no source_dir, so its file assets resolve against the
    # subprocess working directory — spawn at the task-pack root.
    proc = subprocess.run(
        [sys.executable, "-m", "tolokaforge.cli.main", "run-trial"],
        input=json.dumps(start) + "\n",
        cwd=str(task_dir),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )

    line = next((line for line in proc.stdout.splitlines() if line.strip()), None)
    if line is None:
        print(f"run-trial produced no wire output (exit {proc.returncode}):\n{proc.stderr}")
        raise SystemExit(1)

    message = json.loads(line)
    if message["type"] == "error":
        print(f"error [{message['error_type']}]: {message['message']}")
        raise SystemExit(proc.returncode or 1)

    print(message["result"]["trajectory"]["grade"])


if __name__ == "__main__":
    main()
