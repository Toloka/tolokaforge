"""Adapter-side cross-check of ``state_checks.id_fields`` against ``initial_state``.

Runs inside :meth:`NativeAdapter.to_task_description` — the entry point the
orchestrator's pre-run gradeability pass resolves every selected task through,
so a refusal here lands before the first trial is paid for. Fail loud on typos
so a bad task doesn't reach the runner and hard-fail (or silently corrupt
state) at trial time on a message the author never sees. One gate, three
findings: a declared table absent from ``initial_state``, a declared key
component absent from every seeded record of its table, and a declared key
that does not uniquely identify the table's seeded records (the record-level
findings are skipped for a table seeded empty, whose rows may arrive via
``initialization_actions``).

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
    id_fields: dict[str, str | list[str]] | None,
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
        initial_state={"widgets": [{"widget_id": "W1"}, {"widget_id": "W2"}]},
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


# Each component alone repeats (A1 twice, AAPL twice); only the pair is unique.
_SEEDED_POSITIONS = {
    "positions": [
        {"account_id": "A1", "symbol": "AAPL", "qty": 5},
        {"account_id": "A1", "symbol": "MSFT", "qty": 7},
        {"account_id": "A2", "symbol": "AAPL", "qty": 3},
    ]
}


def test_composite_key_unique_where_components_alone_are_not_builds(tmp_path: Path) -> None:
    _write_task_pack(
        tmp_path,
        initial_state=_SEEDED_POSITIONS,
        id_fields={"positions": ["account_id", "symbol"]},
    )
    td = _adapter(tmp_path).to_task_description("widgets")
    assert td.grading.state_checks.id_fields == {"positions": ["account_id", "symbol"]}


def test_non_unique_single_key_raises(tmp_path: Path) -> None:
    _write_task_pack(
        tmp_path,
        initial_state=_SEEDED_POSITIONS,
        id_fields={"positions": "account_id"},
    )
    with pytest.raises(ValueError) as ei:
        _adapter(tmp_path).to_task_description("widgets")
    msg = str(ei.value)
    assert "positions" in msg  # names the table
    assert "account_id" in msg  # names the declared key
    assert "A1" in msg  # names the colliding value
    assert "A2" not in msg  # the unique value is not blamed
    assert "relaxed_validation" in msg  # hints at the escape hatch


def test_non_unique_key_with_relaxed_warns(tmp_path: Path, caplog) -> None:
    _write_task_pack(
        tmp_path,
        initial_state=_SEEDED_POSITIONS,
        id_fields={"positions": "account_id"},
        relaxed=True,
    )
    with caplog.at_level(logging.WARNING):
        td = _adapter(tmp_path).to_task_description("widgets")

    warns = [rec.getMessage() for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert any("account_id" in m and "A1" in m for m in warns)
    assert td.grading.state_checks.id_fields == {"positions": "account_id"}


def test_absent_composite_component_raises(tmp_path: Path) -> None:
    _write_task_pack(
        tmp_path,
        initial_state=_SEEDED_POSITIONS,
        id_fields={"positions": ["account_id", "ticker"]},
    )
    with pytest.raises(ValueError) as ei:
        _adapter(tmp_path).to_task_description("widgets")
    msg = str(ei.value)
    assert "ticker" in msg  # names the absent component
    assert "positions" in msg  # names the table
    assert "symbol" in msg  # surfaces the fields the records do carry
    assert "relaxed_validation" in msg  # hints at the escape hatch


def test_misspelled_single_key_raises(tmp_path: Path) -> None:
    # A single field is a one-component key; the rule cannot hold for lists only.
    _write_task_pack(
        tmp_path,
        initial_state={"widgets": [{"widget_id": "W1", "status": "new"}]},
        id_fields={"widgets": "widgit_id"},
    )
    with pytest.raises(ValueError) as ei:
        _adapter(tmp_path).to_task_description("widgets")
    msg = str(ei.value)
    assert "widgit_id" in msg
    assert "widget_id" in msg  # the seeded field the author meant
    assert "relaxed_validation" in msg


def test_table_seeded_empty_is_not_component_checked(tmp_path: Path) -> None:
    # An empty seeded table may be populated by initialization_actions, so its
    # declared components cannot be checked against anything.
    _write_task_pack(
        tmp_path,
        initial_state={"positions": []},
        id_fields={"positions": ["account_id", "symbol"]},
    )
    td = _adapter(tmp_path).to_task_description("widgets")
    assert td.grading.state_checks.id_fields == {"positions": ["account_id", "symbol"]}


def test_relaxed_downgrades_both_findings_in_one_warning(tmp_path: Path, caplog) -> None:
    _write_task_pack(
        tmp_path,
        initial_state={"widgets": [{"widget_id": "W1"}]},
        id_fields={"widgetz": "widget_id", "widgets": "widgit_id"},
        relaxed=True,
    )
    with caplog.at_level(logging.WARNING):
        td = _adapter(tmp_path).to_task_description("widgets")

    warns = [rec.getMessage() for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert any("widgetz" in m and "widgit_id" in m for m in warns)  # one message, both findings
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
