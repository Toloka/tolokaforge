"""``load_grading_substrate`` discovery + composite-level parity gate.

Locks the two seams the substrate design commits to (per ADR-0040):

1. ``plugin_registry.load_grading_substrate`` resolves substrates registered
   via ``importlib.metadata`` entry-points. The dispatch case injects a
   synthetic entry-point pointing at :class:`DummyGradingSubstrate` under
   the group ``tolokaforge.grading_substrates`` and asserts the loader
   returns the dummy class. No wheel pollution: the dummy stays discoverable
   only under the monkeypatched mapping.

2. **Composite-level parity gate.** Given one representative multi-component
   pack and one running runner, the two shipped substrates score the trial
   identically on every wire field an operator reads:

   - **InProcess leg** — drive the runner's ``GradeTrial`` RPC. Its
     ``_grade_trial_async`` builds :class:`InProcessGradingSubstrate`
     internally and reassembles the ``Grade`` proto via
     ``compose_runner_trial_verdict`` + ``build_grade_reasons``.
   - **LiveRunnerCallback leg** — construct
     :class:`LiveRunnerCallbackGradingSubstrate` against the runner's
     in-process gRPC channel; call each ``composite.grade_*`` function
     directly with that substrate; reassemble the same ``Grade`` proto
     via the same helpers.

   The two ``Grade`` protos must be byte-equal on the eleven fields the
   operator sees: ``binary_pass``, ``score``, ``components``, ``reasons``,
   ``state_diff_json``, ``custom_checks``, ``criterion_results``,
   ``judge_status``, ``trace_checks``, ``trace_checks_summary``,
   ``judge_report``.

   The parity gate exercises the composite functions directly against both
   shipped substrates; a ``GraderServiceImpl.Grade``-level dispatch swap is
   out of scope for this suite.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
from concurrent import futures
from contextlib import contextmanager
from typing import Any

import grpc
import pytest

from tests.utils.dummy_grading_substrate import DummyGradingSubstrate
from tolokaforge.core.grading import composite
from tolokaforge.core.grading.grade_components import GRADE_COMPONENTS
from tolokaforge.core.grading.judge_result import JudgeStatus
from tolokaforge.core.grading.key_manifest import EVALUATED
from tolokaforge.core.grading.rubric_evaluator import RubricEvaluatorContext
from tolokaforge.core.grading.state_diff import render_state_diff
from tolokaforge.core.grading.substrate_live import LiveRunnerCallbackGradingSubstrate
from tolokaforge.core.grading.trace_checks import TraceChecksResult
from tolokaforge.core.grading.trace_timeline import build_trial_timeline
from tolokaforge.core.grading.transcript_wire import (
    decode_transcript_wire,
    split_leading_system_message,
)
from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import ModelConfig, ToolCall
from tolokaforge.core.plugin_registry import (
    GRADING_SUBSTRATES_GROUP,
    _clear_discovery_cache,
    load_grading_substrate,
    load_rubric_evaluator,
    load_state_check_backend,
    load_transcript_rule_matcher,
)
from tolokaforge.runner import (
    add_RunnerServiceServicer_to_server,
    add_SubstrateServiceServicer_to_server,
)
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.grading import (
    build_grade_reasons,
    compose_runner_trial_verdict,
    resolve_state_checks_component,
)
from tolokaforge.runner.grading_ledger import (
    CUSTOM_CHECKS_DISABLED_SKIP,
    CUSTOM_CHECKS_KEY,
    LLM_JUDGE_KEY,
    audit_accounted_keys,
)
from tolokaforge.runner.models import (
    Criterion,
    LLMJudgeConfig,
    PresentConstraint,
    Rubric,
    RunnerGradeComponents,
    RunnerGradingConfig,
    RunnerInitialStateConfig,
    RunnerStateChecksConfig,
    StableStateResponse,
    StateResponse,
    TaskDescription,
    TraceChecksConfig,
    TraceConstraint,
    TraceConstraintExpr,
    TraceMatcher,
    TranscriptRulesConfig,
)
from tolokaforge.runner.service import RunnerServiceImpl, TrialContextRuntime
from tolokaforge.runner.substrate_service import SubstrateServicer

pytestmark = pytest.mark.canonical


# ---------------------------------------------------------------------------
# 1. Dispatch — load_grading_substrate resolves a monkeypatched entry point
# ---------------------------------------------------------------------------


class _EntryPointStub:
    """Duck-typed ``importlib.metadata.EntryPoint`` for the discovery scan.

    Enumerates ``name`` / ``dist`` and returns ``value`` on ``load()`` — the
    surface :func:`discover_entry_points` reads.
    """

    def __init__(self, name: str, value: Any, dist_name: str = "tests-fixture") -> None:
        self.name = name
        self.value = value

        class _Dist:
            def __init__(self, dn: str) -> None:
                self.name = dn

        self.dist = _Dist(dist_name)

    def load(self) -> Any:
        return self.value


def test_load_grading_substrate_resolves_a_monkeypatched_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A synthetic entry-point under ``tolokaforge.grading_substrates`` is
    resolved by the loader without any wheel-side registration — the shape
    the future trajectory-storage substrate will register with when it ships.
    """
    _clear_discovery_cache()

    shipped = list(importlib.metadata.entry_points(group=GRADING_SUBSTRATES_GROUP))
    injected = _EntryPointStub("test_grading_substrate", DummyGradingSubstrate)

    def fake_entry_points(*, group: str) -> list[Any]:
        if group == GRADING_SUBSTRATES_GROUP:
            return [*shipped, injected]
        return list(importlib.metadata.entry_points(group=group))

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)
    _clear_discovery_cache()
    try:
        resolved = load_grading_substrate("test_grading_substrate")
        assert resolved is DummyGradingSubstrate
    finally:
        _clear_discovery_cache()


