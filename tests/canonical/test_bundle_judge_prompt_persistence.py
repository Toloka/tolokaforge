"""``prompts.yaml`` records the effective judge system prompt verbatim.

Every trial bundle's ``prompts.yaml`` must carry a top-level ``judge_prompt``
key equal to ``_compose_judge_system_prompt(customization.system_prompt)`` for
the trial's effective ``LLMJudgeConfig`` — the exact prose the judge would have
graded under, byte-for-byte. A human analyst who opens the bundle sees which
contract graded it without trusting the current engine's constant; a bundle-
native replay reads the same string to reconstruct the judge's system prompt.

Scenarios locked here:
    (1) ``customization=None`` (or no customization block at all) → ``judge_prompt``
        is byte-for-byte the engine default ``_JUDGE_SYSTEM_PROMPT``.
    (2) ``customization.system_prompt = "STRICT-VIBE"`` → ``judge_prompt`` begins
        with ``"STRICT-VIBE"`` (the author's body verbatim) and ends with
        ``_JUDGE_MARKER_CONTRACT`` (the harness always appends the marker so
        ``submit_report`` validation is unbreakable).

The write path is driven end-to-end: ``InProcessConductor._write_artifacts``
runs against a real ``FileArtifactWriter``, a real ``GradingConfig`` carrying an
``LLMJudgeConfig``, and the real ``_compose_judge_system_prompt`` composition —
no mocks of the code under test. The judge itself is never invoked; the
effective prompt is derived from the ``GradingConfig`` the adapter returns, so
an auto-fail trial (which never called a judge) still records the contract it
would have graded under.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from tests.canonical._factories import make_task_config, make_trial_spec
from tests.utils.conductor_phases import (
    make_conductor,
    make_run_config,
    make_setup,
    runner_stub,
)
from tolokaforge.core.grading.judge import (
    _JUDGE_MARKER_CONTRACT,
    _JUDGE_SYSTEM_PROMPT,
    _compose_judge_system_prompt,
)
from tolokaforge.core.models import (
    GradingCombineConfig,
    GradingConfig,
    Metrics,
    Trajectory,
    TrialStatus,
)
from tolokaforge.runner.models import Criterion, JudgeCustomization, LLMJudgeConfig, Rubric

pytestmark = pytest.mark.canonical


_RUBRIC = Rubric(
    criteria=[
        Criterion(
            id="agent_answered_the_question",
            description="Agent produced a final answer that addresses the user's question.",
            kind="binary",
            required=True,
        )
    ]
)


def _grading_config(customization: JudgeCustomization | None) -> GradingConfig:
    """Build a ``GradingConfig`` whose ``llm_judge`` block carries the given
    customization (``None`` means no customization block at all — the engine
    default judge prompt is in effect)."""
    return GradingConfig(
        combine=GradingCombineConfig(),
        llm_judge=LLMJudgeConfig(rubric=_RUBRIC, customization=customization),
    )


def _write_prompts_yaml(
    tmp_path: Path,
    customization: JudgeCustomization | None,
    *,
    status: TrialStatus = TrialStatus.COMPLETED,
) -> dict:
    """Drive ``_write_artifacts`` end-to-end for one synthetic trial whose task
    carries an ``LLMJudgeConfig``; return the loaded ``prompts.yaml``.

    ``status`` selects the trajectory's terminal state — ``COMPLETED`` for the
    graded happy path, ``ERROR`` for the auto-fail short-circuit that never
    invoked a judge. ``_write_artifacts`` derives the judge prompt from
    ``grading_config`` regardless of status; the same bundle shape reaches
    disk on both paths."""
    conductor = make_conductor(make_run_config(tmp_path / "results"), tmp_path, MagicMock())
    conductor.adapter.get_grading_config.return_value = _grading_config(customization)

    task = make_task_config("task_with_llm_judge")
    setup = make_setup(tmp_path, task.task_id, 0)
    now = datetime.now(UTC)
    trajectory = Trajectory(
        task_id=task.task_id,
        trial_index=0,
        start_ts=now,
        end_ts=now,
        status=status,
        messages=[],
        metrics=Metrics(),
    )

    conductor._write_artifacts(
        make_trial_spec(trial_id=f"{task.task_id}:0", task_id=task.task_id),
        task,
        setup,
        trajectory,
        runner_stub(),
    )

    return yaml.safe_load((setup.trial_dir / "prompts.yaml").read_text(encoding="utf-8"))


def test_prompts_yaml_records_the_default_judge_prompt_when_no_customization_is_configured(
    tmp_path: Path,
) -> None:
    """A task whose ``llm_judge`` block carries no ``customization`` records the
    engine default composition — byte-for-byte ``_JUDGE_SYSTEM_PROMPT``.

    Direct key access is intentional: a bundle written before this contract
    existed carries no ``judge_prompt`` key, and the ``KeyError`` names the
    missing contract instead of surfacing as a bare ``None`` any downstream
    consumer would have to guard against."""
    data = _write_prompts_yaml(tmp_path, customization=None)

    assert data["judge_prompt"] == _JUDGE_SYSTEM_PROMPT
    assert data["judge_prompt"] == _compose_judge_system_prompt(None)


def test_prompts_yaml_records_a_customized_judge_prompt_verbatim_with_the_marker_appended(
    tmp_path: Path,
) -> None:
    """A task's ``customization.system_prompt`` reaches ``prompts.yaml`` verbatim
    at the front, followed by the harness-owned marker contract at the tail —
    the exact composition the judge would have graded under, so a reader
    reconstructing the contract need not re-run ``_compose_judge_system_prompt``.

    Byte-for-byte equality against the composer's own output locks the whole
    string (body + separator + marker); the prefix / suffix assertions name what
    the shape is for a reader tracking down a regression."""
    body = "STRICT-VIBE"
    data = _write_prompts_yaml(tmp_path, customization=JudgeCustomization(system_prompt=body))

    assert data["judge_prompt"].startswith(body)
    assert data["judge_prompt"].endswith(_JUDGE_MARKER_CONTRACT)
    assert data["judge_prompt"] == _compose_judge_system_prompt(body)


def test_prompts_yaml_records_the_judge_prompt_on_an_auto_fail_trial_that_never_invoked_the_judge(
    tmp_path: Path,
) -> None:
    """An auto-fail trial (``TrialStatus.ERROR`` — the runner short-circuited
    before grading, so the judge was never called) still records the composed
    judge contract when the task carries an ``llm_judge`` block.

    ``_write_artifacts`` derives the effective prompt from ``grading_config``,
    not from the judge itself, so the bundle shape stays consistent across
    every trial of a run: a rejudge once the underlying auto-fail cause is
    fixed has the recorded contract to reconstruct against."""
    data = _write_prompts_yaml(tmp_path, customization=None, status=TrialStatus.ERROR)

    assert data["judge_prompt"] == _JUDGE_SYSTEM_PROMPT
