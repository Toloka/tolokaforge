"""Offline judge replay engine — reconstructing ``LLMJudge.run()`` inputs.

Locks the contract of :mod:`tolokaforge.core.grading.replay`:

* the reconstructed transcript is **byte-identical** to the live
  ``encode_transcript_wire`` + strip path the runner uses;
* a judge-eligible trial with no rubric and no override fails loud with a named
  error, while a not-applicable trial is a declared skip even under a rubric
  override (a rubric override never conjures a judge stage onto a trial that
  never had one);
* the offline read shims return the explicit "unavailable in replay" marker,
  never a silent empty result;
* ``replay_trial`` drives the one production ``LLMJudge`` with an injected client
  and never touches the network.

The bundle is written by the real :class:`FileArtifactWriter` (no hand-rolled
YAML) so the tests exercise the same on-disk contract the eval writer produces.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from tests.unit.grading.test_judge import MessageCapturingClient, ScriptedClient
from tests.utils.five_shape_run import write_five_shape_run
from tests.utils.provision_failure import write_provision_failure_bundle
from tolokaforge.core.conductor import InProcessConductor
from tolokaforge.core.failure_attribution import TrialOutcomeClass
from tolokaforge.core.grading.judge_result import JudgeStatus as JudgeRunStatus
from tolokaforge.core.grading.replay import (
    REPLAY_UNAVAILABLE,
    CustomizationRecord,
    FidelityMode,
    GradingOverride,
    MissingReplayInputError,
    OfflineDBReader,
    OfflineKnowledgeSearch,
    ProvenanceSource,
    ReplayBatchCensus,
    ReplayOutcomeStatus,
    ReplayProvenance,
    TrialEligibility,
    classify_trial,
    emit_replay_report,
    load_grading_override,
    read_replay_inputs,
    replay_trial,
    run_replay_batch,
)
from tolokaforge.core.grading.replay_layout import discover_trial_bundles
from tolokaforge.core.grading.transcript_wire import (
    encode_transcript_wire,
    split_leading_system_message,
)
from tolokaforge.core.llm.reasoning import ReasoningConfig
from tolokaforge.core.models import (
    RESOLVED_RECORD_KEY,
    EvaluationConfig,
    Grade,
    GradeComponents,
    JudgeInputs,
    JudgeKbGating,
    JudgeStatus,
    Message,
    MessageRole,
    ModelConfig,
    ModelSessionConfig,
    OpenRouterConfig,
    OrchestratorConfig,
    RunConfig,
    TerminationReason,
    ToolCall,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.output.artifacts import FileArtifactWriter
from tolokaforge.core.tool_effects import ToolEffects
from tolokaforge.runner.models import FINAL_STATE_INPUTS, JudgeEvidence, ToolResultsOptions

pytestmark = pytest.mark.unit

_AGENT_PROMPT = "You are the agent. Follow the refund policy exactly."
_RUBRIC = {
    "reference": "The refund must be issued for $328.50.",
    "criteria": [
        {"id": "refund_amount", "description": "Refund quotes $328.50", "kind": "binary"},
    ],
}
_JUDGE_MODEL = ModelConfig(provider="openrouter", name="openai/gpt-4.1-mini", temperature=0.0)
_GRADING_REFUSAL = "judge returned no verdict after 3 attempts"


def _trajectory(
    *,
    status: TrialStatus = TrialStatus.COMPLETED,
    termination_reason: TerminationReason | None = None,
    grading_error: str | None = None,
) -> Trajectory:
    now = datetime.now(UTC)
    return Trajectory(
        task_id="refund_task",
        trial_index=0,
        start_ts=now,
        end_ts=now,
        status=status,
        termination_reason=termination_reason,
        grading_error=grading_error,
        messages=[
            Message(role=MessageRole.USER, content="I want a refund for order O-1."),
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="c1", name="get_order", arguments={"id": "O-1"})],
            ),
            Message(role=MessageRole.TOOL, content='{"total": 328.5}', tool_call_id="c1"),
            Message(role=MessageRole.ASSISTANT, content="Your refund of $328.50 is issued."),
        ],
    )


def _conductor_written_model_config(judge: ModelConfig, output_dir: Path) -> dict[str, Any]:
    """The ``task.yaml`` ``model_config`` block the conductor writes for a run judged by *judge*."""
    agent = ModelConfig(provider="openai", name="gpt-4o-mini")
    config = RunConfig(
        models={"agent": agent, "judge": judge},
        orchestrator=OrchestratorConfig(workers=1, repeats=1, auto_start_services=False),
        evaluation=EvaluationConfig(output_dir=str(output_dir)),
    )
    conductor = InProcessConductor(
        adapter=MagicMock(),
        artifact_writer=MagicMock(),
        config=config,
        logger=MagicMock(),
        agent_client=MagicMock(),
        runtime_backend=MagicMock(),
        trial_grader=MagicMock(),
        output_dir=output_dir,
    )
    return conductor._serialize_model_config(agent_config=agent, judge_config=judge)


def _write_recorded_inputs(
    trial_dir: Path,
    trajectory: Trajectory,
    *,
    judge: ModelConfig = _JUDGE_MODEL,
    with_rubric: bool = True,
    recorded_customization: dict[str, Any] | None = None,
    judge_prompt: str | None = None,
) -> None:
    """Write everything a bundle records except its grade, via the real writer.

    *recorded_customization* is the ``task.yaml`` customization; *judge_prompt* the
    ``prompts.yaml`` composed judge prompt.
    """
    writer = FileArtifactWriter()
    writer.write_trajectory(trial_dir, trajectory)
    writer.write_prompts(trial_dir, _AGENT_PROMPT, "user-sim prompt", judge_prompt=judge_prompt)

    if with_rubric:
        llm_judge: dict = {"rubric": _RUBRIC}
        if recorded_customization:
            llm_judge["customization"] = recorded_customization
        grading_config = {"llm_judge": llm_judge}
    else:
        grading_config = {"llm_judge": None}
    writer.write_task(
        trial_dir,
        {
            "task_id": "refund_task",
            "trial_index": 0,
            "grading_config": grading_config,
            "model_config": _conductor_written_model_config(judge, trial_dir),
        },
    )


def _write_bundle(
    trial_dir: Path,
    *,
    judge_status: JudgeStatus,
    judge_inputs: JudgeInputs | None,
    judge: ModelConfig = _JUDGE_MODEL,
    with_rubric: bool = True,
    kb_gating: JudgeKbGating | None = None,
    recorded_customization: dict[str, Any] | None = None,
    judge_prompt: str | None = None,
) -> Trajectory:
    """Write a new-shape bundle via the real writer; return the source trajectory."""
    trajectory = _trajectory()
    _write_recorded_inputs(
        trial_dir,
        trajectory,
        judge=judge,
        with_rubric=with_rubric,
        recorded_customization=recorded_customization,
        judge_prompt=judge_prompt,
    )
    FileArtifactWriter().write_grade(
        trial_dir,
        Grade(
            binary_pass=True,
            score=1.0,
            components=GradeComponents(llm_judge=1.0),
            judge_status=judge_status,
            judge_inputs=judge_inputs,
            judge_kb_gating=kb_gating,
        ),
    )
    return trajectory


_SUBMIT_REPORT_SCRIPT = [
    [
        (
            "submit_report",
            {
                "reasons": "Refund quoted correctly.",
                "refund_amount": True,
                "refund_amount_justification": "Reply quotes $328.50. VERDICT: MET",
            },
        )
    ]
]


def _submit_report_client() -> ScriptedClient:
    return ScriptedClient(_SUBMIT_REPORT_SCRIPT)


def test_reconstructed_transcript_is_byte_identical_to_live_path(tmp_path: Path) -> None:
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    trajectory = _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(
            state_diff_text="orders[O-1]: refunded false -> true",
            read_tools_offered=["get_db_state", "query_db", "read_file"],
        ),
    )

    # The live grading path the runner takes for this trajectory + agent prompt.
    wire = encode_transcript_wire(trajectory, _AGENT_PROMPT)
    assert wire is not None
    expected_prompt, expected_transcript = split_leading_system_message(json.loads(wire))

    inputs = read_replay_inputs(trial_dir)

    assert inputs.agent_system_prompt == expected_prompt
    assert inputs.transcript == expected_transcript
    # Byte-identical when re-serialised, not just structurally equal.
    assert json.dumps(inputs.transcript) == json.dumps(expected_transcript)
    # New bundle → full fidelity, recorded rubric + model, and the recorded
    # read surface is offered offline.
    assert inputs.state_diff == "orders[O-1]: refunded false -> true"
    assert inputs.provenance.fidelity_mode is FidelityMode.FULL
    assert inputs.provenance.rubric_source is ProvenanceSource.RECORDED
    assert inputs.provenance.judge_model_source is ProvenanceSource.RECORDED
    assert isinstance(inputs.db_reader, OfflineDBReader)
    assert [t.name for t in inputs.extra_read_tools] == ["read_file"]


def test_a_judge_block_the_conductor_wrote_replays_as_the_model_config_it_recorded(
    tmp_path: Path,
) -> None:
    judge = ModelConfig(
        provider="openrouter",
        name="anthropic/claude-sonnet-4.6",
        temperature=0.0,
        reasoning=ReasoningConfig(mode="budget", budget_tokens=2048),
        openrouter=OpenRouterConfig(provider_order=["Together"], allow_fallbacks=False),
        session=ModelSessionConfig(header="x-session-id"),
        fallbacks=[ModelConfig(provider="openai", name="gpt-4o-mini")],
    )
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(trial_dir, judge_status=JudgeStatus.COMPLETED, judge_inputs=None, judge=judge)
    recorded = yaml.safe_load((trial_dir / "task.yaml").read_text())["model_config"]["judge"]
    assert RESOLVED_RECORD_KEY in recorded

    inputs = read_replay_inputs(trial_dir)

    assert inputs.judge_model_config == judge
    assert inputs.provenance.judge_model_source is ProvenanceSource.RECORDED


def test_eligible_bundle_without_rubric_raises_named_missing_input(tmp_path: Path) -> None:
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(read_tools_offered=[]),
        with_rubric=False,
    )

    assert classify_trial(trial_dir) is TrialEligibility.ELIGIBLE
    with pytest.raises(MissingReplayInputError, match="no rubric"):
        read_replay_inputs(trial_dir)


def test_classify_trial_fails_loud_when_grade_yaml_is_present_but_unreadable(
    tmp_path: Path,
) -> None:
    """A ``grade.yaml`` that is present and holds something other than a mapping
    cannot say what the run concluded — a different state from a bundle that
    carries no grade at all. Reading it as NO_GRADE would skip it at exit 0, and
    classifying it as NOT_APPLICABLE would silently drop it from the batch."""
    trial_dir = tmp_path / "unreadable_grade"
    trial_dir.mkdir()
    (trial_dir / "grade.yaml").write_text("- not\n- a\n- mapping\n")

    with pytest.raises(MissingReplayInputError, match="cannot say what the run concluded"):
        classify_trial(trial_dir)


def _grade_less_bundle(
    trial_dir: Path,
    *,
    status: TrialStatus = TrialStatus.COMPLETED,
    termination_reason: TerminationReason | None = None,
    grading_error: str | None = None,
) -> None:
    """A trial the run recorded without a grade: what a trial grading refused, or
    one the infrastructure aborted, leaves on disk."""
    _write_recorded_inputs(
        trial_dir,
        _trajectory(
            status=status, termination_reason=termination_reason, grading_error=grading_error
        ),
    )


def _aborted_bundle(trial_dir: Path) -> None:
    _grade_less_bundle(
        trial_dir, status=TrialStatus.ERROR, termination_reason=TerminationReason.API_TIMEOUT
    )


def _lost_bundle(trial_dir: Path) -> None:
    _grade_less_bundle(
        trial_dir, status=TrialStatus.ERROR, termination_reason=TerminationReason.TRIAL_LOST
    )


def test_grade_less_bundles_are_skips_that_say_which_grade_less_shape_they_are(
    tmp_path: Path,
) -> None:
    """A bundle carrying no ``grade.yaml`` is a declared skip, not a failure, and
    its reason comes from its own recorded trajectory: the trial grading refused
    carries the recorded ``grading_error``, the aborted one carries its termination
    reason, the one whose runner lost it says the trial was measured and the
    registration went before a verdict could be computed, and one in none of those
    shapes — a completed trial the run simply never graded — is named as the anomaly
    it is. All four sentences DIFFER — one hardcoded "(infrastructure abort)" for the
    lot would mislabel most of the population, and a lost trial reaching the anomaly
    arm would report a shape the harness writes deliberately as one it does not write
    at all. Classification precedes input reconstruction, so the judge is never
    reached and nothing is written."""
    source = tmp_path / "run"
    ungradeable = source / "trials" / "ungradeable" / "0"
    aborted = source / "trials" / "aborted" / "0"
    lost = source / "trials" / "lost" / "0"
    anomalous = source / "trials" / "anomalous" / "0"
    _grade_less_bundle(ungradeable, grading_error=_GRADING_REFUSAL)
    _aborted_bundle(aborted)
    _lost_bundle(lost)
    _grade_less_bundle(anomalous)
    client = _submit_report_client()

    assert classify_trial(ungradeable) is TrialEligibility.NO_GRADE
    outcomes = [
        run_replay_batch(source, replay_id="r1", trial=bundle, judge_client=client)[0]
        for bundle in (ungradeable, aborted, lost, anomalous)
    ]

    assert [o.status for o in outcomes] == [ReplayOutcomeStatus.SKIPPED_NO_GRADE] * 4
    ungradeable_reason, aborted_reason, lost_reason, anomalous_reason = (
        o.reason or "" for o in outcomes
    )
    assert _GRADING_REFUSAL in ungradeable_reason
    assert _GRADING_REFUSAL not in aborted_reason
    assert TerminationReason.API_TIMEOUT.value in aborted_reason
    assert TerminationReason.TRIAL_LOST.value in lost_reason
    assert "lost its registration" in lost_reason
    assert "neither grade-less shape" not in lost_reason
    assert "neither grade-less shape" in anomalous_reason
    assert TrialOutcomeClass.MEASURED.value in anomalous_reason
    assert len({ungradeable_reason, aborted_reason, lost_reason, anomalous_reason}) == 4
    assert client.calls == 0
    assert not (source / "replays").exists()


@pytest.mark.parametrize(
    "trajectory_content", [None, '{"messages": ['], ids=["absent", "unparseable"]
)
def test_grade_less_bundle_that_cannot_say_what_happened_is_a_named_failure(
    tmp_path: Path, trajectory_content: str | None
) -> None:
    """A bundle with no grade whose ``trajectory.yaml`` is absent or unreadable is
    FAILED naming that file — it cannot say why it has no grade, which is a
    different state from saying nothing was graded."""
    source = tmp_path / "run"
    bundle = source / "trials" / "aborted" / "0"
    _aborted_bundle(bundle)
    (bundle / "trajectory.yaml").unlink()
    if trajectory_content is not None:
        (bundle / "trajectory.yaml").write_text(trajectory_content)

    outcomes = run_replay_batch(source, replay_id="r1", trial=bundle, dry_run=True)

    assert [o.status for o in outcomes] == [ReplayOutcomeStatus.FAILED]
    assert "trajectory.yaml" in (outcomes[0].reason or "")


def test_missing_prompts_yaml_fails_loud_and_null_prompt_is_faithful(tmp_path: Path) -> None:
    """No ``prompts.yaml`` means the agent policy is unknown → named failure at
    full fidelity, never a silent empty policy; a recorded ``system_prompt: null``
    inside an existing file IS the faithful empty policy."""
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(read_tools_offered=[]),
    )

    (trial_dir / "prompts.yaml").unlink()
    with pytest.raises(MissingReplayInputError, match="no agent policy"):
        read_replay_inputs(trial_dir)

    (trial_dir / "prompts.yaml").write_text(
        yaml.dump({"system_prompt": None, "user_system_prompt": None})
    )
    inputs = read_replay_inputs(trial_dir)
    assert inputs.agent_system_prompt == ""


def test_offline_shims_return_explicit_unavailable_marker() -> None:
    reader = OfflineDBReader()
    state = reader.get_state()
    assert state != {}
    assert REPLAY_UNAVAILABLE in state["replay_unavailable"]
    assert REPLAY_UNAVAILABLE in reader.query("$.orders")["replay_unavailable"]

    hits = OfflineKnowledgeSearch().search("refund policy")
    assert len(hits) == 1
    assert REPLAY_UNAVAILABLE in hits[0].text


def test_replay_trial_uses_injected_client_and_produces_result(tmp_path: Path) -> None:
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(
            state_diff_text="orders[O-1]: refunded false -> true",
            read_tools_offered=["get_db_state", "query_db", "read_file"],
        ),
    )
    inputs = read_replay_inputs(trial_dir)
    client = _submit_report_client()

    result = replay_trial(inputs, judge_client=client)

    assert result.status is JudgeRunStatus.COMPLETED
    assert result.score == 1.0
    # The one production judge was driven by the injected client — no network.
    assert client.calls == 1
    # And it echoes the reconstructed state_diff + the FULL offline read surface —
    # read_file included, shimmed for the workspace part — so a replay bundle is
    # itself re-replayable without losing tools.
    assert result.state_diff == "orders[O-1]: refunded false -> true"
    assert result.read_tools_offered == ("get_db_state", "query_db", "read_file")
    assert result.kb_tools_offered == ()


def _completed_bundle(trial_dir: Path) -> None:
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(read_tools_offered=["get_db_state", "query_db"]),
    )


def test_a_re_run_does_not_re_judge_what_the_first_run_wrote(tmp_path: Path) -> None:
    """End to end over a real batch: what replay writes is never a trial to replay.

    Two things hold it, and the source is pointed at after a real replay so both are
    exercised together — the artifacts sit under the reserved ``replays/`` name, and
    a replay bundle records a judge trajectory rather than a trial's, so it carries
    no ``trajectory.yaml`` to be identified by.
    """
    source = tmp_path / "run"
    _completed_bundle(source / "trials" / "refund_task" / "0")

    run_replay_batch(source, replay_id="r1", judge_client=_submit_report_client())

    assert (source / "replays" / "r1").is_dir()
    assert discover_trial_bundles(source) == [source / "trials" / "refund_task" / "0"]


def test_every_shape_a_run_writes_is_discovered_and_named(tmp_path: Path) -> None:
    """Nothing recorded under ``--source`` is absent from the batch's output.

    All five shapes the harness writes today, including the two grade-less ones and
    the provision failure that carries no ``task.yaml`` either: each is discovered
    and each gets the disposition its own contents earn. Before identity keyed on
    the trajectory, three of these five appeared in no line of the output.

    The bundle named with ``--trial`` gets the same answer as the one discovery
    found — two entry points reach classification by different paths, and a
    disagreement between them is the specific regression this rule is exposed to.
    """
    source = tmp_path / "run"
    run = write_five_shape_run(source)

    outcomes = run_replay_batch(source, replay_id="r1", dry_run=True)

    assert [outcome.bundle for outcome in outcomes] == run.bundles
    by_bundle = {outcome.bundle: outcome.status for outcome in outcomes}
    assert by_bundle == {
        run.judged: ReplayOutcomeStatus.WOULD_REPLAY,
        run.state_only: ReplayOutcomeStatus.SKIPPED_NOT_APPLICABLE,
        run.ungradeable: ReplayOutcomeStatus.SKIPPED_NO_GRADE,
        run.aborted: ReplayOutcomeStatus.SKIPPED_NO_GRADE,
        run.provision_failure: ReplayOutcomeStatus.SKIPPED_NO_GRADE,
    }

    named = run_replay_batch(source, replay_id="r1", trial=run.ungradeable, dry_run=True)
    assert [outcome.status for outcome in named] == [by_bundle[run.ungradeable]]


def test_the_discovered_total_is_stable_when_a_trial_aborts_instead_of_running(
    tmp_path: Path,
) -> None:
    """Two runs of one suite, differing only in that one trial never provisioned.

    The eligible count *must* move — an aborted trial genuinely has no judge stage —
    so what is delivered is a stable denominator with the difference named: the
    discovered total is equal and one eligible trades for exactly one no-grade skip.
    Before this rule the aborted run simply reported a smaller batch, with nothing
    saying why.

    The aborted arm is built through the production writer rather than by hand, so
    the shape under test is the one a run leaves behind.
    """
    completed = tmp_path / "completed"
    aborted = tmp_path / "aborted"
    for source in (completed, aborted):
        _completed_bundle(source / "trials" / "refund_task" / "0")
        _write_bundle(
            source / "trials" / "state_only" / "0",
            judge_status=JudgeStatus.UNSPECIFIED,
            judge_inputs=None,
            with_rubric=False,
        )
    _completed_bundle(completed / "trials" / "billing" / "0")
    write_provision_failure_bundle(aborted, task_id="billing")

    counted = [
        Counter(
            outcome.status for outcome in run_replay_batch(source, replay_id="r1", dry_run=True)
        )
        for source in (completed, aborted)
    ]

    assert [sum(counts.values()) for counts in counted] == [3, 3]
    assert counted[0] - counted[1] == Counter({ReplayOutcomeStatus.WOULD_REPLAY: 1})
    assert counted[1] - counted[0] == Counter({ReplayOutcomeStatus.SKIPPED_NO_GRADE: 1})


def test_not_applicable_trial_reported_skipped_even_with_grading_override(tmp_path: Path) -> None:
    from tolokaforge.runner.models import Rubric

    source = tmp_path / "run"
    _completed_bundle(source / "trials" / "judged" / "0")
    _write_bundle(
        source / "trials" / "state_only" / "0",
        judge_status=JudgeStatus.UNSPECIFIED,
        judge_inputs=None,
        with_rubric=False,
    )

    outcomes = run_replay_batch(
        source,
        replay_id="r1",
        grading_override=GradingOverride(rubric=Rubric.model_validate(_RUBRIC), customization=None),
        dry_run=True,
    )

    by_name = {o.bundle.parent.name: o.status for o in outcomes}
    assert by_name["state_only"] is ReplayOutcomeStatus.SKIPPED_NOT_APPLICABLE
    assert by_name["judged"] is ReplayOutcomeStatus.WOULD_REPLAY


@pytest.mark.parametrize(
    ("grade_content", "reason_match"),
    [
        ("- not\n- a\n- mapping\n", "cannot say what the run concluded"),
        ('{"a": [', "unreadable YAML"),
    ],
    ids=["not_mapping", "syntax_corrupt"],
)
def test_batch_records_unreadable_grade_as_named_failure_and_continues(
    tmp_path: Path, grade_content: str, reason_match: str
) -> None:
    """A discovered bundle whose ``grade.yaml`` turns out unreadable — wrong shape
    or YAML syntax that does not even parse — is a NAMED per-trial FAILED, never a
    silent NOT_APPLICABLE skip and never a batch-aborting raw parser traceback,
    and the rest of the batch still runs."""
    source = tmp_path / "run"
    _completed_bundle(source / "trials" / "good" / "0")
    corrupt = source / "trials" / "corrupt" / "0"
    _completed_bundle(corrupt)
    (corrupt / "grade.yaml").write_text(grade_content)

    outcomes = run_replay_batch(source, replay_id="r1", dry_run=True)

    by_name = {o.bundle.parent.name: o for o in outcomes}
    assert by_name["good"].status is ReplayOutcomeStatus.WOULD_REPLAY
    failed = by_name["corrupt"]
    assert failed.status is ReplayOutcomeStatus.FAILED
    assert failed.reason is not None and reason_match in failed.reason


def _corrupt_trajectory(trial_dir: Path) -> None:
    _completed_bundle(trial_dir)
    (trial_dir / "trajectory.yaml").write_text(yaml.dump({"task_id": "corrupt"}))


def _uncompilable_read_rule(trial_dir: Path) -> None:
    from tolokaforge.runner.models import JudgeCustomization

    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(
            read_tools_offered=["get_db_state", "query_db"],
            customization=JudgeCustomization(evidence={"state_changing_calls": True}),
        ),
    )
    _record_tool_schemas(
        trial_dir,
        schema_version=8,
        get_order={"read_when": {"args": {"id": {"regex": r"(\w)\1"}}}},
    )


@pytest.mark.parametrize(
    ("corrupt_bundle", "reason"),
    [
        (_corrupt_trajectory, "Trajectory"),
        (_uncompilable_read_rule, "the linear regex engine cannot compile"),
    ],
    ids=["corrupt-trajectory", "uncompilable-read-rule"],
)
def test_batch_records_invalid_recorded_input_as_named_failure_and_continues(
    tmp_path: Path, corrupt_bundle: Any, reason: str
) -> None:
    """A recorded input that fails validation is a named per-trial FAILED — it must
    never abort the batch after other trials have already spent judge tokens."""
    source = tmp_path / "run"
    _completed_bundle(source / "trials" / "good" / "0")
    corrupt_bundle(source / "trials" / "corrupt" / "0")

    outcomes = run_replay_batch(source, replay_id="r1", dry_run=True)

    by_name = {o.bundle.parent.name: o for o in outcomes}
    assert by_name["good"].status is ReplayOutcomeStatus.WOULD_REPLAY
    failed = by_name["corrupt"]
    assert failed.status is ReplayOutcomeStatus.FAILED
    assert failed.reason is not None and reason in failed.reason


def _checksums(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_replay_writes_artifacts_and_leaves_originals_byte_identical(tmp_path: Path) -> None:
    source = tmp_path / "run"
    bundle = source / "trials" / "refund_task" / "0"
    _completed_bundle(bundle)

    before = _checksums(bundle)

    outcomes = run_replay_batch(source, replay_id="r1", judge_client=_submit_report_client())

    assert len(outcomes) == 1 and outcomes[0].status is ReplayOutcomeStatus.REPLAYED
    replay_dir = source / "replays" / "r1" / "trials" / "refund_task" / "0"
    assert (replay_dir / "grade.yaml").exists()
    assert (replay_dir / "judge_trajectory.yaml").exists()
    assert (replay_dir / "judge_inputs.yaml").exists()
    assert (replay_dir / "replay_provenance.yaml").exists()

    # Every original file is byte-identical, and no new file was written into the
    # original bundle — replay never opens an original for write.
    assert _checksums(bundle) == before


def test_rejudge_cli_dry_run_spends_nothing(tmp_path: Path) -> None:
    from tolokaforge.dx.cli.main import cli

    source = tmp_path / "run"
    _completed_bundle(source / "trials" / "refund_task" / "0")

    result = CliRunner().invoke(cli, ["rejudge", "--source", str(source), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "1 eligible" in result.output
    # Dry-run spends nothing and writes nothing.
    assert not (source / "replays").exists()


@pytest.mark.parametrize(
    "flag", [["--knowledge-search", "off"], ["--evidence", "transcript"]], ids=lambda f: f[0]
)
def test_rejudge_takes_customization_only_through_grading(tmp_path: Path, flag: list[str]) -> None:
    """``--grading`` carries every customization key, so no flag overrides one of them."""
    from tolokaforge.dx.cli.main import cli

    source = tmp_path / "run"
    _completed_bundle(source / "trials" / "refund_task" / "0")

    result = CliRunner().invoke(cli, ["rejudge", "--source", str(source), "--dry-run", *flag])

    assert result.exit_code == 2
    assert f"No such option: {flag[0]}" in result.output


_CENSUS_LINE = re.compile(
    r"(?:Re-judged|Would re-judge): (?P<discovered>\d+) discovered: "
    r"(?P<eligible>\d+) eligible, "
    r"(?P<not_applicable>\d+) not-applicable, "
    r"(?P<no_grade>\d+) no-grade, "
    r"(?P<failed>\d+) failed"
)
_PER_TRIAL_MARKERS = ("re-judged", "would re-judge", "skip (not applicable)", "skip (no grade)")


def _census_and_lines(output: str) -> tuple[dict[str, int], int]:
    """The census line's counts, and how many per-trial lines were printed above it.

    Whitespace-normalised because Rich wraps both the census line and the reasons.
    The census label is matched as part of the line so it is not counted as one of
    the per-trial lines above it.
    """
    printed = " ".join(output.split())
    match = _CENSUS_LINE.search(printed)
    assert match is not None, output
    above = printed[: match.start()]
    lines = sum(above.count(marker) for marker in _PER_TRIAL_MARKERS)
    return {key: int(value) for key, value in match.groupdict().items()}, lines


def test_rejudge_cli_reports_a_grade_less_bundle_and_exits_zero(tmp_path: Path) -> None:
    """A grade-less bundle named with ``--trial`` is a reported skip at exit 0, not
    a failure: the same disposition the batch gives it, through the other entry
    point. Its count sits on the census line, which therefore accounts for every
    per-trial line printed above it — a census omitting a disposition the same
    function prints is arithmetically incomplete."""
    from tolokaforge.dx.cli.main import cli

    source = tmp_path / "run"
    bundle = source / "trials" / "aborted" / "0"
    _aborted_bundle(bundle)

    result = CliRunner().invoke(cli, ["rejudge", "--source", str(source), "--trial", str(bundle)])

    assert result.exit_code == 0, result.output
    assert "skip (no grade)" in result.output
    assert TerminationReason.API_TIMEOUT.value in " ".join(result.output.split())
    assert not (source / "replays").exists()

    census, per_trial_lines = _census_and_lines(result.output)
    assert census == {
        "discovered": 1,
        "eligible": 0,
        "not_applicable": 0,
        "no_grade": 1,
        "failed": 0,
    }
    assert census["discovered"] == per_trial_lines


def test_rejudge_cli_prints_bracket_bearing_grading_errors_verbatim(tmp_path: Path) -> None:
    """A recorded ``grading_error`` is free text: ``judge[v2] refused …`` must reach
    the operator as written on the skip line, not with ``[v2]`` eaten as console
    markup. Digit-leading brackets stay literal on their own, so only a
    letter-leading bracket discriminates."""
    from tolokaforge.dx.cli.main import cli

    source = tmp_path / "run"
    bundle = source / "trials" / "ungradeable" / "0"
    _grade_less_bundle(bundle, grading_error="judge[v2] refused the transcript")

    result = CliRunner().invoke(
        cli, ["rejudge", "--source", str(source), "--trial", str(bundle)], env={"COLUMNS": "200"}
    )

    assert result.exit_code == 0, result.output
    assert "judge[v2] refused the transcript" in " ".join(result.output.split())


def test_rejudge_cli_census_accounts_for_every_bundle_under_the_source(tmp_path: Path) -> None:
    """One batch, four dispositions, and a census that adds up to what it found.

    The mixed batch is where the arithmetic bites: five recorded trials, one of
    each disposition the command can reach, so a census counting the wrong status
    or omitting one cannot be right by coincidence. Bundle paths print relative to
    ``--source``, as retrace already prints them — an absolute path per line is
    unreadable on a real run dir and says nothing the header has not.
    """
    from tolokaforge.dx.cli.main import cli

    source = tmp_path / "run"
    run = write_five_shape_run(source)

    result = CliRunner().invoke(
        cli, ["rejudge", "--source", str(source), "--dry-run"], env={"COLUMNS": "200"}
    )

    assert result.exit_code == 0, result.output
    census, per_trial_lines = _census_and_lines(result.output)
    assert census == {
        "discovered": 5,
        "eligible": 1,
        "not_applicable": 1,
        "no_grade": 3,
        "failed": 0,
    }
    assert census["discovered"] == per_trial_lines
    assert sum(count for key, count in census.items() if key != "discovered") == per_trial_lines

    printed = " ".join(result.output.split())
    assert str(run.judged.relative_to(source)) in printed
    assert str(run.judged) not in printed


def test_rejudge_cli_exits_nonzero_when_the_source_holds_no_bundle(tmp_path: Path) -> None:
    """A batch that discovered nothing claimed nothing and returned success.

    That is the maximal case of the defect this command's census exists to remove:
    zeros on every line read as "the suite was clean" to a scripted caller. It now
    exits non-zero naming the directory searched, matching ``retrace``.
    """
    from tolokaforge.dx.cli.main import cli

    empty = tmp_path / "nowhere"
    empty.mkdir()

    result = CliRunner().invoke(
        cli, ["rejudge", "--source", str(empty), "--dry-run"], env={"COLUMNS": "200"}
    )

    assert result.exit_code == 1, result.output
    assert "no trial bundle" in result.output
    assert str(empty) in " ".join(result.output.split())
    assert "discovered:" not in result.output


def test_the_written_report_accounts_for_the_batch_not_the_judged_subset(tmp_path: Path) -> None:
    """``replay_report.yaml`` says what the batch found, not only what it compared.

    One trial of five was judge-eligible here; a report carrying that one trial and
    nothing else would read as a five-trial suite that shrank. The census crosses
    the artifact boundary, so an operator diffing two runs reads it off the file.
    """
    source = tmp_path / "run"
    write_five_shape_run(source)

    outcomes = run_replay_batch(source, replay_id="r1", judge_client=_submit_report_client())
    report = emit_replay_report(outcomes, source=source, replay_id="r1")

    assert report is not None
    assert report.batch == ReplayBatchCensus(
        discovered=5, replayed=1, skipped_not_applicable=1, skipped_no_grade=3, failed=0
    )
    assert len(report.trials) == 1
    written = yaml.safe_load((source / "replays" / "r1" / "replay_report.yaml").read_text())
    assert written["batch"] == {
        "discovered": 5,
        "replayed": 1,
        "skipped_not_applicable": 1,
        "skipped_no_grade": 3,
        "failed": 0,
    }


def test_a_census_that_cannot_account_for_the_batch_is_refused() -> None:
    """The model enforces its own arithmetic: four dispositions, one denominator.

    A census whose parts do not sum to ``discovered`` under-reports the batch by
    exactly the bundles it lost, which is the silence this issue is about.
    """
    with pytest.raises(ValidationError, match="cannot say what became of every bundle"):
        ReplayBatchCensus(
            discovered=5, replayed=1, skipped_not_applicable=1, skipped_no_grade=1, failed=0
        )


def test_rejudge_cli_exits_nonzero_when_a_trial_fails(tmp_path: Path) -> None:
    """A batch with a FAILED trial must not exit 0 — a scripted caller (CI, sweep)
    would otherwise read a partially-failed replay as a clean one."""
    from tolokaforge.dx.cli.main import cli

    source = tmp_path / "run"
    _completed_bundle(source / "trials" / "refund_task" / "0")
    non_bundle = tmp_path / "not_a_bundle"
    non_bundle.mkdir()

    result = CliRunner().invoke(
        cli, ["rejudge", "--source", str(source), "--trial", str(non_bundle)]
    )

    assert result.exit_code == 1, result.output
    assert "failed" in result.output


# ---------------------------------------------------------------------------
# Judge customization: the recorded value with a --grading layer over it
# ---------------------------------------------------------------------------

_TRANSCRIPT_EVIDENCE: dict[str, Any] = {
    "evidence": {
        "state_diff": False,
        "db": False,
        "workspace": False,
        "tool_results": {"max_chars": 500},
        "state_changing_calls": True,
    }
}


def _dotted_paths(layer: dict[str, Any], prefix: str = "") -> list[str]:
    """The dotted key paths a raw customization layer sets, as provenance names them."""
    paths: list[str] = []
    for key, value in layer.items():
        if isinstance(value, dict):
            paths += _dotted_paths(value, f"{prefix}{key}.")
        else:
            paths.append(f"{prefix}{key}")
    return paths


def _override(*, rubric: bool = False, **customization: Any) -> GradingOverride:
    """A ``--grading`` override carrying *customization* and, with *rubric*, a rubric."""
    from tolokaforge.runner.models import Rubric

    return GradingOverride(
        rubric=Rubric.model_validate(_RUBRIC) if rubric else None,
        customization=customization or None,
    )


@pytest.mark.parametrize(
    "recorded",
    [
        {"system_prompt": "Grade strictly against the refund policy handbook."},
        {"evidence": {"agent_policy": False}},
        {"evidence": {"knowledge_search": False}},
        {"evidence": {"knowledge_search": {"snippet_chars": None}}},
        {"evidence": {"knowledge_search": {"snippet_chars": 40}}},
        _TRANSCRIPT_EVIDENCE,
    ],
    ids=[
        "system-prompt",
        "agent-policy-off",
        "kb-off",
        "whole-documents",
        "snippet-40",
        "transcript",
    ],
)
@pytest.mark.parametrize("record", list(CustomizationRecord))
def test_a_recorded_customization_replays_with_every_key_it_sets_stamped_recorded(
    tmp_path: Path, recorded: dict[str, Any], record: CustomizationRecord
) -> None:
    """``judge_inputs.yaml customization`` is the record a bundle carries; one that
    predates it is read from ``task.yaml`` instead, and either way every key it sets
    reaches the judge's options with the same source."""
    from tolokaforge.runner.models import JudgeCustomization

    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    in_judge_inputs = record is CustomizationRecord.JUDGE_INPUTS
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(
            customization=JudgeCustomization.model_validate(recorded) if in_judge_inputs else None
        ),
        recorded_customization=None if in_judge_inputs else recorded,
    )
    _record_tool_schemas(trial_dir, schema_version=8, get_order=None)

    inputs = read_replay_inputs(trial_dir)

    assert inputs.options.customization == JudgeCustomization.model_validate(recorded)
    assert inputs.provenance.customization == inputs.options.customization
    assert inputs.provenance.customization_sources == dict.fromkeys(
        _dotted_paths(recorded), ProvenanceSource.RECORDED
    )
    assert inputs.provenance.customization_record is record


