"""Discover Harbor task directories under a local pack.

Harbor tasks are Terminal-Bench 2.0 tasks: a ``task.toml`` beside an
``environment/`` build context and a ``tests/test.sh`` that writes a reward
to ``/logs/verifier/reward.txt``. That is the exact shape
:func:`tolokaforge_adapter_terminal_bench.task_parser.discover_tasks`
already enumerates, so discovery here is a thin, named delegation rather than
a second parser that could drift from it. Dataset download is a manual
pre-step (see the package README); this function reads an already-present
local pack.
"""

from __future__ import annotations

from pathlib import Path

# Reuse the terminal-bench adapter's environment synthesis + task parsing:
# Harbor tasks are Terminal-Bench 2.0 tasks, so this is a deliberate shared
# surface across the two external adapter packages (approach b, no `harbor run`).
from tolokaforge_adapter_terminal_bench.task_parser import (
    TerminalBenchTask,
    discover_tasks,
)

HarborTask = TerminalBenchTask
"""Harbor and Terminal-Bench 2.0 share one on-disk task shape, so they share
one parsed representation. Re-exported under a Harbor-facing name so callers
in this package read against ``HarborTask`` without reaching into the
terminal-bench package for a type."""


def discover_harbor_tasks(base_dir: Path) -> dict[str, HarborTask]:
    """Harbor tasks under *base_dir*, keyed by task id (the task directory name).

    A task declares itself with ``task.toml`` (or the legacy ``task.yaml``).
    Compose is optional: a single-container task ships ``environment/Dockerfile``
    alone and the compose doc is synthesised from it at materialisation.
    """
    return discover_tasks(base_dir)
