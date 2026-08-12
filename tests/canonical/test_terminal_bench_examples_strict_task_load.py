"""Every bundled terminal-bench example opts in to ``strict_task_load``.

Terminal-bench adapters synthesise their environment from the task's
``docker-compose.yaml`` — a task-pack that fails to load is a config error
the operator must see, not a task silently dropped from the run.  The
assertion runs over a glob so a newly added example config cannot ship
without the opt-in and slip past review.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES_DIR = _REPO_ROOT / "examples" / "terminal_bench"


def _example_run_configs() -> list[Path]:
    return sorted(_EXAMPLES_DIR.glob("*.yaml"))


def test_terminal_bench_examples_dir_has_run_configs() -> None:
    """Guard the glob: an empty match set would pass every parametrised case."""
    configs = _example_run_configs()
    assert configs, f"no run configs found under {_EXAMPLES_DIR}"


@pytest.mark.parametrize(
    "config_path",
    _example_run_configs(),
    ids=lambda path: path.name,
)
def test_terminal_bench_example_opts_in_to_strict_task_load(config_path: Path) -> None:
    payload = yaml.safe_load(config_path.read_text())
    orchestrator = payload.get("orchestrator", {})
    assert orchestrator.get("strict_task_load") is True, (
        f"{config_path.relative_to(_REPO_ROOT)} must declare "
        "`orchestrator.strict_task_load: true` — terminal-bench runs refuse "
        "to start on a failed task load rather than silently dropping it."
    )