def test_a_bundle_recording_no_customization_replays_the_defaults_with_no_source(
    tmp_path: Path,
) -> None:
    from tolokaforge.runner.models import JudgeCustomization

    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(customization=JudgeCustomization()),
    )

    inputs = read_replay_inputs(trial_dir)

    assert inputs.options.customization == JudgeCustomization()
    assert inputs.provenance.customization_sources == {}


def test_an_override_wins_on_the_keys_it_sets_and_inherits_the_rest(tmp_path: Path) -> None:
    from tolokaforge.runner.models import JudgeCustomization

    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(
            customization=JudgeCustomization(
                system_prompt="Recorded judge voice.",
                evidence={"agent_policy": False, "tool_results": {"max_chars": 40}},
            )
        ),
    )

    inputs = read_replay_inputs(
        trial_dir,
        grading_override=_override(
            system_prompt="Override judge voice.", evidence={"knowledge_search": False}
        ),
    )

    assert inputs.options.customization == JudgeCustomization(
        system_prompt="Override judge voice.",
        evidence={
            "agent_policy": False,
            "knowledge_search": False,
            "tool_results": {"max_chars": 40},
        },
    )
    assert inputs.provenance.customization_sources == {
        "system_prompt": ProvenanceSource.OVERRIDE,
        "evidence.agent_policy": ProvenanceSource.RECORDED,
        "evidence.knowledge_search": ProvenanceSource.OVERRIDE,
        "evidence.tool_results.max_chars": ProvenanceSource.RECORDED,
    }


