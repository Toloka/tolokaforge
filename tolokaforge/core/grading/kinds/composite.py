"""``CompositeGraderKind`` — reference impl over the shared composite fold.

Two-mode dispatch:

1. **Pre-computed components mode.** ``kind_config["components"]`` carries
   the sub-component scores (``hash_score``, ``jsonpath_score``,
   ``db_probe_score``, ``transcript_score``, ``trace_checks_score``,
   ``llm_judge_score``, ``custom_checks_score``). The kind reads those,
   folds via :class:`~tolokaforge.core.grading.composite_fold.CompositeFold`,
   and returns a :class:`Grade`. Reference-impl shape a downstream adapter
   can register its own composite variant against; also the fallback the
   CLI uses when the offline recompute mode cannot run (v1.0 bundle
   without ``task_description.json`` / ``judge_model_config.json``).

2. **Full offline recompute mode.** When ``kind_config`` does not carry
   pre-computed scores, the kind recomputes every sub-component from the
   substrate. Reads ``task_description`` + ``trajectory`` +
   ``judge_model_config`` via the substrate accessors added in bundle
   format v1.1; loads the five shipped sub-component plug-ins
   (state_check_backends, transcript_rule_matcher,
   custom_check_executor, judge_model_provider, rubric_evaluator);
   drives ``composite.grade_state_checks_reads`` /
   ``grade_transcript_rules`` / ``grade_trace_checks`` /
   ``build_judge_state_diff`` + ``grade_llm_judge`` /
   ``grade_custom_checks``; folds; returns a :class:`Grade` byte-parity
   with the runner-side ``_grade_trial_async`` dispatch (minus hash +
   accounted-keys ledger, which are runner-only surfaces).

**Hash refusal.** A task declaring ``state_checks.hash_enabled`` is
refused up-front with the same fragment
:class:`~tolokaforge.grader.composite_dispatch.GraderCompositeDispatch`
raises — hash grading requires runner DB write access and is
substrate-write territory the offline path cannot serve.

**Empty active set.** With no component scored, ``evaluate`` returns
``None`` — the composite fold's empty-active-set semantic.
:class:`~tolokaforge.core.grading.substrate.SubstrateUnreachableError`
from any substrate read (bundle corrupt, part missing) translates to
:class:`GraderKindRefusedError` at the boundary.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar

from tolokaforge.core.grading.composite_fold import CompositeFold
from tolokaforge.core.grading.kinds._protocol import GraderKindRefusedError
from tolokaforge.core.models.grade import Grade
from tolokaforge.core.models.grade_components import GradeComponents

if TYPE_CHECKING:
    from tolokaforge.core.grading.substrate import GradingSubstrate
    from tolokaforge.core.logging import StructuredLogger
    from tolokaforge.runner.models import RunnerGradingConfig

__all__ = ["CompositeGraderKind"]


_STATE_CHECK_SCORE_KEYS: tuple[str, ...] = ("hash_score", "jsonpath_score", "db_probe_score")
_COMPONENT_SCORE_KEYS: tuple[str, ...] = (
    "transcript_score",
    "trace_checks_score",
    "llm_judge_score",
    "custom_checks_score",
)


_HASH_REFUSAL_FRAGMENT: str = (
    "CompositeGraderKind cannot execute hash-based grading — the substrate is "
    "read-only. Configure grader: runner_rpc for this task, or disable "
    "hash_enabled."
)
"""Same message class :class:`~tolokaforge.grader.composite_dispatch.GraderCompositeDispatch`
raises on the LIVE-callback lane. Lane B parity gate at
``tests/canonical/test_grader_parity_reference.py`` locks the
``"cannot execute hash-based grading"`` fragment; the offline kind
inherits the same lock."""


class CompositeGraderKind:
    """Reference composite grader kind — folds sub-component scores.

    Two operating modes selected by ``kind_config`` shape; see module
    docstring for the full behaviour contract.
    """

    NAME: ClassVar[str] = "composite"

    def evaluate(
        self,
        *,
        substrate: GradingSubstrate,
        task_config: RunnerGradingConfig,
        kind_config: Mapping[str, Any] | None,
        trial_id: str,
        agent_tools: Mapping[str, Any],  # noqa: ARG002
        logger: StructuredLogger,
    ) -> Grade | None:
        # Substrate liveness probe — a SubstrateUnreachableError here
        # propagates through this seam, the composite grader's substrate
        # contract.
        substrate.final_state()

        # Hash refusal — inherited from the grader-side dispatcher; the
        # offline substrate cannot execute hash grading (runner DB write
        # required).
        state_checks_config = task_config.state_checks if task_config is not None else None
        if state_checks_config is not None and getattr(state_checks_config, "hash_enabled", False):
            raise GraderKindRefusedError(_HASH_REFUSAL_FRAGMENT)

        components_dict: dict[str, Any] = dict((kind_config or {}).get("components") or {})
        if _any_active(components_dict):
            return self._fold_precomputed(components_dict, task_config)

        # No pre-computed components: either the caller wants full offline
        # recompute (v1.1 substrate exposes the runner-only inputs) or the
        # substrate cannot serve those inputs (v1.0 bundle without the
        # optional parts, or LIVE substrate that stubs the accessors to
        # None). The latter is the current reference-impl semantic —
        # "empty active set returns None" — preserved so a caller
        # dispatching against a v1.0 bundle still gets a defined outcome.
        if substrate.task_description() is None:
            return None

        return self._recompute_from_substrate(
            substrate=substrate,
            task_config=task_config,
            trial_id=trial_id,
            logger=logger,
        )

    def _fold_precomputed(
        self,
        components_dict: dict[str, Any],
        task_config: RunnerGradingConfig,
    ) -> Grade | None:
        """Pre-computed components mode — the reference-impl fallback."""
        grading_dict = task_config.model_dump() if task_config is not None else {}
        state_config = grading_dict.get("state_checks") or {}
        hash_weight = state_config.get("hash_weight") if isinstance(state_config, Mapping) else None

        result = CompositeFold.finalise(
            components_dict=components_dict,
            grading_config_dict=grading_dict,
            hash_weight=hash_weight,
            judge_gate_failed=False,
            trace_gate_failed=False,
        )
        if result.refusal:
            raise GraderKindRefusedError(result.verdict_reason or "composite fold refused")
        return Grade(
            binary_pass=result.binary_pass,
            score=result.score,
            components=GradeComponents(
                state_checks=result.state_checks_component,
                transcript_rules=_score_or_none(components_dict.get("transcript_score")),
                trace_checks=_score_or_none(components_dict.get("trace_checks_score")),
                llm_judge=_score_or_none(components_dict.get("llm_judge_score")),
                custom_checks=_score_or_none(components_dict.get("custom_checks_score")),
            ),
            reasons=result.reasons,
        )

    def _recompute_from_substrate(
        self,
        *,
        substrate: GradingSubstrate,
        task_config: RunnerGradingConfig,
        trial_id: str,
        logger: StructuredLogger,
    ) -> Grade | None:
        """Full offline recompute — mirrors the runner's ``_grade_trial_async``
        composite path minus hash grading + accounted-keys ledger. Reads
        every runner-only input (``TaskDescription``, ``ModelConfig``,
        trajectory) from the substrate; loads the five shipped plug-ins.

        Refuses actionably when the substrate does not carry the
        required inputs (v1.0 bundle without the v1.1-optional parts,
        or a LIVE substrate that stubs the accessors to ``None``).
        """
        import json as _json

        from tolokaforge.core.grading import composite
        from tolokaforge.core.grading.substrate import SubstrateUnreachableError
        from tolokaforge.core.grading.tool_artifacts import extract_tool_artifacts
        from tolokaforge.core.grading.trace_timeline import build_timeline_from_wire
        from tolokaforge.core.grading.transcript_wire import encode_transcript_wire
        from tolokaforge.core.models.grade import CustomCheckDetail, JudgeStatus
        from tolokaforge.core.models.run_config import ModelConfig
        from tolokaforge.core.models.trajectory import Trajectory as _Trajectory
        from tolokaforge.core.plugin_registry import (
            load_custom_check_executor,
            load_judge_model_provider,
            load_rubric_evaluator,
            load_state_check_backend,
            load_transcript_rule_matcher,
        )
        from tolokaforge.runner.models import (
            RunnerGradeComponents,
            TaskDescription,
            TraceChecksSummary,
            TraceConstraintResult,
            TracePathResult,
        )
        from tolokaforge.runner.protocol import parse_termination_reason

        task_description_dict = substrate.task_description()
        if task_description_dict is None:
            raise GraderKindRefusedError(
                "CompositeGraderKind offline recompute mode needs task_description.json "
                "(bundle format v1.1). This bundle carries neither pre-computed "
                "components in kind_config nor the v1.1 part — regrade with "
                "--grader-config components:{...} against a v1.0 bundle, or "
                "reproduce the trial with a v1.1-capable runtime backend."
            )
        trajectory_dict = substrate.trajectory()
        if trajectory_dict is None:
            raise GraderKindRefusedError(
                "CompositeGraderKind offline recompute mode needs trajectory.json — "
                "the substrate returned None (LIVE substrate cannot serve the "
                "offline kind; only SnapshotGradingSubstrate does)."
            )
        judge_model_config_dict = substrate.judge_model_config()

        # Refuse fast when llm_judge is declared but the substrate has no
        # judge_model_config — cheaper than the trajectory rehydration below
        # and more actionable for the operator.
        if task_config.llm_judge is not None and judge_model_config_dict is None:
            raise GraderKindRefusedError(
                "CompositeGraderKind offline recompute mode requires judge_model_config.json "
                "when the task declares llm_judge — bundle v1.1 optional part missing."
            )

        task_description = TaskDescription.model_validate(task_description_dict)
        judge_model_config: ModelConfig | None = (
            ModelConfig.model_validate(judge_model_config_dict)
            if judge_model_config_dict is not None
            else None
        )
        # Reconstruct the wire-encoded llm_messages the runner + grader
        # dispatchers pass to the composite helpers. trajectory.messages
        # in the bundle carries the Trajectory Pydantic dump — a different
        # shape from the encode_transcript_wire wire (which nests tool_calls
        # under {function: {name, arguments}} OpenAI-style). Rehydrate the
        # Trajectory, encode it against the task's system_prompt, then parse
        # the returned JSON string — the exact input the LIVE dispatchers
        # feed build_timeline_from_wire. Trajectory.tool_log carries the
        # RecordedToolCall history the runner passes as ``recorded`` for
        # required_action / no-matching-tool-call transcript rule checks;
        # without it those rules could not distinguish declared-but-unrun
        # calls from failed ones (closes #1517).
        trajectory_obj = _Trajectory.model_validate(trajectory_dict)
        wire_str = encode_transcript_wire(trajectory_obj, task_description.system_prompt)
        llm_messages: list[dict[str, Any]] = _json.loads(wire_str) if wire_str else []
        recorded_tool_calls = list(trajectory_obj.tool_log)
        termination_reason = parse_termination_reason(trajectory_dict.get("termination_reason"))

        state_check_backends = {
            "jsonpath": load_state_check_backend("jsonpath")(),
            "db_probes": load_state_check_backend("db_probes")(),
        }
        transcript_rule_matcher = load_transcript_rule_matcher("default")()
        check_executor = load_custom_check_executor("check_runner")()
        judge_model_provider = load_judge_model_provider("litellm")()

        initial_state = task_description.initial_state
        id_fields: dict[str, str | list[str]] = (
            state_checks_config.id_fields
            if (state_checks_config := task_config.state_checks)
            else {}
        )
        unstable_fields = {(u.table_name, u.field_name) for u in initial_state.unstable_fields}
        initial_state_schemas = list(initial_state.schemas)
        tool_artifacts = task_description.tool_artifacts or {}
        timeline = build_timeline_from_wire(llm_messages, recorded_tool_calls, termination_reason)

        artifacts_dir = None
        added_sys_path: list[str] = []
        if tool_artifacts:
            artifacts_dir = extract_tool_artifacts(trial_id, tool_artifacts)
            for entry in (str(artifacts_dir), str(artifacts_dir / "tools")):
                if entry not in sys.path:
                    sys.path.insert(0, entry)
                    added_sys_path.append(entry)

        try:
            try:
                return self._run_composite(
                    substrate=substrate,
                    task_config=task_config,
                    task_description=task_description,
                    judge_model_config=judge_model_config,
                    llm_messages=llm_messages,
                    timeline=timeline,
                    id_fields=id_fields,
                    unstable_fields=unstable_fields,
                    initial_state_schemas=initial_state_schemas,
                    artifacts_dir=artifacts_dir,
                    trial_id=trial_id,
                    state_check_backends=state_check_backends,
                    transcript_rule_matcher=transcript_rule_matcher,
                    check_executor=check_executor,
                    judge_model_provider=judge_model_provider,
                    logger=logger,
                    composite_mod=composite,
                    runner_components_cls=RunnerGradeComponents,
                    load_rubric_evaluator=load_rubric_evaluator,
                    judge_status_cls=JudgeStatus,
                    trace_summary_cls=TraceChecksSummary,
                    trace_constraint_cls=TraceConstraintResult,
                    trace_path_cls=TracePathResult,
                    custom_detail_cls=CustomCheckDetail,
                )
            except SubstrateUnreachableError as exc:
                raise GraderKindRefusedError(
                    f"substrate unreachable during offline composite grading: {exc}"
                ) from exc
        finally:
            for entry in added_sys_path:
                while entry in sys.path:
                    sys.path.remove(entry)
            if artifacts_dir is not None:
                shutil.rmtree(artifacts_dir, ignore_errors=True)

    def _run_composite(
        self,
        *,
        substrate: Any,
        task_config: Any,
        task_description: Any,
        judge_model_config: Any,
        llm_messages: list[dict[str, Any]],
        timeline: Any,
        id_fields: dict[str, str | list[str]],
        unstable_fields: set[tuple[str, str]],
        initial_state_schemas: list[Any],
        artifacts_dir: Any,
        trial_id: str,
        state_check_backends: dict[str, Any],
        transcript_rule_matcher: Any,
        check_executor: Any,
        judge_model_provider: Any,
        logger: Any,
        composite_mod: Any,
        runner_components_cls: Any,
        load_rubric_evaluator: Any,
        judge_status_cls: Any,
        trace_summary_cls: Any,
        trace_constraint_cls: Any,
        trace_path_cls: Any,
        custom_detail_cls: Any,
    ) -> Grade | None:
        """Drive the five composite helpers + fold. Mirrors
        :meth:`~tolokaforge.grader.composite_dispatch.GraderCompositeDispatch._run_composite`
        which we cannot import directly (importlinter contract
        ``grader-kinds-purity`` forbids reaching ``tolokaforge.grader``)."""
        from tolokaforge.core.grading.judge_result import JudgeStatus as JudgeRunStatus
        from tolokaforge.core.grading.rubric_evaluator import RubricEvaluatorContext
        from tolokaforge.runner.models import TraceChecksResult

        components = runner_components_cls()
        state_checks_config = task_config.state_checks

        if state_checks_config and (
            state_checks_config.jsonpath_checks or state_checks_config.db_probes
        ):
            state_reads = composite_mod.grade_state_checks_reads(
                trial_id=trial_id,
                config=state_checks_config,
                substrate=substrate,
                state_check_backends=state_check_backends,
                logger=logger,
            )
            if state_reads.jsonpath_score is not None:
                components.jsonpath_score = state_reads.jsonpath_score
                components.jsonpath_reasons = state_reads.jsonpath_reasons or ""
            if state_reads.db_probe_score is not None:
                components.db_probe_score = state_reads.db_probe_score
                components.db_probe_reasons = state_reads.db_probe_reasons or ""

        transcript_result = None
        if task_config.transcript_rules:
            transcript_result, _accounting = composite_mod.grade_transcript_rules(
                trial_id=trial_id,
                config=task_config.transcript_rules,
                timeline=timeline,
                matcher=transcript_rule_matcher,
                logger=logger,
            )
            if transcript_result is not None:
                components.transcript_pass = transcript_result.passed
                components.transcript_score = transcript_result.score

        if task_config.trace_checks:
            trace_result = composite_mod.grade_trace_checks(
                trial_id=trial_id,
                config=task_config.trace_checks,
                timeline=timeline,
                logger=logger,
            )
            if trace_result.constraints:
                components.trace_checks_score = trace_result.score
        else:
            trace_result = TraceChecksResult()

        judge_result = None
        judge_status = judge_status_cls.UNSPECIFIED
        judge_gate_failed = False
        if task_config.llm_judge and llm_messages:
            customization = task_config.llm_judge.customization
            disable_kb = bool(customization and customization.disable_knowledge_search)
            custom_prompt = customization.system_prompt if customization else None
            include_agent_prompt = (
                customization.include_agent_system_prompt
                if customization and customization.include_agent_system_prompt is not None
                else True
            )
            rubric_evaluator = load_rubric_evaluator("llm_judge")(
                RubricEvaluatorContext(
                    judge_model_provider=judge_model_provider,
                    disable_knowledge_search=disable_kb,
                    custom_system_prompt=custom_prompt,
                    include_agent_system_prompt=include_agent_prompt,
                )
            )
            state_diff_text = composite_mod.build_judge_state_diff(
                trial_id=trial_id,
                substrate=substrate,
                initial_state_schemas=initial_state_schemas,
                id_fields=id_fields,
                unstable_fields=unstable_fields,
                logger=logger,
            )
            judge_result = composite_mod.grade_llm_judge(
                trial_id=trial_id,
                config=task_config.llm_judge,
                substrate=substrate,
                rubric_evaluator=rubric_evaluator,
                llm_messages=llm_messages,
                judge_model_config=judge_model_config,
                extra_read_tools=[],
                state_diff=state_diff_text,
                logger=logger,
            )
            if judge_result.status is JudgeRunStatus.ERRORED:
                judge_status = judge_status_cls.ERRORED
            else:
                judge_status = judge_status_cls.COMPLETED
                judge_gate_failed = judge_result.gate_failed
                if judge_result.score is not None:
                    components.llm_judge_score = judge_result.score

        custom_score, custom_check_results, custom_reasons = composite_mod.grade_custom_checks(
            trial_id=trial_id,
            config=task_config.custom_checks,
            substrate=substrate,
            llm_messages=llm_messages,
            task_description=task_description,
            artifacts_dir=artifacts_dir,
            check_executor=check_executor,
            logger=logger,
        )
        components.custom_checks_score = custom_score

        fold_result = CompositeFold.finalise(
            components_dict=components.model_dump(),
            grading_config_dict=task_config.model_dump(),
            hash_weight=state_checks_config.hash_weight if state_checks_config else None,
            judge_gate_failed=judge_gate_failed,
            trace_gate_failed=trace_result.gate_failed,
            transcript_result_dict=(
                transcript_result.model_dump() if transcript_result is not None else None
            ),
            judge_reasons=(judge_result.reasons if judge_result is not None else None) or None,
            trace_checks_result_dict=trace_result.model_dump(mode="json"),
            custom_checks_reasons=custom_reasons,
            judge_errored=judge_status is judge_status_cls.ERRORED,
        )
        components.llm_judge_score = fold_result.judge_component
        if fold_result.refusal:
            raise GraderKindRefusedError(fold_result.verdict_reason or "composite fold refused")

        def _slot(v: float) -> float | None:
            return None if v < 0 else v

        return Grade(
            binary_pass=fold_result.binary_pass,
            score=fold_result.score,
            components=GradeComponents(
                state_checks=fold_result.state_checks_component,
                transcript_rules=_slot(components.transcript_score),
                trace_checks=_slot(components.trace_checks_score),
                llm_judge=_slot(components.llm_judge_score),
                custom_checks=_slot(components.custom_checks_score),
            ),
            reasons=fold_result.reasons,
            custom_checks_details=[
                custom_detail_cls(
                    check_name=r.check_name,
                    status=r.status.value if hasattr(r.status, "value") else str(r.status),
                    score=r.score,
                    message=r.message,
                    details=r.details or None,
                )
                for r in custom_check_results
            ]
            or None,
            trace_check_results=[
                trace_constraint_cls(
                    id=c.id,
                    kind=c.kind,
                    passed=c.passed,
                    weight=c.weight,
                    message=c.message,
                    matched_positions=list(c.matched_positions),
                    severity=c.severity,
                    undecided=c.undecided,
                    withheld=c.withheld,
                )
                for c in trace_result.constraints
            ],
            trace_checks_summary=trace_summary_cls(
                winning_path=trace_result.winning_path,
                gate_failed=trace_result.gate_failed,
                failed_gate_ids=list(trace_result.failed_gate_ids),
                paths=[
                    trace_path_cls(id=p.id, score=p.score, gate_failed=p.gate_failed)
                    for p in trace_result.paths
                ],
            ),
            judge_status=judge_status,
        )


def _any_active(components: Mapping[str, Any]) -> bool:
    for key in (*_STATE_CHECK_SCORE_KEYS, *_COMPONENT_SCORE_KEYS):
        value = components.get(key)
        if isinstance(value, int | float) and value >= 0:
            return True
    return False


def _score_or_none(value: Any) -> float | None:
    if isinstance(value, int | float) and value >= 0:
        return float(value)
    return None
