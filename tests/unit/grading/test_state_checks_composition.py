"""How the core engine folds a state hash and JSONPath assertions into one component.

Drives the real :class:`~tolokaforge.core.grading.combine.GradingEngine` rather
than the composer directly (that arithmetic is locked in
``test_state_composition.py``), because what these cases pin is the *routing*: what
state each half is evaluated against, when a source contributes nothing, and what
happens when a hash was configured but no verdict could be produced.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.grading.state_checks import (
    GoldenReplayError,
    consistent_hash,
    to_hashable,
)
from tolokaforge.core.grading.state_composition import MISSING_HASH_WEIGHT_MESSAGE
from tolokaforge.core.models import (
    GradingConfig,
    InitialStateConfig,
    Trajectory,
    TrialStatus,
)

pytestmark = pytest.mark.unit

_TS = datetime(2025, 1, 1, tzinfo=timezone.utc)

# A wrapped final environment state, the shape every grading path receives.
_DB_STATE = {"widgets": [{"id": "W1", "status": "closed"}, {"id": "W2", "status": "open"}]}
_FINAL_ENV_STATE = {"agent": {}, "user": {}, "db": _DB_STATE}

# Two conventionally rooted assertions, exactly one of them satisfied.
_HALF_SATISFIED_JSONPATHS = [
    {"path": "$.db.widgets[0].status", "equals": "closed", "description": "W1 closed"},
    {"path": "$.db.widgets[1].status", "equals": "closed", "description": "W2 closed"},
]

_MATCHING_HASH = consistent_hash(to_hashable(_DB_STATE))
_MISMATCHING_HASH = "0" * 64


def _trajectory() -> Trajectory:
    return Trajectory(
        task_id="state-composition",
        trial_index=0,
        start_ts=_TS,
        end_ts=_TS,
        status=TrialStatus.COMPLETED,
        messages=[],
    )


def _engine(state_checks: dict, **engine_kwargs) -> GradingEngine:
    config = GradingConfig(
        **{"state_checks": state_checks, "combine": {"weights": {"state_checks": 1.0}}}
    )
    return GradingEngine(grading_config=config, **engine_kwargs)


def _grade(state_checks: dict, **engine_kwargs):
    return _engine(state_checks, **engine_kwargs).grade_trajectory(_trajectory(), _FINAL_ENV_STATE)


class TestJsonpathStateRoot:
    """JSONPath assertions read the whole final env state; the hash reads the
    unwrapped database inside it.

    One case guards both halves of that split. The hash is pinned to the
    unwrapped ``db`` level, so hashing the wrapped state scores ``0.0``; the
    assertions are rooted ``$.db.…``, so evaluating them against the unwrapped
    level reports ``Path not found`` and scores ``0.0``. Either root swapped moves
    every expected number below.
    """

    @pytest.mark.parametrize(
        ("hash_weight", "expected"),
        [(0.0, 0.5), (0.25, 0.625), (0.6, 0.8)],
    )
    def test_expected_hash_branch_reads_both_roots(self, hash_weight, expected):
        grade = _grade(
            {
                "jsonpaths": _HALF_SATISFIED_JSONPATHS,
                "hash": {
                    "enabled": True,
                    "expected_state_hash": _MATCHING_HASH,
                    "weight": hash_weight,
                },
            }
        )
        assert grade.components.state_checks == pytest.approx(expected)
        assert "Path not found" not in grade.reasons
        assert "hash matches" in grade.reasons

    def test_no_hash_branch_reads_the_wrapped_state(self):
        grade = _grade({"jsonpaths": _HALF_SATISFIED_JSONPATHS})
        assert grade.components.state_checks == pytest.approx(0.5)
        assert "Path not found" not in grade.reasons


class TestTauStylePackScoresItsHashVerdict:
    """A hash-on pack with no assertions scores the hash verdict itself.

    ``check_jsonpaths`` scores an empty assertion list a vacuous ``1.0``; blending
    that against a real hash verdict would award jsonpath credit for assertions
    the author never wrote. Pinned across the whole weight domain because that
    invariance *is* the tau-bench parity argument.
    """

    @pytest.mark.parametrize("hash_weight", [0.0, 0.25, 0.5, 0.75, 1.0, None])
    @pytest.mark.parametrize(
        ("expected_state_hash", "expected"),
        [(_MATCHING_HASH, 1.0), (_MISMATCHING_HASH, 0.0)],
    )
    def test_empty_jsonpaths_contribute_nothing(self, hash_weight, expected_state_hash, expected):
        hash_config = {"enabled": True, "expected_state_hash": expected_state_hash}
        if hash_weight is not None:
            hash_config["weight"] = hash_weight

        grade = _grade({"jsonpaths": [], "hash": hash_config})
        assert grade.components.state_checks == pytest.approx(expected)


class TestHashWeightIsRequiredAndBounded:
    """Grading a config that needs a weight and carries none fails loud, and an
    out-of-range weight is rejected instead of producing a score outside [0, 1]."""

    def test_both_sources_without_a_weight_raise_the_shared_message(self):
        with pytest.raises(ValueError) as excinfo:
            _grade(
                {
                    "jsonpaths": _HALF_SATISFIED_JSONPATHS,
                    "hash": {"enabled": True, "expected_state_hash": _MATCHING_HASH},
                }
            )
        assert str(excinfo.value) == MISSING_HASH_WEIGHT_MESSAGE

    @pytest.mark.parametrize("weight", [2.0, -0.1])
    def test_out_of_range_weight_is_rejected(self, weight):
        with pytest.raises(ValueError, match="state_checks.hash.weight"):
            _grade(
                {
                    "jsonpaths": _HALF_SATISFIED_JSONPATHS,
                    "hash": {
                        "enabled": True,
                        "expected_state_hash": _MISMATCHING_HASH,
                        "weight": weight,
                    },
                }
            )


class TestUnevaluatedHashIsReported:
    """Hash grading that was configured but could not run says so on the grade
    rather than falling through to jsonpath-only grading in silence."""

    def test_golden_actions_without_replay_context_name_the_skipped_check(self):
        grade = _grade(
            {
                "jsonpaths": _HALF_SATISFIED_JSONPATHS,
                "hash": {"enabled": True, "golden_actions": [{"name": "close_widget"}]},
            }
        )
        assert "golden_actions" in grade.reasons
        assert "state hash was not checked" in grade.reasons
        assert grade.components.state_checks == pytest.approx(0.5)

    def test_hash_enabled_with_no_source_names_the_skipped_check(self):
        grade = _grade({"jsonpaths": _HALF_SATISFIED_JSONPATHS, "hash": {"enabled": True}})
        assert "neither expected_state_hash nor golden_actions" in grade.reasons
        assert "state hash was not checked" in grade.reasons
        assert grade.components.state_checks == pytest.approx(0.5)


class TestFailedGoldenReplayIsNotAScore:
    """A golden replay that could not execute produces no verdict at all.

    The pack below carries live assertions and a weight, so any implementation
    that turned the failure into an absent hash score would return the full,
    unweighted jsonpath score (``0.5``) — a pass-shaped number for an
    infrastructure failure.
    """

    def test_replay_failure_raises_instead_of_scoring(self, tmp_path):
        with pytest.raises(GoldenReplayError, match="golden actions"):
            _grade(
                {
                    "jsonpaths": _HALF_SATISFIED_JSONPATHS,
                    "hash": {
                        "enabled": True,
                        "golden_actions": [{"name": "close_widget"}],
                        "weight": 0.6,
                    },
                },
                task_dir=tmp_path,
                task_initial_state=InitialStateConfig(json_db="absent_initial_state.json"),
                task_mcp_server="mcp_server.py",
            )

    def test_missing_initial_state_declaration_raises(self):
        with pytest.raises(GoldenReplayError, match="initial_state.json_db"):
            _grade(
                {
                    "jsonpaths": _HALF_SATISFIED_JSONPATHS,
                    "hash": {
                        "enabled": True,
                        "golden_actions": [{"name": "close_widget"}],
                        "weight": 0.6,
                    },
                },
                task_dir=Path("."),
                task_initial_state=InitialStateConfig(),
                task_mcp_server="mcp_server.py",
            )
