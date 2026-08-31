"""Equivalence lock: :meth:`NativeAdapter.grading_source` reproduces the
NATIVE arm of :func:`grading_source_under_adapter` byte-for-byte.

Two things locked. First, for every row of the semantics matrix (declared +
on-disk, declared + missing, undeclared) the override and the free function
return equal :class:`GradingSource` values — ``GradingSource`` is a frozen
dataclass so ``==`` covers ``kind``, ``path``, and ``reason``. Second, the
author-facing sentences the two absence rows carry are locked by substring
against the same prose the free-function tests hold, so a divergence in either
copy of the reason strings breaks this file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tolokaforge.adapters._task_loader import (
    GradingSource,
    GradingSourceKind,
    grading_source_under_adapter,
    load_task_yaml,
)
from tolokaforge.adapters.native import NativeAdapter

pytestmark = pytest.mark.canonical


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _write_minimal_task(task_dir: Path, **extra: object) -> Path:
    data: dict = {"task_id": "min", "description": "minimal task"}
    data.update(extra)
    _write_yaml(task_dir / "task.yaml", data)
    return task_dir / "task.yaml"


def _write_grading(path: Path) -> None:
    _write_yaml(
        path,
        {
            "combine": {
                "method": "weighted",
                "weights": {"state_checks": 1.0},
                "pass_threshold": 1.0,
            }
        },
    )


def _a_native_adapter(base_dir: Path) -> NativeAdapter:
    return NativeAdapter({"tasks_glob": "*/task.yaml", "base_dir": str(base_dir)})


@pytest.mark.parametrize(
    ("declares_grading", "writes_the_file", "expected_kind"),
    [
        (True, True, GradingSourceKind.ON_DISK),
        (True, False, GradingSourceKind.WITHHELD),
        (False, False, GradingSourceKind.WITHHELD),
    ],
    ids=[
        "declared_and_on_disk_resolves_to_on_disk",
        "declared_and_missing_is_withheld",
        "undeclared_is_withheld",
    ],
)
def test_the_override_returns_the_same_grading_source_the_free_function_returns_under_native(
    tmp_path: Path,
    declares_grading: bool,
    writes_the_file: bool,
    expected_kind: GradingSourceKind,
) -> None:
    """Structural equality across the three-row semantics matrix.

    The override and the free-function NATIVE arm must produce equal
    :class:`GradingSource` values for every combination of declared /
    on-disk state; the frozen dataclass equality covers ``kind``, ``path``,
    and the full reason prose so a per-branch divergence trips here.
    """
    task_dir = tmp_path / "flat_task"
    extra: dict[str, object] = {"adapter_type": "native"}
    if declares_grading:
        extra["grading"] = "grading.yaml"
    task_path = _write_minimal_task(task_dir, **extra)
    if writes_the_file:
        _write_grading(task_dir / "grading.yaml")
    task, resolved_dir = load_task_yaml(task_path)

    adapter = _a_native_adapter(tmp_path)
    override_source = adapter.grading_source(task, resolved_dir)
    free_function_source = grading_source_under_adapter(task, resolved_dir, "native")

    assert override_source == free_function_source
    assert override_source.kind is expected_kind
    if expected_kind is GradingSourceKind.ON_DISK:
        assert override_source.path == task_dir / "grading.yaml"
        assert override_source.reason == ""
    else:
        assert override_source.path is None


def test_the_override_reason_for_a_declared_missing_file_names_the_ref_and_both_fixes(
    tmp_path: Path,
) -> None:
    """Prose lock on the declared-but-missing branch of the override.

    Mirrors the free-function prose test at
    ``tests/unit/test_task_loader.py::test_a_declared_file_that_is_not_there_names_the_path_and_both_ways_to_supply_it``
    — the two implementations must carry the same author-facing sentence.
    """
    task_dir = tmp_path / "flat_task"
    task, resolved_dir = load_task_yaml(
        _write_minimal_task(task_dir, adapter_type="native", grading="grading.yaml")
    )

    reason = _a_native_adapter(tmp_path).grading_source(task, resolved_dir).reason

    assert "'min'" in reason
    assert "'grading.yaml'" in reason
    assert "Correct the `grading:` path" in reason
    assert "create that file" in reason
    assert "no `grading:` field" not in reason


def test_the_override_reason_for_an_undeclared_source_names_the_task_and_both_supply_ways(
    tmp_path: Path,
) -> None:
    """Prose lock on the undeclared branch of the override.

    Mirrors the free-function prose test at
    ``tests/unit/test_task_loader.py::test_a_withheld_source_names_the_task_and_both_ways_to_supply_one``
    — the two implementations must carry the same author-facing sentence.
    """
    task_path = _write_minimal_task(tmp_path / "flat_task", adapter_type="native")
    task, task_dir = load_task_yaml(task_path)

    reason = _a_native_adapter(tmp_path).grading_source(task, task_dir).reason

    assert "'min'" in reason
    assert "`grading:`" in reason
    assert "grading.yaml beside its task.yaml" in reason
    assert "before any trial is scheduled" in reason


def test_the_override_returns_a_grading_source(tmp_path: Path) -> None:
    """The return type is :class:`GradingSource` — the shape callers destructure."""
    task_path = _write_minimal_task(tmp_path / "flat_task", adapter_type="native")
    task, task_dir = load_task_yaml(task_path)

    source = _a_native_adapter(tmp_path).grading_source(task, task_dir)

    assert isinstance(source, GradingSource)
