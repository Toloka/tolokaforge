"""``TestExecutionGraderKind`` — outcome-cell coverage.

Locks every observable outcome cell of the reference-suite kind:

- **Happy** (rc=0, reward parseable) → ``Grade(binary_pass, score=reward)``
  with the ``"test-execution reward: {r:.4f}\\n\\ntest output (truncated):\\n{out}"``
  reasons format.
- **rc≠0 + reward present** → scored by reward. The kind does NOT gate on
  ``exit_code``.
- **Reward absent** — the servicer's shell fallback produces ``b"0.0\\n"``;
  the kind uses the same "test-execution reward" reasons format (NOT
  "execution failed").
- **Reward parse error** — ``float(...)`` raises ``ValueError``; the kind
  falls back to ``reward=0.0`` and the same reasons format.
- **Reward clamped** — outside ``[0.0, 1.0]`` clamps to the bound.
- **Tool absent** — the substrate reports ``tool_absent=True``; the kind
  raises :class:`GraderKindRefusedError` with the substrate's message.
- **Script exec error** — the substrate reports ``script_exec_error``;
  the suite never ran, so the kind raises :class:`GraderKindRefusedError`
  with ``"test.sh execution failed: {msg}"`` rather than reporting a score
  for tests nobody executed.
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
    TestExecutionKindConfig,
)
from tolokaforge.core.grading.substrate import RunTestSuiteResult
from tolokaforge.runner.models import RunnerGradingConfig

pytestmark = pytest.mark.unit


class _ScriptedSubstrate:
    """Records the ``run_test_suite`` kwargs; returns a scripted result."""

    def __init__(self, result: RunTestSuiteResult, exec_result: tuple[int, str] = (0, "")) -> None:
        self._result = result
        self._exec_result = exec_result
        self.calls: list[dict[str, Any]] = []
        self.materialise_calls: list[dict[str, Any]] = []
        self.exec_calls: list[dict[str, Any]] = []
        self.events: list[str] = []

    def exec_in_service(
        self,
        *,
        service: str | None,
        command: str,
        timeout_s: float,
    ) -> tuple[int, str]:
        self.exec_calls.append({"service": service, "command": command, "timeout_s": timeout_s})
        self.events.append(f"collect:{service}")
        return self._exec_result

    def run_test_suite(
        self,
        *,
        script_path: str,
        reward_path: str,
        timeout_s: float,
        reward_read_timeout_s: float,
        service: str | None = None,
        user: str | None = None,
        require_reward: bool = False,
    ) -> RunTestSuiteResult:
        self.calls.append(
            {
                "script_path": script_path,
                "reward_path": reward_path,
                "timeout_s": timeout_s,
                "reward_read_timeout_s": reward_read_timeout_s,
                "service": service,
                "user": user,
                "require_reward": require_reward,
            }
        )
        self.events.append("run")
        return self._result

    def materialise_path(
        self,
        *,
        src_service: str | None,
        dst_service: str,
        path: str,
        exclude: Any = (),
    ) -> None:
        self.materialise_calls.append(
            {
                "src_service": src_service,
                "dst_service": dst_service,
                "path": path,
                "exclude": list(exclude),
            }
        )
        self.events.append(f"materialise:{path}")


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


def test_happy_path_rc_zero_with_reward_parses_and_matches_reasons_format() -> None:
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
    """The kind must not gate on ``returncode``. A ``rc≠0`` script that
    wrote a valid reward.txt is scored by the reward. This test WOULD
    fail on an implementation that gated on ``exit_code``."""
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


def test_script_exec_error_refuses_rather_than_scoring_the_unrun_suite() -> None:
    """A populated ``script_exec_error`` means the suite never ran, so the kind
    produces no grade at all.

    The refusal message is ``f"test.sh execution failed: {msg}"`` — NOT the
    "test-execution reward" reasons format. ``script_exec_error`` carries
    ``str(exception)`` verbatim (no class-name prefix) so the message matches
    the ``{e}`` interpolation shape the wire consumer expects.
    """
    substrate = _ScriptedSubstrate(
        _result(script_exec_error="Command 'bash test.sh' timed out after 120.0 seconds"),
    )

    with pytest.raises(GraderKindRefusedError) as exc_info:
        _evaluate(substrate)

    assert exc_info.value.reason == (
        "test.sh execution failed: Command 'bash test.sh' timed out after 120.0 seconds"
    )


def test_a_verifier_timeout_never_produces_a_custom_checks_zero() -> None:
    """Regression for the measurement defect: a verifier the subprocess killed
    used to be recorded as ``custom_checks: 0.0``, which asserts the tests ran
    and every check failed. Nothing ran, so there is no component to carry a
    number and no ``Grade`` to carry a score."""
    substrate = _ScriptedSubstrate(
        _result(
            exit_code=-1,
            reward_bytes=b"",
            stdout="",
            script_exec_error="Command 'bash test.sh' timed out after 120.0 seconds",
        ),
    )

    with pytest.raises(GraderKindRefusedError) as exc_info:
        _evaluate(substrate)

    assert "timed out after 120.0 seconds" in exc_info.value.reason


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
        "service": None,
        "user": None,
        "require_reward": False,
    }

    with pytest.raises(ValidationError):
        _evaluate(_ScriptedSubstrate(_result()), kind_config={"unknown_key": "x"})


def test_kind_config_defaults_map_to_standard_verifier_paths() -> None:
    substrate = _ScriptedSubstrate(_result(reward_bytes=b"0.5\n"))
    _evaluate(substrate, kind_config=None)
    assert substrate.calls[-1] == {
        "script_path": "/tests/test.sh",
        "reward_path": "/logs/verifier/reward.txt",
        "timeout_s": 300.0,
        "reward_read_timeout_s": 10.0,
        "service": None,
        # Unset by default, so a pack that says nothing about users keeps
        # inheriting the container's USER and the `|| echo 0.0` reward read.
        "user": None,
        "require_reward": False,
    }


def test_stdout_truncated_at_configured_char_cap() -> None:
    """The kind truncates ``result.stdout`` for the reasons string at
    ``output_truncation_chars``. The default matches the servicer's 65_536
    wire cap, so a verifier traceback the runner shipped is quoted whole in
    the grade rather than cut mid-way."""
    big = "X" * 70_000
    substrate = _ScriptedSubstrate(_result(reward_bytes=b"1.0\n", stdout=big))
    grade = _evaluate(substrate)

    assert grade is not None
    output_marker = "test output (truncated):\n"
    idx = grade.reasons.index(output_marker) + len(output_marker)
    tail = grade.reasons[idx:]
    assert len(tail) == 65_536
    assert tail == big[:65_536]


def test_service_and_artifacts_parse_into_config() -> None:
    cfg = TestExecutionKindConfig(
        service="verifier",
        artifacts=[
            {"source": "/app/cracker.py"},
            {"source": "/srv/data", "exclude": ["__pycache__", "*.pyc"]},
        ],
    )
    assert cfg.service == "verifier"
    assert [a.source for a in cfg.artifacts] == ["/app/cracker.py", "/srv/data"]
    assert cfg.artifacts[1].exclude == ["__pycache__", "*.pyc"]


def test_artifacts_without_service_is_rejected() -> None:
    with pytest.raises(ValidationError):
        TestExecutionKindConfig(artifacts=[{"source": "/app/cracker.py"}])


def test_artifact_spec_forbids_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        TestExecutionKindConfig(service="verifier", artifacts=[{"src": "/app"}])


def test_separate_verifier_orchestration_materialises_then_runs_in_service() -> None:
    substrate = _ScriptedSubstrate(_result(reward_bytes=b"1\n"))
    grade = _evaluate(
        substrate,
        kind_config={
            "service": "verifier",
            "artifacts": [
                {"source": "/app/cracker.py"},
                {"source": "/app/requirements.txt", "exclude": ["*.pyc"]},
            ],
        },
    )
    # Every declared artifact is re-materialised into the verifier first, from the
    # agent (src None), then the suite runs in the verifier's container.
    assert [c["path"] for c in substrate.materialise_calls] == [
        "/app/cracker.py",
        "/app/requirements.txt",
    ]
    assert all(c["src_service"] is None for c in substrate.materialise_calls)
    assert all(c["dst_service"] == "verifier" for c in substrate.materialise_calls)
    assert substrate.materialise_calls[1]["exclude"] == ["*.pyc"]
    assert substrate.calls[-1]["service"] == "verifier"
    assert grade is not None and grade.binary_pass is True


def test_collect_runs_before_materialise_before_run() -> None:
    substrate = _ScriptedSubstrate(_result(reward_bytes=b"1\n"))
    _evaluate(
        substrate,
        kind_config={
            "service": "verifier",
            "collect": [{"service": "postgres", "command": "pg_dumpall > /tmp/d"}],
            "artifacts": [{"source": "/tmp/d", "src_service": "postgres"}],
        },
    )
    assert substrate.events == ["collect:postgres", "materialise:/tmp/d", "run"]
    assert substrate.exec_calls[0]["service"] == "postgres"
    assert substrate.materialise_calls[0]["src_service"] == "postgres"
    assert substrate.calls[-1]["service"] == "verifier"


def test_collect_without_service_runs_in_agent() -> None:
    substrate = _ScriptedSubstrate(_result(reward_bytes=b"1\n"))
    _evaluate(
        substrate,
        kind_config={
            "service": "verifier",
            "collect": [{"command": "find /etc -name '*.conf' > /tmp/p"}],
            "artifacts": [{"source": "/tmp/p"}],
        },
    )
    assert substrate.exec_calls[0]["service"] is None
    assert substrate.materialise_calls[0]["src_service"] is None


def test_nonzero_collect_is_refused() -> None:
    substrate = _ScriptedSubstrate(_result(reward_bytes=b"1\n"), exec_result=(1, "boom"))
    with pytest.raises(GraderKindRefusedError):
        _evaluate(
            substrate,
            kind_config={
                "service": "verifier",
                "collect": [{"service": "postgres", "command": "pg_dumpall > /tmp/d"}],
            },
        )
    assert substrate.materialise_calls == []  # never reached materialise after a failed collect


def test_collect_requires_a_verifier_service() -> None:
    with pytest.raises(ValidationError):
        TestExecutionKindConfig(collect=[{"command": "snapshot"}])


# ---------------------------------------------------------------------------
# Verifier user + require_reward
#
# Regression cover for the 2026-10-30 terminal-bench finding: a pack whose
# verifier must run as root (``task.toml [verifier] user = "root"``) had its
# suite executed as the agent's unprivileged user, so the reward write to a
# root-only directory failed with EACCES. The empty reward was then folded to
# ``0.0`` by the servicer's ``|| echo 0.0`` fallback and this kind's parse
# guard, making a broken verifier byte-identical to a model that scored zero.
# 77 of 100 tasks on the TBench Arena board were unscoreable and nothing in
# the artifacts said so.
#
# Both knobs are opt-in: ``user`` defaults to ``None`` and ``require_reward``
# to ``False``, so every adapter that does not set them is unaffected.
# ---------------------------------------------------------------------------


def test_verifier_user_reaches_the_substrate() -> None:
    """``user`` rides kind_config to the substrate, which execs as that user."""
    substrate = _ScriptedSubstrate(_result(reward_bytes=b"1.0\n"))
    _evaluate(substrate, kind_config={"user": "root"})

    assert substrate.calls[0]["user"] == "root"


def test_user_and_require_reward_default_to_the_pre_fix_behaviour() -> None:
    """Unset, the substrate is called exactly as it was before this change.

    This is the whole no-regression guarantee for every other adapter: an
    adapter that names neither key sends ``user=None`` / ``require_reward=False``
    and keeps the ``|| echo 0.0`` reward read.
    """
    substrate = _ScriptedSubstrate(_result(reward_bytes=b"0.5\n"))
    _evaluate(substrate)

    assert substrate.calls[0]["user"] is None
    assert substrate.calls[0]["require_reward"] is False


def test_require_reward_refuses_a_trial_whose_verifier_wrote_no_reward() -> None:
    """An empty reward is ungradeable, not a zero — the defect this prevents.

    With ``require_reward`` the servicer drops its ``|| echo 0.0`` fallback, so
    a verifier that could not write leaves ``reward_bytes`` empty. Scoring that
    0.0 is what hid the permission failure across 804 trials; refusing it
    surfaces the trial the same way ``script_exec_error`` already does.
    """
    substrate = _ScriptedSubstrate(
        _result(exit_code=0, reward_bytes=b"", stdout="reward: 9/14"),
    )

    with pytest.raises(GraderKindRefusedError) as excinfo:
        _evaluate(substrate, kind_config={"require_reward": True})

    assert "reward" in str(excinfo.value).lower()


def test_require_reward_refuses_an_unparseable_reward() -> None:
    """Garbage in the reward file is ungradeable too, not a zero."""
    substrate = _ScriptedSubstrate(_result(reward_bytes=b"Permission denied\n"))

    with pytest.raises(GraderKindRefusedError):
        _evaluate(substrate, kind_config={"require_reward": True})


def test_without_require_reward_an_empty_reward_still_scores_zero() -> None:
    """The pre-fix fold is preserved for callers that do not opt in."""
    substrate = _ScriptedSubstrate(_result(reward_bytes=b""))
    grade = _evaluate(substrate)

    assert grade is not None
    assert grade.score == pytest.approx(0.0)
    assert grade.binary_pass is False
