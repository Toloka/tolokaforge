"""``composite.grade_state_checks_reads`` — end-to-end shape parity lock.

The composite owns jsonpath + probes scoring. This suite constructs an
:class:`InProcessGradingSubstrate` over a hand-built ``{tables: [rows],
filesystem: {rel: text}}`` fixture, drives the composite, and asserts the
``(jsonpath_score, jsonpath_reasons)`` pair matches what
:func:`evaluate_jsonpath_checks` produces over the same reshaped state
(``{db, tables, filesystem}``). A drift in the composite's reshaping /
gating / STABLE routing surfaces here.

The three semantic contracts under test:
- STABLE routing: ``substrate.final_state_stable()`` feeds ``$.db.*`` /
  ``$.tables.*``, so jsonpath grading sees the DB-service-filtered view.
- filesystem routing: ``substrate.filesystem_state()`` feeds
  ``$.filesystem[…]`` with the ``/env/fs/agent-visible/<rel>`` layout the
  RegisterTrial provisioner writes and jsonpath assertions address.
- gating: a path-glob-only pack fetches nothing (proved by
  ``test_composite_state_checks_gating.py``); a DB-addressing pack fetches
  only STABLE; a filesystem-only-``path:`` pack fetches only the fs walk.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.grading.composite import grade_state_checks_reads
from tolokaforge.core.grading.substrate import InProcessGradingSubstrate
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.runner.grading import evaluate_jsonpath_checks
from tolokaforge.runner.grading_ledger import DB_PROBES_KEY, JSONPATHS_KEY
from tolokaforge.runner.models import RunnerStateChecksConfig

pytestmark = pytest.mark.canonical


_STABLE_TABLES = {
    "users": [
        {"id": "u1", "name": "Alice", "email": "alice@example.com"},
        {"id": "u2", "name": "Bob", "email": "bob@example.com"},
    ],
    "orders": [{"id": "o1", "status": "shipped", "amount": 42}],
}

_FILESYSTEM = {
    "/env/fs/agent-visible/notes.md": "hello world",
    "/env/fs/agent-visible/todo.txt": "- item one",
}


def _substrate(
    *,
    stable_tables: dict[str, Any] | None = None,
    filesystem: dict[str, str] | None = None,
) -> InProcessGradingSubstrate:
    return InProcessGradingSubstrate(
        db_reader=MagicMock(),
        knowledge_search=None,
        filesystem_root=None,
        initial_state={},
        final_state_stable_factory=((lambda: stable_tables) if stable_tables is not None else None),
        filesystem_state_factory=(lambda: filesystem) if filesystem is not None else None,
    )


def _logger() -> StructuredLogger:
    return StructuredLogger(name="test-composite-state-checks")


def _runner_shipped_shape(
    stable: dict[str, Any] | None, filesystem: dict[str, str] | None
) -> dict[str, Any]:
    """The composite's reshape — two roots for the same DB view plus the
    ``$.filesystem`` overlay. Kept here in one place so the assertion
    below reads as a direct byte-for-byte comparison."""
    db = stable or {}
    return {"db": db, "tables": db, "filesystem": filesystem or {}}


class TestJsonpathCompositeMatchesRunnerPath:
    """Every jsonpath scoring the composite produces matches the runner's
    ``evaluate_jsonpath_checks`` over the same reshaped state. Extraction is
    byte-preserving; the composite is only a reshape + call."""

    def test_db_addressing_pack_matches_the_runner_path(self) -> None:
        checks = [
            {"path": "$.db.users[0].name", "equals": "Alice", "description": "alice named"},
            {"path": "$.db.users[1].name", "equals": "Bob", "description": "bob named"},
            {"path": "$.db.orders[0].amount", "equals": 42, "description": "order amount"},
        ]
        config = RunnerStateChecksConfig(jsonpath_checks=checks)
        result = grade_state_checks_reads(
            trial_id="task:0",
            config=config,
            substrate=_substrate(stable_tables=_STABLE_TABLES),
            logger=_logger(),
        )
        expected_score, expected_reasons = evaluate_jsonpath_checks(
            checks, state=_runner_shipped_shape(_STABLE_TABLES, None)
        )
        assert result.jsonpath_score == expected_score
        assert result.jsonpath_reasons == expected_reasons
        assert result.accounted_keys == {JSONPATHS_KEY: result.accounted_keys[JSONPATHS_KEY]}
        assert result.db_probe_score is None
        assert result.db_probe_reasons is None

    def test_filesystem_addressing_pack_matches_the_runner_path(self) -> None:
        checks = [
            {
                "path": "$.filesystem['/env/fs/agent-visible/notes.md']",
                "contains": "hello",
                "description": "notes contain hello",
            },
            {
                "path": "$.filesystem['/env/fs/agent-visible/todo.txt']",
                "contains": "item one",
                "description": "todo carries the item",
            },
        ]
        config = RunnerStateChecksConfig(jsonpath_checks=checks)
        result = grade_state_checks_reads(
            trial_id="task:0",
            config=config,
            substrate=_substrate(filesystem=_FILESYSTEM),
            logger=_logger(),
        )
        expected_score, expected_reasons = evaluate_jsonpath_checks(
            checks, state=_runner_shipped_shape(None, _FILESYSTEM)
        )
        assert result.jsonpath_score == expected_score
        assert result.jsonpath_reasons == expected_reasons

    def test_mixed_pack_matches_the_runner_path(self) -> None:
        checks = [
            {"path": "$.db.users[0].name", "equals": "Alice", "description": "alice named"},
            {
                "path": "$.filesystem['/env/fs/agent-visible/notes.md']",
                "contains": "hello",
                "description": "notes contain hello",
            },
            {"path": "$.db.orders[0].status", "equals": "shipped", "description": "shipped"},
        ]
        config = RunnerStateChecksConfig(jsonpath_checks=checks)
        result = grade_state_checks_reads(
            trial_id="task:0",
            config=config,
            substrate=_substrate(stable_tables=_STABLE_TABLES, filesystem=_FILESYSTEM),
            logger=_logger(),
        )
        expected_score, expected_reasons = evaluate_jsonpath_checks(
            checks, state=_runner_shipped_shape(_STABLE_TABLES, _FILESYSTEM)
        )
        assert result.jsonpath_score == expected_score
        assert result.jsonpath_reasons == expected_reasons

    def test_path_glob_only_pack_matches_the_runner_path(self, tmp_path) -> None:
        # Write a file so the glob check can find something on disk. The
        # runner's ``evaluate_jsonpath_file_checks`` reads from cwd; setting
        # cwd via monkeypatch keeps the shipped shape.
        target = tmp_path / "output.txt"
        target.write_text("hello disk", encoding="utf-8")
        # A pack whose only assertion is a path_glob does not touch the
        # substrate at all — this only asserts the composite behaves as
        # expected for an all-glob pack.
        checks = [
            {
                "path_glob": str(target),
                "contains_ci": "hello",
                "description": "output on disk",
            },
        ]
        config = RunnerStateChecksConfig(jsonpath_checks=checks)
        result = grade_state_checks_reads(
            trial_id="task:0",
            config=config,
            substrate=_substrate(),
            logger=_logger(),
        )
        expected_score, expected_reasons = evaluate_jsonpath_checks(checks, state=None)
        assert result.jsonpath_score == expected_score
        assert result.jsonpath_reasons == expected_reasons


class TestEmptyPacksLeaveTheComponentsUntouched:
    """A pack that carried no jsonpath and no probe leaves both slots
    ``None`` so the runner does not overwrite ``RunnerGradeComponents``
    with sentinels the combine treats as evaluated."""

    def test_no_jsonpaths_no_probes_returns_all_none(self) -> None:
        config = RunnerStateChecksConfig()
        result = grade_state_checks_reads(
            trial_id="task:0",
            config=config,
            substrate=_substrate(),
            logger=_logger(),
        )
        assert result.jsonpath_score is None
        assert result.jsonpath_reasons is None
        assert result.db_probe_score is None
        assert result.db_probe_reasons is None
        assert result.accounted_keys == {}
        assert JSONPATHS_KEY not in result.accounted_keys
        assert DB_PROBES_KEY not in result.accounted_keys
