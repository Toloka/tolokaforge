"""Core's hash checks apply a declared comparison view first, and let its errors through.

``check_hash`` and ``check_hash_against_golden_replay`` run both full states through
:func:`~tolokaforge.core.grading.pre_hash.view_the_pair` before core's own masks and
digest. A view that cannot be computed is no verdict: it propagates, where every other
hashing error still scores ``0.0``. The golden replay mutates the initial state it
loads, so the view reads a load of its own.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.grading.comparison_view import (
    ComparisonViewCollision,
    ComparisonViewConfig,
    ComparisonViewError,
)
from tolokaforge.core.grading.pre_hash import PreHashDeclaration, view_the_pair
from tolokaforge.core.grading.state_checks import StateChecker
from tolokaforge.core.models import GradingConfig, InitialStateConfig, Trajectory

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[3]
_SHOP = _REPO / "tests" / "data" / "tasks" / "shop_orders_02"

_DOCUMENTS = ComparisonViewConfig.model_validate(
    {
        "version": 1,
        "rules": [
            {
                "kind": "normalize_ids",
                "table": "documents",
                "key": ["source_id"],
                "references": [{"table": "corrections", "field": "document_ref"}],
            }
        ],
    }
)
_INITIAL = {"documents": [{"id": "D1", "source_id": "S1"}], "corrections": []}


def _filed(new_id: str, reason: str = "typo") -> dict[str, Any]:
    return {
        "documents": [{"id": "D1", "source_id": "S1"}, {"id": new_id, "source_id": "S2"}],
        "corrections": [{"id": "C1", "document_ref": new_id, "reason": reason}],
    }


def _check(trial: dict, golden: dict, **kwargs: Any):
    return StateChecker().check_hash(
        trial,
        expected_state=golden,
        comparison_view=_DOCUMENTS,
        initial_state=copy.deepcopy(_INITIAL),
        unstable_fields=["documents.id"],
        **kwargs,
    )


def test_a_pair_that_differs_in_a_generated_id_matches_through_the_view() -> None:
    score, reason, record = _check(_filed("D3"), _filed("D2"))
    assert score == 1.0, reason
    assert record is not None and record.view_diff is None
    unviewed, _, none = StateChecker().check_hash(
        _filed("D3"), expected_state=_filed("D2"), unstable_fields=["documents.id"]
    )
    assert unviewed == 0.0 and none is None, "the control: without the view it is a mismatch"


def test_a_mismatch_names_the_view_diff() -> None:
    score, reason, record = _check(_filed("D3", reason="wrong"), _filed("D2"))
    assert score == 0.0
    assert record is not None and record.view_diff is not None
    assert reason.startswith("State hash mismatch: expected ")
    assert f"Comparison view: {record.view_diff.summary}" in reason


def test_a_trial_collision_scores_zero_with_the_collision_as_the_reason() -> None:
    trial = _filed("D3")
    trial["documents"].append({"id": "D4", "source_id": "S2"})
    score, reason, record = _check(trial, _filed("D2"))
    assert score == 0.0
    assert reason.startswith("Comparison view: the trial's state cannot be viewed")
    assert record is not None and set(record.trial_error.ids) == {"D3", "D4"}


def test_a_trial_record_no_rule_can_read_scores_zero_with_the_error_as_the_reason() -> None:
    """Once the golden's view succeeded, what the trial side cannot view is the trial's."""
    trial = _filed("D3")
    del trial["documents"][1]["source_id"]
    score, reason, record = _check(trial, _filed("D2"))
    assert score == 0.0
    assert reason.startswith("Comparison view: the trial's state cannot be viewed — ")
    assert "ComparisonViewError: " in reason and "lacks the key field" in reason
    assert record is not None and record.trial_error is not None
    assert record.trial_error.error == "ComparisonViewError" and record.trial_error.ids == []


def test_a_golden_view_error_propagates_instead_of_scoring_zero() -> None:
    golden = _filed("D2")
    golden["documents"].append({"id": "D5", "source_id": "S2"})
    with pytest.raises(ComparisonViewCollision):
        _check(_filed("D3"), golden)
    golden = _filed("D2")
    del golden["documents"][1]["source_id"]
    with pytest.raises(ComparisonViewError, match="lacks the key field"):
        _check(_filed("D3"), golden)


def test_every_other_hashing_error_still_scores_zero() -> None:
    """No behaviour change outside the view: the catch-all is kept for the rest."""
    score, reason, record = StateChecker().check_hash(
        {"t": [{"id": 1}]},
        expected_state={"t": [{"id": 1}]},
        numeric_string_fields=42,  # type: ignore[arg-type]
    )
    assert score == 0.0 and reason.startswith("Error computing hash") and record is None


def test_a_view_needs_the_expected_state_not_a_stored_digest() -> None:
    with pytest.raises(ValueError, match="needs the expected state"):
        StateChecker().check_hash(_filed("D3"), "0" * 64, comparison_view=_DOCUMENTS)


# ---------------------------------------------------------------------------
# Golden replay: the view reads an initial state the replay never touched
# ---------------------------------------------------------------------------

_ORDERS_BY_CONTENT = ComparisonViewConfig.model_validate(
    {
        "version": 1,
        "rules": [{"kind": "normalize_ids", "table": "orders", "key": ["customer_id", "total"]}],
    }
)


def _golden_actions() -> list[dict[str, Any]]:
    grading = yaml.safe_load((_SHOP / "grading.yaml").read_text())
    return grading["state_checks"]["hash"]["golden_actions"]


def _replayed_by_hand() -> dict[str, Any]:
    """What the pack's golden path leaves, built here by the pack's own tools."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("shop_mcp_server", _SHOP / "mcp_server.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    data = json.loads((_SHOP / "initial_state.json").read_text())
    for action in _golden_actions():
        module.TOOLS[action["name"]].invoke(data=data, **action["kwargs"])
    return data


