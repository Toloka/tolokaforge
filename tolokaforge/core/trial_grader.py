"""``TrialGrader`` Protocol — the per-trial grading seam.

The conductor's job (per ``docs/CLOUD_RUNTIME_ARCHITECTURE.md`` §6.3) is to
*trigger* grading, not own it. This module defines the swappable seam:

* :class:`TrialGrader` — Protocol with a single method ``grade`` that maps a
  completed :class:`Trajectory` to a :class:`Grade`, or to ``None`` when there
  is nothing to grade. Any conductor holds a ``TrialGrader`` and delegates the
  phase to it.
* :class:`RunnerRPCTrialGrader` — production implementation. Encapsulates
  the grading strategies the conductor previously carried inline:

  1. An infrastructure abort (:func:`classify_trial_outcome` returns
     ``INFRASTRUCTURE_ABORT``) — **no grade at all**. The provider or the
     substrate killed the trial before the agent could be measured, and any
     score would describe work that never happened.
  2. ``TrialStatus.ERROR`` / ``TrialStatus.TIMEOUT`` — auto-fail without
     touching the runner. The trial did end badly on the agent's watch, but
     running the judge on a truncated transcript would produce a false
     positive.
  3. ``TerminationReason.STUCK_DETECTED`` — auto-fail. A stuck agent fails
     even if the state hash happens to match the golden.
  4. Otherwise — the runner's ``grade_trial`` gRPC computes state / rule /
     judge components against the golden state and returns a raw dict that
     is parsed into :class:`Grade`. A grading run that could not produce a
     verdict raises :class:`GradingFailedError`; the verdict is the runner's
     to compute, so the host has none to substitute.

The Protocol is deliberately narrow. A future :class:`TrialGrader`
implementation may live inside the runner sandbox (per §6.4), speak to a
remote grader service, or route to an entirely different Judge component
(GH #131). None of those variants require touching the conductor.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import ValidationError

from tolokaforge.core.failure_attribution import TrialOutcomeClass, classify_trial_outcome
from tolokaforge.core.grading.grade_components import GRADE_COMPONENTS
from tolokaforge.core.grading.transcript_wire import encode_transcript_wire
from tolokaforge.core.models import (
    CriterionResult,
    CustomCheckDetail,
    Grade,
    GradeComponents,
    JudgeInputs,
    JudgeKbGating,
    JudgeStatus,
    JudgeUsage,
    TerminationReason,
    TraceChecksSummary,
    TraceConstraintResult,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.trial import TrialSpec

if TYPE_CHECKING:
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.plugin_registry import TrialGraderContext
    from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient
    from tolokaforge.grader.client import GrpcGraderClient
    from tolokaforge.grader.queue import GradeBroker

__all__ = [
    "GraderRPCTrialGrader",
    "GradingFailedError",
    "JudgeBackedTrialGrader",
    "JudgeGradeFn",
    "QueueTrialGrader",
    "RunnerRPCTrialGrader",
    "TrialGrader",
    "grader_rpc_trial_grader_factory",
    "judge_backed_trial_grader_factory",
    "queue_trial_grader_factory",
]


class GradingFailedError(Exception):
    """Grading ran and could not produce a verdict.

    The trial was measured, so its verdict exists to be computed and only the
    grading substrate can compute it. A host-side stand-in would land in
    ``success_rate``, ``avg_score``, ``pass@k`` and ``binary_pass`` as an agent
    failure that no measurement supports, so the failure is raised instead.

    The conductor's grading phase catches it, records the reason on
    ``Trajectory.grading_error`` and leaves ``grade`` unset. The trial keeps its
    own ``termination_reason``, writes its bundle, and counts as an attempt that
    scored nothing — never as an attempt the agent failed.
    """


@runtime_checkable
class TrialGrader(Protocol):
    """Trial-plane seam for computing a :class:`Grade` from a completed
    :class:`Trajectory`.

    Any conductor holds a ``TrialGrader`` and delegates the grading phase
    to it. The Protocol is intentionally narrow: one method, immutable
    inputs, a value return.

    Implementations may short-circuit on trajectory shape (auto-fail on
    error / timeout / stuck) or dispatch to any grading substrate — an
    in-process rule engine, a runner-side gRPC, or a remote judge
    service. Callers do not need to know which.
    """

    def grade(
        self,
        spec: TrialSpec,
        trajectory: Trajectory,
        agent_system_prompt: str,
    ) -> Grade | None:
        """Return the :class:`Grade` for a completed trial, or ``None`` when
        the trial produced nothing to grade.

        ``spec`` carries trial identity, per-trial metadata, and the
        runner-side ``spec.task`` projection needed for dispatch.
        ``trajectory`` carries the full message trace, tool log, status
        and termination reason. ``agent_system_prompt`` is the
        post-policy system prompt the judge receives as the agent's
        policy for rubric evaluation.

        ``None`` is the answer for a trial the agent never got to run —
        the absence is not representable as a score, so a caller that
        forgets to branch fails instead of reading a fabricated zero.

        Raises:
            GradingFailedError: the trial was measured but grading could
                not produce a verdict. Distinct from ``None``: there is a
                verdict to compute and computing it failed.
        """
        ...


class RunnerRPCTrialGrader:
    """Production :class:`TrialGrader`. Dispatches to the runner's
    ``grade_trial`` gRPC for real grading, short-circuits with an
    auto-fail :class:`Grade` when the trajectory shape rules out a
    meaningful judge result, returns ``None`` for a trial that never
    ran, and raises :class:`GradingFailedError` when the RPC could not
    produce a verdict.

    Built per-run from a :class:`TrialGraderContext` — a *serialisable*
    configuration (``runner_address`` + logger). The grader owns its own
    :class:`~tolokaforge.core.shared_stack_runtime.GrpcRunnerClient` bound
    to that address, so it stays independent of the orchestrator's runtime
    backend — the seam that lets a future grader run on a different machine.

    Tests may pass a ``runner_client`` directly to bypass real gRPC; the
    stub must expose a ``grade_trial(trial_id, llm_messages_json, ...)``
    method returning the same dict shape as
    :meth:`GrpcRunnerClient.grade_trial`.
    """

    def __init__(
        self,
        runner_address: str,
        logger: StructuredLogger,
        *,
        runner_client: GrpcRunnerClient | None = None,
    ) -> None:
        self.runner_address = runner_address
        self.logger = logger
        if runner_client is None:
            from tolokaforge.core.shared_stack_runtime import GrpcRunnerClient as _Client

            runner_client = _Client(runner_address=runner_address)
        self.runner_client = runner_client

    def grade(
        self,
        spec: TrialSpec,
        trajectory: Trajectory,
        agent_system_prompt: str,
    ) -> Grade | None:
        task_id, trial_idx = _split_trial_id(spec.trial_id)

        if classify_trial_outcome(trajectory) is TrialOutcomeClass.INFRASTRUCTURE_ABORT:
            self.logger.info(
                "Trial aborted by infrastructure - not graded",
                task_id=task_id,
                trial_index=trial_idx,
                status=trajectory.status.value,
                termination_reason=(
                    trajectory.termination_reason.value if trajectory.termination_reason else None
                ),
            )
            return None

        if trajectory.status in (TrialStatus.ERROR, TrialStatus.TIMEOUT):
            self.logger.info(
                "Trial did not complete successfully - automatic fail",
                task_id=task_id,
                trial_index=trial_idx,
                status=trajectory.status.value,
            )
            return Grade(
                binary_pass=False,
                score=0.0,
                components=GradeComponents(state_checks=0.0),
                reasons=f"Trial failed with status: {trajectory.status.value}",
            )

        if trajectory.termination_reason == TerminationReason.STUCK_DETECTED:
            self.logger.info(
                "Trial stuck - automatic fail",
                task_id=task_id,
                trial_index=trial_idx,
                termination_reason=trajectory.termination_reason.value,
            )
            return Grade(
                binary_pass=False,
                score=0.0,
                components=GradeComponents(state_checks=0.0),
                reasons="Agent got stuck (repeated actions without progress)",
            )

        llm_messages_json = encode_transcript_wire(trajectory, agent_system_prompt)
        grade_result = self.runner_client.grade_trial(
            trial_id=spec.trial_id,
            llm_messages_json=llm_messages_json,
            termination_reason=(
                trajectory.termination_reason.value if trajectory.termination_reason else None
            ),
        )

        if not (grade_result["success"] and grade_result["grade"]):
            error_msg = grade_result.get("error", "Unknown grading error")
            self.logger.error(
                "Grading RPC failed",
                task_id=task_id,
                trial_index=trial_idx,
                error=error_msg,
            )
            raise GradingFailedError(f"Grading failed for trial {spec.trial_id!r}: {error_msg}")

        grade = _parse_grade_result(grade_result["grade"])
        self.logger.info(
            "Grading via Runner RPC",
            task_id=task_id,
            trial_index=trial_idx,
            score=grade.score,
            binary_pass=grade.binary_pass,
        )
        return grade


def _split_trial_id(trial_id: str) -> tuple[str, int]:
    """Return ``(task_id, trial_index)`` from a canonical ``"{task_id}:{idx}"`` id."""
    task_id, idx_s = trial_id.rsplit(":", 1)
    return task_id, int(idx_s)


def _parse_trace_check_results(payload: list[dict[str, Any]]) -> list[TraceConstraintResult]:
    """The runner's per-constraint trace-check verdicts, or a grading failure.

    Unlike the judge transcript and the state diff, nothing else in the bundle
    records which trace constraint failed, and an empty payload already means "no
    trace checks ran" — so a payload the host cannot read is a grade it cannot
    report, not an absent artifact. A ``kind`` outside the vocabulary is the
    version-skew shape this catches: it rejects rather than degrading into a
    grade whose sub-checks name conditions this engine does not have.
    """
    try:
        return [TraceConstraintResult(**item) for item in payload]
    except (TypeError, ValidationError) as exc:
        raise GradingFailedError(
            f"the runner's Grade.trace_checks payload is not readable: {exc}"
        ) from exc


def _parse_trace_checks_summary(payload: dict[str, Any] | None) -> TraceChecksSummary | None:
    """Which route won and whether a gate shut, or a grading failure.

    ``None`` is the runner that predates the field, and is preserved rather than
    filled in with an empty summary: a summary the host invented would read as a
    gate that was evaluated and held. A payload that is present and unreadable
    rejects for the reason :func:`_parse_trace_check_results` rejects one — a gate
    decides whether the trial passed, so a summary this engine cannot read is a
    grade it cannot report.
    """
    if payload is None:
        return None
    try:
        return TraceChecksSummary(**payload)
    except (TypeError, ValidationError) as exc:
        raise GradingFailedError(
            f"the runner's Grade.trace_checks_summary payload is not readable: {exc}"
        ) from exc


def _parse_grade_result(raw_grade: dict[str, Any]) -> Grade:
    """Materialise a :class:`Grade` from the runner's ``grade_trial`` dict.

    Handles optional sub-payloads: ``state_diff_json`` (post-mortem
    diagnostic), ``criterion_results`` (per-criterion breakdown),
    ``judge_report`` (usage + audit transcript when the rubric judge
    ran).
    """
    state_diff_parsed: dict[str, Any] | None = None
    if raw_grade.get("state_diff_json"):
        try:
            state_diff_parsed = json.loads(raw_grade["state_diff_json"])
        except (json.JSONDecodeError, TypeError):
            pass

    criterion_results = None
    raw_criterion_results = raw_grade.get("criterion_results")
    if raw_criterion_results:
        criterion_results = [CriterionResult(**cr) for cr in raw_criterion_results]

    # Per-check custom-checks breakdown. The wire carries the ``details``
    # payload JSON-encoded (proto ``string details_json``) so it can hold
    # arbitrary check-defined data; decode back to a dict, or leave ``None``
    # when the check emitted none. Malformed JSON is dropped to ``None``
    # rather than failing the whole grade parse.
    custom_checks_details: list[CustomCheckDetail] | None = None
    raw_custom_checks = raw_grade.get("custom_checks")
    if raw_custom_checks:
        custom_checks_details = []
        for entry in raw_custom_checks:
            details_payload: dict[str, Any] | None = None
            raw_details_json = entry.get("details_json") or ""
            if raw_details_json:
                try:
                    parsed_details = json.loads(raw_details_json)
                except (json.JSONDecodeError, TypeError):
                    parsed_details = None
                if isinstance(parsed_details, dict):
                    details_payload = parsed_details
            custom_checks_details.append(
                CustomCheckDetail(
                    check_name=entry.get("check_name", ""),
                    status=entry.get("status", ""),
                    score=float(entry.get("score", 0.0)),
                    message=entry.get("message", ""),
                    details=details_payload,
                )
            )

    trace_check_results = _parse_trace_check_results(raw_grade.get("trace_checks") or [])
    trace_checks_summary = _parse_trace_checks_summary(raw_grade.get("trace_checks_summary"))

    judge_usage: JudgeUsage | None = None
    judge_transcript: list[dict[str, Any]] | None = None
    judge_kb_gating: JudgeKbGating | None = None
    judge_inputs: JudgeInputs | None = None
    judge_custom_prompt: bool | None = None
    judge_agent_prompt_included: bool | None = None
    raw_report = raw_grade.get("judge_report")
    if raw_report:
        judge_custom_prompt = raw_report.get("custom_system_prompt", False)
        # Default include: a legacy/skewed runner that never carried field 15
        # graded with the agent policy present, so its faithful value is True.
        judge_agent_prompt_included = raw_report.get("include_agent_system_prompt", True)
        judge_usage = JudgeUsage(
            calls=raw_report.get("calls", 0),
            prompt_tokens=raw_report.get("prompt_tokens", 0),
            completion_tokens=raw_report.get("completion_tokens", 0),
            reasoning_tokens=raw_report.get("reasoning_tokens", 0),
            cost_usd=raw_report.get("cost_usd", 0.0),
            tool_calls=raw_report.get("tool_calls", 0),
            consistency_rejections=raw_report.get("consistency_rejections", 0),
        )
        judge_kb_gating = JudgeKbGating(
            knowledge_search_disabled=raw_report.get("knowledge_search_disabled", False),
            offered=list(raw_report.get("kb_tools_offered", [])),
            withheld=list(raw_report.get("kb_tools_withheld", [])),
        )
        # The judge's non-derivable run() inputs. An empty state_diff_text is the
        # wire encoding of "no diff was built" — map it back to None.
        judge_inputs = JudgeInputs(
            state_diff_text=raw_report.get("state_diff_text") or None,
            read_tools_offered=list(raw_report.get("read_tools_offered", [])),
        )
        raw_transcript = raw_report.get("transcript_json")
        if raw_transcript:
            try:
                parsed = json.loads(raw_transcript)
                if isinstance(parsed, list):
                    judge_transcript = parsed
            except (json.JSONDecodeError, TypeError):
                pass

    return Grade(
        binary_pass=raw_grade["binary_pass"],
        score=raw_grade["score"],
        components=GradeComponents(
            **{
                spec.core_field: raw_grade["components"].get(spec.name, -1.0)
                for spec in GRADE_COMPONENTS
            }
        ),
        reasons=raw_grade.get("reasons", ""),
        state_diff=state_diff_parsed,
        custom_checks_details=custom_checks_details,
        criterion_results=criterion_results,
        judge_status=JudgeStatus.from_proto(raw_grade.get("judge_status", 0)),
        judge_usage=judge_usage,
        judge_transcript=judge_transcript,
        judge_kb_gating=judge_kb_gating,
        judge_inputs=judge_inputs,
        judge_custom_prompt=judge_custom_prompt,
        judge_agent_prompt_included=judge_agent_prompt_included,
        trace_check_results=trace_check_results,
        trace_checks_summary=trace_checks_summary,
    )


def runner_rpc_trial_grader_factory(ctx: TrialGraderContext) -> RunnerRPCTrialGrader:
    """Build a :class:`RunnerRPCTrialGrader` from a grader context.

    Accepts ``ctx.runner_address is None`` at construction so orchestrator
    fixtures paired with an in-memory backend can still build the grader
    without touching the network — the misconfiguration surfaces when
    :meth:`RunnerRPCTrialGrader.grade` is first called against the empty
    address (see the loud-failure branch inside ``RunnerRPCTrialGrader``).
    """
    return RunnerRPCTrialGrader(runner_address=ctx.runner_address or "", logger=ctx.logger)


JudgeGradeFn = Callable[[TrialSpec, Trajectory, str], "Grade | None"]
"""The judge-backed grader's dispatch surface: given the trial's spec, its
completed trajectory, and the agent's system prompt, return the :class:`Grade`
the judge produced (or ``None`` when the trial produced nothing to grade).

