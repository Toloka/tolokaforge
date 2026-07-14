"""End-to-end proof that a seed-backed reset recipe fires through
``tolokaforge run``.

Drives the shipped ``examples/native/multi_service_postgres_reset`` pack
as a real subprocess at ``repeats: 2``. The pack labels ``app-db``
``isolation: reset`` bound to the ``postgres_baseline`` seed: the
compose's ``init.sql`` seeds the widget row ``factory_default``, and the
reset recipe (``sql_dump``) overwrites it to ``baseline`` at every
provision. The agent reads the row back over the PostgREST API and writes
it to a submission file; grading's ``state_checks`` glob asserts the file
reads ``baseline``. Both trials passing proves ``_apply_reset_recipes`` →
``sql_dump`` dispatch → ``psql`` applied the seed on top of the compose
default, once per trial.

**Gated.** Requires a real Docker daemon (the per-trial backend brings up
a fresh compose stack) and a real LLM provider key (the agent must emit
real tool calls; the ``mock`` provider emits none). ``requires_api``
auto-skips without a key (see ``tests/conftest.py``); the ``docker`` skip
below covers a missing daemon.

**Cost.** Two trials of one single-GET-plus-write task (max 8 turns) on
``anthropic/claude-haiku-4-5`` via OpenRouter. Cents.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.utils.docker_helpers import is_docker_daemon_available

pytestmark = [
    pytest.mark.integration,
    pytest.mark.docker,
    pytest.mark.requires_api,
    pytest.mark.llm,
    pytest.mark.slow,
]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACK = "examples/native/multi_service_postgres_reset"
_RUN_CONFIG = f"{_PACK}/run_config.yaml"


def _output_basename() -> str:
    """The configured ``output_dir`` basename the orchestrator suffixes
    with a run timestamp (``<basename>_<YYYYmmdd_HHMMSS>``)."""
    cfg = yaml.safe_load((_REPO_ROOT / _RUN_CONFIG).read_text())
    return Path(cfg["evaluation"]["output_dir"]).name


@pytest.mark.skipif(
    not is_docker_daemon_available(),
    reason="Docker daemon not available (per-trial backend needs it)",
)
def test_reset_recipe_fires_end_to_end_across_two_trials() -> None:
    """Both trials observe the seeded ``baseline`` — the reset recipe
    fired via the CLI on each provision."""
    basename = _output_basename()
    results_root = _REPO_ROOT / "results"
    before = set(results_root.glob(f"{basename}_*")) if results_root.exists() else set()

    proc = subprocess.run(
        ["uv", "run", "tolokaforge", "run", "--config", _RUN_CONFIG],
        cwd=str(_REPO_ROOT),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=1800,
        check=False,
    )
    assert proc.returncode == 0, (
        f"tolokaforge run failed (rc={proc.returncode}):\n"
        f"stdout:\n{proc.stdout[-4000:]}\n"
        f"stderr:\n{proc.stderr[-4000:]}"
    )

    combined = proc.stdout + proc.stderr
    assert "runtime.backend.selected" in combined and "PerTrialRuntimeBackend" in combined, (
        "run did not route onto PerTrialRuntimeBackend — the reset seam never fired.\n"
        f"stderr tail:\n{proc.stderr[-2000:]}"
    )

    after = set(results_root.glob(f"{basename}_*"))
    created = after - before
    assert len(created) == 1, (
        f"expected exactly one new run dir under {results_root} matching "
        f"{basename}_*; got {sorted(created)}"
    )
    run_dir = created.pop()

    try:
        for trial_index in (0, 1):
            grade_path = run_dir / "trials" / "reset_probe" / str(trial_index) / "grade.yaml"
            assert grade_path.exists(), (
                f"missing grade.yaml for trial {trial_index} at {grade_path}.\n"
                f"run dir contents: {sorted(p.name for p in run_dir.rglob('*'))[:50]}"
            )
            grade = yaml.safe_load(grade_path.read_text())
            assert grade["binary_pass"] is True, (
                f"trial {trial_index} did not pass — the recipe likely did not seed "
                f"'baseline' (row would still read init.sql's 'factory_default').\n"
                f"grade: {grade}"
            )
            assert grade["components"]["state_checks"] == 1.0, (
                f"trial {trial_index} state_checks != 1.0; the submission did not "
                f"contain the seeded 'baseline' value.\ngrade: {grade}"
            )
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
