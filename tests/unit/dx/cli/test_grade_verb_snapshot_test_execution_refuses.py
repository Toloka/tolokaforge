"""``test_execution`` kind + snapshot substrate = actionable refusal, not a silent 0.0.

Bundle format v1.0 carries no test-suite hook: any pack whose grading
config declares ``grading_method: test_execution`` fails
:meth:`SnapshotGradingSubstrate.run_test_suite` with
:class:`SubstrateUnreachableError` naming "cannot run a test suite
offline". The CLI catches that class, prints the reason, and exits 1 —
this test locks the "known limitation" contract so a future regression
that silently scores 0.0 is caught at CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


def _store_yaml(store_root: Path, tmp_path: Path) -> Path:
    text = f"type: local_disk\nroot_dir: {store_root}\n"
    path = tmp_path / "store.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_test_execution_kind_refuses_on_snapshot_substrate(
    runner: CliRunner,
    tmp_path: Path,
    test_execution_bundle: tuple[str, Path],
) -> None:
    uri, store_root = test_execution_bundle
    out_dir = tmp_path / "out"

    result = runner.invoke(
        cli,
        [
            "grade",
            uri,
            "--grader-kind",
            "test_execution",
            "--store-config",
            str(_store_yaml(store_root, tmp_path)),
            "--out",
            str(out_dir),
        ],
    )

    assert result.exit_code == 1, result.stderr
    combined = result.stderr + result.stdout
    assert "cannot run a test suite offline" in combined
    assert not (out_dir / "grade.json").exists()
