"""A bundle that says it was redacted is refused by every offline grading command.

**The stamps here are written by hand on purpose.** Nothing in the tree writes one
yet — the writer arrives in a later commit. Landing the refusal first is what makes
it impossible for any commit on this branch to leave a redacted bundle silently
gradeable, and a hand-written stamp is the only way to express that intent before a
producer exists.

What each claim carries:

1. **Every CLI command is classified**, either as one of the four that grade a
   recorded bundle or as exempt with a reason. Derived from ``cli.commands`` rather
   than from a list written here, so a fifth grading command fails this file until
   someone decides which it is.
2. **Each of the four refuses a stamped bundle by name.** The assertion is the
   operator-visible *message*, not the exit code: an unreadable bundle already exits
   non-zero, so an exit-code assertion would pass for the wrong reason.
3. **``retrace`` counts the refusal as its own disposition**, not as an unreadable
   input — the bundle is intact, and telling an operator otherwise sends them to
   look for damage there is none of.
4. **An unstamped bundle is untouched** — the control that stops a gate which
   refuses everything from passing every claim above.
5. **A stamp that does not read raises**, rather than being taken for an absent one.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner, Result

from tests.utils.recorded_calls import recorded_call
from tolokaforge.core.grading.trace_replay import (
    TraceReplayFailure,
    TraceReplayOutcomeStatus,
    run_trace_replay_batch,
)
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import (
    CriterionResult,
    Grade,
    GradeComponents,
    JudgeStatus,
    Message,
    MessageRole,
    ToolCall,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.output.artifacts import (
    FileArtifactWriter,
    RedactedBundleError,
    bundle_redaction,
    read_recorded_tool_log,
)
from tolokaforge.dx.cli.main import cli

pytestmark = [pytest.mark.canonical, pytest.mark.grading]

_REPO = Path(__file__).resolve().parents[2]
#: The committed corpus and pack root ``reconcile`` reads — the only one of the four
#: whose reach into a bundle needs a pack declaring a migration over it.
_MIGRATION_CORPUS = _REPO / "tests/data/migration_corpora/notes_duplicate_check/not_met"
_MIGRATION_PACKS = _REPO / "tests/data/migration_packs"

_CRITERION = "checked_duplicates_first"
_AGENT_PROMPT = "You are the agent. Follow the refund policy exactly."
_RUBRIC = {
    "reference": "The refund must be issued for $328.50.",
    "criteria": [{"id": _CRITERION, "description": "Checked duplicates", "kind": "binary"}],
}
_JUDGE_MODEL = {"provider": "openrouter", "name": "openai/gpt-4.1-mini", "temperature": 0.0}
#: ``curate`` records which model produced each trial it admits, and refuses a
#: bundle that cannot say.
_AGENT_MODEL = {"provider": "anthropic", "name": "claude-sonnet-4.5", "temperature": 0.0}
_TRACE_CHECKS: dict[str, Any] = {
    "constraints": [
        {
            "id": "the_order_was_read",
            "description": "the agent read the order before answering",
            "require": {
                "present": {"match": {"kind": "tool_call", "tool": {"equals": "get_order"}}}
            },
        }
    ]
}

#: The four commands that read a recorded bundle back and derive a verdict from it.
GRADING_COMMANDS = frozenset({"curate", "reconcile", "rejudge", "retrace"})

#: Every other command, against the reason it reads no recorded bundle. A command
#: added without a classification fails the totality lock below rather than
#: silently escaping the gate.
EXEMPT: dict[str, str] = {
    "adapter": "converts task packs between benchmark formats; reads no trial bundle",
    "analyze": "renders one --trajectory file and grades nothing",
    "assets": "stamps a project's asset seeds; reads no trial bundle",
    "browse": "opens a run directory in the OS handler; reads nothing itself",
    "config": "inspects and edits configuration; reads no trial bundle",
    "docker": "manages images and service stacks; reads no trial bundle",
    "prepare": "lays out a queue-backed run directory before any trial exists",
    "repl": "interactive shell; grades nothing on its own",
    "run": "executes trials and writes bundles; never reads one back to grade it",
    "run-trial": "executes one trial over a pipe; writes a bundle, reads none back",
    "status": "reports a run directory's live progress; derives no verdict",
    "validate": "validates task configurations; reads no trial bundle",
    "worker": "executes queued trials; writes bundles, reads none back to grade",
}


def _stamp(bundle: Path, stamp: dict[str, Any] | str) -> None:
    """Write *stamp* under ``redaction:`` in the bundle's ``metrics.yaml``."""
    path = bundle / "metrics.yaml"
    metrics = yaml.safe_load(path.read_text())
    metrics["redaction"] = stamp
    path.write_text(yaml.safe_dump(metrics, sort_keys=False))


