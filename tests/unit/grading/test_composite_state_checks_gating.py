"""``composite.grade_state_checks_reads`` — gate + STABLE/RAW split locks.

Seven cases, each direct behaviour-lock:

(a) path-glob-only pack — neither DB factory nor the filesystem factory
    fires. Both counter-mocks raise on entry, so a stray read would fail
    the case rather than silently costing an RPC.

(b) DB-addressing-only pack — the STABLE factory fires exactly once; the
    filesystem factory and the RAW factory stay untouched.

(c) filesystem-only-``path:`` pack — the filesystem factory fires once,
    both DB factories are untouched.

(d) jsonpath check whose ``path:`` addresses an unstable field — the STABLE
    factory returns the server-filtered rows, so the value the RAW factory
    would carry is never in scope and the assertion fails. Direct
    behaviour-lock for the STABLE/RAW split.

(e) ``DBTrialNotFoundError`` from the STABLE factory — composite catches,
    logs the shipped wording, returns empty DB state, jsonpath scoring
    proceeds against the filesystem alone.

(f) ``InProcessGradingSubstrate(final_state=..., final_state_factory=...)``
    — construction-time ``ValueError`` naming both fields.

(g) ``InProcessGradingSubstrate(...)`` with no STABLE wiring —
    ``.final_state_stable()`` raises ``RuntimeError`` naming the missing
    factory. A caller reaching for STABLE without wiring is a bug the
    audit surfaces; never silently returns RAW rows.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.grading.composite import grade_state_checks_reads
from tolokaforge.core.grading.default_state_check_backends import JsonpathStateCheckBackend
from tolokaforge.core.grading.substrate import (
    InProcessGradingSubstrate,
    SubstrateUnreachableError,
)
from tolokaforge.core.plugin_registry import load_state_check_backend
from tolokaforge.runner.db_client import TrialNotFoundError as DBTrialNotFoundError
from tolokaforge.runner.models import RunnerStateChecksConfig

pytestmark = pytest.mark.unit


class _Counter:
    """A one-shot counter used as a factory placeholder that must never fire."""

    def __init__(self, name: str, value: Any = None) -> None:
        self._name = name
        self._value = value
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        if self._value is None:
            raise AssertionError(
                f"factory {self._name!r} was invoked but this case requires it never fires"
            )
        return self._value


def _logger() -> logging.Logger:
    return logging.getLogger("test-composite-gating")


def _shipped_backends() -> dict[str, Any]:
    return {
        "jsonpath": load_state_check_backend("jsonpath")(),
        "db_probes": load_state_check_backend("db_probes")(),
    }


def _run(
    *,
    config: RunnerStateChecksConfig,
    substrate: InProcessGradingSubstrate,
) -> Any:
    return grade_state_checks_reads(
        trial_id="task:0",
        config=config,
        substrate=substrate,
        state_check_backends=_shipped_backends(),
        logger=_logger(),  # type: ignore[arg-type]  # StructuredLogger satisfied at runtime
    )


class TestGatingByConfigShape:
    """(a) path-glob-only, (b) DB-only, (c) filesystem-only-`path:` — each
    reads only the substrate slots the pack's assertions address."""

    def test_a_path_glob_only_pack_touches_neither_db_nor_filesystem(self, tmp_path) -> None:
        target = tmp_path / "output.txt"
        target.write_text("done", encoding="utf-8")
        stable_factory = _Counter("stable")
        fs_factory = _Counter("filesystem")
        raw_factory = _Counter("raw")
        substrate = InProcessGradingSubstrate(
            db_reader=MagicMock(),
            knowledge_search=None,
            filesystem_root=None,
            initial_state={},
            final_state_factory=raw_factory,
            final_state_stable_factory=stable_factory,
            filesystem_state_factory=fs_factory,
        )
        config = RunnerStateChecksConfig(
            jsonpath_checks=[
                {
                    "path_glob": str(target),
                    "contains_ci": "done",
                    "description": "output on disk",
                }
            ],
        )
        _run(config=config, substrate=substrate)
        assert stable_factory.calls == 0
        assert fs_factory.calls == 0
        assert raw_factory.calls == 0

    def test_b_db_addressing_only_pack_fires_stable_once_and_not_raw(self) -> None:
        stable_factory = _Counter("stable", {"users": [{"id": "u1", "name": "Alice"}]})
        fs_factory = _Counter("filesystem")
        raw_factory = _Counter("raw")
        substrate = InProcessGradingSubstrate(
            db_reader=MagicMock(),
            knowledge_search=None,
            filesystem_root=None,
            initial_state={},
            final_state_factory=raw_factory,
            final_state_stable_factory=stable_factory,
            filesystem_state_factory=fs_factory,
        )
        config = RunnerStateChecksConfig(
            jsonpath_checks=[
                {
                    "path": "$.db.users[0].name",
                    "equals": "Alice",
                    "description": "alice named",
                }
            ],
        )
        _run(config=config, substrate=substrate)
        assert stable_factory.calls == 1
        assert fs_factory.calls == 0
        assert raw_factory.calls == 0

    def test_c_filesystem_only_path_pack_fires_only_the_filesystem_factory(
        self,
    ) -> None:
        stable_factory = _Counter("stable")
        raw_factory = _Counter("raw")
        fs_factory = _Counter(
            "filesystem",
            {"/env/fs/agent-visible/notes.md": "hello"},
        )
        substrate = InProcessGradingSubstrate(
            db_reader=MagicMock(),
            knowledge_search=None,
            filesystem_root=None,
            initial_state={},
            final_state_factory=raw_factory,
            final_state_stable_factory=stable_factory,
            filesystem_state_factory=fs_factory,
        )
        config = RunnerStateChecksConfig(
            jsonpath_checks=[
                {
                    "path": "$.filesystem['/env/fs/agent-visible/notes.md']",
                    "contains": "hello",
                    "description": "notes contain hello",
                }
            ],
        )
        _run(config=config, substrate=substrate)
        assert fs_factory.calls == 1
        assert stable_factory.calls == 0
        assert raw_factory.calls == 0


