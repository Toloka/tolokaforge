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

import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from tests.unit.grading.test_judge import ScriptedClient
from tests.utils.five_shape_run import write_five_shape_run
from tests.utils.provision_failure import write_provision_failure_bundle
from tolokaforge.core.failure_attribution import TrialOutcomeClass
from tolokaforge.core.grading.judge import JudgeStatus as JudgeRunStatus
from tolokaforge.core.grading.replay import (
    REPLAY_UNAVAILABLE,
    FidelityMode,
    GradingOverride,
    KnowledgeSearchMode,
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
    read_replay_inputs,
    replay_trial,
    run_replay_batch,
)
from tolokaforge.core.grading.replay_layout import discover_trial_bundles
from tolokaforge.core.grading.transcript_wire import (
    encode_transcript_wire,
    split_leading_system_message,
)
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    JudgeInputs,
    JudgeKbGating,
    JudgeStatus,
    Message,
    MessageRole,
    TerminationReason,
    ToolCall,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.output.artifacts import FileArtifactWriter

pytestmark = pytest.mark.unit

_AGENT_PROMPT = "You are the agent. Follow the refund policy exactly."
_RUBRIC = {
    "reference": "The refund must be issued for $328.50.",
    "criteria": [
        {"id": "refund_amount", "description": "Refund quotes $328.50", "kind": "binary"},
    ],
}
_JUDGE_MODEL = {"provider": "openrouter", "name": "openai/gpt-4.1-mini", "temperature": 0.0}
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


def _write_recorded_inputs(
    trial_dir: Path,
    trajectory: Trajectory,
    *,
    with_rubric: bool = True,
    recorded_system_prompt: str | None = None,
    recorded_include_agent_system_prompt: bool | None = None,
) -> None:
    """Write everything a bundle records except its grade, via the real writer."""
    writer = FileArtifactWriter()
    writer.write_trajectory(trial_dir, trajectory)
    writer.write_prompts(trial_dir, _AGENT_PROMPT, "user-sim prompt")

    if with_rubric:
        llm_judge: dict = {"rubric": _RUBRIC}
        customization: dict = {}
        if recorded_system_prompt is not None:
            customization["system_prompt"] = recorded_system_prompt
        if recorded_include_agent_system_prompt is not None:
            customization["include_agent_system_prompt"] = recorded_include_agent_system_prompt
        if customization:
            llm_judge["customization"] = customization
        grading_config = {"llm_judge": llm_judge}
    else:
        grading_config = {"llm_judge": None}
    writer.write_task(
        trial_dir,
        {
            "task_id": "refund_task",
            "trial_index": 0,
            "grading_config": grading_config,
            "model_config": {"judge": _JUDGE_MODEL},
        },
    )


