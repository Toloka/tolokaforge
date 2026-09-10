"""Every LLM-judge dispatch site reads ``LLMJudgeConfig.judge_kind`` from config.

Four parametrised sub-tests drive one LLM-judge dispatch each through
the four call sites the runner + grader package expose:

- Runner-side composite (``RunnerServiceImpl._grade_llm_judge``)
- Grader-service composite dispatch
  (``GraderCompositeDispatch._grade_llm_judge_block``)
- Offline ``CompositeGraderKind._run_composite`` bundle-regrade path
- ``run_judge_only_for_trajectory`` (judge_only helper)

A fake ``JudgeKind`` (``NAME = "call_site_probe"``) is registered
alongside the shipped kinds via an ``importlib.metadata.entry_points``
monkey-patch fixture that injects a stub entry-point into the
``tolokaforge.judge_kinds`` group; the probe records every
``.evaluate(**kwargs)`` call so the sub-tests can assert BOTH the
name-resolution (the site read ``judge_kind`` from config, not a
hardcoded string) AND, on the runner-side sub-test, the verbatim
identity of ``kind_config`` (the site forwarded
``llm_judge_config.kind_config``, not ``None``). A future hardcode
regression at any one site fails loudly here.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock

import pytest

from tests.canonical._factories import make_trajectory
from tolokaforge.core.grading.judge_result import JudgeResult, JudgeStatus, JudgeUsage
from tolokaforge.core.grading.substrate import InProcessGradingSubstrate
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import (
    Message,
    MessageRole,
    ModelConfig,
    TerminationReason,
    TrialStatus,
)
from tolokaforge.core.plugin_registry import JUDGE_KINDS_GROUP, _clear_discovery_cache
from tolokaforge.grader.composite_dispatch import GraderCompositeDispatch
from tolokaforge.runner.models import (
    Criterion,
    LLMJudgeConfig,
    Rubric,
    RunnerGradingConfig,
    RunnerInitialStateConfig,
)
from tolokaforge.runner.service import RunnerServiceImpl

pytestmark = pytest.mark.canonical


_PROBE_NAME = "call_site_probe"
_KIND_CONFIG_PAYLOAD = {"key": "value", "nested": {"n": 1}}
_JUDGE_MODEL = ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)


class _CallSiteProbeJudgeKind:
    """Records every ``.evaluate(**kwargs)`` invocation the seam reaches it with."""

    NAME: ClassVar[str] = _PROBE_NAME

    calls: ClassVar[list[dict[str, Any]]] = []

    def evaluate(self, **kwargs: Any) -> JudgeResult:
        type(self).calls.append(kwargs)
        return JudgeResult(
            status=JudgeStatus.COMPLETED,
            usage=JudgeUsage(),
            reasons="call_site_probe reached",
            score=1.0,
        )


class _EntryPointStub:
    """Duck-typed ``importlib.metadata.EntryPoint`` for the discovery scan."""

    def __init__(self, name: str, value: Any, dist_name: str = "tests-fixture") -> None:
        self.name = name
        self.value = value

        class _Dist:
            def __init__(self, dn: str) -> None:
                self.name = dn

        self.dist = _Dist(dist_name)

    def load(self) -> Any:
        return self.value


@pytest.fixture
def probe_judge_kind_registered(monkeypatch: pytest.MonkeyPatch):
    """Inject :class:`_CallSiteProbeJudgeKind` into the ``tolokaforge.judge_kinds``
    discovery scan alongside the shipped kinds, and clear the discovery cache on
    both ends of the fixture so subsequent tests see the shipped set only."""
    _clear_discovery_cache()
    original_entry_points = importlib.metadata.entry_points
    shipped = list(original_entry_points(group=JUDGE_KINDS_GROUP))
    injected = _EntryPointStub(_PROBE_NAME, _CallSiteProbeJudgeKind)

    def fake_entry_points(*, group: str) -> list[Any]:
        if group == JUDGE_KINDS_GROUP:
            return [*shipped, injected]
        return list(original_entry_points(group=group))

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)
    _clear_discovery_cache()
    _CallSiteProbeJudgeKind.calls.clear()
    try:
        yield
    finally:
        _CallSiteProbeJudgeKind.calls.clear()
        _clear_discovery_cache()


def _rubric() -> Rubric:
    return Rubric(
        criteria=[
            Criterion(
                id="answered",
                description="Agent answered",
                kind="binary",
                weight=1.0,
            )
        ]
    )


def _llm_judge_config(*, with_kind_config: bool) -> LLMJudgeConfig:
    return LLMJudgeConfig(
        rubric=_rubric(),
        judge_kind=_PROBE_NAME,
        kind_config=dict(_KIND_CONFIG_PAYLOAD) if with_kind_config else None,
    )


def _empty_substrate() -> InProcessGradingSubstrate:
    return InProcessGradingSubstrate(
        db_reader=MagicMock(),
        knowledge_search=None,
        filesystem_root=None,
        initial_state={},
        final_state={},
    )


class _FakeTaskDesc:
    """Minimal task description exposing what ``_grade_llm_judge`` reads."""

    def __init__(self) -> None:
        self.initial_state = RunnerInitialStateConfig()
        self.grading = None


class _RunnerTrialCtx:
    """Lightweight trial context surface for ``RunnerServiceImpl._grade_llm_judge``."""

    def __init__(self, model_config: ModelConfig) -> None:
        self.judge_model_config = model_config
        self.agent_tools: dict = {}
        self.task_description = _FakeTaskDesc()

    def resolve_kb_search(self) -> None:
        return None


def test_runner_side_composite_reads_judge_kind_and_forwards_kind_config_verbatim(
    probe_judge_kind_registered,
) -> None:
    """The runner-side composite site
    (:meth:`RunnerServiceImpl._grade_llm_judge`) reads ``judge_kind`` off
    ``LLMJudgeConfig`` and forwards ``kind_config`` verbatim to
    ``JudgeKind.evaluate``. Locks BOTH the name-resolution AND the
    opaque-passthrough contract in one probe."""
    service = RunnerServiceImpl(db_client=MagicMock(), rag_client=None)
    try:
        cfg = _llm_judge_config(with_kind_config=True)
        ctx = _RunnerTrialCtx(_JUDGE_MODEL)
        service.trials["probe:0"] = ctx
        fut = asyncio.run_coroutine_threadsafe(
            service._grade_llm_judge(
                "probe:0",
                cfg,
                [
                    {"role": "system", "content": "policy"},
                    {"role": "user", "content": "please answer"},
                    {"role": "assistant", "content": "the answer is 42"},
                ],
                ctx,
                substrate=_empty_substrate(),
            ),
            service._loop,
        )
        result = fut.result(timeout=5.0)
    finally:
        service.shutdown()

    assert result.reasons == "call_site_probe reached"
    assert len(_CallSiteProbeJudgeKind.calls) == 1
    passed_kind_config = _CallSiteProbeJudgeKind.calls[0]["kind_config"]
    # Pydantic coerces dict[str, Any] into a fresh dict on model construction,
    # so this is content-equality, not object-identity.
    assert passed_kind_config == _KIND_CONFIG_PAYLOAD


def test_grader_service_composite_dispatch_reads_judge_kind(
    probe_judge_kind_registered,
) -> None:
    """The standalone grader's composite dispatch
    (:meth:`GraderCompositeDispatch._grade_llm_judge_block`) reads
    ``judge_kind`` off ``LLMJudgeConfig`` and drives the resolved kind's
    ``evaluate``. This sub-test only verifies the field is read; the
    verbatim ``kind_config`` passthrough is locked separately in the
    runner-side sub-test."""
    from tolokaforge.core.grading.grade_components import CompositeGradeComponents

    dispatch = GraderCompositeDispatch(logger=MagicMock())
    cfg = _llm_judge_config(with_kind_config=False)

    substrate = _empty_substrate()
    try:
        dispatch._grade_llm_judge_block(
            trial_id="probe:0",
            llm_judge_config=cfg,
            judge_model_config=_JUDGE_MODEL,
            llm_messages=[
                {"role": "system", "content": "policy"},
                {"role": "user", "content": "please answer"},
                {"role": "assistant", "content": "the answer is 42"},
            ],
            substrate=substrate,
            initial_state_schemas=[],
            id_fields={},
            unstable_fields=set(),
            components=CompositeGradeComponents(),
        )
    finally:
        substrate.close()

    assert len(_CallSiteProbeJudgeKind.calls) == 1
    assert _CallSiteProbeJudgeKind.calls[0]["kind_config"] is None


def test_offline_composite_grader_kind_reads_judge_kind(
    probe_judge_kind_registered, tmp_path: Path
) -> None:
    """The offline bundle-regrade path
    (:meth:`CompositeGraderKind._run_composite`) reads ``judge_kind`` off
    ``task_config.llm_judge`` and drives the resolved kind's
    ``evaluate``. Invokes ``_run_composite`` directly with the fixture
    surface — the ``build_grade`` shell loads task/trajectory/model via
    the bundle reader, all orthogonal to the rewire under test."""
    from tolokaforge.core.grading import composite as composite_mod
    from tolokaforge.core.grading.grade_components import CompositeGradeComponents
    from tolokaforge.core.grading.kinds import CompositeGraderKind
    from tolokaforge.core.grading.trace_timeline import build_timeline_from_wire
    from tolokaforge.core.models import CustomCheckDetail
    from tolokaforge.core.models import JudgeStatus as JudgeStatusEnum
    from tolokaforge.core.plugin_registry import (
        load_custom_check_executor,
        load_judge_kind,
        load_judge_model_provider,
        load_state_check_backend,
        load_transcript_rule_matcher,
    )
    from tolokaforge.runner.models import TaskDescription as _TaskDescription
    from tolokaforge.runner.models import (
        TraceChecksSummary,
        TraceConstraintResult,
        TracePathResult,
    )

    cfg = _llm_judge_config(with_kind_config=False)
    task_config = RunnerGradingConfig(
        weights={"llm_judge": 1.0},
        llm_judge=cfg,
    )
    task_description = _TaskDescription(
        task_id="probe-task",
        name="probe task",
        category="test",
        description="offline path probe",
        adapter_type="native",
        system_prompt="you are the agent",
        grading=task_config,
    )
    llm_messages = [
        {"role": "system", "content": "policy"},
        {"role": "user", "content": "please answer"},
        {"role": "assistant", "content": "the answer is 42"},
    ]
    timeline = build_timeline_from_wire(llm_messages, [], TerminationReason.AGENT_DONE)

    substrate = _empty_substrate()
    try:
        CompositeGraderKind()._run_composite(
            substrate=substrate,
            task_config=task_config,
            task_description=task_description,
            judge_model_config=_JUDGE_MODEL,
            llm_messages=llm_messages,
            timeline=timeline,
            id_fields={},
            unstable_fields=set(),
            initial_state_schemas=[],
            artifacts_dir=None,
            trial_id="probe:0",
            state_check_backends={
                "jsonpath": load_state_check_backend("jsonpath")(),
                "db_probes": load_state_check_backend("db_probes")(),
            },
            transcript_rule_matcher=load_transcript_rule_matcher("default")(),
            check_executor=load_custom_check_executor("check_runner")(),
            judge_model_provider=load_judge_model_provider("litellm")(),
            logger=MagicMock(),
            composite_mod=composite_mod,
            composite_components_cls=CompositeGradeComponents,
            load_judge_kind=load_judge_kind,
            judge_status_cls=JudgeStatusEnum,
            trace_summary_cls=TraceChecksSummary,
            trace_constraint_cls=TraceConstraintResult,
            trace_path_cls=TracePathResult,
            custom_detail_cls=CustomCheckDetail,
        )
    finally:
        substrate.close()

    assert len(_CallSiteProbeJudgeKind.calls) == 1
    assert _CallSiteProbeJudgeKind.calls[0]["kind_config"] is None


def test_judge_only_helper_reads_judge_kind(probe_judge_kind_registered) -> None:
    """The judge-only helper
    (:func:`run_judge_only_for_trajectory`) reads ``judge_kind`` off
    ``LLMJudgeConfig`` and drives the resolved kind's ``evaluate``."""
    from tolokaforge.core.grading.judge_only_helpers import run_judge_only_for_trajectory

    cfg = _llm_judge_config(with_kind_config=False)
    trajectory = make_trajectory(
        task_id="probe-task",
        status=TrialStatus.COMPLETED,
        termination_reason=TerminationReason.AGENT_DONE,
        messages=[
            Message(role=MessageRole.USER, content="please answer"),
            Message(role=MessageRole.ASSISTANT, content="the answer is 42"),
        ],
    )

    grade = run_judge_only_for_trajectory(
        trial_id="probe:0",
        llm_judge_config=cfg,
        judge_model_config=_JUDGE_MODEL,
        trajectory=trajectory,
        agent_system_prompt="you are the agent",
        override=None,
        llm_client=None,
        logger=StructuredLogger(name="probe"),
    )

    assert grade is not None
    assert len(_CallSiteProbeJudgeKind.calls) == 1
    assert _CallSiteProbeJudgeKind.calls[0]["kind_config"] is None