def test_a_rubric_only_override_keeps_every_customization_key_recorded(tmp_path: Path) -> None:
    from tolokaforge.runner.models import JudgeCustomization

    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    recorded = JudgeCustomization(
        system_prompt="Recorded judge voice.",
        evidence={"knowledge_search": {"snippet_chars": 40}},
    )
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(customization=recorded),
    )

    inputs = read_replay_inputs(trial_dir, grading_override=_override(rubric=True))

    assert inputs.provenance.rubric_source is ProvenanceSource.OVERRIDE
    assert inputs.options.customization == recorded
    assert inputs.provenance.customization_sources == {
        "system_prompt": ProvenanceSource.RECORDED,
        "evidence.knowledge_search.snippet_chars": ProvenanceSource.RECORDED,
    }


@pytest.mark.parametrize(
    ("override", "offered", "withheld"),
    [
        ({"evidence": {"knowledge_search": False}}, (), ("search_kb",)),
        ({"evidence": {"knowledge_search": True}}, ("search_kb",), ()),
        ({}, ("search_kb",), ()),
    ],
    ids=["withheld", "offered", "recorded"],
)
def test_a_grading_file_decides_whether_the_replayed_judge_gets_the_knowledge_base(
    tmp_path: Path, override: dict[str, Any], offered: tuple[str, ...], withheld: tuple[str, ...]
) -> None:
    """``evidence.knowledge_search`` in a ``--grading`` file reaches the replayed judge's
    tool surface: the recorded run offered ``search_kb``, and the override withholds it."""
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(),
        kb_gating=JudgeKbGating(offered=["search_kb"], withheld=[]),
    )
    grading = tmp_path / "grading.yaml"
    grading.write_text(
        yaml.safe_dump({"llm_judge": {"rubric": _RUBRIC, "customization": override or None}})
    )

    inputs = read_replay_inputs(trial_dir, grading_override=load_grading_override(grading))
    result = replay_trial(inputs, judge_client=_submit_report_client())

    assert (result.kb_tools_offered, result.kb_tools_withheld) == (offered, withheld)


