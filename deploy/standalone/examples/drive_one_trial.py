"""Drive one real trial through the standalone composed runner stack.

Run this from ``deploy/standalone/examples/`` once the four-service stack is up
(``docker compose up -d --wait`` in ``deploy/standalone/``). It serialises a
bundled task pack with the public :func:`tolokaforge.runner.load_task`, copies
the pack's file assets into the runner container (a wire task carries no source
directory, so its relative rubric/fixture files must be present in the
container), and drives ``tolokaforge run-trial`` over the JSON-Lines exec wire.
On success it prints the graded ``TrialResult``'s grade; on a typed trial error
it prints the error and exits non-zero.

Host prerequisites: ``pip install tolokaforge`` (for ``load_task`` +
``model_dump``), Docker with the compose plugin, a running standalone stack, and
a provider key in ``deploy/standalone/.env``. The trial reads that key from the
runner container's environment (compose injects it from ``.env``), so this
driver forwards no secrets of its own. The agent model defaults to OpenRouter's
``anthropic/claude-sonnet-4.6``; override with ``TOLOKAFORGE_EXAMPLE_PROVIDER``
and ``TOLOKAFORGE_EXAMPLE_MODEL`` to match the provider key you set.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from tolokaforge.core.trial import TrialResult
from tolokaforge.runner import load_task

_STANDALONE_DIR = Path(__file__).resolve().parents[1]
_COMPOSE_FILE = _STANDALONE_DIR / "docker-compose.yaml"
_REPO_ROOT = _STANDALONE_DIR.parents[1]
_TASK_YAML = (
    _REPO_ROOT
    / "examples/native/tool_use/dataset/tasks/tool_use/tool_use_public_example_01/task.yaml"
)

# The shared-stack runtime reaches the executor at this address; the wire
# default ``executor:50051`` does not resolve standalone, so the trial is
# pointed at the runner's own gRPC server inside the composed stack.
_EXECUTOR_ADDRESS = "localhost:50051"


def _agent_model() -> dict[str, Any]:
    return {
        "provider": os.environ.get("TOLOKAFORGE_EXAMPLE_PROVIDER", "openrouter"),
        "name": os.environ.get("TOLOKAFORGE_EXAMPLE_MODEL", "anthropic/claude-sonnet-4.6"),
        "temperature": 0.0,
        "max_tokens": 4096,
    }


def _compose(args: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run ``docker compose`` against the standalone recipe from its own directory.

    Invoking from the compose file's directory with no ``-p`` targets the same
    default project a cold user's ``docker compose up`` created.
    """
    return subprocess.run(
        ["docker", "compose", "-f", str(_COMPOSE_FILE), *args],
        cwd=_STANDALONE_DIR,
        capture_output=True,
        text=True,
        input=input_text,
    )


def main() -> int:
    task = load_task(_TASK_YAML)
    source_dir = task.source_dir
    if source_dir is None:
        raise RuntimeError(f"loaded task carries no source_dir: {_TASK_YAML}")

    start = {
        "v": 1,
        "type": "start",
        "task": task.model_dump(mode="json"),
        "models": {"agent": _agent_model()},
        "runtime": "shared",
        "conductor": "in_process",
    }

    copied = _compose(["cp", str(source_dir), "runner:/tmp/"])
    if copied.returncode != 0:
        raise RuntimeError(f"copying the task pack into the runner failed:\n{copied.stderr}")

    proc = _compose(
        [
            "exec",
            "-T",
            "-e",
            f"EXECUTOR_ADDRESS={_EXECUTOR_ADDRESS}",
            "-w",
            f"/tmp/{source_dir.name}",
            "runner",
            "tolokaforge",
            "run-trial",
        ],
        input_text=json.dumps(start) + "\n",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"run-trial exec failed (rc={proc.returncode}):\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )

    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise RuntimeError(f"expected exactly one wire line, got: {proc.stdout!r}")
    envelope = json.loads(lines[0])

    if envelope["type"] == "error":
        print(
            f"trial error [{envelope.get('error_type')}]: {envelope.get('message')}",
            file=sys.stderr,
        )
        return 1
    if envelope["type"] != "result":
        raise RuntimeError(f"unexpected wire envelope type: {envelope!r}")

    result = TrialResult.model_validate(envelope["result"])
    print(result.trajectory.grade)
    return 0


if __name__ == "__main__":
    sys.exit(main())
