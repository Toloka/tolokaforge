"""``GraderCompositeDispatch`` — centrepiece behaviour lock.

Drives :meth:`GraderCompositeDispatch.grade` against a synthesized
:class:`~tolokaforge.grader.service.GradeDispatch` payload for five
scenarios:

1. A task with all five component types → the dispatch returns a
   :class:`Grade` with binary_pass + score computed by the same combine
   helpers the runner uses; every component slot on
   :attr:`Grade.components` is populated.
2. A task with only ``transcript_rules`` → the other components stay
   ``None`` on :attr:`Grade.components`; the score comes off the
   transcript component.
3. A task with ``llm_judge`` but no ``judge_model_config_json`` →
   :class:`GradingFailedError` naming ``judge_model_config_json``.
4. A task with ``state_checks.hash_enabled=true`` →
   :class:`GradingFailedError` naming the actionable branch.
5. A substrate whose ``final_state`` raises
   :class:`SubstrateUnreachableError` on read → the dispatch translates
   into :class:`GradingFailedError` naming ``substrate unreachable``.

The substrate seam is monkeypatched at
:func:`load_grading_substrate` so a stub substrate replaces
``LiveRunnerCallbackGradingSubstrate`` for the duration of each test;
that keeps the dispatch surface pure and the rubric-evaluator's
scripted LLM client is installed at the shipping
``default_judge_model_provider`` seam so the judge loop runs
deterministically without any real key.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.grading.substrate import SubstrateUnreachableError
from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.models import JudgeStatus, ModelConfig, ToolCall
from tolokaforge.core.trial_grader import GradingFailedError
from tolokaforge.grader.composite_dispatch import GraderCompositeDispatch
from tolokaforge.grader.service import GradeDispatch
from tolokaforge.runner.models import (
    Criterion,
    LLMJudgeConfig,
    PresentConstraint,
    Rubric,
    RunnerGradingConfig,
    RunnerInitialStateConfig,
    RunnerStateChecksConfig,
    TaskDescription,
    TraceChecksConfig,
    TraceConstraint,
    TraceConstraintExpr,
    TraceMatcher,
    TranscriptRulesConfig,
)

pytestmark = pytest.mark.canonical


_TRIAL_ID = "task:0"
_ADDRESS = "runner-under-test:50051"
_JUDGE_MODEL = ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)

_STATE = {"users": [{"id": "u1", "name": "Alice"}]}

_LLM_MESSAGES = [
    {"role": "system", "content": "you are a test assistant"},
    {"role": "user", "content": "please help"},
    {"role": "assistant", "content": "done"},
]


class _StubSubstrate:
    """Canned-snapshot substrate that matches
    ``LiveRunnerCallbackGradingSubstrate``'s two-positional-arg constructor.

    A ``final_state_impl`` hook lets one test raise
    :class:`SubstrateUnreachableError` on the first read; every other
    accessor returns a constant snapshot.
    """

    def __init__(self, address: str, trial_id: str) -> None:
        self.address = address
        self.trial_id = trial_id
        self.closed = False

    initial_state_value: dict[str, Any] = _STATE
    final_state_value: dict[str, Any] = _STATE
    final_state_impl: Any = None

    def initial_state(self) -> dict[str, Any]:
        return dict(self.initial_state_value)

    def final_state(self) -> dict[str, Any]:
        if self.final_state_impl is not None:
            return self.final_state_impl()
        return dict(self.final_state_value)

    def final_state_stable(self) -> dict[str, Any]:
        return dict(self.final_state_value)

    def filesystem_root(self):  # type: ignore[no-untyped-def]
        return None

    def filesystem_state(self) -> dict[str, str] | None:
        return None

    def db_reader(self) -> Any:
        reader = MagicMock()
        reader.get_state = lambda tables=None: dict(self.final_state_value)
        reader.query = lambda jp: {"results": []}
        return reader

    def knowledge_search(self) -> Any:
        return None

    def close(self) -> None:
        self.closed = True


class _ScriptedClient:
    """A scripted ``LoopLLMClient``: returns queued ``GenerationResult`` in order."""

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self._i = 0

    def generate(
        self,
        system,  # noqa: ARG002
        messages,  # noqa: ARG002
        tools,  # noqa: ARG002
        tool_choice="auto",  # noqa: ARG002
        observation=None,  # noqa: ARG002
    ) -> GenerationResult:
        if self._i >= len(self._script):
            return GenerationResult(text="(exhausted)", tool_calls=[], usage=Usage())
        step = self._script[self._i]
        self._i += 1
        if isinstance(step, str):
            return GenerationResult(text=step, tool_calls=[], usage=Usage())
        tool_calls = [
            ToolCall(id=f"call_{self._i}_{j}", name=name, arguments=args)
            for j, (name, args) in enumerate(step)
        ]
        return GenerationResult(
            text="",
            tool_calls=tool_calls,
            usage=Usage(prompt_tokens=10, completion_tokens=5),
            cost_usd=0.001,
        )

    def classify_loop_error(self, exc: Exception):
        from tolokaforge.core.loop import classify_loop_error

        return classify_loop_error(exc, ())

    def sanitize_tools_for_execution(self, tools: list[dict]) -> dict[str, dict]:
        return {}


def _install_scripted_client(monkeypatch: pytest.MonkeyPatch, script: list[Any]) -> None:
    monkeypatch.setattr(
        "tolokaforge.core.grading.default_judge_model_provider.LLMClient",
        lambda *args, **kwargs: _ScriptedClient(script),
    )


def _install_stub_substrate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tolokaforge.grader.composite_dispatch.load_grading_substrate",
        lambda name: _StubSubstrate,
    )


def _rubric() -> Rubric:
    return Rubric(
        criteria=[Criterion(id="ok", description="Task completed", kind="binary", weight=1.0)]
    )


def _submit_report_call() -> tuple[str, dict[str, Any]]:
    return (
        "submit_report",
        {
            "reasons": "the agent completed the task",
            "ok": True,
            "ok_justification": "because ok\nVERDICT: MET",
        },
    )


def _task(
    *,
    state_checks: RunnerStateChecksConfig | None = None,
    transcript_rules: TranscriptRulesConfig | None = None,
    trace_checks: TraceChecksConfig | None = None,
    llm_judge: LLMJudgeConfig | None = None,
    weights: dict[str, float] | None = None,
) -> TaskDescription:
    grading = RunnerGradingConfig(
        weights=weights or {},
        state_checks=state_checks,
        transcript_rules=transcript_rules,
        trace_checks=trace_checks,
        llm_judge=llm_judge,
    )
    return TaskDescription.model_validate(
        {
            "task_id": "grader-composite-test",
            "name": "GraderComposite test",
            "category": "test",
            "description": "GraderCompositeDispatch fixture",
            "adapter_type": "tau",
            "system_prompt": "You are a test assistant.",
            "initial_state": RunnerInitialStateConfig(tables=_STATE).model_dump(),
            "agent_tools": [],
            "user_tools": [],
            "grading": grading.model_dump(),
        }
    )


def _dispatch(
    task: TaskDescription,
    *,
    judge_model: ModelConfig | None = _JUDGE_MODEL,
) -> GradeDispatch:
    return GradeDispatch(
        trial_id=_TRIAL_ID,
        llm_messages_json=json.dumps(_LLM_MESSAGES),
        termination_reason="",
        task_config_json=task.grading.model_dump_json(),
        judge_model_config_json=judge_model.model_dump_json() if judge_model is not None else "",
        task_description_json=task.model_dump_json(),
        runner_substrate_address=_ADDRESS,
    )


def _make_dispatcher() -> GraderCompositeDispatch:
    return GraderCompositeDispatch(logger=MagicMock())


def test_grade_returns_verdict_for_full_five_component_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task with jsonpath + transcript_rules + trace_checks + llm_judge
    dispatches every seam, folds through the runner's combine helpers,
    and returns a :class:`Grade` carrying a populated component slot for
    each configured component."""
    task = _task(
        state_checks=RunnerStateChecksConfig(
            jsonpath_checks=[
                {"path": "$.db.users[0].name", "equals": "Alice", "description": "alice named"},
            ],
        ),
        transcript_rules=TranscriptRulesConfig(min_assistant_turns=1),
        trace_checks=TraceChecksConfig(
            constraints=[
                TraceConstraint(
                    id="assistant_spoke",
                    description="the agent produced an assistant turn",
                    require=TraceConstraintExpr(
                        present=PresentConstraint(match=TraceMatcher(kind="assistant_message"))
                    ),
                )
            ],
        ),
        llm_judge=LLMJudgeConfig(rubric=_rubric()),
        weights={
            "state_checks": 1.0,
            "transcript_rules": 1.0,
            "trace_checks": 1.0,
            "llm_judge": 1.0,
        },
    )
    _install_stub_substrate(monkeypatch)
    _install_scripted_client(monkeypatch, [[_submit_report_call()]])

    dispatcher = _make_dispatcher()
    grade = dispatcher.grade(_dispatch(task))

    assert grade is not None
    assert grade.binary_pass is True
    assert grade.score == pytest.approx(1.0)
    assert grade.components.state_checks == pytest.approx(1.0)
    assert grade.components.transcript_rules == pytest.approx(1.0)
    assert grade.components.trace_checks == pytest.approx(1.0)
    assert grade.components.llm_judge == pytest.approx(1.0)
    assert grade.judge_status is JudgeStatus.COMPLETED


