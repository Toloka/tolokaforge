"""Every bundled terminal-bench example is a whole document of its own kind.

Two kinds ship side by side under ``examples/terminal_bench/``: run configs, and
``*_overlay.yaml`` harness-registry fragments an operator points
``harness_presets_file`` at. Each is asserted against its own contract — a run
config opts in to ``strict_task_load`` and survives ``RunConfig`` validation, an
overlay loads as a registry document — so neither a new example missing the
opt-in nor a fragment with a typo can ship past review, and neither is asserted
against the other's shape.

Terminal-bench adapters synthesise their environment from the task's
``docker-compose.yaml``, so a task-pack that fails to load is a config error the
operator must see rather than a task silently dropped from the run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES_DIR = _REPO_ROOT / "examples" / "terminal_bench"
_OVERLAY_SUFFIX = "_overlay.yaml"


def _example_run_configs() -> list[Path]:
    return sorted(p for p in _EXAMPLES_DIR.glob("*.yaml") if not p.name.endswith(_OVERLAY_SUFFIX))


def _example_overlays() -> list[Path]:
    return sorted(_EXAMPLES_DIR.glob(f"*{_OVERLAY_SUFFIX}"))


def test_terminal_bench_examples_dir_has_run_configs() -> None:
    """Guard the glob: an empty match set would pass every parametrised case."""
    configs = _example_run_configs()
    assert configs, f"no run configs found under {_EXAMPLES_DIR}"


@pytest.mark.parametrize(
    "overlay_path",
    _example_overlays(),
    ids=lambda path: path.name,
)
def test_terminal_bench_overlay_is_a_loadable_registry_document(overlay_path: Path) -> None:
    """The naming convention that excuses a file from the run-config assertions
    is itself asserted: a fragment named ``*_overlay.yaml`` must load as a
    harness registry, so a run config given that name fails here on its unknown
    top-level keys instead of quietly escaping both contracts."""
    from tolokaforge_coding_harnesses import load_harness_registry

    assert load_harness_registry(overlay_path), (
        f"{overlay_path.relative_to(_REPO_ROOT)} declares no harness; an "
        "`*_overlay.yaml` example exists to document the overlay shape."
    )


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
    from tolokaforge.core.models import RunConfig
    from tolokaforge_coding_harnesses import ENGINE_LOOP, HARNESSES

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