# ---------------------------------------------------------------------------
# 2. Composite-level parity gate
# ---------------------------------------------------------------------------


_TRIAL_ID = "task:0"
_RAW_STATE = {"users": [{"id": "u1", "name": "Alice", "session_token": "S-tok"}]}
_STABLE_STATE = {"users": [{"id": "u1", "name": "Alice"}]}


class _FakeDBServiceClient:
    """Deterministic DB stand-in exposing the reads both legs make.

    ``get_state`` serves the RAW final-DB view (judge state-diff + custom
    checks + the substrate service's ``ReadFinalDBState``); ``get_stable_state``
    serves the STABLE view (jsonpath). Neither is mutated across the two legs
    — parity assumes a stationary trial.
    """

    def __init__(self) -> None:
        self.raw_calls = 0
        self.stable_calls = 0

    async def get_state(
        self,
        trial_id: str,  # noqa: ARG002
        tables: list[str] | None = None,  # noqa: ARG002
    ) -> StateResponse:
        self.raw_calls += 1
        return StateResponse(data=_RAW_STATE, version=1, full_hash="full", stable_hash="stable")

    async def get_stable_state(self, trial_id: str) -> StableStateResponse:  # noqa: ARG002
        self.stable_calls += 1
        return StableStateResponse(
            data=_STABLE_STATE, version=1, stable_hash="stable", filtered_fields=[]
        )

    async def health_check(self) -> Any:
        raise AssertionError("parity gate does not exercise health_check")

    async def close(self) -> None:
        return None


class _ScriptedClient:
    """A scripted ``LoopLLMClient`` — returns queued ``GenerationResult``s.

    Both parity legs share this client via a monkeypatched ``LLMClient``
    constructor, so the judge draws from the same finite script both times.
    Two calls to ``generate`` are exhausted in order.
    """

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


_JUDGE_MODEL = ModelConfig(provider="openai", name="gpt-4o-mini", temperature=0.0)


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


