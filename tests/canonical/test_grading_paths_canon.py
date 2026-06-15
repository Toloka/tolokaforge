"""Golden coverage for the deterministic grading paths, end-to-end.

The hash path is already pinned by ``test_golden_trajectory_grading_canon.py``.
This module pins the *other* deterministic paths through the full
``GradingEngine.grade_trajectory`` pipeline — the parts previously exercised only
at the component level — so a future grading refactor can prove it didn't change
verdicts:

- jsonpath partial-credit state checks (``satisfied / total``)
- transcript ``required_actions`` + ``communicate_info`` (tau2 evaluators, combined
  multiplicatively with the legacy transcript checker)
- the weighted-combine normalization across multiple live components
- a FAIL verdict (``binary_pass=False``)

Determinism notes (verified against the engine source):
- Scores use no time/random; hashing is canonical (sorted keys). Safe to pin.
- ``reasons`` strings are NOT pinned: they can embed Python set reprs
  (tool_expectations), glob ordering, or diff text whose order isn't guaranteed.
  We snapshot only ``binary_pass`` + ``score`` + ``components`` (the verdict), and
  round floats to dodge float-repr churn under exact snapshot equality.

No LLM, no network, no Docker: ``GradingEngine`` is built with no ``judge_model``
and no ``task_dir``, so the judge and custom-checks paths never run.
"""

from datetime import datetime, timezone

import pytest

from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.models import (
    GradingConfig,
    Message,
    MessageRole,
    ToolCall,
    Trajectory,
    TrialStatus,
)

pytestmark = [pytest.mark.canonical, pytest.mark.grading]

# Fixed timestamps — construction only; they do not feed grading.
_TS = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _trajectory(messages: list[Message]) -> Trajectory:
    return Trajectory(
        task_id="grading-paths",
        trial_index=0,
        start_ts=_TS,
        end_ts=_TS,
        status=TrialStatus.COMPLETED,
        messages=messages,
    )


def _assistant_with_tool(name: str, content: str = "") -> Message:
    return Message(
        role=MessageRole.ASSISTANT,
        content=content,
        tool_calls=[ToolCall(id=f"call_{name}", name=name, arguments={})],
    )


# --- Scenarios: each returns (grading_config_dict, trajectory, final_env_state) ---


def _scenario_jsonpath_partial():
    """2 of 4 jsonpath assertions satisfied -> state_checks = 0.5 (pass_threshold 0.5)."""
    config = {
        "combine": {"method": "weighted", "weights": {"state_checks": 1.0}, "pass_threshold": 0.5},
        "state_checks": {
            "jsonpaths": [
                {"path": "$.db.counter", "equals": 5, "description": "counter is 5"},
                {"path": "$.db.status", "equals": "done", "description": "status is done"},
                {"path": "$.db.counter", "equals": 999, "description": "wrong counter (fails)"},
                {"path": "$.db.missing", "equals": "x", "description": "absent path (fails)"},
            ],
        },
    }
    traj = _trajectory([Message(role=MessageRole.USER, content="go")])
    final_env_state = {"db": {"counter": 5, "status": "done"}}
    return config, traj, final_env_state


def _scenario_transcript_actions_and_info():
    """required_actions 1/2 found (0.5) x communicate_info 1/1 found (1.0) -> transcript 0.5."""
    config = {
        "combine": {
            "method": "weighted",
            "weights": {"transcript_rules": 1.0},
            "pass_threshold": 0.75,
        },
        "transcript_rules": {
            "required_actions": [
                {
                    "action_id": "a1",
                    "requestor": "assistant",
                    "name": "list_products",
                    "arguments": {},
                    "compare_args": [],
                },
                {
                    "action_id": "a2",
                    "requestor": "assistant",
                    "name": "place_order",
                    "arguments": {},
                    "compare_args": [],
                },
            ],
            "communicate_info": [{"info": "ORDER-001", "required": True}],
        },
    }
    traj = _trajectory(
        [
            Message(role=MessageRole.USER, content="buy it"),
            _assistant_with_tool("list_products"),  # matches a1; a2 (place_order) never called
            Message(role=MessageRole.ASSISTANT, content="All set — your order is ORDER-001."),
        ]
    )
    return config, traj, {}