def _write_bundle(
    trial_dir: Path,
    *,
    judge_status: JudgeStatus,
    judge_inputs: JudgeInputs | None,
    with_rubric: bool = True,
    kb_gating: JudgeKbGating | None = None,
    recorded_system_prompt: str | None = None,
    recorded_include_agent_system_prompt: bool | None = None,
) -> Trajectory:
    """Write a new-shape bundle via the real writer; return the source trajectory."""
    trajectory = _trajectory()
    _write_recorded_inputs(
        trial_dir,
        trajectory,
        with_rubric=with_rubric,
        recorded_system_prompt=recorded_system_prompt,
        recorded_include_agent_system_prompt=recorded_include_agent_system_prompt,
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


def _submit_report_client() -> ScriptedClient:
    return ScriptedClient(
        [
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
    )


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


def test_grade_less_bundles_are_skips_that_say_which_grade_less_shape_they_are(
    tmp_path: Path,
) -> None:
    """A bundle carrying no ``grade.yaml`` is a declared skip, not a failure, and
    its reason comes from its own recorded trajectory: the trial grading refused
    carries the recorded ``grading_error``, the aborted one carries its termination
    reason, and one in neither shape — a completed trial the run simply never
    graded — is named as the anomaly it is. All three sentences DIFFER — one
    hardcoded "(infrastructure abort)" for the lot would mislabel most of the
    population. Classification precedes input reconstruction, so the judge is
    never reached and nothing is written."""
    source = tmp_path / "run"
    ungradeable = source / "trials" / "ungradeable" / "0"
    aborted = source / "trials" / "aborted" / "0"
    anomalous = source / "trials" / "anomalous" / "0"
    _grade_less_bundle(ungradeable, grading_error=_GRADING_REFUSAL)
    _aborted_bundle(aborted)
    _grade_less_bundle(anomalous)
    client = _submit_report_client()

    assert classify_trial(ungradeable) is TrialEligibility.NO_GRADE
    outcomes = [
        run_replay_batch(source, replay_id="r1", trial=bundle, judge_client=client)[0]
        for bundle in (ungradeable, aborted, anomalous)
    ]

    assert [o.status for o in outcomes] == [ReplayOutcomeStatus.SKIPPED_NO_GRADE] * 3
    ungradeable_reason, aborted_reason, anomalous_reason = (o.reason or "" for o in outcomes)
    assert _GRADING_REFUSAL in ungradeable_reason
    assert _GRADING_REFUSAL not in aborted_reason
    assert TerminationReason.API_TIMEOUT.value in aborted_reason
    assert "neither grade-less shape" in anomalous_reason
    assert TrialOutcomeClass.MEASURED.value in anomalous_reason
    assert len({ungradeable_reason, aborted_reason, anomalous_reason}) == 3
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
            read_tools_offered=["get_db_state", "query_db", "read_file", "fetch_ticket"],
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
    # including read_file and the recorded name outside the known set
    # (fetch_ticket), which enter replay via generic non-KB shims — so a replay
    # bundle is itself re-replayable without losing tools.
    assert result.state_diff == "orders[O-1]: refunded false -> true"
    assert result.read_tools_offered == ("get_db_state", "query_db", "read_file", "fetch_ticket")
    assert result.kb_tools_offered == ()


def test_knowledge_search_override_forces_gating(tmp_path: Path) -> None:
    """A bundle whose recorded run WITHHELD the KB (disabled gating) must still
    yield a KB shim: ``on`` actually offers ``search_kb`` to the judge, and
    ``recorded`` reproduces the recorded withheld surface — the tool surface, not
    just the boolean flags."""
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(read_tools_offered=[]),
        kb_gating=JudgeKbGating(knowledge_search_disabled=True, offered=[], withheld=["search_kb"]),
    )

    off = read_replay_inputs(trial_dir, knowledge_search=KnowledgeSearchMode.OFF)
    on = read_replay_inputs(trial_dir, knowledge_search=KnowledgeSearchMode.ON)
    recorded = read_replay_inputs(trial_dir, knowledge_search=KnowledgeSearchMode.RECORDED)

    assert off.disable_knowledge_search is True
    assert on.disable_knowledge_search is False
    assert recorded.disable_knowledge_search is True
    assert off.provenance.knowledge_search_mode is KnowledgeSearchMode.OFF

    # Forced `on` OFFERS the offline KB shim in the judge's tool surface.
    on_result = replay_trial(on, judge_client=_submit_report_client())
    assert on_result.kb_tools_offered == ("search_kb",)
    assert on_result.kb_tools_withheld == ()

    # `recorded` re-withholds it, matching the original audit record.
    recorded_result = replay_trial(recorded, judge_client=_submit_report_client())
    assert recorded_result.kb_tools_offered == ()
    assert recorded_result.kb_tools_withheld == ("search_kb",)


def _grading_override(
    system_prompt: str | None = None, include_agent_system_prompt: bool | None = None
) -> GradingOverride:
    from tolokaforge.runner.models import Rubric

    return GradingOverride(
        rubric=Rubric.model_validate(_RUBRIC),
        custom_system_prompt=system_prompt,
        include_agent_system_prompt=include_agent_system_prompt,
    )


def test_recorded_custom_prompt_is_reconstructed_and_stamped(tmp_path: Path) -> None:
    """A bundle whose task.yaml records a custom system prompt reconstructs it, the
    judge is constructed with it, and provenance stamps RECORDED."""
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(read_tools_offered=[]),
        recorded_system_prompt="Grade strictly against the refund policy handbook.",
    )

    inputs = read_replay_inputs(trial_dir)

    assert inputs.custom_system_prompt == "Grade strictly against the refund policy handbook."
    assert inputs.provenance.custom_system_prompt is True
    assert inputs.provenance.custom_prompt_source is ProvenanceSource.RECORDED

    # The one production judge is actually constructed with the custom prompt.
    result = replay_trial(inputs, judge_client=_submit_report_client())
    assert result.custom_system_prompt is True


def test_grading_override_custom_prompt_wins_over_recorded(tmp_path: Path) -> None:
    """A --grading override carrying its own customization.system_prompt replaces
    the recorded prompt; provenance stamps OVERRIDE."""
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(read_tools_offered=[]),
        recorded_system_prompt="Recorded judge voice.",
    )

    inputs = read_replay_inputs(
        trial_dir, grading_override=_grading_override("Override judge voice.")
    )

    assert inputs.custom_system_prompt == "Override judge voice."
    assert inputs.provenance.custom_system_prompt is True
    assert inputs.provenance.custom_prompt_source is ProvenanceSource.OVERRIDE