def test_grade_dispatches_only_transcript_rules_when_that_is_the_only_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task with only transcript_rules → other component slots stay
    ``None`` on the returned Grade; the transcript slot is populated."""
    task = _task(
        transcript_rules=TranscriptRulesConfig(min_assistant_turns=1),
        weights={"transcript_rules": 1.0},
    )
    _install_stub_substrate(monkeypatch)
    dispatcher = _make_dispatcher()
    grade = dispatcher.grade(_dispatch(task, judge_model=None))

    assert grade is not None
    assert grade.components.transcript_rules == pytest.approx(1.0)
    assert grade.components.state_checks is None
    assert grade.components.trace_checks is None
    assert grade.components.llm_judge is None
    assert grade.components.custom_checks is None


def test_grade_refuses_llm_judge_task_without_judge_model_config_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A task declaring ``llm_judge`` MUST arrive with a
    ``judge_model_config_json`` — a client bug the grader surfaces as a
    :class:`GradingFailedError` naming the missing field.
    """
    task = _task(
        llm_judge=LLMJudgeConfig(rubric=_rubric()),
        weights={"llm_judge": 1.0},
    )
    _install_stub_substrate(monkeypatch)
    dispatcher = _make_dispatcher()
    dispatch = _dispatch(task, judge_model=None)

    with pytest.raises(GradingFailedError) as exc:
        dispatcher.grade(dispatch)

    assert "judge_model_config_json" in str(exc.value)


