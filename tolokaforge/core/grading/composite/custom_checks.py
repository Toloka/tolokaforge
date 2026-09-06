"""Custom-checks composite dispatch.

The pack's ``checks.py`` is run through :func:`grade_custom_checks` — the
executor consumes the trial's evidence via :class:`GradingSubstrate` reads,
and the composite returns the resulting :class:`CheckResult` values.
Runner-wire encoding lives in
:func:`tolokaforge.runner.grading.project_check_result_to_runner_wire`;
the grader-side wrapper reads the same :class:`CheckResult` values
directly and constructs :class:`CustomCheckDetail` from each.
"""

from __future__ import annotations

import json
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
    CheckStatus,
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
from tolokaforge.core.grading.substrate import SubstrateUnreachableError

if TYPE_CHECKING:
    from pathlib import Path

    from tolokaforge.core.grading.substrate import GradingSubstrate
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.runner.models import TaskDescription


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


def _executor_error_result(error: str) -> CheckResult:
    """Wrap a top-level :class:`CheckResultSet` error as a synthetic result.

    The audit — module-load failure / timeout / executor crash — travels to
    the host under the reserved :data:`_CHECK_EXECUTOR_ERROR_NAME` sentinel
    so the reasons string is not the only place it survives.
    """
    return CheckResult(
        check_name=_CHECK_EXECUTOR_ERROR_NAME,
        status=CheckStatus.ERROR,
        score=0.0,
        message=error,
        details={},
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
) -> tuple[float, list[CheckResult], str | None]:
    """Run the pack's ``checks.py`` against the trial's evidence.

    Returns ``(score, results, reason)``. The reason is the sentence
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
    sentinel :class:`CheckResult` preserves the audit, and the score follows
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
            [_executor_error_result(error_msg)],
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
            [_executor_error_result(str(exc))],
            custom_checks_reason(CheckResultSet(error=str(exc))),
        )

    check_results = list(result.results)
    reason = custom_checks_reason(result)

    if result.error:
        logger.error(f"GradeTrial: {trial_id} - custom checks executor error: {result.error}")
        check_results.append(_executor_error_result(result.error))
        score = 0.0 if resolved_config.fail_on_error else -1.0
        return score, check_results, reason

    logger.info(
        f"GradeTrial: {trial_id} - custom checks: "
        f"{result.passed}/{result.total} passed, score={result.aggregate_score:.2f}"
    )
    if not result.decided_something:
        return -1.0, check_results, reason
    return result.aggregate_score, check_results, reason
