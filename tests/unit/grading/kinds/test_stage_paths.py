"""The grading half of keeping the suite away from the agent.

The compose half (``tests-vault``, no ``/tests`` on the agent) is only half
the fix: the suite still has to reach the container it is run in, exactly
once, after the episode, and a staging that did not land must refuse the
trial rather than grade it zero. That is what these pin.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.kinds._protocol import GraderKindRefusedError
from tolokaforge.core.grading.kinds.test_execution import (
    TestExecutionGraderKind,
    TestExecutionKindConfig,
)
from tolokaforge.core.grading.substrate import RunTestSuiteResult


class FakeSubstrate:
    """Records the order of grading-substrate calls and what each was given."""

    def __init__(self, *, staged_present=True, reward=b"1.0\n"):
        self.calls: list[tuple] = []
        self._staged_present = staged_present
        self._reward = reward

    def materialise_path(self, *, src_service, dst_service, path, exclude=()):
        self.calls.append(("materialise_path", src_service, dst_service, path, tuple(exclude)))

    def exec_in_service(self, *, service, command, timeout_s):
        self.calls.append(("exec_in_service", service, command, timeout_s))
        if command.startswith("test -e"):
            return (0, "") if self._staged_present else (1, "")
        return (0, "")

    def run_test_suite(self, **kwargs):
        self.calls.append(("run_test_suite", kwargs["script_path"], kwargs["service"]))
        return RunTestSuiteResult(
            exit_code=0,
            reward_bytes=self._reward,
            stdout="2 passed",
            tool_absent=False,
            tool_absent_reason="",
            script_exec_error="",
        )


def _evaluate(substrate, cfg):
    return TestExecutionGraderKind().evaluate(
        substrate=substrate,
        task_config=None,
        kind_config=cfg,
        trial_id="t1",
        agent_tools={},
        logger=None,
    )


STAGE = {"stage_paths": [{"source": "/tests", "src_service": "tests-vault"}]}


def test_stage_paths_is_allowed_without_a_service():
    """The whole point: the destination may be the agent's own container."""
    cfg = TestExecutionKindConfig(**STAGE)
    assert cfg.service is None
    assert cfg.stage_paths[0].src_service == "tests-vault"


def test_artifacts_still_require_a_service():
    with pytest.raises(ValueError, match="separate-verifier shape"):
        TestExecutionKindConfig(artifacts=[{"source": "/app"}])


def test_stage_path_cannot_be_its_own_source():
    with pytest.raises(ValueError, match="cannot be staged from the container"):
        TestExecutionKindConfig(
            service="verifier",
            stage_paths=[{"source": "/tests", "src_service": "verifier"}],
        )


def test_stage_path_requires_a_source_service():
    with pytest.raises(ValueError):
        TestExecutionKindConfig(stage_paths=[{"source": "/tests"}])


def test_copy_happens_before_the_suite_runs_and_targets_the_agent():
    sub = FakeSubstrate()
    grade = _evaluate(sub, STAGE)
    kinds = [c[0] for c in sub.calls]
    assert kinds == ["materialise_path", "exec_in_service", "run_test_suite"], kinds
    assert sub.calls[0] == ("materialise_path", "tests-vault", None, "/tests", ())
    assert sub.calls[2] == ("run_test_suite", "/tests/test.sh", None)
    assert grade.score == 1.0 and grade.binary_pass


def test_a_missing_staged_suite_refuses_instead_of_grading_zero():
    sub = FakeSubstrate(staged_present=False, reward=b"0.0\n")
    with pytest.raises(GraderKindRefusedError) as err:
        _evaluate(sub, STAGE)
    msg = str(err.value)
    assert "test suite was not staged" in msg
    assert "ungradeable rather than a zero" in msg
    # It must refuse BEFORE running anything against an empty /tests.
    assert "run_test_suite" not in [c[0] for c in sub.calls]


def test_staging_runs_before_collect_hooks_and_artifacts():
    sub = FakeSubstrate()
    _evaluate(
        sub,
        {
            "service": "verifier",
            "stage_paths": [{"source": "/tests", "src_service": "tests-vault"}],
            "collect": [{"command": "pg_dumpall > /d.sql", "service": "db"}],
            "artifacts": [{"source": "/app"}],
        },
    )
    kinds = [c[0] for c in sub.calls]
    assert kinds == [
        "materialise_path",  # stage the suite
        "exec_in_service",  # probe it landed
        "exec_in_service",  # collect hook
        "materialise_path",  # artifact
        "run_test_suite",
    ], kinds


def test_no_stage_paths_leaves_the_old_behaviour_untouched():
    sub = FakeSubstrate()
    _evaluate(sub, {})
    assert [c[0] for c in sub.calls] == ["run_test_suite"]
