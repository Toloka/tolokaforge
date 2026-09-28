"""Unit tests for the Inspect -> tolokaforge result mapping.

Inputs are duck-typed stand-ins for Inspect objects, so these run without an
inspect_ai eval.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from tolokaforge_adapter_inspect_ai import normalize

from tolokaforge.core.models.trajectory import MessageRole

pytestmark = pytest.mark.unit


def _score(value, explanation=""):
    return SimpleNamespace(value=value, explanation=explanation, reason="")


def _sample(value, messages=None):
    return SimpleNamespace(scores={"includes": _score(value)}, messages=messages or [])


def _log(status, sample_values):
    return SimpleNamespace(status=status, samples=[_sample(v) for v in sample_values])


@pytest.mark.parametrize(
    ("value", "expected_pass", "expected_score"),
    [("C", True, 1.0), ("I", False, 0.0), ("P", False, 0.5), ("N", False, 0.0)],
)
def test_letter_values_map_to_pass_and_score(value, expected_pass, expected_score):
    grade = normalize.sample_grade(_sample(value))
    assert grade.binary_pass is expected_pass
    assert grade.score == expected_score
    assert grade.components.custom_checks == expected_score


def test_numeric_value_passes_through():
    grade = normalize.sample_grade(_sample(0.75))
    assert grade.score == 0.75
    assert grade.binary_pass is False


def test_missing_score_is_a_fail():
    grade = normalize.sample_grade(SimpleNamespace(scores={}, messages=[]))
    assert grade.binary_pass is False
    assert grade.score == 0.0
    assert "no score" in grade.reasons


def test_run_grade_aggregates_mean_and_threshold():
    grade = normalize.run_grade(_log("success", ["C", "I"]))
    assert grade.score == 0.5
    assert grade.binary_pass is False  # default threshold is 1.0

    all_correct = normalize.run_grade(_log("success", ["C", "C"]))
    assert all_correct.score == 1.0
    assert all_correct.binary_pass is True


def test_run_grade_no_samples():
    grade = normalize.run_grade(SimpleNamespace(status="error", samples=[]))
    assert grade.binary_pass is False
    assert "no scored samples" in grade.reasons


def test_reward_from_log_is_mean_score():
    assert normalize.reward_from_log(_log("success", ["C", "I"])) == 0.5


def test_trajectory_maps_roles_and_tool_calls():
    messages = [
        SimpleNamespace(role="system", text="be terse", tool_calls=None, tool_call_id=None),
        SimpleNamespace(role="user", text="hi", tool_calls=None, tool_call_id=None),
        SimpleNamespace(
            role="assistant",
            text="calling",
            tool_calls=[SimpleNamespace(id="tc1", function="bash", arguments={"cmd": "ls"})],
            tool_call_id=None,
        ),
        SimpleNamespace(role="tool", text="file.txt", tool_calls=None, tool_call_id="tc1"),
    ]
    sample = SimpleNamespace(scores={"s": _score("C")}, messages=messages)

    traj = normalize.sample_trajectory(sample, task_id="poc_smoke")

    assert traj.task_id == "poc_smoke"
    assert [m.role for m in traj.messages] == [
        MessageRole.SYSTEM,
        MessageRole.USER,
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
    ]
    call = traj.messages[2].tool_calls[0]
    assert (call.id, call.name, call.arguments) == ("tc1", "bash", {"cmd": "ls"})
    assert traj.messages[3].tool_call_id == "tc1"
    assert traj.grade is not None and traj.grade.binary_pass is True
