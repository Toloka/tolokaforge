"""LLM-judge composite dispatch and state-diff renderer.

Every deployment shape's LLM-judge scoring goes through
:func:`grade_llm_judge`. :func:`build_judge_state_diff` sits alongside as
the runner-resolved passthrough helper the runner + grader wrappers call
BEFORE :func:`grade_llm_judge` to prepare the ``state_diff`` kwarg.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from tolokaforge.core.grading.judge_result import JudgeResult
from tolokaforge.core.grading.state_diff import render_state_diff
from tolokaforge.core.grading.substrate import SubstrateUnreachableError
from tolokaforge.core.grading.transcript_wire import split_leading_system_message

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge_kinds import JudgeKind
    from tolokaforge.core.grading.judge_model_provider import JudgeModelProvider
    from tolokaforge.core.grading.judge_tools import DelegatingReadTool
    from tolokaforge.core.grading.substrate import GradingSubstrate
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.core.models import ModelConfig
    from tolokaforge.runner.models import LLMJudgeConfig


def grade_llm_judge(
    *,
    trial_id: str,  # noqa: ARG001 — kept for call-site parity + future per-trial logging
    config: LLMJudgeConfig,
    substrate: GradingSubstrate,
    judge_kind: JudgeKind,
    judge_model_provider: JudgeModelProvider,
    disable_knowledge_search: bool,
    custom_system_prompt: str | None,
    include_agent_system_prompt: bool,
    kind_config: Mapping[str, Any] | None,
    llm_messages: list[dict[str, Any]],
    judge_model_config: ModelConfig,
    extra_read_tools: list[DelegatingReadTool],
    state_diff: str | None,
    logger: StructuredLogger,
) -> JudgeResult:
    """Run the read-only rubric judge against ``substrate`` and return its verdict.

    Deconstructs the substrate reads into the per-trial evidence kwargs
    :class:`JudgeKind.evaluate` takes — :meth:`substrate.db_reader` for
    the read-only DB tools it exposes, :meth:`substrate.knowledge_search`
    for ``search_kb``, and :meth:`substrate.filesystem_root` for
    ``read_file`` (``None`` withholds it). ``state_diff`` is a
    runner-resolved passthrough — the caller renders the
    ``initial → final`` DB delta and hands it in; the composite forwards
    it verbatim.

    ``extra_read_tools`` is a runner-resolved passthrough: the caller
    reconstructs the agent's ``search_policy`` connector as
    :class:`DelegatingReadTool` s so the judge can reuse the SAME
    TypeSense connector the agent used.

    ``judge_kind`` is the resolved :class:`JudgeKind` seam the runner
    supplies — a plug-in impl (``single_shot_rubric`` in the shipping
    config, a downstream chunked / agentic / jury kind alongside) that
    owns the actual grade dispatch. Per-trial customization (KB gate,
    custom system-prompt, include-agent-system-prompt) rides alongside
    on the ``evaluate`` kwargs — the composite forwards them so the kind
    constructs the judge instance itself.

    Fail-loud contract: any judge malfunction — malformed
    ``submit_report`` past retries, budget/turn exhaustion, or a
    loop-terminal exception — surfaces as :attr:`JudgeStatus.ERRORED`
    with ``score is None``. Never a 0.0 / 0.5 fallback.
    :class:`SubstrateUnreachableError` from a substrate read propagates
    so the dispatch site can translate it to ``GradingFailedError``.

    Sync-in-async note: the composite is a **sync** function. The
    InProcess substrate's factories block on ``run_coroutine_threadsafe``
    to bridge to the runner's dedicated event-loop thread, which
    deadlocks when called from that loop. The runner therefore
    dispatches this function via ``loop.run_in_executor(None, ...)`` —
    matching the shipped ``grade_state_checks_reads`` bridge — so the
    substrate's blocking reads (and the kind's own DB reads) land off
    the loop thread.
    """
    agent_system_prompt, transcript = split_leading_system_message(list(llm_messages))
    return judge_kind.evaluate(
        rubric=config.rubric,
        agent_system_prompt=agent_system_prompt,
        transcript=transcript,
        db_reader=substrate.db_reader(),
        kb_search=substrate.knowledge_search(),
        workspace_dir=substrate.filesystem_root(),
        extra_read_tools=list(extra_read_tools),
        state_diff=state_diff,
        judge_model_config=judge_model_config,
        judge_model_provider=judge_model_provider,
        disable_knowledge_search=disable_knowledge_search,
        custom_system_prompt=custom_system_prompt,
        include_agent_system_prompt=include_agent_system_prompt,
        kind_config=kind_config,
        logger=logger,
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
