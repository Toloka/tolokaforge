"""``tolokaforge grade-run`` — an empty ``trials/`` subtree exits 0.

An empty-set batch is a legitimate answer, not an error. The census
census reads ``discovered 0`` so the operator can distinguish this from
"every trial succeeded" without reading further.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.unit


def test_grade_run_over_empty_trials_subtree_exits_zero(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    (run_dir / "trials").mkdir(parents=True)
    out_dir = tmp_path / "regrades"

    result = CliRunner(mix_stderr=False).invoke(
        cli,
        [
            "grade-run",
            str(run_dir),
            "--with-kind",
            "composite",
            "--out",
            str(out_dir),
        ],
    )

    assert result.exit_code == 0, (result.stderr, result.stdout)
    combined = result.stderr + result.stdout
    assert "discovered 0" in combined
    assert out_dir.exists()
    assert list(out_dir.iterdir()) == []
