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


def test_no_example_puts_adapter_params_at_the_wrong_depth() -> None:
    """Adapter knobs belong under ``evaluation.harness_adapter.params``.

    ``EvaluationConfig`` is ``extra="ignore"``, so an ``adapter_params:`` block
    (the shape of the ``--adapter-params`` CLI flag, not a run-config key) is
    dropped without a word — the run then goes through the engine loop while
    the operator believes a harness is driving it. No schema error can catch
    that, so the shape is pinned here.
    """
    for config_path in _example_run_configs():
        payload = yaml.safe_load(config_path.read_text())
        evaluation = payload.get("evaluation", {})
        assert "adapter_params" not in evaluation, (
            f"{config_path.relative_to(_REPO_ROOT)} declares "
            "`evaluation.adapter_params`, which is not a run-config key and is "
            "silently ignored; nest the knobs under "
            "`evaluation.harness_adapter.params`."
        )


@pytest.mark.parametrize(
    "config_path",
    _example_run_configs(),
    ids=lambda path: path.name,
)
def test_terminal_bench_example_validates_and_keeps_its_adapter_params(
    config_path: Path,
) -> None:
    """Each example survives ``RunConfig`` validation with its params intact.

    The round-trip is what proves the documented depth is the one the schema
    reads: params nested anywhere else vanish here.
    """
    from tolokaforge.core.models import RunConfig

    config = RunConfig(**yaml.safe_load(config_path.read_text()))
    adapter = config.evaluation.harness_adapter
    assert adapter is not None, (
        f"{config_path.relative_to(_REPO_ROOT)} must declare "
        "`evaluation.harness_adapter` — terminal-bench tasks need the adapter."
    )
    assert adapter.type == "terminal_bench"
    assert adapter.params.get("terminal_bench_dir"), (
        f"{config_path.relative_to(_REPO_ROOT)}: `harness_adapter.params` came "
        "back without `terminal_bench_dir` — the params block is at the wrong depth."
    )


def test_a_harness_example_exists_and_is_fully_specified() -> None:
    """At least one example exercises harness mode, with what it requires.

    Without one, ``examples/terminal_bench/`` documents only the engine loop
    and the next operator has to reverse-engineer the shape.
    """
    from tolokaforge_adapter_terminal_bench.harness import ENGINE_LOOP, HARNESSES

    from tolokaforge.core.models import RunConfig

    harness_configs = []
    for config_path in _example_run_configs():
        config = RunConfig(**yaml.safe_load(config_path.read_text()))
        adapter = config.evaluation.harness_adapter
        harness = (adapter.params.get("agent_harness") if adapter else None) or ENGINE_LOOP
        if harness != ENGINE_LOOP:
            harness_configs.append((config_path, config, harness))

    assert harness_configs, (
        "no bundled example runs a coding-harness CLI; add one so the config "
        "shape is documented by something executable"
    )
    for config_path, config, harness in harness_configs:
        rel = config_path.relative_to(_REPO_ROOT)
        params = config.evaluation.harness_adapter.params
        assert harness in HARNESSES, f"{rel}: unknown agent_harness {harness!r}"
        assert params.get("agent_model"), f"{rel}: a harness run must pin `agent_model`"
        # The exec cannot be cut short, so the run budget must cover the
        # harness budget or the adapter refuses the run at trial setup.
        assert config.orchestrator.timeouts.episode_s >= 1800, (
            f"{rel}: `orchestrator.timeouts.episode_s` must be at least the "
            "task's `[agent] timeout_sec`, or the harness trial is refused"
        )
