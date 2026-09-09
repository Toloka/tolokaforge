"""``tolokaforge grade`` CLI wire — happy path + argument-error paths.

Five cases exercise the verb end-to-end via :class:`CliRunner`:

* A — a fixture kind (``_FixedScoreKind``) round-trips through
  URI resolve -> substrate -> ``load_grader_kind`` -> ``evaluate`` ->
  ``grade.json``, and the parsed :class:`Grade` equals the canned value.
* B — a fixture kind (``_RefusingTestKind``) raises
  :class:`GraderKindRefusedError`; the CLI prints an actionable message
  and exits 1 without writing ``grade.json``.
* C — unknown ``--grader-kind`` triggers :class:`click.BadParameter`
  (exit 2) and stderr names the registered set.
* D — a malformed positional URI triggers :class:`click.BadParameter`
  (exit 2) with "not a bundle URI" in the message.
* E — pre-existing content under ``--out`` triggers the runtime
  empty-dir guard (:class:`click.ClickException`, exit 1). The
  pre-existing file is untouched and ``grade.json`` is not written.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from tests.unit.dx.cli.conftest import _FixedScoreKind, _RefusingTestKind, canned_grade
from tolokaforge.core.models.grade import Grade
from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


def _read_grade(path: Path) -> Grade:
    return Grade.model_validate_json(path.read_text(encoding="utf-8"))


def _store_yaml(store_root: Path, tmp_path: Path) -> Path:
    text = f"type: local_disk\nroot_dir: {store_root}\n"
    path = tmp_path / "store.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_grade_verb_writes_grade_json_from_fixture_kind(
    runner: CliRunner,
    tmp_path: Path,
    stored_bundle: tuple[str, Path],
    register_grader_kind: Callable[[str, type], None],
) -> None:
    uri, store_root = stored_bundle
    register_grader_kind(_FixedScoreKind.NAME, _FixedScoreKind)
    out_dir = tmp_path / "out"
    store_config = _store_yaml(store_root, tmp_path)

    result = runner.invoke(
        cli,
        [
            "grade",
            uri,
            "--grader-kind",
            _FixedScoreKind.NAME,
            "--store-config",
            str(store_config),
            "--out",
            str(out_dir),
        ],
    )

    assert result.exit_code == 0, (result.stderr, result.stdout)
    grade_path = out_dir / "grade.json"
    assert grade_path.exists()
    assert _read_grade(grade_path) == canned_grade()


def test_grade_verb_reports_refusal_and_exits_one(
    runner: CliRunner,
    tmp_path: Path,
    stored_bundle: tuple[str, Path],
    register_grader_kind: Callable[[str, type], None],
) -> None:
    uri, store_root = stored_bundle
    register_grader_kind(_RefusingTestKind.NAME, _RefusingTestKind)
    out_dir = tmp_path / "out"

    result = runner.invoke(
        cli,
        [
            "grade",
            uri,
            "--grader-kind",
            _RefusingTestKind.NAME,
            "--store-config",
            str(_store_yaml(store_root, tmp_path)),
            "--out",
            str(out_dir),
        ],
    )

    assert result.exit_code == 1, result.stderr
    combined = result.stderr + result.stdout
    assert "refused" in combined
    assert "no exec tool" in combined
    assert not (out_dir / "grade.json").exists()


def test_grade_verb_refuses_unknown_kind_at_arg_time(
    runner: CliRunner,
    tmp_path: Path,
    stored_bundle: tuple[str, Path],
) -> None:
    uri, store_root = stored_bundle
    out_dir = tmp_path / "out"

    result = runner.invoke(
        cli,
        [
            "grade",
            uri,
            "--grader-kind",
            "nonexistent_kind",
            "--store-config",
            str(_store_yaml(store_root, tmp_path)),
            "--out",
            str(out_dir),
        ],
    )

    assert result.exit_code == 2, result.stderr
    assert "nonexistent_kind" in result.stderr
    assert "Registered" in result.stderr or "grader_kinds" in result.stderr


def test_grade_verb_refuses_malformed_uri(
    runner: CliRunner,
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "out"

    result = runner.invoke(
        cli,
        [
            "grade",
            "not-a-uri",
            "--grader-kind",
            "composite",
            "--out",
            str(out_dir),
        ],
    )

    assert result.exit_code == 2, result.stderr
    assert "not a bundle URI" in result.stderr


def test_grade_verb_refuses_non_empty_out_dir(
    runner: CliRunner,
    tmp_path: Path,
    stored_bundle: tuple[str, Path],
    register_grader_kind: Callable[[str, type], None],
) -> None:
    uri, store_root = stored_bundle
    register_grader_kind(_FixedScoreKind.NAME, _FixedScoreKind)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    marker = out_dir / "existing.txt"
    marker.write_text("do not touch\n", encoding="utf-8")

    result = runner.invoke(
        cli,
        [
            "grade",
            uri,
            "--grader-kind",
            _FixedScoreKind.NAME,
            "--store-config",
            str(_store_yaml(store_root, tmp_path)),
            "--out",
            str(out_dir),
        ],
    )

    assert result.exit_code == 1, result.stderr
    assert "not empty" in result.stderr
    assert marker.read_text(encoding="utf-8") == "do not touch\n"
    assert not (out_dir / "grade.json").exists()
