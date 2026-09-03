"""``TestExecutionGraderKind`` — outcome-cell byte-parity with the pre-move dispatch.

Locks every observable outcome cell of the reference-suite kind against
the runner-side pre-move :meth:`RunnerServiceImpl._grade_via_test_execution`
behaviour at ``tolokaforge/runner/service.py:2727-2808``:

- **Happy** (rc=0, reward parseable) → ``Grade(binary_pass, score=reward)``
  with the ``"test-execution reward: {r:.4f}\\n\\ntest output (truncated):\\n{out}"``
  reasons format byte-identical to pre-move.
- **rc≠0 + reward present** (regression check for r0's ``exit_code``
  gate — pre-move never inspects returncode) → scored by reward.
- **Reward absent** — the servicer's shell fallback produces ``b"0.0\\n"``;
  the kind uses the same "test-execution reward" reasons format (NOT
  "execution failed").
- **Reward parse error** — ``float(...)`` raises ``ValueError``; the kind
  falls back to ``reward=0.0`` and the same reasons format.
- **Reward clamped** — outside ``[0.0, 1.0]`` clamps to the bound.
- **Tool absent** — the substrate reports ``tool_absent=True``; the kind
  raises :class:`GraderKindRefusedError` with the substrate's message.
- **Script exec error** — the substrate reports ``script_exec_error``;
  the kind returns
  ``Grade(score=0.0, reasons="test.sh execution failed: {msg}")``
  byte-identical to pre-move at ``service.py:2777``.
- **``kind_config`` validation** — override reaches the substrate;
  ``extra="forbid"`` refuses unknown keys with ``ValidationError``.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from pydantic import ValidationError

from tolokaforge.core.grading.kinds import (
    GraderKindRefusedError,
    TestExecutionGraderKind,
)
from tolokaforge.core.grading.substrate import RunTestSuiteResult
from tolokaforge.runner.models import RunnerGradingConfig

pytestmark = pytest.mark.unit


class _ScriptedSubstrate:
    """Records the ``run_test_suite`` kwargs; returns a scripted result."""

    def __init__(self, result: RunTestSuiteResult) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def run_test_suite(
        self,
        *,
        script_path: str,
        reward_path: str,
        timeout_s: float,
        reward_read_timeout_s: float,
    ) -> RunTestSuiteResult:
        self.calls.append(
            {
                "script_path": script_path,
                "reward_path": reward_path,
                "timeout_s": timeout_s,
                "reward_read_timeout_s": reward_read_timeout_s,
            }
        )
        return self._result


def _result(
    *,
    exit_code: int = 0,
    reward_bytes: bytes = b"0.0\n",
    stdout: str = "",
    tool_absent: bool = False,
    tool_absent_reason: str = "",
    script_exec_error: str = "",
) -> RunTestSuiteResult:
    return RunTestSuiteResult(
        exit_code=exit_code,
        reward_bytes=reward_bytes,
        stdout=stdout,
        tool_absent=tool_absent,
        tool_absent_reason=tool_absent_reason,
        script_exec_error=script_exec_error,
    )


def _evaluate(
    substrate: _ScriptedSubstrate,
    *,
    kind_config: dict[str, Any] | None = None,
) -> Any:
    return TestExecutionGraderKind().evaluate(
        substrate=substrate,  # type: ignore[arg-type]
        task_config=RunnerGradingConfig(),
        kind_config=kind_config,
        trial_id="task:0",
        agent_tools={},
        logger=logging.getLogger("test-test-execution-kind"),  # type: ignore[arg-type]
    )


def test_happy_path_rc_zero_with_reward_parses_and_matches_pre_move_reasons() -> None:
    substrate = _ScriptedSubstrate(
        _result(exit_code=0, reward_bytes=b"0.85\n", stdout="PASS: 42/42 tests"),
    )
    grade = _evaluate(substrate)

    assert grade is not None
    assert grade.binary_pass is True
    assert grade.score == pytest.approx(0.85)
    assert grade.components.custom_checks == pytest.approx(0.85)
    assert grade.reasons == (
        "test-execution reward: 0.8500\n\ntest output (truncated):\nPASS: 42/42 tests"
    )


def test_rc_nonzero_with_reward_is_scored_by_reward_not_exit_code() -> None:
    """Regression lock — pre-move ``_grade_via_test_execution`` at
    ``service.py:2758-2808`` never inspects ``returncode``. A ``rc≠0``
    script that wrote a valid reward.txt is scored by the reward. This
    test WOULD fail on an implementation that gated on ``exit_code``."""
    substrate = _ScriptedSubstrate(
        _result(exit_code=1, reward_bytes=b"0.7\n", stdout="FAIL: 1/42 tests"),
    )
    grade = _evaluate(substrate)

    assert grade is not None
    assert grade.binary_pass is True
    assert grade.score == pytest.approx(0.7)
    assert grade.reasons.startswith("test-execution reward: 0.7000\n\ntest output")


def test_reward_absent_shell_fallback_uses_test_execution_reward_reasons() -> None:
    """The servicer's shell fallback (``|| echo 0.0``) yields
    ``b"0.0\\n"``; the kind uses the SAME "test-execution reward"
    reasons format as the happy path — NOT "execution failed"."""
    substrate = _ScriptedSubstrate(
        _result(exit_code=0, reward_bytes=b"0.0\n", stdout="run OK"),
    )
    grade = _evaluate(substrate)

    assert grade is not None
    assert grade.binary_pass is False
    assert grade.score == pytest.approx(0.0)
    assert grade.reasons.startswith("test-execution reward: 0.0000")
    assert "execution failed" not in grade.reasons


def test_reward_parse_error_falls_back_to_zero() -> None:
    substrate = _ScriptedSubstrate(
        _result(exit_code=0, reward_bytes=b"not a number\n", stdout=""),
    )
    grade = _evaluate(substrate)

    assert grade is not None
    assert grade.score == pytest.approx(0.0)
    assert grade.reasons.startswith("test-execution reward: 0.0000")


@pytest.mark.parametrize(
    ("reward_bytes", "expected_score"),
    [(b"2.5\n", 1.0), (b"-0.5\n", 0.0)],
)
def test_reward_clamped_to_unit_interval(reward_bytes: bytes, expected_score: float) -> None:
    substrate = _ScriptedSubstrate(_result(reward_bytes=reward_bytes))
    grade = _evaluate(substrate)

    assert grade is not None
    assert grade.score == pytest.approx(expected_score)


def test_tool_absent_raises_grader_kind_refused_with_reason() -> None:
    reason = (
        "test-execution grading was requested but no exec-capable env tool was found in this trial."
    )
    substrate = _ScriptedSubstrate(
        _result(tool_absent=True, tool_absent_reason=reason),
    )
    with pytest.raises(GraderKindRefusedError, match="no exec-capable env tool") as exc_info:
        _evaluate(substrate)
    assert exc_info.value.reason == reason


def test_script_exec_error_returns_grade_with_pre_move_reasons_format() -> None:
    """Byte-parity with pre-move at ``service.py:2777``:
    ``Grade(0.0, "test.sh execution failed: {e}")``. The reasons string
    is exactly this format — NOT the "test-execution reward" one."""
    substrate = _ScriptedSubstrate(
        _result(script_exec_error="TimeoutExpired: Command timed out after 300s"),
    )
    grade = _evaluate(substrate)

    assert grade is not None
    assert grade.binary_pass is False
    assert grade.score == pytest.approx(0.0)
    assert grade.components.custom_checks == pytest.approx(0.0)
    assert grade.reasons == (
        "test.sh execution failed: TimeoutExpired: Command timed out after 300s"
    )


def test_kind_config_overrides_reach_substrate_and_extra_keys_are_refused() -> None:
    substrate = _ScriptedSubstrate(_result(reward_bytes=b"0.5\n"))
    _evaluate(
        substrate,
        kind_config={
            "script_path": "/custom/test.sh",
            "reward_path": "/custom/reward.txt",
            "timeout_s": 60.0,
            "reward_read_timeout_s": 5.0,
            "output_truncation_chars": 100,
        },
    )
    assert substrate.calls[-1] == {
        "script_path": "/custom/test.sh",
        "reward_path": "/custom/reward.txt",
        "timeout_s": 60.0,
        "reward_read_timeout_s": 5.0,
    }

    with pytest.raises(ValidationError):
        _evaluate(_ScriptedSubstrate(_result()), kind_config={"unknown_key": "x"})


def test_kind_config_defaults_preserve_pre_move_paths() -> None:
    substrate = _ScriptedSubstrate(_result(reward_bytes=b"0.5\n"))
    _evaluate(substrate, kind_config=None)
    assert substrate.calls[-1] == {
        "script_path": "/tests/test.sh",
        "reward_path": "/logs/verifier/reward.txt",
        "timeout_s": 300.0,
        "reward_read_timeout_s": 10.0,
    }


def test_stdout_truncated_at_configured_char_cap() -> None:
    """The kind truncates ``result.stdout`` for the reasons string at
    ``output_truncation_chars`` (default 2000). The servicer already
    wire-caps at 65_536 bytes; the reasons-side cap is a separate
    guard for grade rendering readability."""
    big = "X" * 5000
    substrate = _ScriptedSubstrate(_result(reward_bytes=b"1.0\n", stdout=big))
    grade = _evaluate(substrate)

    assert grade is not None
    output_marker = "test output (truncated):\n"
    idx = grade.reasons.index(output_marker) + len(output_marker)
    tail = grade.reasons[idx:]
    assert len(tail) == 2000
    assert tail == big[:2000]