def test_a_customization_only_grading_file_keeps_the_recorded_rubric(tmp_path: Path) -> None:
    """A grading file with no rubric overrides the customization alone: here it makes
    the replay of a judge over the state one from the transcript alone, with the
    rubric recorded.
    The tool declarations are recorded whatever the judge graded from, so it lists the
    same calls as state-changing as a live judge with the same parts would."""
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(
            state_diff_text="orders[O-1]: refunded false -> true",
            read_tools_offered=["get_db_state", "query_db", "read_file"],
        ),
    )
    _record_tool_schemas(trial_dir, schema_version=8, get_order=False)
    grading = tmp_path / "grading.yaml"
    grading.write_text(yaml.safe_dump({"llm_judge": {"customization": _TRANSCRIPT_EVIDENCE}}))

    override = load_grading_override(grading)
    inputs = read_replay_inputs(trial_dir, grading_override=override)

    assert override.rubric is None
    assert inputs.provenance.rubric_source is ProvenanceSource.RECORDED
    evidence = inputs.options.customization.evidence
    assert not any(evidence.needs(view) for view in FINAL_STATE_INPUTS)
    assert inputs.provenance.customization_sources == dict.fromkeys(
        _dotted_paths(_TRANSCRIPT_EVIDENCE), ProvenanceSource.OVERRIDE
    )
    assert inputs.options.tool_effects.can_change_state("get_order", '{"id": "O-1"}') is False
    assert inputs.db_reader is None and inputs.state_diff is None
    assert inputs.extra_read_tools == []


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"llm_judge": {"customization": None}}, "overrides nothing"),
        (
            {"llm_judge": {"customization": {"evidence": {"db": False}}, "judge_kind": "x"}},
            "carries only customization",
        ),
        ({"llm_judge": {"customization": {"evidence": {"db": "vibes"}}}}, "evidence.db"),
        ({"llm_judge": {"customization": ["evidence"]}}, "not a mapping"),
        ({"judge": {}}, "has no llm_judge.rubric"),
    ],
    ids=["null-customization", "stray-key", "invalid-value", "not-a-mapping", "neither"],
)
def test_a_grading_file_that_overrides_nothing_or_nonsense_is_refused_at_load(
    tmp_path: Path, document: dict[str, Any], message: str
) -> None:
    grading = tmp_path / "grading.yaml"
    grading.write_text(yaml.safe_dump(document))

    with pytest.raises((MissingReplayInputError, ValidationError), match=message):
        load_grading_override(grading)