def _scenario_weighted_combine():
    """state_checks=1.0 (w .6) + transcript=0.5 (w .4) -> (0.6 + 0.2)/1.0 = 0.8."""
    config = {
        "combine": {
            "method": "weighted",
            "weights": {"state_checks": 0.6, "transcript_rules": 0.4},
            "pass_threshold": 0.75,
        },
        "state_checks": {
            "jsonpaths": [
                {"path": "$.db.counter", "equals": 5, "description": "counter is 5"},
                {"path": "$.db.status", "equals": "done", "description": "status is done"},
            ],
        },
        "transcript_rules": {
            "required_actions": [
                {
                    "action_id": "a1",
                    "requestor": "assistant",
                    "name": "list_products",
                    "arguments": {},
                    "compare_args": [],
                },
                {
                    "action_id": "a2",
                    "requestor": "assistant",
                    "name": "place_order",
                    "arguments": {},
                    "compare_args": [],
                },
            ],
        },
    }
    traj = _trajectory(
        [
            Message(role=MessageRole.USER, content="go"),
            _assistant_with_tool("list_products"),  # 1 of 2 required actions
        ]
    )
    final_env_state = {"db": {"counter": 5, "status": "done"}}
    return config, traj, final_env_state


def _scenario_fail_hash_mismatch():
    """Wrong expected_state_hash -> state_checks 0.0 -> binary_pass False."""
    config = {
        "combine": {"method": "weighted", "weights": {"state_checks": 1.0}, "pass_threshold": 1.0},
        "state_checks": {
            "jsonpaths": [],
            "hash": {"enabled": True, "weight": 1.0, "expected_state_hash": "0" * 64},
        },
    }
    traj = _trajectory([Message(role=MessageRole.USER, content="go")])
    final_env_state = {"db": {"counter": 5, "status": "done"}}
    return config, traj, final_env_state


_SCENARIOS = {
    "jsonpath_partial": _scenario_jsonpath_partial,
    "transcript_actions_and_info": _scenario_transcript_actions_and_info,
    "weighted_combine": _scenario_weighted_combine,
    "fail_hash_mismatch": _scenario_fail_hash_mismatch,
}


def _verdict(grade) -> dict:
    """Deterministic, snapshot-safe slice of a Grade (no volatile reasons strings)."""

    def r(x):
        return round(x, 6) if isinstance(x, float) else x

    c = grade.components
    return {
        "binary_pass": grade.binary_pass,
        "score": r(grade.score),
        "components": {
            "state_checks": r(c.state_checks),
            "transcript_rules": r(c.transcript_rules),
            "llm_judge": r(c.llm_judge),
            "custom_checks": r(c.custom_checks),
        },
    }


@pytest.mark.parametrize("name", list(_SCENARIOS))
def test_grading_path_verdict_is_pinned(name, canon_snapshot):
    """Each deterministic grading path produces the pinned golden verdict."""
    config_dict, trajectory, final_env_state = _SCENARIOS[name]()
    grading_config = GradingConfig(**config_dict)

    # No judge_model, no task_dir => no LLM judge, no custom checks.
    grade = GradingEngine(grading_config=grading_config).grade_trajectory(
        trajectory, final_env_state
    )

    # Determinism: a second fresh engine yields an identical verdict.
    grade2 = GradingEngine(grading_config=grading_config).grade_trajectory(
        trajectory, final_env_state
    )
    assert _verdict(grade2) == _verdict(grade)

    snap = canon_snapshot("grading_paths")
    snap.assert_match(_verdict(grade), f"{name}.json")