def _task_description() -> TaskDescription:
    """Fixture pack — jsonpath (STABLE), transcript_rules, trace_checks, llm_judge.

    ``custom_checks`` is omitted: exercising it end-to-end requires a real
    ``checks.py`` bundled through the adapter's ``tool_artifacts`` delivery,
    which pulls in an artifacts extractor the parity claim does not depend on.
    The composite's custom-checks path is separately locked by
    ``test_grading_composite_custom_checks.py``; both legs see the same
    ``-1.0`` not-evaluated sentinel for it here.
    """
    grading = RunnerGradingConfig(
        weights={
            "state_checks": 1.0,
            "transcript_rules": 1.0,
            "trace_checks": 1.0,
            "llm_judge": 1.0,
        },
        state_checks=RunnerStateChecksConfig(
            jsonpath_checks=[
                {"path": "$.db.users[0].name", "equals": "Alice", "description": "alice named"},
            ],
        ),
        transcript_rules=TranscriptRulesConfig(
            min_assistant_turns=1,
        ),
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
    )
    return TaskDescription.model_validate(
        {
            "task_id": "parity_gate",
            "name": "Parity gate",
            "category": "test",
            "description": "Composite-level parity gate",
            "adapter_type": "tau",
            "system_prompt": "You are a test assistant.",
            "initial_state": RunnerInitialStateConfig(tables=_STABLE_STATE).model_dump(),
            "agent_tools": [],
            "user_tools": [],
            "grading": grading.model_dump(),
        }
    )


_LLM_MESSAGES = [
    {"role": "system", "content": "you are a test assistant"},
    {"role": "user", "content": "please help"},
    {"role": "assistant", "content": "done"},
]


@contextmanager
def _running_runner():
    """Bring up an in-process gRPC server carrying ``RunnerService`` +
    ``SubstrateService`` with the parity fixture registered."""
    fake_db = _FakeDBServiceClient()
    runner = RunnerServiceImpl(db_client=fake_db)  # type: ignore[arg-type]
    task_description = _task_description()
    trial_context = TrialContextRuntime(
        trial_id=_TRIAL_ID,
        task_description=task_description,
        judge_model_config=_JUDGE_MODEL,
    )
    runner.trials[_TRIAL_ID] = trial_context

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    add_RunnerServiceServicer_to_server(runner, server)
    add_SubstrateServiceServicer_to_server(SubstrateServicer(runner), server)
    port = server.add_insecure_port("[::]:0")
    server.start()
    try:
        with grpc.insecure_channel(f"localhost:{port}") as channel:
            yield runner, trial_context, task_description, channel
    finally:
        server.stop(grace=None)
        if runner._loop.is_running():
            runner._loop.call_soon_threadsafe(runner._loop.stop)


def _install_scripted_client(monkeypatch: pytest.MonkeyPatch, script: list[Any]) -> None:
    """Route :class:`LiteLLMJudgeModelProvider`'s ``LLMClient(model_config)``
    to a scripted stand-in. Fires both when the runner path resolves the
    shipping ``litellm`` provider (composite grade) and when composite
    dispatch is driven directly against the same provider."""
    monkeypatch.setattr(
        "tolokaforge.core.grading.default_judge_model_provider.LLMClient",
        lambda *args, **kwargs: _ScriptedClient(script),
    )