def test_a_grading_file_carries_only_the_customization_keys_it_sets(tmp_path: Path) -> None:
    grading = tmp_path / "grading.yaml"
    grading.write_text(
        yaml.safe_dump(
            {
                "llm_judge": {
                    "rubric": _RUBRIC,
                    "customization": {"evidence": {"knowledge_search": {"snippet_chars": None}}},
                }
            }
        )
    )

    assert load_grading_override(grading).customization == {
        "evidence": {"knowledge_search": {"snippet_chars": None}}
    }


def test_a_bundle_predating_the_record_falls_back_to_task_yaml_and_says_so(
    tmp_path: Path,
) -> None:
    """A bundle without ``judge_inputs.yaml customization`` is read from ``task.yaml``,
    which is taken before the run-level ``grader.judge`` layer: the provenance names
    the record and ``rejudge`` warns about the layer it can miss."""
    from tolokaforge.dx.cli.main import cli

    source = tmp_path / "run"
    trial_dir = source / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(),
        recorded_customization={"evidence": {"agent_policy": False}},
    )

    inputs = read_replay_inputs(trial_dir)
    result = CliRunner().invoke(cli, ["rejudge", "--source", str(source), "--dry-run"])

    assert inputs.provenance.customization_record is CustomizationRecord.TASK_YAML
    assert inputs.options.customization.evidence.agent_policy is False
    assert result.exit_code == 0, result.output
    assert "customization=task_yaml: evidence.agent_policy=recorded" in result.output
    assert "does not carry a run-level grader.judge layer" in result.output


