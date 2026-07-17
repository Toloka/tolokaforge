"""Live end-to-end test of ``tolokaforge rejudge`` over a recorded bundle.

Writes a small recorded trial bundle (an unambiguous, transcript-only refund
criterion), then re-judges it offline with a cheap real model and asserts the
replay reproduces the original per-criterion verdict. The judge loop is agentic
and non-deterministic even at temperature 0, so this is gated on a provider key
and asserts at the verdict level, not byte-level — the deterministic replay
orchestration is pinned by ``tests/unit/grading/test_replay.py`` with an injected
client.

Run with:
    scripts/with_env.sh uv run pytest tests/integration/test_rejudge_live.py \\
        -q -m integration
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tolokaforge.core.grading.judge import JudgeStatus
from tolokaforge.core.grading.replay import ReplayOutcomeStatus, run_replay_batch
from tolokaforge.core.models import (
    CriterionResult,
    Grade,
    GradeComponents,
    JudgeInputs,
    Message,
    MessageRole,
    Trajectory,
)
from tolokaforge.core.output.artifacts import FileArtifactWriter

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_api,
    pytest.mark.llm,
]


def _judge_model_ref() -> str | None:
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter/openai/gpt-4.1-mini"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai/gpt-4.1-mini"
    return None


_AGENT_PROMPT = (
    "You are a customer-service agent. Always quote the exact refund amount to the customer."
)
_RUBRIC = {
    "reference": "The correct refund for order o_1001 is $328.50.",
    "criteria": [
        {
            "id": "amount_quoted",
            "description": "The agent quoted the refund amount $328.50 to the customer.",
            "kind": "binary",
            "weight": 1.0,
        }
    ],
}


def _write_recorded_bundle(trial_dir: Path) -> None:
    """A transcript-only trial whose recorded judge verdict is met=true."""
    now = datetime.now(UTC)
    writer = FileArtifactWriter()
    writer.write_trajectory(
        trial_dir,
        Trajectory(
            task_id="refund_task",
            trial_index=0,
            start_ts=now,
            end_ts=now,
            messages=[
                Message(role=MessageRole.USER, content="Cancel order o_1001 and refund me."),
                Message(
                    role=MessageRole.ASSISTANT,
                    content="I've processed your refund of $328.50 to your original card.",
                ),
            ],
        ),
    )
    writer.write_prompts(trial_dir, _AGENT_PROMPT, "user-sim prompt")
    writer.write_task(
        trial_dir,
        {
            "task_id": "refund_task",
            "trial_index": 0,
            "grading_config": {"llm_judge": {"rubric": _RUBRIC}},
        },
    )
    writer.write_grade(
        trial_dir,
        Grade(
            binary_pass=True,
            score=1.0,
            components=GradeComponents(llm_judge=1.0),
            judge_status=JudgeStatus.COMPLETED,
            criterion_results=[
                CriterionResult(
                    id="amount_quoted",
                    met=True,
                    score=1.0,
                    justification="Reply quotes $328.50.",
                )
            ],
            judge_inputs=JudgeInputs(read_tools_offered=[]),
        ),
    )


@pytest.mark.skipif(_judge_model_ref() is None, reason="no provider key for the judge model")
def test_rejudge_reproduces_recorded_verdict_with_judge_only_spend(tmp_path: Path) -> None:
    source = tmp_path / "run"
    _write_recorded_bundle(source / "trials" / "refund_task" / "0")

    outcomes = run_replay_batch(
        source,
        replay_id="live",
        judge_model_override=_judge_model_ref(),
    )

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.status is ReplayOutcomeStatus.REPLAYED, outcome.reason
    result = outcome.result
    assert result is not None and result.status is JudgeStatus.COMPLETED

    # Reproduces the recorded per-criterion verdict (met=true) on an unambiguous,
    # transcript-only criterion.
    replay_verdicts = {c.id: c.met for c in result.criterion_results}
    assert replay_verdicts["amount_quoted"] is True

    # Judge-only spend: the judge ran its own LLM (no agent stage was re-run).
    assert result.usage.calls >= 1
    assert result.usage.cost_usd >= 0.0

    # The replay bundle landed under replays/ and the original is intact.
    assert (source / "replays" / "live" / "trials" / "refund_task" / "0" / "grade.yaml").exists()
