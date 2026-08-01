"""``make validate`` skips a task directory that was never cloned.

``tolokaforge validate`` treats a glob matching nothing as an invocation error,
and ``tasks/`` lives outside the engine tree (AGENTS.md § Testing) — so without
the target's own directory guard, every contributor who has not cloned a task
pack gets a failing ``make validate``. Both branches are driven through ``make``
itself, because the guard is the recipe.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_validate(tasks_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "validate", f"TASKS_DIR={tasks_dir}"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_make_validate_skips_an_absent_task_directory(tmp_path: Path) -> None:
    result = _make_validate(tmp_path / "never-cloned")

    assert result.returncode == 0, result.stderr
    assert "skipped" in result.stdout
    assert "never-cloned" in result.stdout


def test_make_validate_fails_on_an_invalid_task_in_a_present_directory(tmp_path: Path) -> None:
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "task.yaml").write_text("description: no task_id here\n")

    result = _make_validate(tmp_path)

    assert result.returncode != 0
    assert "0 valid, 1 invalid" in result.stdout + result.stderr