@pytest.mark.parametrize(
    ("turned_on", "named"),
    [
        (
            {"state_diff": True, "db": True, "workspace": True},
            "evidence.state_diff, evidence.db, evidence.workspace",
        ),
        ({"db": True}, "evidence.db"),
    ],
    ids=["every-view", "db-reads"],
)
def test_an_override_turning_on_a_view_the_bundle_never_recorded_is_refused(
    tmp_path: Path, turned_on: dict[str, bool], named: str
) -> None:
    """A judge without the final-state parts read no view of the final state, so its
    bundle records none: an override turning a part back on cannot be rebuilt and is
    refused naming each part."""
    from tolokaforge.runner.models import JudgeCustomization

    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(
            customization=JudgeCustomization.model_validate(_TRANSCRIPT_EVIDENCE)
        ),
    )
    (trial_dir / "env.yaml").write_text(yaml.safe_dump({"db": {"orders": [{"id": "O-1"}]}}))

    with pytest.raises(
        MissingReplayInputError, match=rf"with {re.escape(named)} turned on: the bundle's"
    ):
        read_replay_inputs(trial_dir, grading_override=_override(evidence=turned_on))


def test_an_override_turning_on_a_view_the_bundle_recorded_replays(tmp_path: Path) -> None:
    """The refusal reads the recorded surface, never the recorded customization: a
    judge whose ``db`` part was off over a bundle that still recorded DB reads is not
    refused."""
    from tolokaforge.runner.models import JudgeCustomization

    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(
            customization=JudgeCustomization(evidence={"workspace": False}),
            read_tools_offered=["get_db_state", "query_db", "read_file"],
        ),
    )

    inputs = read_replay_inputs(trial_dir, grading_override=_override(evidence={"workspace": True}))

    assert inputs.db_reader is not None
    assert [tool.name for tool in inputs.extra_read_tools] == ["read_file"]


