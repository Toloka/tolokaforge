"""``judge_only`` grades a trajectory identically to composite ``weights: {llm_judge: 1.0}``.

The umbrella wants ``judge_only`` (external) and ``composite + weights: {llm_judge:
1.0}`` (external) to be two names for the same grading dispatch. This canonical
gate locks that convergence at the ``Grade`` layer under the constrained-input
shape both paths collapse to when a task declares an ``llm_judge`` rubric and
nothing else — no ``state_checks``, no ``transcript_rules``, no ``trace_checks``,
no ``custom_checks``, no ``search_policy`` KB plane, and an empty
``initial_state``. On that shape, both paths reach :class:`LLMJudge` with
byte-identical evidence: the composite's
:func:`~tolokaforge.core.grading.composite.build_judge_state_diff` early-returns
``None`` on empty ``initial_state`` (matching ``judge_only``'s
unconditional ``state_diff=None``); the substrate reads (``db_reader``,
``kb_search``, ``filesystem_root``) all resolve to the same
"no live state" answers ``judge_only`` passes as ``None``; and
``extra_read_tools`` collapses to ``[]`` when no ``search_policy`` connector
was reconstructed. Grade-shape parity across the two names is the property
the umbrella cares about.

Substitution shape (per plan): :meth:`LLMJudge.run` is monkey-patched on the
class itself to return a fixed :class:`JudgeResult`. Both paths construct
:class:`LLMJudge` via the same import (``tolokaforge.core.grading.judge:LLMJudge``);
one class-level patch reaches both paths. The ``llm_client=`` injection point
would NOT bypass the substrate-kwarg divergence — LLMJudge builds its tool
list around ``db_reader`` truthiness before the injected client is called,
so patching ``run`` at the class level is the only place both paths converge
on a byte-identical verdict.

The equivalence pin at the config layer lives in
:data:`tolokaforge.core.trial_grader._JUDGE_ONLY_EQUIVALENT_CONFIG`. This
suite asserts both paths agree on that pin.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import make_trajectory
from tolokaforge.core.grading.judge_result import JudgeResult, JudgeStatus, JudgeUsage
from tolokaforge.core.models import (
    Grade,
    Message,
    MessageRole,
    ModelConfig,
    TerminationReason,
    TrialStatus,
)
from tolokaforge.core.plugin_registry import TrialGraderContext, load_trial_grader
from tolokaforge.core.trial import EnvEndpoints, TrialSpec
from tolokaforge.core.trial_grader import _JUDGE_ONLY_EQUIVALENT_CONFIG
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.models import (
    LLMJudgeConfig,
    Rubric,
    RunnerGradingConfig,
    TaskDescription,
)
from tolokaforge.runner.service import RunnerServiceImpl, TrialContextRuntime

pytestmark = pytest.mark.canonical


_TRIAL_ID = "judge_parity_task:0"
_JUDGE_MODEL = ModelConfig(provider="openai", name="gpt-4", temperature=0.0)


def _fixed_judge_result() -> JudgeResult:
    """A deterministic verdict both legs return byte-identical, given the same
    inputs. ``binary_pass`` matches ``score >= pass_threshold`` (default 0.8),
    so composite's fold-derived ``binary_pass`` and :func:`build_replay_grade`'s
    passthrough of ``result.binary_pass`` agree.
    """
    return JudgeResult(
        status=JudgeStatus.COMPLETED,
        usage=JudgeUsage(),
        reasons="rubric fully satisfied",
        score=0.87,
        binary_pass=True,
    )


def _task_description() -> TaskDescription:
    rubric = Rubric(
        criteria=[
            {
                "id": "answer_correct",
                "description": "did the agent answer",
                "kind": "binary",
                "weight": 1.0,
            }
        ]
    )
    return TaskDescription(
        task_id="judge_parity_task",
        name="judge parity task",
        category="test",
        description="constrained-shape task where judge_only and composite converge",
        adapter_type="native",
        system_prompt="you are the agent",
        grading=RunnerGradingConfig(
            weights={"llm_judge": 1.0},
            llm_judge=LLMJudgeConfig(rubric=rubric),
        ),
    )


def _trajectory() -> Any:
    """A minimal COMPLETED trajectory carrying a user/assistant exchange —
    enough for ``encode_transcript_wire`` to emit a non-empty wire on the
    ``judge_only`` path."""
    return make_trajectory(
        status=TrialStatus.COMPLETED,
        termination_reason=TerminationReason.AGENT_DONE,
        messages=[
            Message(role=MessageRole.USER, content="please answer"),
            Message(role=MessageRole.ASSISTANT, content="the answer is 42"),
        ],
    )


def _install_fixed_judge(monkeypatch: pytest.MonkeyPatch, result: JudgeResult) -> None:
    """Route every :meth:`LLMJudge.run` invocation to the fixed ``result``.

    The class-level patch reaches both paths: ``judge_only``'s helper
    constructs :class:`LLMJudge` directly, and the composite path
    constructs it inside :class:`LLMJudgeRubricEvaluator`. Both import from
    :mod:`tolokaforge.core.grading.judge`, so the same class object serves
    both.
    """
    monkeypatch.setattr(
        "tolokaforge.core.grading.judge.LLMJudge.run",
        lambda self, **_kwargs: result,
    )


def _grade_path_a_judge_only(fixed: JudgeResult, monkeypatch: pytest.MonkeyPatch) -> Grade:
    _install_fixed_judge(monkeypatch, fixed)
    ctx = TrialGraderContext(runner_address="ignored:0", logger=MagicMock())
    factory = load_trial_grader("judge_only")
    grader = factory(ctx)
    spec = TrialSpec(
        trial_id=_TRIAL_ID,
        run_id="parity_run",
        task=_task_description(),
        agent_model_config=ModelConfig(provider="openai", name="gpt-4"),
        judge_model_config=_JUDGE_MODEL,
        env_endpoints=EnvEndpoints(
            db_url="http://db.local:8000",
            runner_url="http://runner.local:50051",
        ),
    )
    grade = grader.grade(spec, _trajectory(), "you are the agent")
    assert grade is not None, "judge_only produced no verdict for the fixture"
    return grade


def _grade_path_b_composite(fixed: JudgeResult, monkeypatch: pytest.MonkeyPatch) -> pb2.Grade:
    _install_fixed_judge(monkeypatch, fixed)
    service = RunnerServiceImpl(db_client=MagicMock())
    try:
        service.trials[_TRIAL_ID] = TrialContextRuntime(
            trial_id=_TRIAL_ID,
            task_description=_task_description(),
            judge_model_config=_JUDGE_MODEL,
        )
        wire_messages = [
            {"role": "system", "content": "you are the agent"},
            {"role": "user", "content": "please answer"},
            {"role": "assistant", "content": "the answer is 42"},
        ]
        response = service.GradeTrial(
            pb2.GradeTrialRequest(
                trial_id=_TRIAL_ID,
                llm_messages_json=json.dumps(wire_messages),
                termination_reason=TerminationReason.AGENT_DONE.value,
            ),
            MagicMock(),
        )
        assert response.success is True, f"composite leg failed: {response.error}"
        return response.grade
    finally:
        service.shutdown()


def test_judge_only_and_composite_llm_judge_only_agree_on_grade_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both paths produce the same ``binary_pass`` / ``score`` /
    ``components.llm_judge`` under a fixed :class:`JudgeResult`.

    Grade-shape parity — not full :meth:`Grade.__eq__` — because the two
    paths compose their :attr:`Grade.reasons` and post-mortem fields
    (``criterion_results``, ``judge_report``) through different code
    (``build_replay_grade`` vs. the runner-side fold + ``compose_runner_trial_verdict``).
    The scoring surface is what the umbrella cares about: two names of
    the same dispatch must not silently produce different verdicts on
    the constrained shape they collapse to.
    """
    fixed = _fixed_judge_result()

    grade_a = _grade_path_a_judge_only(fixed, monkeypatch)
    grade_b = _grade_path_b_composite(fixed, monkeypatch)

    assert grade_a.binary_pass == grade_b.binary_pass
    assert grade_a.score == pytest.approx(grade_b.score)
    assert grade_a.components is not None
    assert grade_a.components.llm_judge == pytest.approx(grade_b.components.llm_judge)


def test_judge_only_equivalent_config_pins_composite_shape() -> None:
    """The module-level equivalent-config constant equals the composite shape
    ``judge_only`` collapses to. Locks the "one implementation, two names"
    contract at the code layer without cross-file drift.
    """
    assert (
        RunnerGradingConfig(
            grading_method="composite",
            weights={"llm_judge": 1.0},
        )
        == _JUDGE_ONLY_EQUIVALENT_CONFIG
    )