class TestStableRawSplit:
    """(d) A jsonpath addressing an unstable field — STABLE returns the
    filtered view, so the assertion fails; the RAW factory's value never
    reaches jsonpath scoring."""

    def test_d_unstable_field_assertion_fails_against_stable_view(self) -> None:
        # RAW rows carry ``session_token``; STABLE strips it server-side.
        stable_view = {"users": [{"id": "u1", "name": "Alice"}]}
        raw_view_never_reached = {"users": [{"id": "u1", "name": "Alice", "session_token": "S-1"}]}
        stable_factory = _Counter("stable", stable_view)
        raw_factory = _Counter("raw", raw_view_never_reached)
        substrate = InProcessGradingSubstrate(
            db_reader=MagicMock(),
            knowledge_search=None,
            filesystem_root=None,
            initial_state={},
            final_state_factory=raw_factory,
            final_state_stable_factory=stable_factory,
        )
        config = RunnerStateChecksConfig(
            jsonpath_checks=[
                {
                    "path": "$.db.users[0].session_token",
                    "equals": "S-1",
                    "description": "unstable field assertion",
                }
            ],
        )
        result = _run(config=config, substrate=substrate)
        assert result.jsonpath_score == 0.0
        assert stable_factory.calls == 1
        assert raw_factory.calls == 0


class TestDbTrialNotFoundDegrades:
    """(e) The composite catches the DB-service's absent-trial error, logs
    the shipped wording, and grades the pack against the filesystem alone."""

    def test_e_db_trial_not_found_logs_and_grades_against_filesystem(self, caplog) -> None:
        def raising_stable() -> dict[str, Any]:
            raise DBTrialNotFoundError("task:0")

        fs_factory = _Counter(
            "filesystem",
            {"/env/fs/agent-visible/notes.md": "hello"},
        )
        substrate = InProcessGradingSubstrate(
            db_reader=MagicMock(),
            knowledge_search=None,
            filesystem_root=None,
            initial_state={},
            final_state_stable_factory=raising_stable,
            filesystem_state_factory=fs_factory,
        )
        # Mix a DB-addressing check (which will resolve to nothing under the
        # empty DB view) with a filesystem check that still passes.
        config = RunnerStateChecksConfig(
            jsonpath_checks=[
                {
                    "path": "$.db.users[0].name",
                    "equals": "Alice",
                    "description": "db assertion under absent trial",
                },
                {
                    "path": "$.filesystem['/env/fs/agent-visible/notes.md']",
                    "contains": "hello",
                    "description": "notes contain hello",
                },
            ],
        )
        with caplog.at_level(logging.WARNING):
            result = _run(config=config, substrate=substrate)
        assert result.jsonpath_score == pytest.approx(0.5)
        assert any(
            "GradeTrial: task:0 - DB trial not found; grading with empty DB state" in rec.message
            for rec in caplog.records
        ), caplog.text
        assert fs_factory.calls == 1


class TestConstructionInvariants:
    """(f) Mutex on the two ``final_state`` shapes, (g) fail-loud STABLE
    without wiring — both surfaces the audit relies on."""

    def test_f_final_state_and_factory_together_raise_at_construction(self) -> None:
        with pytest.raises(ValueError, match="final_state.*final_state_factory"):
            InProcessGradingSubstrate(
                db_reader=MagicMock(),
                knowledge_search=None,
                filesystem_root=None,
                initial_state={},
                final_state={"users": [{"id": "u1"}]},
                final_state_factory=lambda: {"users": [{"id": "u2"}]},
            )

    def test_g_final_state_stable_without_wiring_raises_fail_loud(self) -> None:
        substrate = InProcessGradingSubstrate(
            db_reader=MagicMock(),
            knowledge_search=None,
            filesystem_root=None,
            initial_state={},
            final_state={"users": [{"id": "u1"}]},
        )
        with pytest.raises(RuntimeError, match="final_state_stable_factory"):
            substrate.final_state_stable()


def test_jsonpath_backend_propagates_substrate_unreachable() -> None:
    """A ``SubstrateUnreachableError`` from ``substrate.final_state_stable``
    must NOT degrade — it propagates so the caller can book the trial as
    ungradeable.

    ``JsonpathStateCheckBackend.query`` catches only ``DBTrialNotFoundError``
    (the absent-trial degradation). Any other read failure is a substrate
    transport error the audit surfaces. Locking here catches a future
    tightening of the ``except`` clause back over
    :class:`SubstrateUnreachableError`. Parallels the custom-checks lock
    at ``tests/unit/grading/test_composite_custom_checks_degradation.py``.
    """

    def raising_stable() -> dict[str, Any]:
        raise SubstrateUnreachableError("grader lost the runner mid-grade")

    substrate = InProcessGradingSubstrate(
        db_reader=MagicMock(),
        knowledge_search=None,
        filesystem_root=None,
        initial_state={},
        final_state_stable_factory=raising_stable,
    )
    backend = JsonpathStateCheckBackend()
    with pytest.raises(SubstrateUnreachableError):
        backend.query(
            expression=[{"path": "$.db.users[0].name", "equals": "Alice"}],
            substrate=substrate,
        )