def test_a_recorded_read_tool_no_part_owns_is_refused(tmp_path: Path) -> None:
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(read_tools_offered=["get_db_state", "lookup_ledger"]),
    )

    with pytest.raises(MissingReplayInputError, match="'lookup_ledger', which no evidence part"):
        read_replay_inputs(trial_dir)


_PRE_EVIDENCE_DUMP: dict[str, Any] = {
    "disable_knowledge_search": None,
    "system_prompt": None,
    "include_agent_system_prompt": None,
}
"""``JudgeCustomization().model_dump()`` as the engine recorded it before ``evidence``."""


@pytest.mark.parametrize("rubric", [False, True], ids=["recorded-rubric", "override-rubric"])
@pytest.mark.parametrize(
    ("recorded", "replayed"),
    [
        (_PRE_EVIDENCE_DUMP, {}),
        (
            {**_PRE_EVIDENCE_DUMP, "system_prompt": "Grade strictly."},
            {"system_prompt": "Grade strictly."},
        ),
    ],
    ids=["defaults", "system-prompt"],
)
def test_a_bundle_recorded_before_evidence_replays_its_unset_tri_states_as_absent(
    tmp_path: Path, recorded: dict[str, Any], replayed: dict[str, Any], rubric: bool
) -> None:
    from tolokaforge.runner.models import JudgeCustomization

    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(),
        recorded_customization=recorded,
    )

    inputs = read_replay_inputs(trial_dir, grading_override=_override(rubric=rubric))

    assert inputs.options.customization == JudgeCustomization.model_validate(replayed)
    assert inputs.provenance.customization_sources == dict.fromkeys(
        replayed, ProvenanceSource.RECORDED
    )


@pytest.mark.parametrize("rubric", [False, True], ids=["recorded-rubric", "override-rubric"])
@pytest.mark.parametrize(
    ("renamed", "remedy"),
    [
        (
            {**_PRE_EVIDENCE_DUMP, "disable_knowledge_search": True},
            "write evidence.knowledge_search: false",
        ),
        (
            {**_PRE_EVIDENCE_DUMP, "include_agent_system_prompt": False},
            "write evidence.agent_policy: false",
        ),
        (
            {**_PRE_EVIDENCE_DUMP, "judge_snippet_chars": None},
            "evidence.knowledge_search: {snippet_chars: null}",
        ),
    ],
    ids=["kb", "agent-policy", "snippet"],
)
def test_a_bundle_recording_a_renamed_key_is_refused_naming_its_replacement(
    tmp_path: Path, renamed: dict[str, Any], remedy: str, rubric: bool
) -> None:
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(),
        recorded_customization=renamed,
    )

    with pytest.raises(MissingReplayInputError, match=re.escape(remedy)):
        read_replay_inputs(trial_dir, grading_override=_override(rubric=rubric))


@pytest.mark.parametrize(
    ("key", "bad_value"),
    [
        ("system_prompt", ""),
        ("system_prompt", 7),
        ("evidence", {"agent_policy": "sometimes"}),
        ("evidence", {"knowledge_search": {"snippet_chars": 0}}),
        ("evidence", "vibes"),
    ],
)
def test_a_recorded_customization_the_model_refuses_fails_loud(
    tmp_path: Path, key: str, bad_value: object
) -> None:
    """The writer never records one, so the bundle was edited by hand. A rubric-only
    override keeps the recorded rubric block from being parsed, so the customization
    reader is what meets the value."""
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(),
        recorded_customization={key: bad_value},
    )

    with pytest.raises(MissingReplayInputError, match=f"(?s)customization in .*task.yaml.*{key}"):
        read_replay_inputs(trial_dir, grading_override=_override(rubric=True))


@pytest.mark.parametrize(
    ("recorded_prompt", "override", "bundle_wins"),
    [
        (None, {}, True),
        ("Recorded judge voice.", {}, True),
        (None, {"system_prompt": "Override judge voice."}, False),
        ("Recorded judge voice.", {"system_prompt": None}, False),
    ],
    ids=["default-body", "custom-body", "override-body", "override-reset"],
)
def test_the_bundle_prompt_stands_in_for_the_prompt_it_was_composed_from(
    tmp_path: Path, recorded_prompt: str | None, override: dict[str, Any], bundle_wins: bool
) -> None:
    from tolokaforge.runner.models import JudgeCustomization

    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    task_customization = {"system_prompt": recorded_prompt} if recorded_prompt else None
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(customization=JudgeCustomization(system_prompt=recorded_prompt)),
        recorded_customization=task_customization,
        judge_prompt="The composed prompt the run graded under.",
    )

    inputs = read_replay_inputs(trial_dir, grading_override=_override(**override))
    client = _submit_report_client()
    replay_trial(inputs, judge_client=client)

    if bundle_wins:
        assert inputs.provenance.judge_prompt_source is ProvenanceSource.BUNDLE
        assert "system_prompt" not in inputs.provenance.customization_sources
        assert client.seen_system.startswith("The composed prompt the run graded under.")
    else:
        assert inputs.provenance.judge_prompt_source is None
        assert inputs.provenance.customization_sources["system_prompt"] is (
            ProvenanceSource.OVERRIDE
        )
        assert not client.seen_system.startswith("The composed prompt")


