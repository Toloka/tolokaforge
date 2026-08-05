"""``make validate`` skips a task directory that was never cloned — and nothing else.

``tolokaforge validate`` treats a glob matching nothing as an invocation error,
and ``tasks/`` lives outside the engine tree (AGENTS.md § Testing) — so without
the target's own directory guard, every contributor who has not cloned a task
pack gets a failing ``make validate``. A guard reading the directory alone is the
other failure: overriding ``TASKS_GLOB`` to point at a pack elsewhere would then
print a skip about a directory the caller never mentioned and exit ``0`` having
validated nothing. Every branch is driven through ``make`` itself, because the
guard is the recipe.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _make_validate(**overrides: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["make", "validate", *(f"{name}={value}" for name, value in overrides.items())],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _with_an_invalid_task(root: Path) -> Path:
    (root / "broken").mkdir()
    (root / "broken" / "task.yaml").write_text("description: no task_id here\n")
    return root


def test_make_validate_skips_an_absent_task_directory(tmp_path: Path) -> None:
    result = _make_validate(TASKS_DIR=str(tmp_path / "never-cloned"))

    assert result.returncode == 0, result.stderr
    assert "skipped" in result.stdout
    assert "never-cloned" in result.stdout


def test_make_validate_fails_on_an_invalid_task_in_a_present_directory(tmp_path: Path) -> None:
    result = _make_validate(TASKS_DIR=str(_with_an_invalid_task(tmp_path)))

    assert result.returncode != 0
    assert "0 valid, 1 invalid" in result.stdout + result.stderr


def test_make_validate_honours_a_glob_override_with_no_task_directory(tmp_path: Path) -> None:
    """A named glob is a target the caller chose, so the directory guard steps aside.

    Overriding ``TASKS_GLOB`` alone is the documented way to validate a pack that
    lives anywhere (AGENTS.md § Testing). A guard testing ``TASKS_DIR`` instead of
    the glob turns that into a silent no-op: it prints a skip and exits ``0``.
    """
    _with_an_invalid_task(tmp_path)

    result = _make_validate(TASKS_GLOB=str(tmp_path / "**" / "task.yaml"))

    assert "skipped" not in result.stdout
    assert result.returncode != 0
    assert "0 valid, 1 invalid" in result.stdout + result.stderr