def test_grade_refuses_hash_enabled_task_with_actionable_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Server-side belt-and-braces: even if the client-side refusal is
    bypassed, the grader still refuses a hash-enabled task with the
    documented actionable-branch message."""
    task = _task(
        state_checks=RunnerStateChecksConfig(hash_enabled=True),
        weights={"state_checks": 1.0},
    )
    _install_stub_substrate(monkeypatch)
    dispatcher = _make_dispatcher()
    dispatch = _dispatch(task, judge_model=None)

    with pytest.raises(GradingFailedError) as exc:
        dispatcher.grade(dispatch)

    message = str(exc.value)
    assert "hash-based grading" in message
    assert "runner_rpc" in message


def test_substrate_unreachable_becomes_grading_failed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A substrate whose ``final_state`` raises
    :class:`SubstrateUnreachableError` translates to
    :class:`GradingFailedError` naming ``substrate unreachable``.
    """
    task = _task(
        state_checks=RunnerStateChecksConfig(
            jsonpath_checks=[
                {"path": "$.db.users[0].name", "equals": "Alice", "description": "alice"},
            ],
        ),
        weights={"state_checks": 1.0},
    )

    class _UnreachableSubstrate(_StubSubstrate):
        def final_state(self):
            raise SubstrateUnreachableError("runner disappeared")

        def final_state_stable(self):
            raise SubstrateUnreachableError("runner disappeared")

        def db_reader(self):  # type: ignore[override]
            reader = MagicMock()

            def _boom(*args, **kwargs):
                raise SubstrateUnreachableError("runner disappeared")

            reader.get_state = _boom
            reader.query = _boom
            return reader

    monkeypatch.setattr(
        "tolokaforge.grader.composite_dispatch.load_grading_substrate",
        lambda name: _UnreachableSubstrate,
    )
    dispatcher = _make_dispatcher()
    dispatch = _dispatch(task, judge_model=None)

    with pytest.raises(GradingFailedError) as exc:
        dispatcher.grade(dispatch)

    message = str(exc.value)
    assert "substrate unreachable" in message
    assert _ADDRESS in message
