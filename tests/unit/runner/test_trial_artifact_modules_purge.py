"""A module imported from a trial's artefacts does not outlive the artefacts (ADR-0056).

The runner imports in-process tool modules, and the grader ``checks.py``, from
the trial's extracted ``tool_artifacts``; Python caches them in ``sys.modules``.
A later trial that pins another version of the same library ships it under a
fresh directory, but an import by the same name would be served the cached
copy. Tearing an artefact directory down — the runner's trial cleanup and
shutdown, the offline grader's ``try/finally`` — purges the modules loaded
from it, and nothing loaded from elsewhere.
"""

from __future__ import annotations

import base64
import importlib
import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.unit.grading.kinds.test_composite_grader_kind import _OfflineV11Substrate
from tolokaforge.core.grading.kinds import CompositeGraderKind
from tolokaforge.core.grading.tool_artifacts import extract_tool_artifacts, purge_modules_under
from tolokaforge.runner.models import RunnerGradingConfig

pytestmark = pytest.mark.unit

_LIBRARY = "versioned_lib"


def _version(version: str) -> dict[str, str]:
    source = f"VERSION = {version!r}\n".encode()
    return {f"{_LIBRARY}/__init__.py": base64.b64encode(source).decode("ascii")}


def _imported_version() -> str:
    return importlib.import_module(_LIBRARY).VERSION


@pytest.fixture(autouse=True)
def no_stale_library() -> Iterator[None]:
    """The library is absent from ``sys.modules`` before and after every test."""
    sys.modules.pop(_LIBRARY, None)
    yield
    sys.modules.pop(_LIBRARY, None)


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


def test_purge_drops_the_modules_loaded_from_the_directory_and_no_other(tmp_path: Path) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "unrelated_lib.py").write_text("X = 1\n")
    sys.path.insert(0, str(other))
    first = extract_tool_artifacts("purge:0", _version("1"))
    try:
        assert _imported_version() == "1"
        importlib.import_module("unrelated_lib")

        purged = purge_modules_under(first)

        assert purged == [_LIBRARY]
        assert _LIBRARY not in sys.modules
        assert "unrelated_lib" in sys.modules and "json" in sys.modules
    finally:
        sys.path.remove(str(other))
        sys.path.remove(str(first))
        sys.modules.pop("unrelated_lib", None)


def test_after_a_purge_the_next_extraction_is_imported(tmp_path: Path) -> None:
    first = extract_tool_artifacts("purge:1", _version("1"))
    assert _imported_version() == "1"
    purge_modules_under(first)
    sys.path.remove(str(first))

    second = extract_tool_artifacts("purge:2", _version("2"))
    try:
        assert _imported_version() == "2"
    finally:
        purge_modules_under(second)
        sys.path.remove(str(second))


# ---------------------------------------------------------------------------
# The runner: trial cleanup and shutdown
# ---------------------------------------------------------------------------


def test_trial_cleanup_lets_the_next_trial_import_its_own_version(runner_service: Any) -> None:
    runner_service._extract_tool_artifacts("purge:a", _version("1"))
    assert _imported_version() == "1"

    runner_service._cleanup_trial_artifacts("purge:a")
    runner_service._extract_tool_artifacts("purge:b", _version("2"))
    try:
        assert _imported_version() == "2"
    finally:
        runner_service._cleanup_trial_artifacts("purge:b")
    assert _LIBRARY not in sys.modules


def test_shutdown_purges_every_trial_still_extracted(runner_service: Any) -> None:
    extracted = runner_service._extract_tool_artifacts("purge:c", _version("1"))
    assert _imported_version() == "1"

    runner_service.shutdown()

    assert _LIBRARY not in sys.modules
    assert str(extracted) not in sys.path
    assert not extracted.exists()


# ---------------------------------------------------------------------------
# The grader: the offline composite kind's try/finally
# ---------------------------------------------------------------------------

_CHECKS = f"""\
import {_LIBRARY}

from tolokaforge.core.grading.checks_interface import CheckPassed, check


@check
def library_version():
    return CheckPassed(message={_LIBRARY}.VERSION)
"""


def _grade(version: str) -> str:
    """The message of the one custom check: the library version it imported."""
    artifacts = {
        **_version(version),
        "checks.py": base64.b64encode(_CHECKS.encode()).decode("ascii"),
    }
    task_description = {
        "task_id": "task-1",
        "name": "task-1",
        "category": "test",
        "description": "a task",
        "adapter_type": "native",
        "system_prompt": "You are a helpful assistant.",
        "initial_state": {},
        "tool_artifacts": artifacts,
    }
    task_config = RunnerGradingConfig(
        combine_method="weighted",
        weights={"custom_checks": 1.0},
        pass_threshold=0.5,
        custom_checks={"enabled": True, "file": "checks.py"},
    )
    trajectory = {
        "task_id": "task-1",
        "trial_index": 0,
        "start_ts": "2026-01-01T00:00:00Z",
        "end_ts": "2026-01-01T00:00:01Z",
        "messages": [],
        "termination_reason": "agent_done",
    }
    substrate = _OfflineV11Substrate(task_description=task_description, trajectory=trajectory)
    grade = CompositeGraderKind().evaluate(
        substrate=substrate,  # type: ignore[arg-type]
        task_config=task_config,
        kind_config=None,
        trial_id="task:0",
        agent_tools={},
        logger=logging.getLogger("test-artifact-purge"),  # type: ignore[arg-type]
    )
    assert grade is not None and grade.custom_checks_details is not None
    (detail,) = grade.custom_checks_details
    assert detail.status == "passed", detail
    return detail.message


def test_each_offline_grade_imports_the_version_its_bundle_ships() -> None:
    assert _grade("1") == "1"
    assert _LIBRARY not in sys.modules

    assert _grade("2") == "2"
