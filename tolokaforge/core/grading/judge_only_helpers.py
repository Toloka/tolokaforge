"""Pure helper the ``judge_only`` factory delegates its per-trial dispatch to.

``run_judge_only_for_trajectory`` runs :class:`LLMJudge` against a trajectory
with the constrained-input shape ``judge_only`` grades under — no
``db_reader``, no ``kb_search``, no ``workspace_dir``, no ``state_diff``, no
``extra_read_tools``. That constrained shape is the same one the
runner-side composite path collapses to when a task declares
``grading.grading_method = "composite"`` (or omits it) with
``weights: {llm_judge: 1.0}`` and no state / transcript-rule / trace-check /
custom-check / KB surface — so both paths route through this helper's
:class:`LLMJudge` invocation with byte-identical inputs, and the parity
gate at
``tests/canonical/test_judge_only_composite_llm_judge_only_parity.py``
locks that convergence.

Callers resolve their own ``spec``-level fail-loud contract (a task
without ``grading.llm_judge``, a run without ``models.judge``) before
calling this helper: the helper receives already-non-``None``
``llm_judge_config`` and ``judge_model_config`` and raises
:class:`GradingFailedError` only on the failure modes the judge run
itself surfaces (an ``ERRORED`` :class:`JudgeResult`). An empty
trajectory returns ``None`` — the same "no evidence to judge against"
answer the :class:`JudgeBackedTrialGrader` seam logs at the caller.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from tolokaforge.core.grading.judge import LLMJudge
from tolokaforge.core.grading.judge_result import JudgeStatus as JudgeRunStatus
from tolokaforge.core.grading.replay import build_replay_grade
from tolokaforge.core.grading.transcript_wire import (
    encode_transcript_wire,
    split_leading_system_message,
)

if TYPE_CHECKING:
    from tolokaforge.core.llm.client import LLMClient
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import (
        Grade,
        LLMJudgeConfig,
        ModelConfig,
        Trajectory,
    )
    from tolokaforge.core.models.run_config import JudgeGraderConfig

__all__ = [
    "run_judge_only_for_trajectory",
]


def run_judge_only_for_trajectory(
    *,
    trial_id: str,
    llm_judge_config: LLMJudgeConfig,
    judge_model_config: ModelConfig,
    trajectory: Trajectory,
    agent_system_prompt: str,
    override: JudgeGraderConfig | None,
    llm_client: LLMClient | None,
    logger: StructuredLogger,
) -> Grade | None:
    """Run :class:`LLMJudge` over ``trajectory`` and return the persistable Grade.

    Encodes the trajectory through the same
    :func:`encode_transcript_wire` + :func:`split_leading_system_message`
    the replay path uses, so the transcript the judge sees is
    byte-identical to the runner-side composite grading path.

    Task customization is the base; a run-level ``override`` wins
    per-field when the field is not ``None`` (see :class:`JudgeGraderConfig`
    — the override cannot express "reset to library default").

    ``llm_client`` is a test-only injection point that lands on
    :class:`LLMJudge`; production callers pass ``None`` and the judge
    builds its own client. Fail-loud contract: an
    :attr:`JudgeStatus.ERRORED` verdict raises
    :class:`~tolokaforge.core.trial_grader.GradingFailedError` — the
    trial is ungradeable rather than an agent failure. Only a
    ``COMPLETED`` verdict is translated through
    :func:`build_replay_grade` into a persistable :class:`Grade`.
    """
    from tolokaforge.core.trial_grader import GradingFailedError

    wire = encode_transcript_wire(trajectory, agent_system_prompt)
    if wire is None:
        return None
    judge_agent_prompt, transcript = split_leading_system_message(json.loads(wire))

    customization = llm_judge_config.customization
    base_disable_kb = customization.disable_knowledge_search if customization else None
    base_custom_prompt = customization.system_prompt if customization else None
    base_include_agent = customization.include_agent_system_prompt if customization else None

    disable_kb_resolved = (
        override.disable_knowledge_search
        if override is not None and override.disable_knowledge_search is not None
        else base_disable_kb
    )
    custom_prompt = (
        override.custom_system_prompt
        if override is not None and override.custom_system_prompt is not None
        else base_custom_prompt
    )
    include_agent_resolved = (
        override.include_agent_system_prompt
        if override is not None and override.include_agent_system_prompt is not None
        else base_include_agent
    )
    judge = LLMJudge(
        judge_model_config,
        disable_knowledge_search=bool(disable_kb_resolved),
        custom_system_prompt=custom_prompt,
        include_agent_system_prompt=(
            include_agent_resolved if include_agent_resolved is not None else True
        ),
        llm_client=llm_client,
        logger=logger,
    )
    result = judge.run(
        rubric=llm_judge_config.rubric,
        agent_system_prompt=judge_agent_prompt,
        transcript=transcript,
    )
    if result.status is JudgeRunStatus.ERRORED:
        raise GradingFailedError(
            f"judge_only grader errored for trial {trial_id!r}: "
            f"{result.reasons or 'no reason recorded'}"
        )
    return build_replay_grade(result)
