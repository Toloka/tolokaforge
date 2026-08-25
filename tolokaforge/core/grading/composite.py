"""Composite grading dispatch — the surface every topology consumes.

Every deployment shape (aggregate image, independent grader container,
future trajectory-storage callback, future snapshot, future shared-mount)
runs one composite grade against a :class:`GradingSubstrate`. This module
carries the per-component helpers so they can be reused verbatim by every
topology.

Sub-component seams. Every sub-component evaluator reaches the composite
through its Protocol via a resolved instance passed as a kwarg — never a
direct import of the reference impl. The six shipped seams
(:class:`~tolokaforge.core.grading.check_runner.CheckExecutor`,
:class:`~tolokaforge.core.grading.judge_model_provider.JudgeModelProvider`,
:class:`~tolokaforge.core.grading.rubric_evaluator.RubricEvaluator`,
:class:`~tolokaforge.core.grading.transcript_rule_matcher.TranscriptRuleMatcher`,
:class:`~tolokaforge.core.grading.trace_check_operator.TraceCheckOperator`,
:class:`~tolokaforge.core.grading.state_check_backend.StateCheckBackend`)
resolve through :mod:`tolokaforge.core.plugin_registry` at the runner
boundary and are threaded here as constructor args. The ``.importlinter``
``composite-sub-component-seams`` contract enforces the negative-space of
this rule by forbidding module-level imports of the six reference-impl
modules from this file.

Layering exception. The composite lives at ``tolokaforge/core/grading/``
and imports a handful of symbols from the runner package: the wire-shape
config models and the ``pb2`` proto types from :mod:`tolokaforge.runner`,
and ``transcript_rules_author_keys`` from
:mod:`tolokaforge.runner.grading_ledger`. The runner is the sole owner of
the wire shapes; a composite → runner import for those is the accepted
one-way exception documented in ADR-0040.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tolokaforge.core.grading.check_runner import (
    _CHECK_EXECUTOR_ERROR_NAME,
    CheckExecutor,
)
from tolokaforge.core.grading.checks_helpers import (
    build_check_context,
    custom_checks_enabled,
    custom_checks_reason,
)
from tolokaforge.core.grading.checks_interface import (
    CheckResult,
    CheckResultSet,
    CustomChecksConfig,
    TaskContext,
    ToolCallStatus,
    Transcript,
)
from tolokaforge.core.grading.checks_interface import (
    Message as CheckMessage,
)
from tolokaforge.core.grading.checks_interface import (
    ToolCall as CheckToolCall,
)
from tolokaforge.core.grading.judge_result import JudgeResult
from tolokaforge.core.grading.key_manifest import EVALUATED, NO_TIMELINE_EVENTS_SKIP
from tolokaforge.core.grading.state_diff import render_state_diff
from tolokaforge.core.grading.substrate import SubstrateUnreachableError
from tolokaforge.core.grading.trace_checks import evaluate_trace_checks
from tolokaforge.core.grading.transcript import scored_transcript_rules
from tolokaforge.core.grading.transcript_wire import split_leading_system_message
from tolokaforge.runner import runner_pb2 as pb2
from tolokaforge.runner.grading_ledger import (
    DB_PROBES_KEY,
    JSONPATHS_KEY,
    transcript_rules_author_keys,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from tolokaforge.core.grading.judge_tools import DelegatingReadTool
    from tolokaforge.core.grading.rubric_evaluator import RubricEvaluator
    from tolokaforge.core.grading.state_check_backend import StateCheckBackend
    from tolokaforge.core.grading.substrate import GradingSubstrate
    from tolokaforge.core.grading.trace_checks import TraceChecksResult
    from tolokaforge.core.grading.trace_timeline import TrialTimeline
    from tolokaforge.core.grading.transcript import (
        TranscriptEvaluationResult,
        TranscriptRulesConfig,
    )
    from tolokaforge.core.grading.transcript_rule_matcher import TranscriptRuleMatcher
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import KeyAccountingRecord, ModelConfig
    from tolokaforge.runner.models import (
        LLMJudgeConfig,
        RunnerStateChecksConfig,
        TaskDescription,
        TraceChecksConfig,
    )


def grade_transcript_rules(
    *,
    trial_id: str,
    config: TranscriptRulesConfig,
    timeline: TrialTimeline,
    matcher: TranscriptRuleMatcher,
    logger: StructuredLogger,
) -> tuple[TranscriptEvaluationResult | None, dict[str, KeyAccountingRecord]]:
    """Score the pack's transcript rules against a trial timeline.

    Returns ``(result, accounted_keys)``. A ``None`` result is the empty-timeline
    decision coming back empty — every rule is skipped and its key gets an
    ``NO_TIMELINE_EVENTS_SKIP`` record. When only the activity floor is scored,
    its siblings are recorded skipped first so the floor's own record survives
    the blanket skip.

    :func:`scored_transcript_rules` stays inside this composite — it is the
    events-less-trial gate every deployment topology needs applied at the same
    layer, not per-matcher policy. The resolved
    :class:`~tolokaforge.core.grading.transcript_rule_matcher.TranscriptRuleMatcher`
    is invoked with the gate-narrowed config: a plug-in impl (``default`` in
    the shipping config; a downstream ruleset alongside) that owns the
    actual per-rule scoring.

    Substrate-independent: reads only the timeline (the runner already built
    it from the transcript). Called identically by every deployment topology.
    """
    scored_rules = scored_transcript_rules(timeline, config)
    if scored_rules is None:
        logger.info(
            f"GradeTrial: {trial_id} - Skipping transcript rules (no messages or tool calls)"
        )
        return None, dict.fromkeys(transcript_rules_author_keys(), NO_TIMELINE_EVENTS_SKIP)

    skipped_siblings: dict[str, KeyAccountingRecord] = {}
    if timeline.events:
        logger.info(f"GradeTrial: {trial_id} - Evaluating transcript rules")
    else:
        logger.info(
            f"GradeTrial: {trial_id} - Evaluating the activity floor alone "
            "(no messages or tool calls)"
        )
        skipped_siblings = dict.fromkeys(transcript_rules_author_keys(), NO_TIMELINE_EVENTS_SKIP)

    result = matcher.evaluate(rules=scored_rules, timeline=timeline)
    return result, {**skipped_siblings, **result.accounted_keys}


@dataclass(frozen=True)
class StateChecksReadResult:
    """The scored slots :func:`grade_state_checks_reads` returns.

    ``jsonpath_score`` / ``db_probe_score`` are ``None`` when the composite
    did not reach an assertion for that half — an empty checks list, or a
    probe-less pack — and the runner leaves the corresponding
    :class:`RunnerGradeComponents` slot untouched (which then folds as
    'component not evaluated'). Every author key the composite reached is
    added to ``accounted_keys`` so the caller can merge it into the
    RPC-level ledger.
    """

    jsonpath_score: float | None
    jsonpath_reasons: str | None
    db_probe_score: float | None
    db_probe_reasons: str | None
    accounted_keys: dict[str, KeyAccountingRecord] = field(default_factory=dict)


def grade_state_checks_reads(
    *,
    trial_id: str,
    config: RunnerStateChecksConfig,
    substrate: GradingSubstrate,
    state_check_backends: Mapping[str, StateCheckBackend],
    logger: StructuredLogger,
) -> StateChecksReadResult:
    """Score the pack's ``jsonpaths`` and ``db_probes`` against ``substrate``.

    ``state_check_backends`` is a name → resolved
    :class:`~tolokaforge.core.grading.state_check_backend.StateCheckBackend`
    mapping the runner resolves at startup — the shipping runner supplies
    ``{"jsonpath": JsonpathStateCheckBackend(), "db_probes":
    DbProbesStateCheckBackend()}``. Each backend owns its source's read
    strategy (which substrate accessors it consumes, how it handles absent
    state, whether it opens its own connections) so the composite dispatches
    without knowing either evaluator's internals. A downstream package
    registering a third source under the ``tolokaforge.state_check_backends``
    entry-point group (say, ``s3_diff``) becomes reachable here without a
    framework change.

    Substrate-independent: the composite forwards ``substrate`` verbatim and
    the resolved backend consumes only the accessors it needs. Hash grading
    is NOT part of this seam — its snapshot-and-replay semantics need write
    access to the trial's DB and stay runner-integrated on
    :meth:`RunnerServiceImpl._execute_hash_grading` (see the seam module
    docstring and ``docs/GRADER_SERVICE.md`` § "Sub-component plug-in seams").

    Sync-in-async note: the composite is a **sync** function. The InProcess
    substrate's factories block on ``run_coroutine_threadsafe`` to bridge to
    the runner's dedicated event-loop thread, which deadlocks when called
    from that loop. The runner therefore dispatches this function via
    ``loop.run_in_executor(None, ...)`` — matching the shipped
    ``_grade_llm_judge`` bridge — so the substrate's blocking reads and the
    ``db_probes`` backend's :func:`asyncio.run` land off the loop thread.
    """
    accounted: dict[str, KeyAccountingRecord] = {}

    jsonpath_checks = config.jsonpath_checks or []
    jsonpath_score: float | None = None
    jsonpath_reasons: str | None = None
    if jsonpath_checks:
        logger.info(f"GradeTrial: {trial_id} - Evaluating {len(jsonpath_checks)} jsonpath checks")
        jsonpath_score, jsonpath_reasons = state_check_backends["jsonpath"].query(
            expression=jsonpath_checks,
            substrate=substrate,
            trial_id=trial_id,
        )
        accounted[JSONPATHS_KEY] = EVALUATED
        if jsonpath_score is not None:
            logger.info(f"GradeTrial: {trial_id} - Jsonpath checks: score={jsonpath_score:.2f}")

    db_probe_score: float | None = None
    db_probe_reasons: str | None = None
    if config.db_probes:
        logger.info(f"GradeTrial: {trial_id} - Evaluating {len(config.db_probes)} db probes")
        probes = [probe.model_dump() for probe in config.db_probes]
        db_probe_score, db_probe_reasons = state_check_backends["db_probes"].query(
            expression=probes,
            substrate=substrate,
            trial_id=trial_id,
        )
        accounted[DB_PROBES_KEY] = EVALUATED
        if db_probe_score is not None:
            logger.info(f"GradeTrial: {trial_id} - DB probes: score={db_probe_score:.2f}")

    return StateChecksReadResult(
        jsonpath_score=jsonpath_score,
        jsonpath_reasons=jsonpath_reasons,
        db_probe_score=db_probe_score,
        db_probe_reasons=db_probe_reasons,
        accounted_keys=accounted,
    )


def grade_trace_checks(
    *,
    trial_id: str,
    config: TraceChecksConfig,
    timeline: TrialTimeline,
    logger: StructuredLogger,
) -> TraceChecksResult:
    """Score the pack's trace checks over the trial's event timeline.

    A result carrying no constraint verdicts is the trial that left no trace
    of itself — a timeline with neither a conversational turn nor a tool
    call — where every constraint would score against evidence the trial
    does not have. The component is then left out of the combine, and the
    evaluator's own accounting records the skip against each kind the block
    declared.

    Substrate-independent: reads only the timeline. Called identically by
    every deployment topology.
    """
    result = evaluate_trace_checks(timeline, config)
    if not result.constraints:
        logger.info(f"GradeTrial: {trial_id} - Skipping trace checks (no messages or tool calls)")
        return result
    logger.info(f"GradeTrial: {trial_id} - Trace checks: score={result.score:.2f}")
    return result


def grade_llm_judge(
    *,
    trial_id: str,
    config: LLMJudgeConfig,
    substrate: GradingSubstrate,
    rubric_evaluator: RubricEvaluator,
    llm_messages: list[dict[str, Any]],
    judge_model_config: ModelConfig,
    extra_read_tools: list[DelegatingReadTool],
    state_diff: str | None,
    logger: StructuredLogger,
) -> JudgeResult:
    """Run the read-only rubric judge against ``substrate`` and return its verdict.

    The evaluator reads through the substrate seam for all live evidence:
    :meth:`substrate.db_reader` for the read-only DB tools it exposes,
    :meth:`substrate.knowledge_search` for ``search_kb``, and
    :meth:`substrate.filesystem_root` for ``read_file`` (``None`` withholds it).
    ``state_diff`` is a runner-resolved passthrough — the caller renders the
    ``initial → final`` DB delta and hands it in; the composite forwards it
    verbatim.

    ``extra_read_tools`` is a runner-resolved passthrough: the caller reconstructs
    the agent's ``search_policy`` connector as :class:`DelegatingReadTool` s so
    the judge can reuse the SAME TypeSense connector the agent used. The composite
    forwards ``extra_read_tools`` verbatim to the evaluator.

    ``rubric_evaluator`` is the resolved :class:`RubricEvaluator` seam the
    runner supplies — a plug-in impl (``llm_judge`` in the shipping config,
    a downstream deterministic ruleset alongside) that owns the actual grade
    dispatch. The trial's :attr:`LLMJudgeConfig.customization` (KB gate,
    custom system-prompt, include-agent-system-prompt) is applied
    construction-side on the evaluator instance itself — the composite only
    forwards the per-trial evidence to :meth:`evaluator.evaluate`.

    Fail-loud contract: any judge malfunction — malformed ``submit_report`` past
    retries, budget/turn exhaustion, or a loop-terminal exception — surfaces as
    :attr:`JudgeStatus.ERRORED` with ``score is None``. Never a 0.0 / 0.5
    fallback. :class:`SubstrateUnreachableError` from a substrate read
    propagates so the dispatch site can translate it to ``GradingFailedError``.

    Sync-in-async note: the composite is a **sync** function. The InProcess
    substrate's factories block on ``run_coroutine_threadsafe`` to bridge to
    the runner's dedicated event-loop thread, which deadlocks when called
    from that loop. The runner therefore dispatches this function via
    ``loop.run_in_executor(None, ...)`` — matching the shipped
    ``grade_state_checks_reads`` bridge — so the substrate's blocking reads
    (and the evaluator's own DB reads) land off the loop thread.
    """
    agent_system_prompt, transcript = split_leading_system_message(list(llm_messages))
    return rubric_evaluator.evaluate(
        rubric=config.rubric,
        agent_system_prompt=agent_system_prompt,
        transcript=transcript,
        substrate=substrate,
        judge_model_config=judge_model_config,
        extra_read_tools=list(extra_read_tools),
        state_diff=state_diff,
    )


def build_judge_state_diff(
    *,
    trial_id: str,
    substrate: GradingSubstrate,
    initial_state_schemas: list[Any],
    id_fields: dict[str, str | list[str]],
    unstable_fields: set[tuple[str, str]],
    logger: StructuredLogger,
) -> str | None:
    """Render the ``initial → final`` DB state diff for the judge, or ``None``.

    ``None`` is the diff-first default declining itself when there is nothing to
    diff against: an empty ``initial_state`` — the shape non-DB tasks carry, and
    what filesystem-only tasks report — has no baseline, so the judge falls back
    to its read-only tools. The distinction between "no diff" and "diff
    unavailable" stays with :func:`render_state_diff`'s explicit "No changes"
    body for a diff that DID build but found no edits.

    The trial's declared ``state_checks.id_fields`` is layered over the task
    schemas' primary keys — the two together are the row-matching contract the
    diff renders against — and ``unstable_fields`` drops server-marked noise so
    only meaningful edits appear.

    Best-effort context, not a grade component: :class:`SubstrateUnreachableError`
    propagates so the seam can book the trial as ungradeable, but any other
    substrate read failure (DB hiccup, unexpected shape) degrades to ``None`` —
    the judge still has its read-only tools and the components already computed
    by this call site are preserved. The judge's own fail-loud contract still
    governs grading.
    """
    initial_tables = substrate.initial_state()
    if not initial_tables:
        return None
    try:
        final_state = substrate.final_state()
    except SubstrateUnreachableError:
        raise
    except Exception as exc:  # noqa: BLE001 — optional context, never fail the grade
        logger.warning(
            "Failed to build judge state diff; grading without it "
            f"(trial_id={trial_id}, error={exc})"
        )
        return None
    primary_keys: dict[str, str | list[str]] = {
        s.table_name: s.primary_key for s in initial_state_schemas
    }
    primary_keys.update(id_fields)
    return render_state_diff(
        initial_tables,
        final_state,
        primary_keys=primary_keys,
        unstable_fields=unstable_fields,
    )


def _build_runner_check_transcript(
    llm_messages: list[dict[str, Any]],
) -> Transcript:
    """Decode wire ``llm_messages`` into a :class:`Transcript` for a custom check.

    Wire ``tool_calls`` are OpenAI-shaped
    (``{"function": {"name", "arguments": <json_str>}}``); this decodes them
    back into :class:`ToolCall` with ``result=None`` (results are not carried in
    ``llm_messages_json``), mirroring the host-side transcript build so a check
    reads identical evidence from either grading path.
    """
    check_messages: list[CheckMessage] = []
    for msg in llm_messages:
        role = str(msg.get("role", ""))
        content = str(msg.get("content", "") or "")
        raw_tool_calls = msg.get("tool_calls") or []
        tool_calls: list[CheckToolCall] = []
        for raw_tc in raw_tool_calls:
            fn = raw_tc.get("function") or {}
            name = str(fn.get("name") or raw_tc.get("name") or "")
            raw_args: Any = fn.get("arguments", raw_tc.get("arguments"))
            if isinstance(raw_args, str):
                try:
                    args_dict = json.loads(raw_args) if raw_args else {}
                except (json.JSONDecodeError, TypeError):
                    args_dict = {}
            elif isinstance(raw_args, dict):
                args_dict = raw_args
            else:
                args_dict = {}
            tool_calls.append(
                CheckToolCall(
                    name=name,
                    arguments=args_dict,
                    result=None,
                    status=ToolCallStatus.SUCCESS,
                )
            )
        check_messages.append(CheckMessage(role=role, content=content, tool_calls=tool_calls))
    return Transcript(messages=check_messages)


def _check_result_to_wire(result: CheckResult) -> pb2.CustomCheckResult:
    """Convert a :class:`CheckResult` to the wire ``pb2.CustomCheckResult``.

    ``details`` (arbitrary dict) is JSON-encoded into the proto's
    ``details_json`` string; empty when the check emitted no details.
    """
    status_str = result.status.value if hasattr(result.status, "value") else str(result.status)
    details_json = json.dumps(result.details) if result.details else ""
    return pb2.CustomCheckResult(
        check_name=result.check_name,
        status=status_str,
        score=result.score,
        message=result.message,
        details_json=details_json,
    )


def _executor_error_to_wire(error: str) -> pb2.CustomCheckResult:
    """Wrap a top-level :class:`CheckResultSet` error as a wire result.

    The audit — module-load failure / timeout / executor crash — travels to
    the host under the reserved :data:`_CHECK_EXECUTOR_ERROR_NAME` sentinel
    so the reasons string is not the only place it survives.
    """
    return pb2.CustomCheckResult(
        check_name=_CHECK_EXECUTOR_ERROR_NAME,
        status="error",
        score=0.0,
        message=error,
        details_json="",
    )


def grade_custom_checks(
    *,
    trial_id: str,
    config: CustomChecksConfig | dict[str, Any] | None,
    substrate: GradingSubstrate,
    llm_messages: list[dict[str, Any]],
    task_description: TaskDescription,
    artifacts_dir: Path | None,
    check_executor: CheckExecutor,
    logger: StructuredLogger,
) -> tuple[float, list[pb2.CustomCheckResult], str | None]:
    """Run the pack's ``checks.py`` against the trial's evidence.

    Returns ``(score, wire_results, reason)``. The reason is the sentence
    :func:`custom_checks_reason` renders and is what ``Grade.reasons`` carries
    for this component; every return that ran or tried to run supplies one, so a
    suite that failed before it started still says why. ``None`` is reserved for
    the one case with no suite to describe: a pack that declared no
    ``custom_checks`` block or disabled the one it declared.

    A missing/disabled config returns ``(-1.0, [], None)`` so
    :func:`combine_grade_components` treats the component as not-evaluated (the
    empty-active-set guard then fires for a custom-checks-only pack instead of
    silently passing). ``config`` accepts either the raw pack dict — the shape
    :attr:`RunnerGradingConfig.custom_checks` carries — or an already-parsed
    :class:`CustomChecksConfig`, so both the runner call site and a direct
    composite caller share this entry point.

    On executor error (missing ``checks.py``, module load failure, timeout): a
    sentinel wire entry preserves the audit, and the score follows
    ``fail_on_error`` — ``0.0`` when true (contributes to the weighted total as
    a fail), ``-1.0`` when false (component not evaluated).

    A suite that ran and decided nothing — every check skipped, or the file
    declared none — also returns ``-1.0``, the same answer the core engine
    reaches through the shared :attr:`CheckResultSet.decided_something`. Its
    aggregate over zero verdicts is ``0.0``, which would fold as a component
    that failed.

    Substrate reads: :meth:`substrate.initial_state` supplies
    ``initial_state_json_db`` (``None`` when the pack declared no baseline);
    :meth:`substrate.final_state` supplies the ``final_env_state`` the pack's
    ``@check`` functions read via ``ctx.final_state``. Both are the RAW tables
    shape the Protocol pins for this call site — no ``['db']`` index.

    Degrade-to-empty per shipped semantics: any failure reading
    :meth:`substrate.final_state` — DB Service unreachable, trial never
    registered (empty ``initial_state`` skips ``RegisterTrial``'s DB init),
    connection reset mid-grade — is caught here and grading proceeds against
    ``final_env_state = {}``. The audit signal is the ``final DB state fetch
    failed`` log line: the wording is a stability contract; downstream tooling
    greps for it verbatim. This is the ONE broad-except in the composite —
    any substrate raising the same class of failure gets the same fallback,
    so a non-InProcess topology does not have to rebuild this branch in its
    own factory.

    Sync-in-async note: the composite is a **sync** function. The InProcess
    substrate's ``final_state`` factory blocks on ``run_coroutine_threadsafe``
    to bridge to the runner's dedicated event-loop thread, and
    ``check_executor.run`` is itself a blocking call. The runner therefore
    dispatches this function via ``loop.run_in_executor(None, ...)`` — matching
    the shipped ``grade_state_checks_reads`` / ``grade_llm_judge`` bridges — so
    both bridges land off the loop thread.
    """
    if not custom_checks_enabled(config):
        return -1.0, [], None
    resolved_config = (
        config if isinstance(config, CustomChecksConfig) else CustomChecksConfig(**config)
    )

    if artifacts_dir is None:
        error_msg = (
            f"custom_checks.enabled but no artifacts_dir for trial {trial_id!r} "
            "(checks.py was not delivered by the adapter)"
        )
        logger.error(f"GradeTrial: {trial_id} - {error_msg}")
        score = 0.0 if resolved_config.fail_on_error else -1.0
        return (
            score,
            [_executor_error_to_wire(error_msg)],
            custom_checks_reason(CheckResultSet(error=error_msg)),
        )
    checks_file = artifacts_dir / resolved_config.file

    initial_tables = task_description.initial_state.tables
    initial_state_json_db: dict[str, Any] | None = dict(initial_tables) if initial_tables else None

    # noqa marker below covers degrade-to-empty per shipped semantics: any
    # failure reading ``substrate.final_state`` falls through with an empty
    # final state; the DB failure surfaces via the log line the audit greps
    # for and via component metadata, not by crashing the grade path.
    try:
        final_env_state: dict[str, Any] = substrate.final_state()
    except SubstrateUnreachableError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"GradeTrial: {trial_id} - final DB state fetch failed ({exc}); "
            "grading against empty state"
        )
        final_env_state = {}

    ctx = build_check_context(
        initial_state_json_db=initial_state_json_db,
        final_env_state=final_env_state,
        transcript=_build_runner_check_transcript(llm_messages),
        task=TaskContext(
            task_id=task_description.task_id,
            task_name=task_description.name,
            task_description=task_description.description,
            domain=task_description.category or "",
        ),
    )

    logger.info(f"GradeTrial: {trial_id} - Running custom checks from {checks_file}")
    try:
        result: CheckResultSet = check_executor.run(
            checks_file=checks_file,
            task_dir=artifacts_dir,
            ctx=ctx,
            config=resolved_config,
        )
    except Exception as exc:
        # An executor that raises rather than capturing into
        # :class:`CheckResultSet` is a contract violation; convert it to the
        # same sentinel-entry shape as ``result.error`` so the audit survives
        # and the whole trial's grade is not lost to the outer handler.
        logger.exception(f"GradeTrial: {trial_id} - custom checks executor raised")
        score = 0.0 if resolved_config.fail_on_error else -1.0
        return (
            score,
            [_executor_error_to_wire(str(exc))],
            custom_checks_reason(CheckResultSet(error=str(exc))),
        )

    wire_results = [_check_result_to_wire(r) for r in result.results]
    reason = custom_checks_reason(result)

    if result.error:
        logger.error(f"GradeTrial: {trial_id} - custom checks executor error: {result.error}")
        wire_results.append(_executor_error_to_wire(result.error))
        score = 0.0 if resolved_config.fail_on_error else -1.0
        return score, wire_results, reason

    logger.info(
        f"GradeTrial: {trial_id} - custom checks: "
        f"{result.passed}/{result.total} passed, score={result.aggregate_score:.2f}"
    )
    if not result.decided_something:
        return -1.0, wire_results, reason
    return result.aggregate_score, wire_results, reason