def _redacted_stamp() -> dict[str, Any]:
    return {
        "policy": "sensitive_keys",
        "artifacts": ["tool_log.yaml", "trajectory.yaml"],
        "omitted": [],
    }


def _trajectory() -> Trajectory:
    now = datetime.now(UTC)
    call = recorded_call(
        "get_order",
        sequence=0,
        arguments={"id": "O-1", "api_token": "sk-live-abc123"},
        output='{"total": 328.5}',
        call_id="c1",
    )
    return Trajectory(
        task_id="refund_task",
        trial_index=0,
        start_ts=now,
        end_ts=now,
        status=TrialStatus.COMPLETED,
        tool_log=[call],
        messages=[
            Message(role=MessageRole.USER, content="I want a refund for order O-1."),
            Message(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=[ToolCall(id="c1", name="get_order", arguments=dict(call.arguments))],
            ),
            Message(role=MessageRole.TOOL, content='{"total": 328.5}', tool_call_id="c1"),
            Message(role=MessageRole.ASSISTANT, content="Your refund of $328.50 is issued."),
        ],
        grade=Grade(
            binary_pass=True,
            score=1.0,
            components=GradeComponents(llm_judge=1.0),
            judge_status=JudgeStatus.COMPLETED,
            criterion_results=[
                CriterionResult(
                    id=_CRITERION,
                    met=True,
                    score=1.0,
                    justification="Checked the order first. VERDICT: MET",
                )
            ],
        ),
    )


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """One recorded run holding one bundle every same-shape command can reach.

    It carries trace constraints to re-check, a completed judge stage to re-judge,
    and a judged criterion to curate on, so the three commands below differ only in
    how they are invoked.
    """
    source = tmp_path / "run"
    trial_dir = source / "trials" / "refund_task" / "0"
    writer = FileArtifactWriter()
    writer.write_trial_bundle(
        trial_dir,
        _trajectory(),
        {
            "task_id": "refund_task",
            "trial_index": 0,
            "grading_config": {"trace_checks": _TRACE_CHECKS, "llm_judge": {"rubric": _RUBRIC}},
            "model_config": {"agent": _AGENT_MODEL, "judge": _JUDGE_MODEL},
        },
        {},
        StructuredLogger("refund_task-0"),
    )
    writer.write_prompts(trial_dir, _AGENT_PROMPT, "user-sim prompt")
    return source


@pytest.fixture
def bundle(run_dir: Path) -> Path:
    return run_dir / "trials" / "refund_task" / "0"


@pytest.fixture
def migration_corpus(tmp_path: Path) -> Path:
    """A writable copy of the committed corpus ``reconcile`` reads.

    Copied because the stamp below is a write, and writing into ``tests/data``
    would dirty the committed tree for every suite after this one.
    """
    destination = tmp_path / "corpus"
    shutil.copytree(_MIGRATION_CORPUS, destination)
    return destination


def _invoke(*args: str) -> Result:
    return CliRunner().invoke(cli, list(args), env={"COLUMNS": "200"})


def _retrace(run_dir: Path, tmp_path: Path) -> Result:
    return _invoke("retrace", "--source", str(run_dir), "--dry-run")


def _rejudge(run_dir: Path, tmp_path: Path) -> Result:
    return _invoke("rejudge", "--source", str(run_dir), "--dry-run")


def _curate(run_dir: Path, tmp_path: Path) -> Result:
    return _invoke(
        "curate",
        "--source",
        str(run_dir),
        "--into",
        str(tmp_path / "corpus_out"),
        "--criterion",
        _CRITERION,
    )


#: How each same-shape command is driven over the one run directory above.
_SAME_SHAPE_COMMANDS: dict[str, Callable[[Path, Path], Result]] = {
    "retrace": _retrace,
    "rejudge": _rejudge,
    "curate": _curate,
}


class TestEveryCommandIsClassified:
    def test_the_registry_is_partitioned_into_grading_and_exempt(self) -> None:
        """Derived from the CLI's own registry, so a new command lands here first."""
        assert GRADING_COMMANDS | EXEMPT.keys() == set(cli.commands)

    def test_no_command_is_both_a_grading_command_and_exempt(self) -> None:
        assert not GRADING_COMMANDS & EXEMPT.keys()

    def test_every_exemption_carries_a_reason(self) -> None:
        assert all(reason.strip() for reason in EXEMPT.values())


