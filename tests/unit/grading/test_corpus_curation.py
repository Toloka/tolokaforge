"""``tolokaforge curate`` — a run directory becomes a corpus, by a rule that runs.

Every claim is driven through the real command rather than the classifier behind it, so
the exit code, the written corpus and the manifest are what is asserted.

The admission rule, one test per row:

1. **A trial whose every recorded call failed is rejected `environment_dead`** — and
   the observation behind the rejection (how many calls, how many succeeded) is written
   into the manifest, so the claim survives the gitignored run directory it was read from.
2. **A record-less trial is admitted and marked unclassifiable** — ``status`` comes from
   ``tool_log.yaml`` alone, so a bundle carrying none is not evidence of a dead
   environment; ``record_carried: false`` is what the manifest says instead.
3. **One succeeding call is enough** — the rule asks whether the environment ever worked,
   not whether every call worked.
4. **Every disposition is named**, on the console and in the manifest, whichever way it
   went: no judge, no verdict for the criterion, the author's own exclusion.
5. **A run that admits nothing writes nothing and exits non-zero** — an empty corpus
   directory would read as a corpus.
6. **A corpus is never a half-refresh** — a destination already holding a ``corpus.yaml``
   is refused without ``--replace``, and ``--replace`` rewrites the whole directory.
7. **An exclusion the rule already covers is refused** — the manifest records one reason
   per trial, and the author's would overwrite the measurement.
8. **The manifest names the models the trial recorded** — the agent model is the one a
   trial cannot lack, and a snapshot recording no judge is admitted with a null judge
   model rather than refused.

The console is the shared stderr one, so ``CliRunner`` captures it in ``result.output``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner, Result

from tests.utils.recorded_calls import recorded_call
from tolokaforge.core.grading.corpus_curation import (
    CORPUS_MANIFEST_FILENAME,
    CorpusManifest,
)
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import (
    CriterionResult,
    Grade,
    GradeComponents,
    JudgeStatus,
    Message,
    MessageRole,
    Metrics,
    RecordedToolCall,
    ToolCall,
    ToolExecutionStatus,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.output.artifacts import FileArtifactWriter
from tolokaforge.dx.cli.main import cli

pytestmark = [pytest.mark.unit, pytest.mark.grading]

_CRITERION = "checked_duplicates_first"
_TASK_ID = "notes_add_note_duplicate_check"
_AGENT_MODEL = "openai/gpt-4o-mini"
_JUDGE_MODEL = "anthropic/claude-sonnet-4-6"
_WIRE_TOOLS = [{"type": "function", "function": {"name": "list_notes", "parameters": {}}}]


def _trajectory(calls: list[RecordedToolCall]) -> Trajectory:
    now = datetime.now(timezone.utc)
    return Trajectory(
        task_id=_TASK_ID,
        trial_index=0,
        start_ts=now,
        end_ts=now,
        status=TrialStatus.COMPLETED,
        messages=[
            Message(role=MessageRole.USER, content="Add a note."),
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[
                    ToolCall(id=call.call_id, name=call.tool_name, arguments=call.arguments)
                    for call in calls
                ],
            ),
            *[
                Message(role=MessageRole.TOOL, content=call.output, tool_call_id=call.call_id)
                for call in calls
            ],
            Message(role=MessageRole.ASSISTANT, content="Saved."),
        ],
        tool_log=calls,
        metrics=Metrics(),
    )


def _write_bundle(
    trial_dir: Path,
    *,
    calls: list[RecordedToolCall] | None = None,
    judge_status: JudgeStatus = JudgeStatus.COMPLETED,
    criteria: tuple[str, ...] = (_CRITERION,),
    met: bool = True,
    models: dict[str, str] | None = None,
) -> Path:
    """One graded bundle on disk, written by the real artifact writer.

    ``calls=None`` writes no ``tool_log.yaml`` at all — the shape a bundle recorded
    before the record was persisted has, and the one the environment-dead rule has no
    evidence to classify. ``models`` is the recorded ``model_config``, whose ``judge``
    the harness omits for a run configuring no run-level judge.
    """
    trajectory = _trajectory(calls or [])
    writer = FileArtifactWriter()
    writer.write_trajectory(trial_dir, trajectory)
    writer.write_metrics(trial_dir, trajectory)
    writer.write_tools_schemas(trial_dir, _WIRE_TOOLS)
    writer.write_env(trial_dir, {"json_db": {}})
    writer.write_logs(trial_dir, StructuredLogger(f"{_TASK_ID}-{trial_dir.name}"))
    if calls is not None:
        writer.write_tool_log(trial_dir, trajectory)
    writer.write_task(
        trial_dir,
        {
            "task_id": _TASK_ID,
            "trial_index": 0,
            "grading_config": {"llm_judge": {"rubric": {"criteria": []}}},
            "model_config": {
                role: {"name": name}
                for role, name in (models or {"agent": _AGENT_MODEL, "judge": _JUDGE_MODEL}).items()
            },
        },
    )
    writer.write_grade(
        trial_dir,
        Grade(
            binary_pass=met,
            score=1.0 if met else 0.0,
            components=GradeComponents(llm_judge=1.0 if met else 0.0),
            judge_status=judge_status,
            criterion_results=[
                CriterionResult(id=name, met=met, score=1.0 if met else 0.0, justification="…")
                for name in criteria
            ],
        ),
    )
    return trial_dir


def _run_dir(root: Path, run_id: str) -> Path:
    return root / run_id / "trials" / _TASK_ID


def _failed_calls(count: int) -> list[RecordedToolCall]:
    return [
        recorded_call("list_notes", sequence=index, status=ToolExecutionStatus.ERROR, output="boom")
        for index in range(count)
    ]


def _curate(*args: str) -> Result:
    """Invoke the real command, with a console wide enough to keep paths intact."""
    return CliRunner().invoke(cli, ["curate", *args], env={"COLUMNS": "200"})


def _manifest(corpus: Path) -> CorpusManifest:
    return CorpusManifest.model_validate(
        yaml.safe_load((corpus / CORPUS_MANIFEST_FILENAME).read_text())
    )


def _written_files(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir())


def _rejection(manifest: CorpusManifest, reason: str) -> Any:
    matching = [entry for entry in manifest.excluded if entry.reason.value == reason]
    assert len(matching) == 1, f"{reason} appears {len(matching)} times in {manifest.excluded}"
    return matching[0]


def test_a_trial_whose_every_recorded_call_failed_is_rejected_as_environment_dead(
    tmp_path: Path,
) -> None:
    """Six calls, none successful: the judge graded a transcript of failures.

    The rejection carries the measurement behind it, because the run directory it was
    read from is gitignored and a reason without an observation cannot be re-checked.
    """
    run = _run_dir(tmp_path / "results", "policy_demo_20260803_062316")
    _write_bundle(run / "0", calls=_failed_calls(6))
    _write_bundle(run / "1", calls=[recorded_call("list_notes")])
    corpus = tmp_path / "corpus"

    result = _curate(
        "--source", str(tmp_path / "results"), "--into", str(corpus), "--criterion", _CRITERION
    )

    assert result.exit_code == 0, result.output
    manifest = _manifest(corpus)
    assert [entry.directory for entry in manifest.bundles] == ["policy_demo_20260803_062316_trial1"]
    dead = _rejection(manifest, "environment_dead")
    assert dead.observation == "tool_calls: 6, succeeded: 0"
    assert dead.by.value == "rule"
    assert dead.source.endswith("policy_demo_20260803_062316/trials/" + _TASK_ID + "/0")
    assert not (corpus / "policy_demo_20260803_062316_trial0").exists()
    assert "environment_dead" in result.output


def test_a_record_less_trial_is_admitted_and_marked_unclassifiable(tmp_path: Path) -> None:
    """``status`` comes from the record alone, so its absence is not evidence of death."""
    run = _run_dir(tmp_path / "results", "example_20260629_133126")
    _write_bundle(run / "0", calls=None)
    corpus = tmp_path / "corpus"

    result = _curate(
        "--source", str(tmp_path / "results"), "--into", str(corpus), "--criterion", _CRITERION
    )

    assert result.exit_code == 0, result.output
    (admitted,) = _manifest(corpus).bundles
    assert admitted.directory == "example_20260629_133126_trial0"
    assert admitted.record_carried is False
    assert "tool_log.yaml" not in _written_files(corpus / admitted.directory)


def test_a_trial_recording_no_judge_is_admitted_and_the_manifest_says_so(tmp_path: Path) -> None:
    """The harness writes no ``model_config.judge`` for a run configuring no run-level judge.

    Two of the seventeen bundles in the committed notes corpus are that shape — old runs
    whose judge came from the pack's ``grading_config`` — so refusing them would make the
    shipped corpus uncurateable by the command that defines it. The agent model is the
    one a trial cannot lack: it produced the transcript.
    """
    run = _run_dir(tmp_path / "results", "gate_demo_20260625_184817")
    _write_bundle(run / "0", calls=None, models={"agent": _AGENT_MODEL})
    corpus = tmp_path / "corpus"

    result = _curate(
        "--source", str(tmp_path / "results"), "--into", str(corpus), "--criterion", _CRITERION
    )

    assert result.exit_code == 0, result.output
    (admitted,) = _manifest(corpus).bundles
    assert (admitted.agent_model, admitted.judge_model) == (_AGENT_MODEL, None)


def test_a_trial_recording_no_agent_model_is_refused(tmp_path: Path) -> None:
    """A corpus that cannot say which model produced a transcript is not a corpus."""
    run = _run_dir(tmp_path / "results", "gate_demo_20260625_184817")
    _write_bundle(run / "0", calls=None, models={"judge": _JUDGE_MODEL})
    corpus = tmp_path / "corpus"

    result = _curate(
        "--source", str(tmp_path / "results"), "--into", str(corpus), "--criterion", _CRITERION
    )

    assert result.exit_code == 1, result.output
    assert "names no model_config.agent.name" in result.output
    assert not corpus.exists()


def test_a_trial_that_reached_one_success_is_admitted_with_its_record(tmp_path: Path) -> None:
    """The rule asks whether the environment ever worked, not whether every call worked."""
    run = _run_dir(tmp_path / "results", "policy_demo_20260803_063010")
    _write_bundle(
        run / "0",
        calls=[
            recorded_call("list_notes", sequence=0, status=ToolExecutionStatus.ERROR),
            recorded_call("list_notes", sequence=1, status=ToolExecutionStatus.SUCCESS),
        ],
    )
    corpus = tmp_path / "corpus"

    result = _curate(
        "--source", str(tmp_path / "results"), "--into", str(corpus), "--criterion", _CRITERION
    )

    assert result.exit_code == 0, result.output
    (admitted,) = _manifest(corpus).bundles
    assert admitted.record_carried is True
    assert "tool_log.yaml" in _written_files(corpus / admitted.directory)


def test_every_disposition_reaches_the_manifest_and_the_console(tmp_path: Path) -> None:
    """One invocation over five trials, one per way in and three ways out.

    The admitted entries carry the labels the corpus exists for — the judge's `met`, the
    models that produced the trial — and each rejection names its reason, its authority
    and what was observed.
    """
    run = _run_dir(tmp_path / "results", "policy_demo_20260803_063150")
    _write_bundle(run / "0", calls=[recorded_call("list_notes")], met=True)
    _write_bundle(run / "1", calls=None, met=False)
    _write_bundle(run / "2", calls=_failed_calls(4))
    _write_bundle(run / "3", calls=None, judge_status=JudgeStatus.ERRORED)
    _write_bundle(run / "4", calls=None, criteria=("clarity",))
    _write_bundle(run / "5", calls=None)
    corpus = tmp_path / "corpus"

    result = _curate(
        "--source",
        str(tmp_path / "results"),
        "--into",
        str(corpus),
        "--criterion",
        _CRITERION,
        "--exclude",
        f"{run / '5'}=recorded against a stale seed",
    )

    assert result.exit_code == 0, result.output
    manifest = _manifest(corpus)
    assert manifest.criterion == _CRITERION
    assert manifest.task_ids == [_TASK_ID]
    assert manifest.curated_from == ["policy_demo_20260803_063150"]
    assert [(entry.directory, entry.met) for entry in manifest.bundles] == [
        ("policy_demo_20260803_063150_trial0", True),
        ("policy_demo_20260803_063150_trial1", False),
    ]
    assert {entry.agent_model for entry in manifest.bundles} == {_AGENT_MODEL}
    assert {entry.judge_model for entry in manifest.bundles} == {_JUDGE_MODEL}

    assert _rejection(manifest, "environment_dead").observation == "tool_calls: 4, succeeded: 0"
    assert _rejection(manifest, "judge_incomplete").observation == "judge_status: 'errored'"
    assert _rejection(manifest, "criterion_not_judged").observation == "criteria judged: clarity"
    author = _rejection(manifest, "author_excluded")
    assert (author.by.value, author.observation) == ("author", "recorded against a stale seed")
    for reason in ("environment_dead", "judge_incomplete", "criterion_not_judged", "author"):
        assert reason in result.output


def test_a_run_that_admits_nothing_writes_nothing_and_exits_non_zero(tmp_path: Path) -> None:
    """An empty corpus directory would read as a corpus of no counter-evidence."""
    run = _run_dir(tmp_path / "results", "policy_demo_20260803_062316")
    _write_bundle(run / "0", calls=_failed_calls(6))
    corpus = tmp_path / "corpus"

    result = _curate(
        "--source", str(tmp_path / "results"), "--into", str(corpus), "--criterion", _CRITERION
    )

    assert result.exit_code == 1
    assert not corpus.exists()
    assert "environment_dead" in result.output
    assert str(tmp_path / "results") in result.output


def test_a_dry_run_classifies_every_trial_and_writes_nothing(tmp_path: Path) -> None:
    run = _run_dir(tmp_path / "results", "policy_demo_20260803_063010")
    _write_bundle(run / "0", calls=[recorded_call("list_notes")])
    corpus = tmp_path / "corpus"

    result = _curate(
        "--source",
        str(tmp_path / "results"),
        "--into",
        str(corpus),
        "--criterion",
        _CRITERION,
        "--dry-run",
    )

    assert result.exit_code == 0, result.output
    assert not corpus.exists()
    assert "would admit" in result.output


class TestTheDestination:
    def test_a_corpus_is_never_a_half_refresh(self, tmp_path: Path) -> None:
        run = _run_dir(tmp_path / "results", "policy_demo_20260803_063010")
        _write_bundle(run / "0", calls=[recorded_call("list_notes")])
        corpus = tmp_path / "corpus"
        first = _curate(
            "--source", str(tmp_path / "results"), "--into", str(corpus), "--criterion", _CRITERION
        )
        assert first.exit_code == 0, first.output

        again = _curate(
            "--source", str(tmp_path / "results"), "--into", str(corpus), "--criterion", _CRITERION
        )

        assert again.exit_code == 1
        assert "--replace" in again.output

    def test_replace_rewrites_the_whole_directory(self, tmp_path: Path) -> None:
        """A bundle the second curation does not admit is gone, not left behind."""
        run = _run_dir(tmp_path / "results", "policy_demo_20260803_063010")
        _write_bundle(run / "0", calls=[recorded_call("list_notes")])
        _write_bundle(run / "1", calls=[recorded_call("list_notes")])
        corpus = tmp_path / "corpus"
        _curate(
            "--source", str(tmp_path / "results"), "--into", str(corpus), "--criterion", _CRITERION
        )
        assert (corpus / "policy_demo_20260803_063010_trial1").exists()

        result = _curate(
            "--source",
            str(run / "0"),
            "--into",
            str(corpus),
            "--criterion",
            _CRITERION,
            "--replace",
        )

        assert result.exit_code == 0, result.output
        assert [entry.directory for entry in _manifest(corpus).bundles] == [
            "policy_demo_20260803_063010_trial0"
        ]
        assert not (corpus / "policy_demo_20260803_063010_trial1").exists()

    def test_a_directory_that_is_not_a_corpus_is_not_written_into(self, tmp_path: Path) -> None:
        run = _run_dir(tmp_path / "results", "policy_demo_20260803_063010")
        _write_bundle(run / "0", calls=[recorded_call("list_notes")])
        occupied = tmp_path / "notes"
        occupied.mkdir()
        (occupied / "README.md").write_text("not a corpus")

        result = _curate(
            "--source",
            str(tmp_path / "results"),
            "--into",
            str(occupied),
            "--criterion",
            _CRITERION,
        )

        assert result.exit_code == 1
        assert CORPUS_MANIFEST_FILENAME in result.output
        assert (occupied / "README.md").exists()


class TestWhatTheLayoutCannotExpress:
    def test_two_tasks_of_one_run_cannot_share_a_corpus(self, tmp_path: Path) -> None:
        """Trial indices restart per task, so two tasks' trial 0 claim one directory name."""
        run = tmp_path / "results" / "policy_demo_20260803_063010" / "trials"
        _write_bundle(run / _TASK_ID / "0", calls=[recorded_call("list_notes")])
        _write_bundle(run / "notes_add_note_duplicate_check_gated" / "0", calls=None)

        result = _curate(
            "--source",
            str(tmp_path / "results"),
            "--into",
            str(tmp_path / "corpus"),
            "--criterion",
            _CRITERION,
        )

        assert result.exit_code == 1
        assert "policy_demo_20260803_063010_trial0" in result.output
        assert not (tmp_path / "corpus").exists()

    def test_a_bundle_outside_a_run_layout_names_no_run_to_be_curated_from(
        self, tmp_path: Path
    ) -> None:
        """A corpus directory's own bundles are not run-shaped, so re-curating one is refused."""
        loose = tmp_path / "loose_bundles" / "some_trial"
        _write_bundle(loose, calls=[recorded_call("list_notes")])

        result = _curate(
            "--source",
            str(tmp_path / "loose_bundles"),
            "--into",
            str(tmp_path / "corpus"),
            "--criterion",
            _CRITERION,
        )

        assert result.exit_code == 1
        assert "<run-id>_trial<index>" in result.output
        assert not (tmp_path / "corpus").exists()

    def test_a_destination_inside_a_source_is_refused_before_anything_is_removed(
        self, tmp_path: Path
    ) -> None:
        """``--replace`` rewrites the whole destination, which would delete the sources."""
        results = tmp_path / "results"
        _write_bundle(
            _run_dir(results, "policy_demo_20260803_063010") / "0",
            calls=[recorded_call("list_notes")],
        )

        result = _curate(
            "--source",
            str(results),
            "--into",
            str(results / "corpus"),
            "--criterion",
            _CRITERION,
            "--replace",
        )

        assert result.exit_code == 1
        assert (_run_dir(results, "policy_demo_20260803_063010") / "0" / "grade.yaml").exists()