def _reassemble_grade_from_composite(
    *,
    runner: RunnerServiceImpl,
    task_description: TaskDescription,
    trial_context: TrialContextRuntime,
    channel: grpc.Channel,
) -> pb2.Grade:
    """Drive each ``composite.grade_*`` against a LiveCallback substrate, then
    reassemble the ``Grade`` via the same helpers ``_grade_trial_async`` uses.

    Every substrate read goes over the runner's ``SubstrateService`` gRPC
    surface — no in-process reuse of ``runner._build_grading_substrate``.
    """
    grading_config = task_description.grading
    assert grading_config is not None
    substrate = LiveRunnerCallbackGradingSubstrate(
        runner_substrate_address="unused",
        trial_id=_TRIAL_ID,
        channel=channel,
    )
    try:
        logger = StructuredLogger(name="parity-gate-live-callback")
        llm_messages = list(_LLM_MESSAGES)
        _, transcript = split_leading_system_message(llm_messages)
        timeline = build_trial_timeline(
            decode_transcript_wire(transcript), trial_context.recorded, None
        )

        components = RunnerGradeComponents()
        accounted_keys: dict[str, Any] = {}

        state_reads = composite.grade_state_checks_reads(
            trial_id=_TRIAL_ID,
            config=grading_config.state_checks,
            substrate=substrate,
            state_check_backends={
                "jsonpath": load_state_check_backend("jsonpath")(),
                "db_probes": load_state_check_backend("db_probes")(),
            },
            logger=logger,  # type: ignore[arg-type]
        )
        if state_reads.jsonpath_score is not None:
            components.jsonpath_score = state_reads.jsonpath_score
            components.jsonpath_reasons = state_reads.jsonpath_reasons
        if state_reads.db_probe_score is not None:
            components.db_probe_score = state_reads.db_probe_score
            components.db_probe_reasons = state_reads.db_probe_reasons
        accounted_keys.update(state_reads.accounted_keys)

        transcript_result, transcript_accounting = composite.grade_transcript_rules(
            trial_id=_TRIAL_ID,
            config=grading_config.transcript_rules,
            timeline=timeline,
            matcher=load_transcript_rule_matcher("default")(),
            logger=logger,  # type: ignore[arg-type]
        )
        accounted_keys.update(transcript_accounting)
        if transcript_result is not None:
            components.transcript_pass = transcript_result.passed
            components.transcript_score = transcript_result.score

        trace_result: TraceChecksResult = composite.grade_trace_checks(
            trial_id=_TRIAL_ID,
            config=grading_config.trace_checks,
            timeline=timeline,
            logger=logger,  # type: ignore[arg-type]
        )
        accounted_keys.update(trace_result.accounted_keys)
        if trace_result.constraints:
            components.trace_checks_score = trace_result.score

        judge_status = pb2.JUDGE_STATUS_UNSPECIFIED
        criterion_results: list[Any] = []
        judge_reasons: str | None = None
        judge_gate_failed = False
        judge_report: pb2.JudgeReport | None = None
        rubric_evaluator = load_rubric_evaluator("llm_judge")(
            RubricEvaluatorContext(
                judge_model_provider=runner._judge_model_provider,
                logger=logger,  # type: ignore[arg-type]
            )
        )
        initial_tables = substrate.initial_state()
        state_diff_text: str | None = None
        if initial_tables:
            primary_keys: dict[str, str | list[str]] = {
                s.table_name: s.primary_key for s in task_description.initial_state.schemas
            }
            state_diff_text = render_state_diff(
                initial_tables,
                substrate.final_state(),
                primary_keys=primary_keys,
                unstable_fields=set(),
            )
        judge_result = composite.grade_llm_judge(
            trial_id=_TRIAL_ID,
            config=grading_config.llm_judge,
            substrate=substrate,
            rubric_evaluator=rubric_evaluator,
            llm_messages=llm_messages,
            judge_model_config=_JUDGE_MODEL,
            extra_read_tools=[],
            state_diff=state_diff_text,
            logger=logger,  # type: ignore[arg-type]
        )
        accounted_keys[LLM_JUDGE_KEY] = EVALUATED
        judge_reasons = judge_result.reasons
        criterion_results = list(judge_result.criterion_results)
        judge_report = pb2.JudgeReport(
            calls=judge_result.usage.calls,
            prompt_tokens=judge_result.usage.prompt_tokens,
            completion_tokens=judge_result.usage.completion_tokens,
            reasoning_tokens=judge_result.usage.reasoning_tokens,
            cost_usd=judge_result.usage.cost_usd,
            tool_calls=judge_result.usage.tool_calls,
            consistency_rejections=judge_result.usage.consistency_rejections,
            transcript_json=json.dumps(list(judge_result.transcript)),
            knowledge_search_disabled=judge_result.knowledge_search_disabled,
            kb_tools_offered=list(judge_result.kb_tools_offered),
            kb_tools_withheld=list(judge_result.kb_tools_withheld),
            state_diff_text=judge_result.state_diff or "",
            read_tools_offered=list(judge_result.read_tools_offered),
            custom_system_prompt=judge_result.custom_system_prompt,
            include_agent_system_prompt=judge_result.include_agent_system_prompt,
        )
        if judge_result.status is JudgeStatus.ERRORED:
            judge_status = pb2.JUDGE_STATUS_ERRORED
        else:
            judge_status = pb2.JUDGE_STATUS_COMPLETED
            judge_gate_failed = judge_result.gate_failed
            components.llm_judge_score = judge_result.score

        # Custom checks: the pack declares none, so the composite returns
        # (-1.0, [], None) and the accounting key is populated with a skip.
        custom_score, custom_wire_results, custom_reasons = composite.grade_custom_checks(
            trial_id=_TRIAL_ID,
            config=grading_config.custom_checks,
            substrate=substrate,
            llm_messages=llm_messages,
            task_description=task_description,
            artifacts_dir=None,
            check_executor=runner.check_executor,
            logger=logger,  # type: ignore[arg-type]
        )
        components.custom_checks_score = custom_score
        accounted_keys[CUSTOM_CHECKS_KEY] = CUSTOM_CHECKS_DISABLED_SKIP

        audit = audit_accounted_keys(grading_config, accounted_keys)
        assert audit.error is None, audit.error

        state_checks_slot = resolve_state_checks_component(
            hash_score=components.hash_score,
            jsonpath_score=components.jsonpath_score,
            db_probe_score=components.db_probe_score,
            hash_weight=(
                grading_config.state_checks.hash_weight if grading_config.state_checks else None
            ),
        )
        verdict = compose_runner_trial_verdict(
            components.model_dump(),
            grading_config.model_dump(),
            judge_gate_failed=judge_gate_failed,
            trace_gate_failed=trace_result.gate_failed,
        )
        components.llm_judge_score = verdict.judge_component
        components_dict = components.model_dump()

        reason_segments = [
            build_grade_reasons(
                components_dict,
                None,
                transcript_result.model_dump() if transcript_result else None,
                judge_reasons=judge_reasons or None,
                trace_checks_result=trace_result.model_dump(mode="json"),
                golden_replay=None,
                custom_checks_reasons=custom_reasons,
            )
        ]
        if judge_status == pb2.JUDGE_STATUS_ERRORED:
            reason_segments.append(f"JUDGE ERRORED: {judge_reasons}")
        if audit.skip_notes:
            reason_segments.append("; ".join(audit.skip_notes))
        if state_checks_slot.inert_weight_reason:
            reason_segments.append(state_checks_slot.inert_weight_reason)
        if verdict.reason:
            reason_segments.append(verdict.reason)
        reasons = " | ".join(segment for segment in reason_segments if segment)

        wire_component_scores: dict[str, float] = {
            spec.name: getattr(components, spec.runner_score_field)
            for spec in GRADE_COMPONENTS
            if spec.runner_score_field is not None
        }
        wire_component_scores["state_checks"] = (
            -1.0 if state_checks_slot.component is None else state_checks_slot.component
        )

        return pb2.Grade(
            binary_pass=verdict.binary_pass,
            score=verdict.score,
            components=pb2.GradeComponents(**wire_component_scores),
            reasons=reasons,
            state_diff_json="",
            custom_checks=custom_wire_results,
            criterion_results=[
                pb2.CriterionResult(
                    id=cr.id, met=cr.met, score=cr.score, justification=cr.justification
                )
                for cr in criterion_results
            ],
            judge_status=judge_status,
            judge_report=judge_report,
            trace_checks=[
                pb2.TraceConstraintResult(
                    id=item.id,
                    kind=item.kind,
                    passed=item.passed,
                    weight=item.weight,
                    message=item.message,
                    matched_positions=item.matched_positions,
                    severity=item.severity,
                    undecided=item.undecided,
                    withheld=item.withheld,
                )
                for item in trace_result.constraints
            ],
            trace_checks_summary=pb2.TraceChecksSummary(
                winning_path=trace_result.winning_path,
                gate_failed=trace_result.gate_failed,
                failed_gate_ids=trace_result.failed_gate_ids,
                paths=[
                    pb2.TracePathResult(id=path.id, score=path.score, gate_failed=path.gate_failed)
                    for path in trace_result.paths
                ],
            ),
        )
    finally:
        substrate.close()


