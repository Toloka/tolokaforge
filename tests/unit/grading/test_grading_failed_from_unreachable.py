"""A lost state source books the trial ungradeable and keeps what the judge spent."""

from __future__ import annotations

from tolokaforge.core.grading import judge_result
from tolokaforge.core.grading.grading_failed import GradingFailedError
from tolokaforge.core.grading.substrate import SubstrateUnreachableError
from tolokaforge.core.models import JudgeUsage


def test_the_judges_spend_rides_into_the_failure() -> None:
    exc = SubstrateUnreachableError("its DB state could not be read for grading")
    exc.judge_usage = judge_result.JudgeUsage(calls=9, prompt_tokens=1200, cost_usd=0.85)

    failure = GradingFailedError.from_unreachable("grading failed", exc)

    assert str(failure) == "grading failed"
    assert isinstance(failure.judge_usage, JudgeUsage)
    assert failure.judge_usage.calls == 9
    assert failure.judge_usage.cost_usd == 0.85
    assert failure.evidence()["judge_usage"]["calls"] == 9


def test_a_failure_no_judge_was_reading_through_carries_no_spend() -> None:
    failure = GradingFailedError.from_unreachable(
        "grading failed", SubstrateUnreachableError("lost")
    )

    assert failure.judge_usage is None
    assert failure.evidence()["judge_usage"] is None
