"""Offline judge replay engine — reconstructing ``LLMJudge.run()`` inputs.

Locks the Stage-2 contract of :mod:`tolokaforge.core.grading.replay`:

* the reconstructed transcript is **byte-identical** to the live
  ``_build_judge_messages_json`` + strip path the runner uses;
* a judge-eligible trial with no rubric and no override fails loud with a named
  error, while a not-applicable trial is a declared skip even under a rubric
  override (a rubric override never conjures a judge stage onto a trial that
  never had one);
* the offline read shims return the explicit "unavailable in replay" marker,
  never a silent empty result;
* ``replay_trial`` drives the one production ``LLMJudge`` with an injected client
  and never touches the network.

The bundle is written by the real :class:`FileArtifactWriter` (no hand-rolled
YAML) so the tests exercise the same on-disk contract Stage 1 produces.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner

from tests.unit.grading.test_judge import ScriptedClient
from tolokaforge.core.grading.judge import JudgeStatus as JudgeRunStatus
from tolokaforge.core.grading.replay import (
    REPLAY_UNAVAILABLE,
    FidelityMode,
    KnowledgeSearchMode,
    MissingReplayInputError,
    OfflineDBReader,
    OfflineKnowledgeSearch,
    ProvenanceSource,
    ReplayOutcomeStatus,
    TrialEligibility,
    classify_trial,
    discover_trial_bundles,
    read_replay_inputs,
    replay_trial,
    run_replay_batch,
)
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    JudgeInputs,
    JudgeStatus,
    Message,
    MessageRole,
    ToolCall,
    Trajectory,
)
from tolokaforge.core.output.artifacts import FileArtifactWriter
from tolokaforge.core.trial_grader import (
    _build_judge_messages_json,
    split_leading_system_message,
)

pytestmark = pytest.mark.unit

_AGENT_PROMPT = "You are the agent. Follow the refund policy exactly."
_RUBRIC = {
    "reference": "The refund must be issued for $328.50.",
    "criteria": [
        {"id": "refund_amount", "description": "Refund quotes $328.50", "kind": "binary"},
    ],
}
_JUDGE_MODEL = {"provider": "openrouter", "name": "openai/gpt-4.1-mini", "temperature": 0.0}


def _trajectory() -> Trajectory:
    now = datetime.now(UTC)
    return Trajectory(
        task_id="refund_task",
        trial_index=0,
        start_ts=now,
        end_ts=now,
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


def _write_bundle(
    trial_dir: Path,
    *,
    judge_status: JudgeStatus,
    judge_inputs: JudgeInputs | None,
    with_rubric: bool = True,
) -> Trajectory:
    """Write a new-shape bundle via the real writer; return the source trajectory."""
    writer = FileArtifactWriter()
    trajectory = _trajectory()
    writer.write_trajectory(trial_dir, trajectory)
    writer.write_prompts(trial_dir, _AGENT_PROMPT, "user-sim prompt")

    grading_config = {"llm_judge": {"rubric": _RUBRIC}} if with_rubric else {"llm_judge": None}
    writer.write_task(
        trial_dir,
        {
            "task_id": "refund_task",
            "trial_index": 0,
            "grading_config": grading_config,
            "model_config": {"judge": _JUDGE_MODEL},
        },
    )
    writer.write_grade(
        trial_dir,
        Grade(
            binary_pass=True,
            score=1.0,
            components=GradeComponents(llm_judge=1.0),
            judge_status=judge_status,
            judge_inputs=judge_inputs,
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
    wire = _build_judge_messages_json(trajectory, _AGENT_PROMPT)
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
    assert inputs.provenance is not None
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


def test_not_applicable_trial_is_skip_even_with_grading_override(tmp_path: Path) -> None:
    trial_dir = tmp_path / "trials" / "state_only" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.UNSPECIFIED,
        judge_inputs=None,
        with_rubric=False,
    )

    # A trial that never had a judge is not-applicable — the classification is
    # independent of any rubric override, so the CLI skips it rather than
    # spending tokens judging a state/transcript-only trial.
    from tolokaforge.runner.models import Rubric

    override = Rubric.model_validate(_RUBRIC)
    assert classify_trial(trial_dir) is TrialEligibility.NOT_APPLICABLE
    # (The override would otherwise satisfy reconstruction; classification, not
    # input availability, is what gates a not-applicable trial.)
    assert override.criteria[0].id == "refund_amount"


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
            read_tools_offered=["get_db_state", "query_db"],
        ),
    )
    inputs = read_replay_inputs(trial_dir)
    client = _submit_report_client()

    result = replay_trial(inputs, judge_client=client)

    assert result.status is JudgeRunStatus.COMPLETED
    assert result.score == 1.0
    # The one production judge was driven by the injected client — no network.
    assert client.calls == 1
    # And it echoes the reconstructed state_diff + offline read surface (Stage 1
    # contract), so a replay bundle is itself replayable.
    assert result.state_diff == "orders[O-1]: refunded false -> true"
    assert result.read_tools_offered == ("get_db_state", "query_db")


def test_knowledge_search_override_forces_gating(tmp_path: Path) -> None:
    trial_dir = tmp_path / "trials" / "refund_task" / "0"
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(read_tools_offered=[]),
    )

    off = read_replay_inputs(trial_dir, knowledge_search=KnowledgeSearchMode.OFF)
    on = read_replay_inputs(trial_dir, knowledge_search=KnowledgeSearchMode.ON)

    assert off.disable_knowledge_search is True
    assert on.disable_knowledge_search is False
    assert off.provenance is not None and off.provenance.knowledge_search_mode is (
        KnowledgeSearchMode.OFF
    )


def _completed_bundle(trial_dir: Path) -> None:
    _write_bundle(
        trial_dir,
        judge_status=JudgeStatus.COMPLETED,
        judge_inputs=JudgeInputs(read_tools_offered=["get_db_state", "query_db"]),
    )


def test_discovery_finds_bundles_across_all_three_layouts(tmp_path: Path) -> None:
    # (a) a run dir with a trials/<task>/<idx> subtree.
    nested = tmp_path / "nested"
    _completed_bundle(nested / "trials" / "refund_task" / "0")
    _completed_bundle(nested / "trials" / "AE-BDG-003_billing" / "1")
    assert discover_trial_bundles(nested) == sorted(
        [nested / "trials" / "refund_task" / "0", nested / "trials" / "AE-BDG-003_billing" / "1"]
    )

    # (b) a flat collection of bundle dirs (the tarball shape, no trials/ parent).
    flat = tmp_path / "flat"
    _completed_bundle(flat / "AE-BDG-002_1")
    _completed_bundle(flat / "AE-BDG-003_0")
    assert discover_trial_bundles(flat) == sorted([flat / "AE-BDG-002_1", flat / "AE-BDG-003_0"])

    # (c) a single bundle dir.
    single = tmp_path / "single"
    _completed_bundle(single)
    assert discover_trial_bundles(single) == [single]


def test_discovery_excludes_the_replays_output_subtree(tmp_path: Path) -> None:
    source = tmp_path / "run"
    _completed_bundle(source / "trials" / "refund_task" / "0")

    run_replay_batch(source, replay_id="r1", judge_client=_submit_report_client())

    # A second discovery must not pick up the replay bundle written under replays/.
    assert discover_trial_bundles(source) == [source / "trials" / "refund_task" / "0"]


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
        rubric_override=Rubric.model_validate(_RUBRIC),
        dry_run=True,
    )

    by_name = {o.bundle.parent.name: o.status for o in outcomes}
    assert by_name["state_only"] is ReplayOutcomeStatus.SKIPPED_NOT_APPLICABLE
    assert by_name["judged"] is ReplayOutcomeStatus.WOULD_REPLAY


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
    from tolokaforge.cli.main import cli

    source = tmp_path / "run"
    _completed_bundle(source / "trials" / "refund_task" / "0")

    result = CliRunner().invoke(cli, ["rejudge", "--source", str(source), "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "1 eligible" in result.output
    # Dry-run spends nothing and writes nothing.
    assert not (source / "replays").exists()