def test_no_recorded_customization_yields_no_custom_prompt(tmp_path: Path) -> None:
    """An old-shape bundle with no recorded customization ⇒ default prompt: None,
    provenance False, source None (the declared fallback)."""
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(read_tools_offered=[]),
    )

    inputs = read_replay_inputs(trial_dir)

    assert inputs.custom_system_prompt is None
    assert inputs.provenance.custom_system_prompt is False
    assert inputs.provenance.custom_prompt_source is None


def test_rubric_only_override_preserves_recorded_custom_prompt(tmp_path: Path) -> None:
    """A --grading override supplying a rubric but NO customization.system_prompt,
    over a bundle that DOES record a custom prompt.
    The recorded prompt survives (source RECORDED) while the rubric is OVERRIDE —
    rubric and prompt resolution are independent; a rubric-only override never
    clears the recorded prompt."""
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(read_tools_offered=[]),
        recorded_system_prompt="Recorded judge voice.",
    )

    inputs = read_replay_inputs(trial_dir, grading_override=_grading_override(None))

    assert inputs.custom_system_prompt == "Recorded judge voice."
    assert inputs.provenance.custom_prompt_source is ProvenanceSource.RECORDED
    assert inputs.provenance.rubric_source is ProvenanceSource.OVERRIDE


@pytest.mark.parametrize("bad_value", ["", "   ", 7], ids=["empty", "whitespace", "non-string"])
def test_blank_or_non_string_recorded_custom_prompt_fails_loud(
    tmp_path: Path, bad_value: object
) -> None:
    """A recorded ``customization.system_prompt`` that is blank or not a string is a
    corrupted or hand-edited bundle, not a default prompt — replay refuses it with a
    named error instead of silently judging with an unintended prompt.

    Exercised under a rubric-only ``--grading`` override: that path skips the
    ``LLMJudgeConfig`` validation of the recorded block (which already rejects a
    blank prompt when no override is supplied), so the prompt resolver is the only
    guard left."""
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(read_tools_offered=[]),
        recorded_system_prompt="Recorded judge voice.",
    )
    task = yaml.safe_load((trial_dir / "task.yaml").read_text())
    task["grading_config"]["llm_judge"]["customization"]["system_prompt"] = bad_value
    (trial_dir / "task.yaml").write_text(yaml.safe_dump(task))

    with pytest.raises(MissingReplayInputError, match="blank or not a string"):
        read_replay_inputs(trial_dir, grading_override=_grading_override(None))


def test_provenance_rejects_incoherent_custom_prompt_stamp() -> None:
    """``ReplayProvenance`` machine-enforces its invariant: ``custom_prompt_source``
    is set exactly when ``custom_system_prompt`` is True."""
    with pytest.raises(ValidationError, match="custom_prompt_source"):
        ReplayProvenance(
            judge_model="openrouter/openai/gpt-4.1-mini",
            judge_model_source=ProvenanceSource.RECORDED,
            rubric_source=ProvenanceSource.RECORDED,
            knowledge_search_mode=KnowledgeSearchMode.RECORDED,
            knowledge_search_disabled=False,
            custom_system_prompt=False,
            custom_prompt_source=ProvenanceSource.RECORDED,
            include_agent_system_prompt=True,
            agent_prompt_source=None,
            fidelity_mode=FidelityMode.FULL,
        )


def test_recorded_agent_prompt_gating_is_reconstructed_and_stamped(tmp_path: Path) -> None:
    """A bundle whose task.yaml records ``include_agent_system_prompt: false``
    reconstructs the gating, the judge is constructed with it, and provenance stamps
    RECORDED."""
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(read_tools_offered=[]),
        recorded_include_agent_system_prompt=False,
    )

    inputs = read_replay_inputs(trial_dir)

    assert inputs.include_agent_system_prompt is False
    assert inputs.provenance.include_agent_system_prompt is False
    assert inputs.provenance.agent_prompt_source is ProvenanceSource.RECORDED

    result = replay_trial(inputs, judge_client=_submit_report_client())
    assert result.include_agent_system_prompt is False


def test_grading_override_agent_prompt_gating_wins_over_recorded(tmp_path: Path) -> None:
    """A --grading override carrying an explicit include_agent_system_prompt replaces
    the recorded value; provenance stamps OVERRIDE."""
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(read_tools_offered=[]),
        recorded_include_agent_system_prompt=False,
    )

    inputs = read_replay_inputs(
        trial_dir, grading_override=_grading_override(include_agent_system_prompt=True)
    )

    assert inputs.include_agent_system_prompt is True
    assert inputs.provenance.include_agent_system_prompt is True
    assert inputs.provenance.agent_prompt_source is ProvenanceSource.OVERRIDE