class _RaisingClient:
    """An ``LLMClient`` stand-in whose every ``generate`` call raises.

    The judge's ``ToolCallingLoop.run`` catches the raise and returns
    ``JudgeStatus.ERRORED`` with no numeric score, so the fold's declared
    ``llm_judge`` component reads as ``-1.0`` on the wire and the refusal
    branch fires downstream.
    """

    def generate(
        self,
        system,  # noqa: ARG002
        messages,  # noqa: ARG002
        tools,  # noqa: ARG002
        tool_choice="auto",  # noqa: ARG002
        observation=None,  # noqa: ARG002
    ) -> GenerationResult:
        raise RuntimeError("scripted judge failure — provider unreachable")

    def classify_loop_error(self, exc: Exception):
        from tolokaforge.core.loop import classify_loop_error

        return classify_loop_error(exc, ())

    def sanitize_tools_for_execution(self, tools: list[dict]) -> dict[str, dict]:
        return {}


def _install_raising_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the judge's ``LLMClient`` construction to a client that raises."""
    monkeypatch.setattr(
        "tolokaforge.core.grading.default_judge_model_provider.LLMClient",
        lambda *args, **kwargs: _RaisingClient(),
    )


def test_grade_trial_refuses_when_a_hash_pack_golden_replay_errors(
    runner_service, mock_grpc_context, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hash-enabled trial whose golden replay recorded a per-action failure
    refuses the fold end-to-end.

    The runner keeps ``components.hash_score`` at the ``-1.0`` not-evaluated
    sentinel when :attr:`HashGradingResult.hash_unscorable` is ``True``.
    ``state_checks`` is in the config's requested set but its component slot
    is empty; the fold's declared-but-unscored refusal fires downstream, and
    ``GradeTrial`` returns ``success=False`` with an error naming
    ``state_checks``. Locks the wire-level parity with the judge-errored case:
    a broken oracle is UNGRADEABLE, not a passing verdict on a fabricated 0.0.
    """
    from tests.utils.runner_requests import (
        register_request,
        simple_task_description,
        trial_spec_json,
    )
    from tolokaforge.core.grading.golden_replay import (
        FailedGoldenAction,
        GoldenActionFailure,
        GoldenReplayRecord,
    )
    from tolokaforge.runner.models import HashComparisonBasis, HashGradingResult

    trial_id = "hash_replay_errors_dispatch:0"
    task_dict = simple_task_description()
    task_dict["task_id"] = trial_id.split(":", 1)[0]
    task_dict["name"] = "hash replay errors dispatch"

    registration = register_request(
        trial_spec_json(task_dict, trial_id=trial_id), trial_id=trial_id
    )
    register_response = runner_service.RegisterTrial(registration, mock_grpc_context)
    assert register_response.success is True, register_response.error

    broken_result = HashGradingResult(
        hash_match=False,
        basis=HashComparisonBasis.GOLDEN_REPLAY,
        golden_replay=GoldenReplayRecord(
            authored=1,
            failures=(
                FailedGoldenAction(
                    index=0,
                    name="create_order",
                    kind=GoldenActionFailure.RAISED,
                    error="RuntimeError: substrate lost mid-replay",
                ),
            ),
        ),
    )

    async def _stubbed_hash_grading(
        _trial_id: str, _trial_context: Any, _state_checks: Any
    ) -> HashGradingResult:
        return broken_result

    monkeypatch.setattr(runner_service, "_execute_hash_grading", _stubbed_hash_grading)

    response = runner_service.GradeTrial(
        pb2.GradeTrialRequest(
            trial_id=trial_id,
            llm_messages_json=json.dumps(
                [{"role": "assistant", "content": "attempted to create the order"}]
            ),
        ),
        mock_grpc_context,
    )

    error = response.error
    grade = response.grade
    assert response.success is False, f"broken replay must refuse; got grade={grade}"
    assert "state_checks" in error, f"refusal must name the component: {error!r}"


def test_grade_trial_refuses_when_a_declared_judge_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A declared+configured ``llm_judge`` that errors refuses the fold.

    The fixture has all four components in ``combine.weights`` and every
    matching config section, so the judge is a declared component. With the
    judge's LLM client raising on every call the judge returns
    :attr:`JudgeStatus.ERRORED` with no numeric score; ``resolve_uncounted_fold``
    reads ``llm_judge`` in ``requested`` but not in ``scored`` and refuses.
    The runner's ``GradeTrial`` surfaces the refusal as
    ``success=False`` with an error that names the missing component — the
    trial lands ungradeable rather than passing on a redistributed weighted
    mean.
    """
    with _running_runner() as (runner, _trial_context, _task_description, _channel):
        _install_raising_client(monkeypatch)
        response = runner.GradeTrial(
            pb2.GradeTrialRequest(
                trial_id=_TRIAL_ID,
                llm_messages_json=json.dumps(_LLM_MESSAGES),
            ),
            _NullGrpcContext(),
        )

    error = response.error
    grade = response.grade
    assert response.success is False, f"errored judge must refuse RPC, got grade={grade}"
    assert "llm_judge" in error, f"refusal must name the component: {error!r}"


def test_composite_level_parity_between_in_process_and_live_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One representative pack, two substrates, one ``Grade`` proto.

    Both legs use the same scripted ``LLMClient`` — the judge draws from
    the same finite script twice — and read against the same fake DB.
    Every wire field the operator sees must match.
    """
    # Two runs — one per leg — so the scripted client is not exhausted
    # by the first leg's turn.
    script = [[_submit_report_call()]]

    with _running_runner() as (runner, trial_context, task_description, channel):
        _install_scripted_client(monkeypatch, script)
        response = runner.GradeTrial(
            pb2.GradeTrialRequest(
                trial_id=_TRIAL_ID,
                llm_messages_json=json.dumps(_LLM_MESSAGES),
            ),
            _NullGrpcContext(),
        )
        assert response.success is True, response.error
        in_process_grade = response.grade

        _install_scripted_client(monkeypatch, script)
        live_callback_grade = _reassemble_grade_from_composite(
            runner=runner,
            task_description=task_description,
            trial_context=trial_context,
            channel=channel,
        )

    _assert_grade_parity(in_process_grade, live_callback_grade)


class _NullGrpcContext:
    """Bare-minimum ``grpc.ServicerContext`` stand-in — the servicer needs
    only an object; none of its methods are exercised by this parity path.
    """

    def set_code(self, *_args, **_kwargs) -> None:  # pragma: no cover
        pass

    def set_details(self, *_args, **_kwargs) -> None:  # pragma: no cover
        pass


def _assert_grade_parity(a: pb2.Grade, b: pb2.Grade) -> None:
    """Every wire field an operator reads is byte-equal on the two grades.

    The eleven fields called out in ADR-0040's parity gate; a scalar mismatch
    surfaces with the raw values, a nested-message mismatch surfaces with the
    field name so a divergence is diagnosable without printing the whole
    grade.
    """
    assert a.binary_pass == b.binary_pass, (a.binary_pass, b.binary_pass)
    assert a.score == pytest.approx(b.score), (a.score, b.score)
    assert a.components == b.components, ("components", a.components, b.components)
    assert a.reasons == b.reasons, ("reasons", a.reasons, b.reasons)
    assert a.state_diff_json == b.state_diff_json
    assert list(a.custom_checks) == list(b.custom_checks)
    assert list(a.criterion_results) == list(b.criterion_results)
    assert a.judge_status == b.judge_status
    assert list(a.trace_checks) == list(b.trace_checks)
    assert a.trace_checks_summary == b.trace_checks_summary
    assert a.judge_report == b.judge_report