class TestAStampedBundleIsRefusedByName:
    @pytest.mark.parametrize("command", sorted(_SAME_SHAPE_COMMANDS))
    def test_the_message_names_redaction_and_the_exit_is_non_zero(
        self, command: str, run_dir: Path, bundle: Path, tmp_path: Path
    ) -> None:
        """The message carries the lock; the exit code alone would pass for an
        unreadable bundle too."""
        _stamp(bundle, _redacted_stamp())

        result = _SAME_SHAPE_COMMANDS[command](run_dir, tmp_path)

        assert result.exit_code != 0, result.output
        assert "redaction policy" in result.output, result.output
        assert "sensitive_keys" in result.output, result.output

    def test_reconcile_refuses_a_stamped_bundle_in_the_corpus_it_reads(
        self, migration_corpus: Path
    ) -> None:
        stamped = sorted(path for path in migration_corpus.iterdir() if path.is_dir())[0]
        _stamp(stamped, _redacted_stamp())

        result = _invoke(
            "reconcile",
            "--source",
            str(migration_corpus),
            "--packs",
            str(_MIGRATION_PACKS),
            "--dry-run",
        )

        assert result.exit_code != 0, result.output
        assert "redaction policy" in result.output, result.output
        assert stamped.name in result.output, result.output


class TestTheRefusalIsCountedAsItself:
    def test_retrace_records_a_redacted_bundle_apart_from_an_unreadable_one(
        self, run_dir: Path, bundle: Path
    ) -> None:
        _stamp(bundle, _redacted_stamp())

        (outcome,) = run_trace_replay_batch(run_dir, replay_id="canon", dry_run=True)

        assert outcome.status is TraceReplayOutcomeStatus.FAILED
        assert outcome.failure is TraceReplayFailure.REDACTED_BUNDLE

    def test_curate_names_redaction_rather_than_an_unreadable_record(
        self, run_dir: Path, bundle: Path, tmp_path: Path
    ) -> None:
        """The message an operator acts on: nothing here is broken, so
        ``unreadable tool-call record`` would send them to repair an intact file."""
        _stamp(bundle, _redacted_stamp())

        result = _curate(run_dir, tmp_path)

        assert "unreadable tool-call record" not in result.output, result.output
        assert "redacted before it was written" in result.output, result.output


class TestAnUnstampedBundleIsUntouched:
    """The control. Without it a gate that refused every bundle would pass every
    claim above."""

    @pytest.mark.parametrize("command", sorted(_SAME_SHAPE_COMMANDS))
    def test_the_command_succeeds_and_says_nothing_about_redaction(
        self, command: str, run_dir: Path, tmp_path: Path
    ) -> None:
        result = _SAME_SHAPE_COMMANDS[command](run_dir, tmp_path)

        assert result.exit_code == 0, result.output
        assert "redaction" not in result.output.lower(), result.output

    def test_the_bundle_reader_returns_the_record_it_holds(self, bundle: Path) -> None:
        record, present = read_recorded_tool_log(bundle)

        assert present is True
        assert [call.tool_name for call in record] == ["get_order"]

    def test_an_unstamped_bundle_declares_no_redaction(self, bundle: Path) -> None:
        assert bundle_redaction(bundle) is None


class TestAStampThatDoesNotReadIsNotAnAbsentOne:
    @pytest.mark.parametrize(
        ("stamp", "why"),
        [
            ({"policy": "sensitive_keys"}, "missing the fields naming what was rewritten"),
            (
                {"policy": "no_such_policy", "artifacts": [], "omitted": []},
                "a policy name nothing implements",
            ),
            (
                {
                    "policy": "sensitive_keys",
                    "artifacts": [],
                    "omitted": [],
                    "scope": "everything",
                },
                "a key the stamp does not define",
            ),
            ("sensitive_keys", "a scalar where the stamp belongs"),
        ],
    )
    def test_it_raises_rather_than_reading_as_faithful(
        self, bundle: Path, stamp: dict[str, Any] | str, why: str
    ) -> None:
        _stamp(bundle, stamp)

        with pytest.raises(RedactedBundleError, match="does not read as one"):
            bundle_redaction(bundle)

    def test_the_refusal_reaches_the_reader_the_grading_commands_call(self, bundle: Path) -> None:
        _stamp(bundle, {"policy": "sensitive_keys"})

        with pytest.raises(RedactedBundleError):
            read_recorded_tool_log(bundle)

    @pytest.mark.parametrize(
        ("header", "why"),
        [(b"\xff\xfe not utf-8", "undecodable bytes"), (b"- a\n- list\n", "not a mapping")],
    )
    def test_an_unreadable_header_is_an_unreadable_input_not_a_redaction(
        self, bundle: Path, header: bytes, why: str
    ) -> None:
        """A bundle whose header cannot be read is refused either way. Reporting it
        as a redaction would send an operator after a policy nothing applied."""
        (bundle / "metrics.yaml").write_bytes(header)

        with pytest.raises(ValueError) as raised:
            bundle_redaction(bundle)

        assert not isinstance(raised.value, RedactedBundleError)
