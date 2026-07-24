"""Adapter-side cross-check: ``state_checks.id_fields`` keys must exist in ``initial_state``.

Runs inside :meth:`NativeAdapter.to_task_description` — the earliest surface
where task authors see conversion output. Fail loud on typos so a bad task
doesn't reach the runner and hard-fail at trial time on a message the author
never sees.

``state_checks.relaxed_validation: true`` downgrades the raise to a warning
for legacy tasks that pre-date the check.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import yaml

from tolokaforge.adapters.native import NativeAdapter

pytestmark = pytest.mark.unit


def _write_task_pack(
    root: Path,
    *,
    initial_state: dict,
    id_fields: dict[str, str] | None,
    relaxed: bool = False,
) -> Path:
    """Emit a minimal native task pack at ``root/tasks/widgets/`` and return the base dir."""
    task_dir = root / "tasks" / "widgets"
    task_dir.mkdir(parents=True)

    (task_dir / "initial_state.json").write_text(json.dumps(initial_state))
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": "widgets",
                "name": "Widgets test",
                "category": "test",
                "description": "id_fields cross-check fixture",
                "initial_state": {"json_db": "initial_state.json"},
                "tools": {"agent": {"enabled": []}, "user": {"enabled": []}},
                "user_simulator": {
                    "mode": "scripted",
                    "scripted_flow": [{"role": "user", "content": "hi"}],
                },
                "policies": {"disallowed_actions": [], "guidance": []},
                "grading": "grading.yaml",
            }
        )
    )

    state_checks: dict = {"jsonpaths": []}
    if id_fields is not None:
        state_checks["id_fields"] = id_fields
    if relaxed:
        state_checks["relaxed_validation"] = True

    (task_dir / "grading.yaml").write_text(
        yaml.safe_dump(
            {
                "combine": {
                    "method": "weighted",
                    "weights": {"state_checks": 1.0, "transcript_rules": 0.0},
                    "pass_threshold": 0.7,
                },
                "state_checks": state_checks,
                "transcript_rules": {"max_turns": 5, "disallow_regex": []},
                "llm_judge": None,
            }
        )
    )
    return root


def _adapter(root: Path) -> NativeAdapter:
    return NativeAdapter({"base_dir": str(root), "tasks_glob": "tasks/**/task.yaml"})


def test_matching_tables_passes(tmp_path: Path) -> None:
    _write_task_pack(
        tmp_path,
        initial_state={"widgets": [{"widget_id": "W1"}]},
        id_fields={"widgets": "widget_id"},
    )
    adapter = _adapter(tmp_path)
    td = adapter.to_task_description("widgets")
    assert td.grading.state_checks.id_fields == {"widgets": "widget_id"}
    assert td.grading.state_checks.relaxed_validation is False


def test_unknown_table_raises(tmp_path: Path) -> None:
    _write_task_pack(
        tmp_path,
        initial_state={"widgets": [{"widget_id": "W1"}]},
        id_fields={"widgetz": "widget_id"},  # typo
    )
    adapter = _adapter(tmp_path)
    with pytest.raises(ValueError) as ei:
        adapter.to_task_description("widgets")
    msg = str(ei.value)
    assert "widgetz" in msg  # names the unknown key
    assert "widgets" in msg  # surfaces the known table
    assert "relaxed_validation" in msg  # hints at the escape hatch


def test_unknown_table_with_relaxed_warns(tmp_path: Path, caplog) -> None:
    _write_task_pack(
        tmp_path,
        initial_state={"widgets": [{"widget_id": "W1"}]},
        id_fields={"widgetz": "widget_id"},
        relaxed=True,
    )
    adapter = _adapter(tmp_path)

    with caplog.at_level(logging.WARNING):
        td = adapter.to_task_description("widgets")

    # Warning was emitted, but the task built successfully.
    warns = [rec for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert any("widgetz" in rec.getMessage() for rec in warns)
    assert td.grading.state_checks.relaxed_validation is True


def test_empty_id_fields_never_raises(tmp_path: Path) -> None:
    # Backward compat: tasks that don't declare id_fields see zero behaviour change.
    _write_task_pack(
        tmp_path,
        initial_state={"items": [{"id": "X1"}]},
        id_fields=None,
    )
    adapter = _adapter(tmp_path)
    td = adapter.to_task_description("widgets")
    assert td.grading.state_checks.id_fields == {}