class TestTheAuthorsExclusion:
    def test_an_exclusion_the_rule_already_covers_is_refused(self, tmp_path: Path) -> None:
        """The manifest records one reason per trial, and the rule's is the measured one."""
        run = _run_dir(tmp_path / "results", "policy_demo_20260803_062316")
        _write_bundle(run / "0", calls=_failed_calls(6))
        _write_bundle(run / "1", calls=[recorded_call("list_notes")])

        result = _curate(
            "--source",
            str(tmp_path / "results"),
            "--into",
            str(tmp_path / "corpus"),
            "--criterion",
            _CRITERION,
            "--exclude",
            f"{run / '0'}=looked wrong to me",
        )

        assert result.exit_code == 1
        assert "environment_dead" in result.output
        assert not (tmp_path / "corpus").exists()

    def test_an_exclusion_naming_no_discovered_bundle_is_refused(self, tmp_path: Path) -> None:
        run = _run_dir(tmp_path / "results", "policy_demo_20260803_063010")
        _write_bundle(run / "0", calls=[recorded_call("list_notes")])

        result = _curate(
            "--source",
            str(tmp_path / "results"),
            "--into",
            str(tmp_path / "corpus"),
            "--criterion",
            _CRITERION,
            "--exclude",
            f"{run / '7'}=never ran",
        )

        assert result.exit_code == 1
        assert "trials/" + _TASK_ID + "/7" in result.output

    def test_an_exclusion_without_a_reason_is_refused_at_the_option(self, tmp_path: Path) -> None:
        run = _run_dir(tmp_path / "results", "policy_demo_20260803_063010")
        _write_bundle(run / "0", calls=[recorded_call("list_notes")])

        result = _curate(
            "--source",
            str(tmp_path / "results"),
            "--into",
            str(tmp_path / "corpus"),
            "--criterion",
            _CRITERION,
            "--exclude",
            str(run / "0"),
        )

        assert result.exit_code == 2
        assert "<bundle-dir>=<reason>" in result.output