def test_the_view_under_golden_replay_reads_a_fresh_initial_state() -> None:
    """The replay appends ``O-001`` to the state it loaded; the view must not see it.

    The trial placed the same order under another id. Read off a fresh initial state,
    both orders are new and re-key alike; read off the replay's state, the golden's
    order would count as seeded, keep its id, and the pair would mismatch.
    """
    trial = _replayed_by_hand()
    assert [order["id"] for order in trial["orders"]] == ["O-001"]
    trial["orders"][0]["id"] = "O-009"

    score, reason, _, replay, record = StateChecker().check_hash_against_golden_replay(
        db_state=copy.deepcopy(trial),
        golden_actions=_golden_actions(),
        task_dir=_SHOP,
        initial_state_path="initial_state.json",
        mcp_server_path="mcp_server.py",
        task_domain="shop",
        comparison_view=_ORDERS_BY_CONTENT,
    )
    assert score == 1.0, reason
    assert not replay.failures
    assert record is not None and record.view_diff is None

    replayed = _replayed_by_hand()
    stale = view_the_pair(
        trial,
        replayed,
        initial=replayed,
        declaration=PreHashDeclaration(view=_ORDERS_BY_CONTENT),
    )
    assert stale.trial != stale.golden, "the copy is what the verdict rests on"


# ---------------------------------------------------------------------------
# GradingEngine: the record on the grade, an error as no grade
# ---------------------------------------------------------------------------


def _engine(view: ComparisonViewConfig) -> GradingEngine:
    return GradingEngine(
        GradingConfig(
            combine={"method": "weighted", "weights": {"state_checks": 1.0}, "pass_threshold": 0.5},
            state_checks={
                "hash": {"enabled": True, "expect_initial_state": True},
                "comparison_view": view.model_dump(mode="json"),
            },
        ),
        task_initial_state=InitialStateConfig(json_db=copy.deepcopy(_INITIAL)),
    )


def _trajectory() -> Trajectory:
    return Trajectory(
        task_id="view",
        trial_index=0,
        start_ts="2026-01-01T00:00:00Z",
        end_ts="2026-01-01T00:00:00Z",
        messages=[],
    )


_ALL_DOCUMENTS = ComparisonViewConfig.model_validate(
    {
        "version": 1,
        "rules": [
            {
                "kind": "normalize_ids",
                "table": "documents",
                "key": ["source_id"],
                "references": [{"table": "corrections", "field": "document_ref"}],
                "scope": "all",
            }
        ],
    }
)


def test_the_engine_records_the_view_on_the_grade() -> None:
    renamed = {"documents": [{"id": "D9", "source_id": "S1"}], "corrections": []}
    grade = _engine(_ALL_DOCUMENTS).grade_trajectory(_trajectory(), {"db": renamed})
    assert grade.components.state_checks == 1.0, grade.reasons
    assert grade.comparison_view is not None
    assert grade.comparison_view["view_diff"] is None
    assert grade.comparison_view["golden"]["config_sha256"] == _ALL_DOCUMENTS.config_sha256()


def test_the_engine_fails_a_trial_whose_own_state_cannot_be_viewed() -> None:
    broken = {"documents": [{"id": "D9"}], "corrections": []}
    grade = _engine(_ALL_DOCUMENTS).grade_trajectory(_trajectory(), {"db": broken})
    assert grade.components.state_checks == 0.0
    assert "Comparison view: the trial's state cannot be viewed" in grade.reasons
    assert grade.comparison_view is not None
    assert grade.comparison_view["trial_error"]["error"] == "ComparisonViewError"


def test_the_engine_leaves_a_trial_ungraded_when_the_expected_side_cannot_be_viewed() -> None:
    """``expect_initial_state``: the expected side is the seeded state, viewed first."""
    engine = GradingEngine(
        GradingConfig(
            combine={"method": "weighted", "weights": {"state_checks": 1.0}, "pass_threshold": 0.5},
            state_checks={
                "hash": {"enabled": True, "expect_initial_state": True},
                "comparison_view": _ALL_DOCUMENTS.model_dump(mode="json"),
            },
        ),
        task_initial_state=InitialStateConfig(
            json_db={"documents": [{"id": "D1"}], "corrections": []}
        ),
    )
    with pytest.raises(ComparisonViewError, match="lacks the key field"):
        engine.grade_trajectory(_trajectory(), {"db": copy.deepcopy(_INITIAL)})


def test_importing_the_core_hash_checks_does_not_pull_the_runner_stack() -> None:
    """The view's composition is imported where a view is declared, as before it existed."""
    probe = (
        "import sys, tolokaforge.core.grading.state_checks; "
        "print(sorted(m for m in ('grpc', 'tolokaforge.runner.models', "
        "'tolokaforge.core.grading.pre_hash') if m in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == "[]", out
