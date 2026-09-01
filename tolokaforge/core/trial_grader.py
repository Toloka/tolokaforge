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
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import ValidationError

from tolokaforge.core.failure_attribution import TrialOutcomeClass, classify_trial_outcome
from tolokaforge.core.grading.grade_components import GRADE_COMPONENTS
from tolokaforge.core.grading.judge_only_helpers import run_judge_only_for_trajectory
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
from tolokaforge.core.plugin_registry import load_grading_method
from tolokaforge.core.trial import TrialSpec
from tolokaforge.grader.wire_snapshot import build_grade_request_fields
from tolokaforge.runner.models import RunnerGradingConfig

if TYPE_CHECKING:
    from tolokaforge.core.llm.client import LLMClient
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.plugin_registry import TrialGraderContext
    from tolokaforge.core.runtime import RuntimeBackend
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

        ``None`` is the answer where no verdict can be computed: the agent
        never got to run, or the party that would compute the verdict is
        the one that lost the trial. The absence is not representable as a
        score, so a caller that forgets to branch fails instead of reading
        a fabricated zero.

        Raises:
            GradingFailedError: the trial was measured but grading could
                not produce a verdict. Distinct from ``None``: there is a
                verdict to compute and computing it failed.
        """
        ...

    # A ``close()`` method is a convention on this Protocol rather than a
    # requirement: adding it to a ``@runtime_checkable`` Protocol would
    # break ``isinstance`` for every duck-typed impl that has none, and
    # would silently reject downstream registered graders that don't own
    # transport resources. The built-in impls each define a ``close()``
    # (a noop for the transport-less ones; a real teardown for the queue
    # transport + grader_rpc); orchestrator teardown calls it via
    # ``getattr(grader, "close", None)`` so a grader without one is
    # tolerated.


class RunnerRPCTrialGrader:
    """Production :class:`TrialGrader`. Dispatches to the runner's
    ``grade_trial`` gRPC for real grading, short-circuits with an
    auto-fail :class:`Grade` when the trajectory shape rules out a
    meaningful judge result, returns ``None`` where no verdict exists to
    compute — a trial that never ran, and one whose runner lost it — and
    raises :class:`GradingFailedError` when the RPC could not produce a
    verdict.

    Built per-run from a :class:`TrialGraderContext` — serialisable
    configuration (``runner_address`` + logger) for the out-of-process
    shape, plus one optional in-process routing shim (``runtime_backend``).

    Two dispatch shapes, one class:

    * **Out-of-process (P2/P3):** ``ctx.runtime_backend`` is ``None`` on the
      wire. The grader owns its own
      :class:`~tolokaforge.core.shared_stack_runtime.GrpcRunnerClient` bound
      to ``runner_address``, so it stays independent of the orchestrator's
      runtime backend — the ADR-0038 seam that lets a grader run on a
      different machine.
    * **In-process (per-trial runtimes):** ``ctx.runtime_backend`` is set to
      the live backend. ``:meth:`grade`` delegates to
      ``runtime_backend.grade_trial(...)``, which for
      :class:`PerTrialRuntimeBackend` routes the RPC to the trial's own
      runner endpoint (each trial owns its endpoint, so no single
      ``runner_address`` can reach them all). No gRPC client is built for
      this shape — the ``runner_address`` on the context is unused here.

    Preference at grade time: ``runtime_backend`` wins when both are set;
    ``runner_client`` is the fallback path. That preserves the P2/P3
    "grader on another machine" story on the wire — a ``TrialGraderContext``
    whose ``runtime_backend`` is ``None`` sees exactly the pre-existing
    address-only dispatch.

    Tests may pass a ``runner_client`` directly to bypass real gRPC; the
    stub must expose a ``grade_trial(trial_id, llm_messages_json, ...)``
    method returning the same dict shape as
    :meth:`GrpcRunnerClient.grade_trial`.

    Note on ``run_trial.py``: the container-shim entry point constructs
    its own ``TrialGraderContext`` with a stable ``EXECUTOR_ADDRESS`` and
    no ``runtime_backend``; the fallback client path is the correct
    behaviour for that call site.
    """

    def __init__(
        self,
        runner_address: str,
        logger: StructuredLogger,
        *,
        runner_client: GrpcRunnerClient | None = None,
        runtime_backend: RuntimeBackend | None = None,
    ) -> None:
        self.runner_address = runner_address
        self.logger = logger
        self.runtime_backend = runtime_backend
        # Skip the gRPC channel entirely when a live backend is the target;
        # the ``runner_address`` on the context is unused for
        # ``PerTrialRuntimeBackend`` (each trial has its own endpoint).
        if runner_client is None and runtime_backend is None:
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

        if trajectory.termination_reason == TerminationReason.TRIAL_LOST:
            self.logger.info(
                "Trial lost by the runner - not graded",
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
        target = self.runtime_backend if self.runtime_backend is not None else self.runner_client
        if target is None:  # __init__ prevents this — defence in depth
            raise RuntimeError("RunnerRPCTrialGrader has no dispatch target")
        grade_result = target.grade_trial(
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

    def close(self) -> None:
        """The runner RPC client owns nothing worth explicit teardown at
        grader-scope; the orchestrator closes the runtime backend that owns
        the channel."""


def _split_trial_id(trial_id: str) -> tuple[str, int]:
    """Return ``(task_id, trial_index)`` from a canonical ``"{task_id}:{idx}"`` id."""
    task_id, idx_s = trial_id.rsplit(":", 1)
    return task_id, int(idx_s)


def _refuse_hash_grading_on_grader_rpc(spec: TrialSpec) -> None:
    """Fail loud when a hash-enabled task is dispatched over ``grader_rpc``.

    The grader-side substrate (:class:`LiveRunnerCallbackGradingSubstrate`) is
    read-only: it dials the runner's ``SubstrateService`` for state reads and
    exposes no snapshot / reset / replay path. Hash grading depends on
    replaying ``golden_actions`` against a reset initial state — the substrate
    surface required to do that ships only inside the runner. Refuse at the
    client so the misconfiguration surfaces without a gRPC round-trip and the
    error names the actionable branch the operator has.
    """
    state_checks = spec.task.grading.state_checks
    if state_checks is not None and state_checks.hash_enabled:
        raise GradingFailedError(
            "grader_rpc cannot execute hash-based grading — the substrate is "
            "read-only. Configure grader: runner_rpc for this task, or "
            "disable hash_enabled."
        )


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

    Dispatch preference at grade time:

    * When ``ctx.runtime_backend`` is set, ``RunnerRPCTrialGrader.grade``
      delegates to ``runtime_backend.grade_trial(...)`` and no gRPC channel
      is built. This is the in-process path for per-trial runtime backends
      (each trial owns its own runner endpoint, so no single
      ``runner_address`` can reach them all).
    * Otherwise, the grader owns its own :class:`GrpcRunnerClient` bound to
      ``ctx.runner_address``. This is the P2/P3 shape ADR-0038 pins — the
      wire carries no live backend, so out-of-process construction sees
      exactly this branch.

    Accepts ``ctx.runner_address is None`` at construction so orchestrator
    fixtures paired with an in-memory backend can still build the grader
    without touching the network. When ``ctx.runtime_backend`` is also
    ``None`` the misconfiguration surfaces at the first :meth:`grade` call
    as a :class:`ConnectionError` from :class:`GrpcRunnerClient.connect`'s
    health-check retry loop (~30 s timeout) rather than a synchronous
    ``ValueError`` — the address is only dialled when it is needed.
    """
    return RunnerRPCTrialGrader(
        runner_address=ctx.runner_address or "",
        logger=ctx.logger,
        runtime_backend=ctx.runtime_backend,
    )


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

    The seam's second registered implementation (see ADR-0038, Decision 5):
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

        if trajectory.termination_reason == TerminationReason.TRIAL_LOST:
            # Same shape as ``RunnerRPCTrialGrader``: a trial the runner
            # lost is never counted as an agent failure. Returning
            # ``None`` keeps the trial ungradeable — in the denominator
            # of ``total_trials`` but excluded from every rate — while
            # ``Grade(binary_pass=False)`` would score it as an agent
            # miss the measurement does not support.
            self.logger.info(
                "Trial lost by the runner - not graded",
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

    def close(self) -> None:
        """No-op: the injected judge callable owns its own lifecycle."""


class GraderRPCTrialGrader:
    """:class:`TrialGrader` that dispatches to the standalone
    :class:`~tolokaforge.grader.service.GraderServiceImpl` over gRPC.

    Deployment-shape sibling of :class:`RunnerRPCTrialGrader`: same call
    shape, same auto-fail branches, same wire semantics — but bound to the
    grader service's address instead of the runner's. Registered under the
    ``grader_rpc`` entry point; selected by task config with
    ``grader: grader_rpc``.

    Per ADR-0038, the grader service is expected to run on a different
    machine from the runner, on its own release cadence and scale unit. This
    grader owns its own :class:`GrpcGraderClient`; tests may inject a stub
    ``grader_client`` to skip real gRPC.
    """

    def __init__(
        self,
        grader_address: str,
        logger: StructuredLogger,
        *,
        runner_substrate_address: str | None = None,
        grader_client: GrpcGraderClient | None = None,
    ) -> None:
        self.grader_address = grader_address
        self.logger = logger
        self.runner_substrate_address = runner_substrate_address or ""
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

        if trajectory.termination_reason == TerminationReason.TRIAL_LOST:
            # Mirror ``RunnerRPCTrialGrader``: a lost trial is
            # ungradeable, not an agent failure. ``None`` keeps it in
            # the denominator (``total_trials``) but excluded from every
            # rate — a ``Grade(binary_pass=False)`` would inflate
            # failure rates with a substrate loss.
            self.logger.info(
                "Trial lost by the runner - not graded",
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

        _refuse_hash_grading_on_grader_rpc(spec)
        if not self.runner_substrate_address:
            raise GradingFailedError(
                "grader_rpc requires runner_substrate_address on the grader context — "
                "see RunConfig.grader. The composite dispatcher dials the runner's "
                "SubstrateService per trial and needs a reachable address."
            )

        llm_messages_json = encode_transcript_wire(trajectory, agent_system_prompt)
        wire = build_grade_request_fields(
            spec=spec,
            runner_substrate_address=self.runner_substrate_address,
        )
        grade_result = self.grader_client.grade(
            trial_id=spec.trial_id,
            llm_messages_json=llm_messages_json,
            termination_reason=(
                trajectory.termination_reason.value if trajectory.termination_reason else None
            ),
            task_config_json=wire.task_config_json,
            judge_model_config_json=wire.judge_model_config_json,
            task_description_json=wire.task_description_json,
            runner_substrate_address=wire.runner_substrate_address,
        )

        if not grade_result["success"]:
            error_msg = grade_result.get("error") or "Unknown grading error"
            self.logger.error(
                "Grader service RPC failed",
                task_id=task_id,
                trial_index=trial_idx,
                error=error_msg,
            )
            raise GradingFailedError(f"Grading failed for trial {spec.trial_id!r}: {error_msg}")

        if grade_result.get("no_verdict"):
            # The wire distinguishes "nothing to grade" (no verdict) from a
            # grading failure — the ``TrialGrader`` Protocol returns ``None``
            # for the former and raises ``GradingFailedError`` for the latter.
            self.logger.info(
                "Grader service produced no verdict",
                task_id=task_id,
                trial_index=trial_idx,
            )
            return None

        grade = _parse_grade_result(grade_result["grade"])
        self.logger.info(
            "Grading via grader service RPC",
            task_id=task_id,
            trial_index=trial_idx,
            score=grade.score,
            binary_pass=grade.binary_pass,
        )
        return grade

    def close(self) -> None:
        """Release the gRPC channel the injected / auto-built client owns."""
        close = getattr(self.grader_client, "close", None)
        if callable(close):
            close()


def grader_rpc_trial_grader_factory(ctx: TrialGraderContext) -> GraderRPCTrialGrader:
    """Build a :class:`GraderRPCTrialGrader` from a grader context.

    Uses ``ctx.grader_address`` when set (grader service on a distinct host)
    and falls back to ``ctx.runner_address`` when the operator has not split
    the two — single-address single-host deployments continue to work.

    Fails loud when neither field is set (in-memory backend + grader_rpc is
    a misconfiguration that would otherwise surface as a 30 s connect hang).
    """
    # ``ctx.grader_address`` / ``ctx.runner_address`` are ``str | None`` on
    # the wire but the in-memory backend threads an empty string through
    # ``getattr(runtime_backend, "runner_address", None)`` when the field
    # is missing. Treat empty and ``None`` the same — either shape would
    # otherwise pass the None guard and surface as a 30 s gRPC connect
    # hang the docstring promises we catch here.
    address = ctx.grader_address or ctx.runner_address
    if not address:
        raise ValueError(
            "grader_rpc trial grader requires either ``grader_address`` or "
            "``runner_address`` on the grader context (got no usable value on "
            "either). Pair a network-reachable backend with the grader_rpc "
            "grader, or select a grader that does not need an address."
        )
    # ``SubstrateService`` shares the runner's listen port —
    # ``runner_substrate_address`` is the same value ``runner_rpc`` uses to
    # dial the runner's ``GradeTrial`` today. The grader-side dispatcher
    # opens a substrate channel to this address per trial.
    return GraderRPCTrialGrader(
        grader_address=address,
        logger=ctx.logger,
        runner_substrate_address=ctx.runner_address or "",
    )


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
    reference backend (ADR-0038's Decision 3: Redis Streams as the
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
        runner_substrate_address: str | None = None,
        timeout_s: float | None = None,
        workers: list[threading.Thread] | None = None,
        owns_broker: bool = False,
    ) -> None:
        self.broker = broker
        self.logger = logger
        self.runner_substrate_address = runner_substrate_address or ""
        self.timeout_s = timeout_s if timeout_s is not None else self.DEFAULT_TIMEOUT_S
        # ``workers`` and ``owns_broker`` name the two lifecycle handles
        # :meth:`close` releases. The factory (``queue_trial_grader_factory``)
        # constructs both the broker and the worker pool and hands them in
        # here; tests that inject only a broker leave both defaults so
        # :meth:`close` is a no-op that does not touch the injected broker.
        self._workers: list[threading.Thread] = list(workers) if workers else []
        self._owns_broker = owns_broker

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

        _refuse_hash_grading_on_grader_rpc(spec)
        if not self.runner_substrate_address:
            raise GradingFailedError(
                "queue-backed grader requires runner_substrate_address on the "
                "grader context — see RunConfig.grader. Each grade job carries "
                "the address so the worker's composite dispatch can dial the "
                "runner's SubstrateService."
            )

        llm_messages_json = encode_transcript_wire(trajectory, agent_system_prompt)
        wire = build_grade_request_fields(
            spec=spec,
            runner_substrate_address=self.runner_substrate_address,
        )
        job = GradeJob(
            job_id=new_job_id(),
            trial_id=spec.trial_id,
            llm_messages_json=llm_messages_json,
            termination_reason=(
                trajectory.termination_reason.value if trajectory.termination_reason else ""
            ),
            task_config_json=wire.task_config_json,
            judge_model_config_json=wire.judge_model_config_json,
            task_description_json=wire.task_description_json,
            runner_substrate_address=wire.runner_substrate_address,
        )
        future = self.broker.publish_job(job)
        try:
            grade = future.result(timeout=self.timeout_s)
        except Exception as exc:  # noqa: BLE001 — surface loudly with our own type
            # Timed-out and errored futures leave their broker entry live;
            # without this, one stuck worker leaks one dict entry per trial.
            # ``cancel_job`` is idempotent — a late ``publish_result`` from
            # the worker then finds no entry and returns cleanly (the
            # broker's post-close path treats the same shape as a no-op).
            self.broker.cancel_job(job.job_id)
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

    def close(self) -> None:
        """Shut down the broker and drain the worker pool this grader owns.

        Idempotent when broker close succeeds. If ``broker.close()`` raises
        (a transient failure a future backend might surface), the flag
        stays set so a second call retries the broker; the worker join
        still runs on both paths so threads always drain.
        """
        broker_closed = not self._owns_broker
        if self._owns_broker:
            close = getattr(self.broker, "close", None)
            if callable(close):
                try:
                    close()
                    broker_closed = True
                except Exception as exc:  # noqa: BLE001 — teardown must survive
                    self.logger.warning("Broker close() raised", error=str(exc))
            else:
                broker_closed = True
        for worker in self._workers:
            worker.join(timeout=5.0)
        self._workers.clear()
        if broker_closed:
            self._owns_broker = False


def queue_trial_grader_factory(ctx: TrialGraderContext) -> QueueTrialGrader:
    """Registered ``queue`` factory — builds broker + worker pool over ``grader_rpc``.

    Reads ``ctx.grader_config.queue`` for the worker count. Constructs
    the broker (:class:`InMemoryGradeBroker` today), spawns N daemon
    workers each holding a :class:`GrpcGraderClient` dialing
    ``ctx.grader_address or ctx.runner_address``, and returns a
    :class:`QueueTrialGrader` wrapping the broker. The returned grader
    owns both — :meth:`QueueTrialGrader.close` shuts them down at run
    end.

    Each worker forwards the ``GradeJob`` wire fields verbatim to the
    grader service (no re-decode: the queue's wire is the same
    projection ``grader_rpc`` already carries), parses the response into
    a :class:`Grade`, and publishes it back through the broker.

    Only ``worker_grader: grader_rpc`` is wired today. ``judge_only``
    workers need the judge-config surface (#1255) before they can be
    layered under the queue.
    """
    from tolokaforge.core.models.run_config import QueueGraderConfig
    from tolokaforge.grader.client import GrpcGraderClient
    from tolokaforge.grader.queue import InMemoryGradeBroker

    # Materialise the sub-block once so every default lives on
    # ``QueueGraderConfig`` and the factory never spells a competing one.
    cfg = (
        ctx.grader_config.queue
        if ctx.grader_config and ctx.grader_config.queue
        else QueueGraderConfig()
    )
    if cfg.worker_grader != "grader_rpc":
        raise ValueError(
            f"queue.worker_grader={cfg.worker_grader!r} is not yet wired. Supported "
            "today: 'grader_rpc'. Judge-backed wiring is tracked as a follow-up (#1255)."
        )
    address = ctx.grader_address or ctx.runner_address
    if not address:
        raise ValueError(
            "queue trial grader requires ``grader_address`` or ``runner_address`` on "
            f"the grader context (received grader_address={ctx.grader_address!r}, "
            f"runner_address={ctx.runner_address!r}; empty string counts as unset). "
            "Its workers need a live grader service to dial."
        )

    broker = InMemoryGradeBroker()
    workers: list[threading.Thread] = []
    for i in range(cfg.workers):
        client = GrpcGraderClient(grader_address=address)
        worker = threading.Thread(
            target=_queue_worker_loop,
            args=(broker, client, ctx.logger),
            name=f"queue-grader-worker-{i}",
            daemon=True,
        )
        worker.start()
        workers.append(worker)
    ctx.logger.info(
        "Queue-backed grader wired",
        worker_count=cfg.workers,
        worker_grader=cfg.worker_grader,
        address=address,
    )
    # Each worker closes its own client on ``BrokerClosed`` — the
    # ownership travels with the thread so we do not need a second
    # store-and-close on the grader.
    return QueueTrialGrader(
        broker=broker,
        logger=ctx.logger,
        runner_substrate_address=ctx.runner_address or "",
        workers=workers,
        owns_broker=True,
    )


def _queue_worker_loop(
    broker: GradeBroker,
    client: GrpcGraderClient,
    logger: StructuredLogger,
) -> None:
    """Consume ``GradeJob``s from ``broker``, forward wire fields to
    ``client``, publish the parsed :class:`Grade` back.

    Terminates cleanly when the broker closes: :class:`BrokerClosed`
    unwinds the loop; :meth:`QueueTrialGrader.close` shuts the broker
    before joining the workers.

    Blocks indefinitely on ``next_job`` — the close sentinel is the only
    shutdown signal, and a polling timeout would burn a wakeup per
    worker per second for the entire idle life of the run.
    """
    from tolokaforge.grader.queue import BrokerClosed, GradeJob, GradeResult

    while True:
        try:
            job = broker.next_job(timeout=None)
        except BrokerClosed:
            client.close()
            return
        if job is None:
            # Shouldn't happen with timeout=None (queue.get without a
            # timeout blocks forever), but the type still permits it.
            continue
        if not isinstance(job, GradeJob):
            # Publish an error so the producer's future.result() fails
            # loud instead of blocking DEFAULT_TIMEOUT_S. Losing a job to
            # a wire-shape regression is a bug — this makes it surface.
            job_id = getattr(job, "job_id", None)
            logger.error(
                "Queue worker received unexpected payload",
                payload_type=type(job).__name__,
                job_id=job_id,
            )
            if job_id is not None:
                broker.publish_result(
                    GradeResult(
                        job_id=job_id,
                        grade=None,
                        error=f"queue worker received unexpected payload of type {type(job).__name__}",
                    )
                )
            continue
        error = ""
        grade: Grade | None = None
        try:
            response = client.grade(
                trial_id=job.trial_id,
                llm_messages_json=job.llm_messages_json,
                termination_reason=job.termination_reason,
                task_config_json=job.task_config_json,
                judge_model_config_json=job.judge_model_config_json,
                task_description_json=job.task_description_json,
                runner_substrate_address=job.runner_substrate_address,
            )
            if not response["success"]:
                error = response.get("error") or "Unknown grading error"
            elif response.get("no_verdict"):
                grade = None
            else:
                grade = _parse_grade_result(response["grade"])
        except Exception as exc:  # noqa: BLE001 — worker must survive to consume next job
            error = f"{type(exc).__name__}: {exc}"
            logger.error("Queue worker dispatch failed", error=error)
        broker.publish_result(GradeResult(job_id=job.job_id, grade=grade, error=error))


_JUDGE_ONLY_EQUIVALENT_CONFIG = RunnerGradingConfig(
    grading_method="composite",
    weights={"llm_judge": 1.0},
)
"""The composite dispatch shape ``judge_only`` is equivalent to.

Pinned as a module constant so the parity gate at
``tests/canonical/test_judge_only_composite_llm_judge_only_parity.py``
can assert both paths agree on the same
:class:`~tolokaforge.runner.models.RunnerGradingConfig` at the config
layer. A future rename of either name reds the parity test at the config
layer before it reaches the grade-shape assertion.
"""


def judge_backed_trial_grader_factory(
    ctx: TrialGraderContext,
    *,
    llm_client: LLMClient | None = None,
) -> JudgeBackedTrialGrader:
    """Registered ``judge_only`` factory — builds a rubric-judge dispatcher.

    Reads the run-level ``grader.judge`` overrides (or the task's own
    ``grading.llm_judge.customization`` when no override is set) and
    routes the per-trial dispatch through
    :func:`~tolokaforge.core.grading.judge_only_helpers.run_judge_only_for_trajectory`
    — the same pure helper the runner-side composite path reaches when a
    task declares ``grading.grading_method = "composite"`` with
    ``weights: {llm_judge: 1.0}`` and no state / transcript-rule /
    trace-check / custom-check / KB surface. The equivalence is pinned
    by :data:`_JUDGE_ONLY_EQUIVALENT_CONFIG` and the byte-parity gate at
    ``tests/canonical/test_judge_only_composite_llm_judge_only_parity.py``.

    Failure surface at grade time (not factory time — the same factory
    serves both rubric-carrying and rubric-less tasks in one run, so
    per-task decisions belong at dispatch): a task with no
    ``grading.llm_judge`` block, and a run with no ``models["judge"]``.
    Both surface as :class:`GradingFailedError` naming the trial. An
    ``ERRORED`` judge result surfaces the same way from the helper.

    Fail-loud at factory time: :func:`load_grading_method` resolves the
    ``composite`` marker so a misconfigured install (missing the
    ``tolokaforge.grading_methods`` entry-point group) surfaces here
    rather than at grade time.

    ``llm_client`` is a test-only injection point mirroring
    :func:`~tolokaforge.core.grading.replay.replay_trial`; production
    callers leave it ``None`` and the judge builds its own client.
    """
    load_grading_method("composite")

    override = ctx.grader_config.judge if ctx.grader_config else None
    logger = ctx.logger

    def judge_fn(spec: TrialSpec, trajectory: Trajectory, agent_system_prompt: str) -> Grade | None:
        llm_judge_config = spec.task.grading.llm_judge
        if llm_judge_config is None:
            raise GradingFailedError(
                f"judge_only cannot grade trial {spec.trial_id!r}: the task "
                "declares no grading.llm_judge block."
            )
        if spec.judge_model_config is None:
            raise GradingFailedError(
                f"judge_only cannot grade trial {spec.trial_id!r}: the run "
                "config declares no models.judge; add one, or select a "
                "grader that does not need a judge model."
            )
        return run_judge_only_for_trajectory(
            trial_id=spec.trial_id,
            llm_judge_config=llm_judge_config,
            judge_model_config=spec.judge_model_config,
            trajectory=trajectory,
            agent_system_prompt=agent_system_prompt,
            override=override,
            llm_client=llm_client,
            logger=logger,
        )

    return JudgeBackedTrialGrader(judge_fn=judge_fn, logger=ctx.logger)