Kept as a plain ``Callable`` alias so the seam accepts any host-side wiring
that reaches a judge: the offline replay path (``dx.cli.rejudge``), a
JudgeBackedTrialGrader wrapping :class:`~tolokaforge.core.grading.judge.LLMJudge`
directly, or a downstream package's judge integration.
"""


class JudgeBackedTrialGrader:
    """:class:`TrialGrader` that invokes an injected judge callable directly,
    without the runner's state / transcript / custom-check machinery.

    The seam's second registered implementation (see ADR-0035, Decision 5):
    a grader that dispatches to a judge rather than a runner-side RPC. Ships
    as the plug-in shape for judge-only tasks (rubric-only fixtures, offline
    replay); production integration with :class:`LLMJudge` is deferred as a
    follow-up (see the milestone umbrella #1181).

    The Protocol contract is unchanged: :meth:`grade` returns a :class:`Grade`,
    or ``None`` when the trial produced nothing to grade. Auto-fail on
    error / timeout / stuck-detected matches :class:`RunnerRPCTrialGrader` so
    both implementations are drop-in swaps for the caller.
    """

    def __init__(self, judge_fn: JudgeGradeFn, logger: StructuredLogger) -> None:
        self.judge_fn = judge_fn
        self.logger = logger

    def grade(
        self,
        spec: TrialSpec,
        trajectory: Trajectory,
        agent_system_prompt: str,
    ) -> Grade | None:
        task_id, trial_idx = _split_trial_id(spec.trial_id)

        if classify_trial_outcome(trajectory) is TrialOutcomeClass.INFRASTRUCTURE_ABORT:
            self.logger.info(
                "Trial aborted by infrastructure - not graded",
                task_id=task_id,
                trial_index=trial_idx,
                status=trajectory.status.value,
                termination_reason=(
                    trajectory.termination_reason.value if trajectory.termination_reason else None
                ),
            )
            return None

        if trajectory.status in (TrialStatus.ERROR, TrialStatus.TIMEOUT):
            self.logger.info(
                "Trial did not complete successfully - automatic fail",
                task_id=task_id,
                trial_index=trial_idx,
                status=trajectory.status.value,
            )
            return Grade(
                binary_pass=False,
                score=0.0,
                components=GradeComponents(llm_judge=0.0),
                reasons=f"Trial failed with status: {trajectory.status.value}",
            )

        if trajectory.termination_reason == TerminationReason.STUCK_DETECTED:
            self.logger.info(
                "Trial stuck - automatic fail",
                task_id=task_id,
                trial_index=trial_idx,
                termination_reason=trajectory.termination_reason.value,
            )
            return Grade(
                binary_pass=False,
                score=0.0,
                components=GradeComponents(llm_judge=0.0),
                reasons="Agent got stuck (repeated actions without progress)",
            )

        grade = self.judge_fn(spec, trajectory, agent_system_prompt)
        if grade is None:
            self.logger.info(
                "Judge returned no verdict",
                task_id=task_id,
                trial_index=trial_idx,
            )
        else:
            self.logger.info(
                "Grading via judge callable",
                task_id=task_id,
                trial_index=trial_idx,
                score=grade.score,
                binary_pass=grade.binary_pass,
            )
        return grade


def _unwired_judge_fn(
    spec: TrialSpec,  # noqa: ARG001 — Protocol arg accepted at the seam
    trajectory: Trajectory,  # noqa: ARG001
    agent_system_prompt: str,  # noqa: ARG001
) -> Grade | None:
    """Default judge dispatch for the ``judge_only`` entry point.

    The factory has no way to receive a real judge instance through the current
    ``TrialGraderContext`` — production wiring (offline-replay integration,
    live :class:`~tolokaforge.core.grading.judge.LLMJudge` construction from
    per-task rubric config) is deferred as a follow-up on the umbrella. Until
    that ships, invoking the grader raises loud rather than degrading to a
    silent zero score.
    """
    raise NotImplementedError(
        "judge_only trial grader is registered but not yet wired to a production "
        "judge. Inject a JudgeGradeFn callable via JudgeBackedTrialGrader(...) "
        "directly, or wait for the follow-up that folds offline rejudge onto this "
        "seam. See ADR-0035 and the grader-detachment umbrella."
    )


class GraderRPCTrialGrader:
    """:class:`TrialGrader` that dispatches to the standalone
    :class:`~tolokaforge.grader.service.GraderServiceImpl` over gRPC.

    Deployment-shape sibling of :class:`RunnerRPCTrialGrader`: same call
    shape, same auto-fail branches, same wire semantics — but bound to the
    grader service's address instead of the runner's. Registered under the
    ``grader_rpc`` entry point; selected by task config with
    ``grader: grader_rpc``.

    Per ADR-0035, the grader service is expected to run on a different
    machine from the runner, on its own release cadence and scale unit. This
    grader owns its own :class:`GrpcGraderClient`; tests may inject a stub
    ``grader_client`` to skip real gRPC.
    """

    def __init__(
        self,
        grader_address: str,
        logger: StructuredLogger,
        *,
        grader_client: GrpcGraderClient | None = None,
    ) -> None:
        self.grader_address = grader_address
        self.logger = logger
        if grader_client is None:
            from tolokaforge.grader.client import GrpcGraderClient as _Client

            grader_client = _Client(grader_address=grader_address)
        self.grader_client = grader_client

    def grade(
        self,
        spec: TrialSpec,
        trajectory: Trajectory,
        agent_system_prompt: str,
    ) -> Grade | None:
        task_id, trial_idx = _split_trial_id(spec.trial_id)

        if classify_trial_outcome(trajectory) is TrialOutcomeClass.INFRASTRUCTURE_ABORT:
            self.logger.info(
                "Trial aborted by infrastructure - not graded",
                task_id=task_id,
                trial_index=trial_idx,
                status=trajectory.status.value,
                termination_reason=(
                    trajectory.termination_reason.value if trajectory.termination_reason else None
                ),
            )
            return None

        if trajectory.status in (TrialStatus.ERROR, TrialStatus.TIMEOUT):
            self.logger.info(
                "Trial did not complete successfully - automatic fail",
                task_id=task_id,
                trial_index=trial_idx,
                status=trajectory.status.value,
            )
            return Grade(
                binary_pass=False,
                score=0.0,
                components=GradeComponents(state_checks=0.0),
                reasons=f"Trial failed with status: {trajectory.status.value}",
            )

        if trajectory.termination_reason == TerminationReason.STUCK_DETECTED:
            self.logger.info(
                "Trial stuck - automatic fail",
                task_id=task_id,
                trial_index=trial_idx,
                termination_reason=trajectory.termination_reason.value,
            )
            return Grade(
                binary_pass=False,
                score=0.0,
                components=GradeComponents(state_checks=0.0),
                reasons="Agent got stuck (repeated actions without progress)",
            )

        llm_messages_json = encode_transcript_wire(trajectory, agent_system_prompt)
        grade_result = self.grader_client.grade(
            trial_id=spec.trial_id,
            llm_messages_json=llm_messages_json,
            termination_reason=(
                trajectory.termination_reason.value if trajectory.termination_reason else None
            ),
        )

        if not (grade_result["success"] and grade_result["grade"]):
            error_msg = grade_result.get("error", "Unknown grading error")
            self.logger.error(
                "Grader service RPC failed",
                task_id=task_id,
                trial_index=trial_idx,
                error=error_msg,
            )
            raise GradingFailedError(f"Grading failed for trial {spec.trial_id!r}: {error_msg}")

        grade = _parse_grade_result(grade_result["grade"])
        self.logger.info(
            "Grading via grader service RPC",
            task_id=task_id,
            trial_index=trial_idx,
            score=grade.score,
            binary_pass=grade.binary_pass,
        )
        return grade


def grader_rpc_trial_grader_factory(ctx: TrialGraderContext) -> GraderRPCTrialGrader:
    """Build a :class:`GraderRPCTrialGrader` from a grader context.

    Uses ``ctx.grader_address`` when set (grader service on a distinct host)
    and falls back to ``ctx.runner_address`` when the operator has not split
    the two — single-address single-host deployments continue to work.

    Fails loud when neither field is set (in-memory backend + grader_rpc is
    a misconfiguration that would otherwise surface as a 30 s connect hang).
    """
    address = ctx.grader_address if ctx.grader_address is not None else ctx.runner_address
    if address is None:
        raise ValueError(
            "grader_rpc trial grader requires either ``grader_address`` or "
            "``runner_address`` on the grader context (got None on both). "
            "Pair a network-reachable backend with the grader_rpc grader, or "
            "select a grader that does not need an address."
        )
    return GraderRPCTrialGrader(grader_address=address, logger=ctx.logger)


class QueueTrialGrader:
    """:class:`TrialGrader` that publishes grade jobs to a :class:`GradeBroker`
    and blocks on a per-trial :class:`concurrent.futures.Future`.

    Where :class:`GraderRPCTrialGrader` gives independent deploy, this gives
    independent *throughput scale*: grader workers consume the queue in
    parallel, so orchestrator worker threads no longer serialise on grader
    latency. The plug-in Protocol stays synchronous — the future keeps the
    call semantics intact — and the queue backend is a plug-in behind the
    :class:`GradeBroker` Protocol.

    Ships with an :class:`~tolokaforge.grader.queue.InMemoryGradeBroker`
    reference backend (ADR-0035's Decision 3: Redis Streams as the
    reference wire; other backends behind the same Protocol are follow-ups).
    Tests inject any :class:`GradeBroker` to exercise the seam.
    """

    #: Timeout the grader waits for the broker's future to resolve. Long
    #: because a queue backend's worker pool may saturate; a hard failure
    #: at the seam layer would mask a scaling problem the operator can
    #: fix by adding consumers. Tunable via the constructor.
    DEFAULT_TIMEOUT_S: float = 600.0

    def __init__(
        self,
        broker: GradeBroker,
        logger: StructuredLogger,
        *,
        timeout_s: float | None = None,
    ) -> None:
        self.broker = broker
        self.logger = logger
        self.timeout_s = timeout_s if timeout_s is not None else self.DEFAULT_TIMEOUT_S

    def grade(
        self,
        spec: TrialSpec,
        trajectory: Trajectory,
        agent_system_prompt: str,
    ) -> Grade | None:
        from tolokaforge.grader.queue import GradeJob, new_job_id

        task_id, trial_idx = _split_trial_id(spec.trial_id)

        if classify_trial_outcome(trajectory) is TrialOutcomeClass.INFRASTRUCTURE_ABORT:
            self.logger.info(
                "Trial aborted by infrastructure - not graded",
                task_id=task_id,
                trial_index=trial_idx,
                status=trajectory.status.value,
            )
            return None

        if trajectory.status in (TrialStatus.ERROR, TrialStatus.TIMEOUT):
            self.logger.info(
                "Trial did not complete successfully - automatic fail",
                task_id=task_id,
                trial_index=trial_idx,
                status=trajectory.status.value,
            )
            return Grade(
                binary_pass=False,
                score=0.0,
                components=GradeComponents(state_checks=0.0),
                reasons=f"Trial failed with status: {trajectory.status.value}",
            )

        if trajectory.termination_reason == TerminationReason.STUCK_DETECTED:
            self.logger.info(
                "Trial stuck - automatic fail",
                task_id=task_id,
                trial_index=trial_idx,
                termination_reason=trajectory.termination_reason.value,
            )
            return Grade(
                binary_pass=False,
                score=0.0,
                components=GradeComponents(state_checks=0.0),
                reasons="Agent got stuck (repeated actions without progress)",
            )

        llm_messages_json = encode_transcript_wire(trajectory, agent_system_prompt)
        job = GradeJob(
            job_id=new_job_id(),
            trial_id=spec.trial_id,
            llm_messages_json=llm_messages_json,
            termination_reason=(
                trajectory.termination_reason.value if trajectory.termination_reason else ""
            ),
            task_config_json="",
            agent_system_prompt=agent_system_prompt,
        )
        future = self.broker.publish_job(job)
        try:
            grade = future.result(timeout=self.timeout_s)
        except Exception as exc:  # noqa: BLE001 — surface loudly with our own type
            self.logger.error(
                "Queue-backed grader failed",
                task_id=task_id,
                trial_index=trial_idx,
                error=str(exc),
            )
            raise GradingFailedError(f"Grading failed for trial {spec.trial_id!r}: {exc}") from exc

        if grade is None:
            self.logger.info(
                "Queue-backed grader returned no verdict",
                task_id=task_id,
                trial_index=trial_idx,
            )
        else:
            self.logger.info(
                "Grading via queue-backed grader",
                task_id=task_id,
                trial_index=trial_idx,
                score=grade.score,
                binary_pass=grade.binary_pass,
            )
        return grade


def queue_trial_grader_factory(ctx: TrialGraderContext) -> QueueTrialGrader:  # noqa: ARG001
    """Registered ``queue`` factory. Fails loud until a broker + workers are wired.

    The context has no broker-selection field yet and no consumer pool is
    provisioned by the engine, so a real ``grade: queue`` selection would
    publish to a broker no one is listening to and hang for
    ``QueueTrialGrader.DEFAULT_TIMEOUT_S`` before failing. Raising here
    surfaces the misconfiguration at orchestrator startup, before any trial
    dispatches, and points the operator at the follow-up that will thread
    broker configuration through the context.

    Tests construct :class:`QueueTrialGrader` directly with a broker + a
    controlled worker pool — see ``tests/canonical/test_queue_trial_grader.py``.
    """
    raise NotImplementedError(
        "``queue`` trial grader is registered but not yet wired to a broker + "
        "worker pool at the engine layer. Instantiate ``QueueTrialGrader`` "
        "directly with your own ``GradeBroker`` for now; the factory will land "
        "once ``TrialGraderContext`` carries broker-selection configuration."
    )


def judge_backed_trial_grader_factory(ctx: TrialGraderContext) -> JudgeBackedTrialGrader:
    """Build a :class:`JudgeBackedTrialGrader` from a grader context.

    The default judge dispatch raises :class:`NotImplementedError` until a
    production judge is wired through the context (see the follow-up on the
    grader-detachment umbrella). The class is directly constructible with a
    real :data:`JudgeGradeFn` for tests and for the future offline-replay
    integration.
    """
    return JudgeBackedTrialGrader(judge_fn=_unwired_judge_fn, logger=ctx.logger)