def test_no_recorded_agent_prompt_gating_yields_include_default(tmp_path: Path) -> None:
    """A bundle with no recorded gating ⇒ include default: True, source None (old
    bundles graded with the policy present)."""
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(read_tools_offered=[]),
    )

    inputs = read_replay_inputs(trial_dir)

    assert inputs.include_agent_system_prompt is True
    assert inputs.provenance.include_agent_system_prompt is True
    assert inputs.provenance.agent_prompt_source is None


def test_rubric_only_override_preserves_recorded_agent_prompt_gating(tmp_path: Path) -> None:
    """A --grading override supplying a rubric but NO include_agent_system_prompt,
    over a bundle that DOES record ``false``: the recorded gating survives (source
    RECORDED) while the rubric is OVERRIDE — gating and rubric resolve
    independently; a rubric-only override never flips a recorded gating."""
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(read_tools_offered=[]),
        recorded_include_agent_system_prompt=False,
    )

    inputs = read_replay_inputs(trial_dir, grading_override=_grading_override(None))

    assert inputs.include_agent_system_prompt is False
    assert inputs.provenance.agent_prompt_source is ProvenanceSource.RECORDED
    assert inputs.provenance.rubric_source is ProvenanceSource.OVERRIDE


def test_non_bool_recorded_agent_prompt_gating_fails_loud(tmp_path: Path) -> None:
    """A recorded ``customization.include_agent_system_prompt`` that is not a bool is
    a corrupted or hand-edited bundle — replay refuses it with a named error rather
    than silently defaulting. Exercised under a rubric-only ``--grading`` override so
    the resolver is the only guard (that path skips ``LLMJudgeConfig`` validation of
    the recorded block)."""
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(read_tools_offered=[]),
        recorded_include_agent_system_prompt=False,
    )
    task = yaml.safe_load((trial_dir / "task.yaml").read_text())
    task["grading_config"]["llm_judge"]["customization"][
        "include_agent_system_prompt"
    ] = "sometimes"
    (trial_dir / "task.yaml").write_text(yaml.safe_dump(task))

    with pytest.raises(MissingReplayInputError, match="include_agent_system_prompt"):
        read_replay_inputs(trial_dir, grading_override=_grading_override(None))


def test_provenance_rejects_incoherent_agent_prompt_stamp() -> None:
    """``ReplayProvenance`` machine-enforces the one-way implication: a defaulted
    stamp (``agent_prompt_source`` None) can only carry the include default, so
    ``include_agent_system_prompt=False`` with no source is rejected."""
    with pytest.raises(ValidationError, match="include_agent_system_prompt"):
        ReplayProvenance(
            judge_model="openrouter/openai/gpt-4.1-mini",
            judge_model_source=ProvenanceSource.RECORDED,
            rubric_source=ProvenanceSource.RECORDED,
            knowledge_search_mode=KnowledgeSearchMode.RECORDED,
            knowledge_search_disabled=False,
            custom_system_prompt=False,
            custom_prompt_source=None,
            include_agent_system_prompt=False,
            agent_prompt_source=None,
            fidelity_mode=FidelityMode.FULL,
        )


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
        grading_override=GradingOverride(
            rubric=Rubric.model_validate(_RUBRIC),
            custom_system_prompt=None,
            include_agent_system_prompt=None,
        ),
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


def test_batch_records_invalid_recorded_input_as_named_failure_and_continues(
    tmp_path: Path,
) -> None:
    """A recorded input that fails validation (here a corrupt ``trajectory.yaml``)
    is a named per-trial FAILED — it must never abort the batch after other trials
    have already spent judge tokens."""
    source = tmp_path / "run"
    _completed_bundle(source / "trials" / "good" / "0")
    corrupt = source / "trials" / "corrupt" / "0"
    _completed_bundle(corrupt)
    (corrupt / "trajectory.yaml").write_text(yaml.dump({"task_id": "corrupt"}))

    outcomes = run_replay_batch(source, replay_id="r1", dry_run=True)

    by_name = {o.bundle.parent.name: o for o in outcomes}
    assert by_name["good"].status is ReplayOutcomeStatus.WOULD_REPLAY
    failed = by_name["corrupt"]
    assert failed.status is ReplayOutcomeStatus.FAILED
    assert failed.reason is not None and "Trajectory" in failed.reason


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