def test_a_judge_only_bundle_replays_under_its_run_level_system_prompt(tmp_path: Path) -> None:
    """``grader.judge`` is layered over the task's customization in the ``judge_only``
    grader, after ``task.yaml`` and ``prompts.yaml`` were written from the task alone:
    the grade's record carries the run layer, so replay judges under its prompt."""
    from tolokaforge.core.grading.judge import effective_judge_system_prompt
    from tolokaforge.core.grading.judge_only_helpers import run_judge_only_for_trajectory
    from tolokaforge.core.logging import get_logger
    from tolokaforge.runner.models import JudgeCustomization, LLMJudgeConfig

    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    trajectory = _trajectory()
    task_config = LLMJudgeConfig.model_validate(
        {"rubric": _RUBRIC, "customization": {"evidence": {"agent_policy": False}}}
    )
    _write_recorded_inputs(
        trial_dir,
        trajectory,
        recorded_customization={"evidence": {"agent_policy": False}},
        judge_prompt=effective_judge_system_prompt(task_config),
    )
    grade = run_judge_only_for_trajectory(
        trial_id="t",
        llm_judge_config=task_config,
        judge_model_config=_JUDGE_MODEL,
        trajectory=trajectory,
        agent_system_prompt=_AGENT_PROMPT,
        agent_tools=[],
        override=JudgeCustomization.model_validate({"system_prompt": "Run-level judge voice."}),
        llm_client=_submit_report_client(),
        logger=get_logger("test"),
    )
    assert grade is not None
    FileArtifactWriter().write_grade(trial_dir, grade)

    inputs = read_replay_inputs(trial_dir)
    client = _submit_report_client()
    replay_trial(inputs, judge_client=client)

    assert inputs.provenance.customization_record is CustomizationRecord.JUDGE_INPUTS
    assert inputs.options.customization == JudgeCustomization(
        system_prompt="Run-level judge voice.", evidence={"agent_policy": False}
    )
    assert inputs.provenance.judge_prompt_source is None
    assert client.seen_system.startswith("Run-level judge voice.")


def test_a_customization_source_is_never_the_bundle() -> None:
    from tolokaforge.runner.models import JudgeCustomization

    with pytest.raises(ValidationError, match="names bundle"):
        ReplayProvenance(
            judge_model="openrouter/openai/gpt-4.1-mini",
            judge_model_source=ProvenanceSource.RECORDED,
            rubric_source=ProvenanceSource.RECORDED,
            customization=JudgeCustomization(),
            customization_sources={"evidence": ProvenanceSource.BUNDLE},
            customization_record=CustomizationRecord.JUDGE_INPUTS,
            fidelity_mode=FidelityMode.FULL,
        )


# ---------------------------------------------------------------------------
# A judge from the transcript alone: no final-state part, the transcript parts on
# ---------------------------------------------------------------------------

_GET_ORDER_READS = {"read_when": {"args": {"id": {"regex": "^O-"}}}}


def _record_tool_schemas(
    trial_dir: Path, *, schema_version: int, get_order: bool | dict[str, Any] | None
) -> None:
    """Stamp the bundle generation and record ``get_order`` (the trajectory's one call)
    in ``tools_schemas.yaml``, declaring *get_order* as its ``mutates_state``."""
    (trial_dir / "metrics.yaml").write_text(yaml.safe_dump({"schema_version": schema_version}))
    entry: dict[str, Any] = {
        "type": "function",
        "function": {"name": "get_order", "description": "Read an order.", "parameters": {}},
    }
    if get_order is not None:
        entry["mutates_state"] = get_order
    FileArtifactWriter().write_tools_schemas(trial_dir, [entry])


def _opening_message(inputs: Any) -> str:
    client = MessageCapturingClient(_SUBMIT_REPORT_SCRIPT)
    result = replay_trial(inputs, judge_client=client)
    assert result.status is JudgeRunStatus.COMPLETED
    assert client.seen_tool_names == ["submit_report", "show_call"]
    return str(client.snapshots[0][0].content)


@pytest.mark.parametrize(
    ("get_order", "listed"),
    [(_GET_ORDER_READS, False), (False, False), (True, True), (None, True)],
    ids=["read-rule-matches", "declared-read", "declared-write", "undeclared"],
)
def test_a_recorded_transcript_judge_reads_the_bundles_tool_declarations(
    tmp_path: Path, get_order: bool | dict[str, Any] | None, listed: bool
) -> None:
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(),
        recorded_customization=_TRANSCRIPT_EVIDENCE,
    )
    _record_tool_schemas(trial_dir, schema_version=8, get_order=get_order)

    inputs = read_replay_inputs(trial_dir)

    assert inputs.options.customization.evidence.tool_results_options() == ToolResultsOptions(
        max_chars=500
    )
    assert inputs.db_reader is None and inputs.state_diff is None
    calls = _opening_message(inputs).split("===== STATE-CHANGING CALLS =====\n")[1]
    assert calls.startswith('[call 1] get_order({"id": "O-1"})') is listed
    assert calls.startswith("(none: the agent made no call that can change state)") is not listed


@pytest.mark.parametrize("schema_version", [7, None], ids=["generation-7", "unstamped"])
def test_a_bundle_predating_the_declarations_is_refused_a_transcript_replay(
    tmp_path: Path, schema_version: int | None
) -> None:
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(read_tools_offered=["get_db_state", "query_db"]),
    )
    if schema_version is not None:
        _record_tool_schemas(trial_dir, schema_version=schema_version, get_order=None)

    with pytest.raises(MissingReplayInputError, match=f"generation-{schema_version} bundle"):
        read_replay_inputs(trial_dir, grading_override=_override(**_TRANSCRIPT_EVIDENCE))


def test_a_generation_7_bundle_replays_as_a_state_judge(tmp_path: Path) -> None:
    """A judge over the state reads no tool declaration, so a bundle predating them
    replays as it always did."""
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(read_tools_offered=["get_db_state", "query_db"]),
    )
    _record_tool_schemas(trial_dir, schema_version=7, get_order=None)

    inputs = read_replay_inputs(trial_dir, grading_override=_override(rubric=True))

    assert inputs.options.customization.evidence == JudgeEvidence()
    assert inputs.options.tool_effects == ToolEffects.empty()
    assert inputs.db_reader is not None
    assert inputs.provenance.customization_sources == {}


def test_a_transcript_override_inherits_the_recorded_tool_results_options(
    tmp_path: Path,
) -> None:
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(
            state_diff_text="orders[O-1]: refunded false -> true",
            read_tools_offered=["get_db_state", "query_db"],
        ),
        recorded_customization={"evidence": {"tool_results": {"max_chars": 500}}},
    )
    _record_tool_schemas(trial_dir, schema_version=8, get_order=_GET_ORDER_READS)

    inputs = read_replay_inputs(
        trial_dir,
        grading_override=_override(
            evidence={
                "state_diff": False,
                "db": False,
                "workspace": False,
                "state_changing_calls": True,
            }
        ),
    )

    assert inputs.options.customization.evidence.tool_results_options() == ToolResultsOptions(
        max_chars=500
    )
    assert inputs.options.tool_effects.can_change_state("get_order", '{"id": "O-1"}') is False
    assert inputs.db_reader is None and inputs.state_diff is None
    assert inputs.provenance.customization_sources == {
        "evidence.state_diff": ProvenanceSource.OVERRIDE,
        "evidence.db": ProvenanceSource.OVERRIDE,
        "evidence.workspace": ProvenanceSource.OVERRIDE,
        "evidence.state_changing_calls": ProvenanceSource.OVERRIDE,
        "evidence.tool_results.max_chars": ProvenanceSource.RECORDED,
    }
