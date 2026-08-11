"""``tolokaforge retrace`` — the operator surface of a replay that spends nothing.

Every claim is driven through the real command rather than the library behind it, so
the exit code and the console text are what is asserted.

The whole exit contract, one test per row:

1. **A corpus that decides everything exits ``0`` and says what it decided** — every
   constraint's verdict reaches the console with its denominators, so an author reads
   the answer without opening the report file.
2. **A non-discriminating finding is a result, not a failure** — ``always_true`` exits
   ``0`` deliberately: an author iterating on a candidate expects to read it and keep
   working, and a CI job must not go red because a corpus turned out uninformative.
3. **A re-check disagreeing with the recorded grade also exits ``0``** — the
   agreement count is reported and the command succeeds; a downstream consumer reads
   the report file, not the exit code. A change making either of these non-zero breaks
   a test on purpose.
4. **A selector matching nothing exits ``1``** — empty discovery prints no table and
   claims nothing, which is what a reporter that prints nothing and returns success
   would otherwise be.
5. **A bundle that cannot be reconstructed exits ``1`` naming the file**, while the
   readable trials are still measured and still reported.
6. **A mis-authored override exits ``1`` before anything is replayed**, and nothing is
   written. A ``--replay-id`` that is not one directory name is refused at the option
   for the same reason: the read-only guarantee is scoped to one subtree, and the id
   is what keeps the writes inside it.
7. **A bundle whose provider reused a tool-call id is re-checked, not refused** — the
   trial's join key is episode-unique, so a bundle already on disk from a provider
   that numbers its ids per turn is gradeable from what it recorded.

And what the console has to carry for the numbers to be readable:

8. **A gate that could not run says so** — per bundle, and once in full with its
   reason, so an unchecked block never reads as a checked one.
9. **A route that won no trial is printed unmeasured beside its denominator**, with
   the route-scoping note, so unanimity on a route is not read as a corpus-wide claim.
10. **A dry run previews the batch and writes nothing** — and states that no
    constraint was measured rather than drawing an empty table.
11. **``--trial`` measures the bundle it names**, not the ones beside it.
12. **The import boundary holds** — a clean subprocess importing the replay module
    reaches neither an LLM client nor the judge, which is what makes "spends nothing"
    a property of the imports rather than a promise.

The console is the shared stderr one, so ``CliRunner`` captures it in
``result.output`` alongside anything on stdout.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner, Result

from tests.canonical._factories import (
    make_trajectory,
    make_trial_messages,
    make_turn_trial_messages,
)
from tests.utils.recorded_calls import recorded_call
from tolokaforge.core.grading.replay_layout import TRACE_REPLAY_DIRNAME
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import (
    Grade,
    GradeComponents,
    Message,
    RecordedToolCall,
    TraceConstraintKind,
    TraceConstraintResult,
)
from tolokaforge.core.output.artifacts import FileArtifactWriter
from tolokaforge.dx.cli.main import cli

pytestmark = [pytest.mark.canonical, pytest.mark.grading]

_TURNS = ("Refund order O-1.", "Reading the order.")

# One constraint the corpus below splits and one it does not, in one block: the
# trial that called nothing fails the first and passes the second, so a single
# invocation reports a DISCRIMINATING row beside an ALWAYS_TRUE one.
_TRACE_CHECKS: dict[str, Any] = {
    "constraints": [
        {
            "id": "the_order_was_read",
            "description": "the agent read the order before answering",
            "require": {
                "present": {"match": {"kind": "tool_call", "tool": {"equals": "get_order"}}}
            },
        },
        {
            "id": "no_account_was_deleted",
            "description": "the agent deleted nothing",
            "require": {
                "absent": {"match": {"kind": "tool_call", "tool": {"equals": "delete_account"}}}
            },
        },
    ]
}

# Two routes where the second wins on every trial that read the order, so the
# first route's constraint is emitted by nothing and is reportable only from the
# declared block.
_ROUTED_CHECKS: dict[str, Any] = {
    "alternatives": [
        {
            "id": "answered_from_memory",
            "description": "answered without reading anything",
            "constraints": [
                {
                    "id": "nothing_was_read",
                    "description": "no read happened",
                    "require": {
                        "absent": {"match": {"kind": "tool_call", "tool": {"equals": "get_order"}}}
                    },
                }
            ],
        },
        {
            "id": "read_the_order",
            "description": "read the order first",
            "constraints": [
                {
                    "id": "the_order_was_read",
                    "description": "the agent read the order",
                    "require": {
                        "present": {"match": {"kind": "tool_call", "tool": {"equals": "get_order"}}}
                    },
                }
            ],
        },
    ]
}

# A tool no recorded wire list declares, which is the defect the authoring gate
# exists to catch before a batch turns it into a corpus of failing trials.
_MISSPELLED_TOOL_CHECKS: dict[str, Any] = {
    "constraints": [
        {
            "id": "the_order_was_read",
            "description": "the agent read the order before answering",
            "require": {
                "present": {"match": {"kind": "tool_call", "tool": {"equals": "get_ordr"}}}
            },
        }
    ]
}

_WIRE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_order",
            "description": "Read one order by id",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
    }
]


def _read_the_order() -> list[RecordedToolCall]:
    return [recorded_call("get_order", sequence=0, arguments={"id": "O-1"}, output='{"t": 1}')]


def _recorded_read_verdict(passed: bool) -> TraceConstraintResult:
    """What a live run froze for ``the_order_was_read``, the report's second source."""
    return TraceConstraintResult(
        id="the_order_was_read", kind=TraceConstraintKind.PRESENT, passed=passed, weight=1.0
    )


def _write_bundle(
    trial_dir: Path,
    calls: list[RecordedToolCall],
    *,
    trace_checks: dict[str, Any] = _TRACE_CHECKS,
    wire_tools: list[dict[str, Any]] | None = None,
    recorded_verdicts: list[TraceConstraintResult] | None = None,
) -> Path:
    """One graded bundle on disk, its calls all declared by one assistant turn.

    ``recorded_verdicts`` is what the live run concluded per constraint — the
    independent source the report joins each recomputed verdict to by id. A bundle
    given none is graded but records no per-constraint verdict, which is a trial the
    agreement count has nothing to compare against rather than one that agreed.
    """
    return _write_graded_bundle(
        trial_dir,
        make_trial_messages(calls, _TURNS),
        calls,
        trace_checks=trace_checks,
        wire_tools=wire_tools,
        recorded_verdicts=recorded_verdicts,
    )


def _write_graded_bundle(
    trial_dir: Path,
    messages: list[Message],
    calls: list[RecordedToolCall],
    *,
    trace_checks: dict[str, Any],
    wire_tools: list[dict[str, Any]] | None,
    recorded_verdicts: list[TraceConstraintResult] | None,
) -> Path:
    trajectory = make_trajectory(
        task_id="refund_task",
        messages=messages,
        tool_log=calls,
    )
    graded = trajectory.model_copy(
        update={
            "grade": Grade(
                binary_pass=True,
                score=1.0,
                components=GradeComponents(),
                reasons="",
                trace_check_results=recorded_verdicts or [],
            )
        }
    )
    writer = FileArtifactWriter()
    writer.write_trial_bundle(
        trial_dir,
        graded,
        {"task_id": "refund_task", "grading_config": {"trace_checks": trace_checks}},
        {},
        StructuredLogger(f"refund_task-{trial_dir.name}"),
    )
    if wire_tools is not None:
        writer.write_tools_schemas(trial_dir, wire_tools)
    return trial_dir


def _write_cross_turn_bundle(trial_dir: Path, calls_per_turn: list[list[RecordedToolCall]]) -> Path:
    """One graded bundle whose calls are spread across several assistant turns."""
    return _write_graded_bundle(
        trial_dir,
        make_turn_trial_messages(calls_per_turn, _TURNS),
        [call for turn in calls_per_turn for call in turn],
        trace_checks=_TRACE_CHECKS,
        wire_tools=None,
        recorded_verdicts=None,
    )


def _corpus(root: Path, runs: list[list[RecordedToolCall]], **kwargs: Any) -> Path:
    for index, calls in enumerate(runs):
        _write_bundle(root / "trials" / "refund_task" / str(index), calls, **kwargs)
    return root


def _override(directory: Path, block: dict[str, Any]) -> Path:
    path = directory / "constraints.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(block, sort_keys=False), encoding="utf-8")
    return path


def _retrace(*args: str) -> Result:
    """Invoke the real command, with a console wide enough to keep paths intact."""
    return CliRunner().invoke(cli, ["retrace", *args], env={"COLUMNS": "200"})


def test_a_corpus_that_decides_everything_exits_zero_and_names_every_verdict(
    tmp_path: Path,
) -> None:
    """The authoring answer, on the console, from the command an author runs.

    Two trials the same block splits differently: one read the order and one
    answered from memory, so ``the_order_was_read`` is proven to discriminate while
    ``no_account_was_deleted`` holds on both. Both rows reach the console with their
    denominators, because ``always_true`` over two trials is a far weaker claim than
    over twenty and the verdict alone does not say which.
    """
    source = _corpus(tmp_path, [_read_the_order(), []])

    result = _retrace("--source", str(source), "--replay-id", "r1")

    assert result.exit_code == 0, result.output
    assert "the_order_was_read" in result.output
    assert "discriminating" in result.output
    assert "no_account_was_deleted" in result.output
    assert "always_true" in result.output
    assert "evaluated 2, decided 2" in result.output
    assert str(source / TRACE_REPLAY_DIRNAME / "r1") in result.output
    assert (source / TRACE_REPLAY_DIRNAME / "r1" / "trace_replay_report.yaml").is_file()


def test_a_constraint_that_separates_nothing_is_a_finding_and_still_exits_zero(
    tmp_path: Path,
) -> None:
    """Standing single case on the deliberate half of the exit contract.

    Every trial read the order, so ``the_order_was_read`` passed on all of them:
    the constraint separates nothing over this corpus, which is exactly what an
    author iterating on a candidate needs to be told without the command failing.
    A downstream consumer reads the report file rather than the exit code, so making
    this exit non-zero would have to break this test on purpose.
    """
    source = _corpus(tmp_path, [_read_the_order(), _read_the_order()])

    result = _retrace("--source", str(source))

    assert result.exit_code == 0, result.output
    assert "always_true" in result.output
    assert "discriminating" not in result.output


def test_a_replay_disagreeing_with_the_recorded_grade_is_reported_and_still_exits_zero(
    tmp_path: Path,
) -> None:
    """Standing single case on the other deliberate half of the exit contract.

    Both trials read the order, so the re-check passes ``the_order_was_read`` on both —
    and one of them recorded a *failure* on that same constraint, which is what a
    corpus whose recorded grades no longer reproduce looks like. The two sources are
    joined by constraint id, so the count is one of two: a change that compared the
    constraint against the trial-level pass instead would read two of two here, since
    both trials recorded a pass.

    The command reports it and exits ``0``. A disagreement is what an author ran the
    command to find, and a downstream consumer reads the report file, not the exit
    code.
    """
    source = tmp_path
    for index, passed in enumerate((True, False)):
        _write_bundle(
            source / "trials" / "refund_task" / str(index),
            _read_the_order(),
            recorded_verdicts=[_recorded_read_verdict(passed)],
        )

    result = _retrace("--source", str(source))

    assert result.exit_code == 0, result.output
    assert "agrees with the recorded verdict on 1/2" in result.output


@pytest.mark.parametrize(
    "replay_id",
    [
        pytest.param("..", id="the_parent_of_the_subtree_the_replay_owns"),
        pytest.param("../../escaped", id="a_path_walking_out_of_the_source"),
        pytest.param("nested/id", id="a_separator_naming_a_tree_of_its_own"),
        pytest.param(".", id="the_subtree_itself"),
    ],
)
def test_a_replay_id_that_is_not_a_directory_name_is_refused_before_anything_runs(
    tmp_path: Path, replay_id: str
) -> None:
    """A replay id names one directory, and the refusal is what keeps it inside the source.

    The read-only guarantee is scoped to a subtree — ``<source>/trace_replay/<id>/`` —
    so an id carrying a separator writes into a tree the operator never named and
    ``..`` walks out of the source entirely, which is where the guarantee stops
    meaning anything. Refused at the option, so nothing is discovered, nothing is read
    and nothing is written; the exit is Click's usage code because a malformed option
    value is a usage error, the way ``--time-limit`` treats one.
    """
    source = _corpus(tmp_path, [_read_the_order()])

    result = _retrace("--source", str(source), "--replay-id", replay_id)

    assert result.exit_code == 2, result.output
    assert "is not a directory name" in result.output
    assert not (source / TRACE_REPLAY_DIRNAME).exists()
    assert list(tmp_path.glob("escaped")) == []


def test_a_source_holding_no_bundle_exits_one_rather_than_reporting_an_empty_table(
    tmp_path: Path,
) -> None:
    """Standing single case at the degenerate boundary: nothing found, nothing claimed.

    A selector matching nothing validates nothing — the rule ``validate`` already
    applies to a glob. Printed as an empty table with a zero exit it would be the
    gate that cannot fail, so the command says the source held no bundle and exits
    non-zero, writing nothing.
    """
    empty = tmp_path / "nowhere"
    empty.mkdir()

    result = _retrace("--source", str(empty))

    assert result.exit_code == 1, result.output
    assert str(empty) in result.output
    assert "no trial bundle" in result.output
    assert "Constraints" not in result.output
    assert not (empty / TRACE_REPLAY_DIRNAME).exists()


def test_an_unreadable_bundle_exits_one_naming_the_file_and_the_rest_still_reports(
    tmp_path: Path,
) -> None:
    """A partial failure is a failure, and the surviving trials are still measured.

    The batch does not abort on a bundle it cannot read — a reader needs the rows
    the readable trials produced — but the command exits non-zero, so a scripted
    caller never reads a partially-failed replay as a clean one. The reason names the
    file, because "one bundle failed" is not actionable.
    """
    source = _corpus(tmp_path, [_read_the_order(), _read_the_order()])
    broken = source / "trials" / "refund_task" / "1"
    (broken / "trajectory.yaml").write_text("messages: [: :\n", encoding="utf-8")

    result = _retrace("--source", str(source), "--replay-id", "r1")

    assert result.exit_code == 1, result.output
    assert str(broken / "trajectory.yaml") in result.output
    assert "always_true" in result.output
    assert "evaluated 1, decided 1" in result.output
    assert (source / TRACE_REPLAY_DIRNAME / "r1" / "trace_replay_report.yaml").is_file()


def test_a_bundle_whose_provider_reused_a_call_id_is_re_checked_rather_than_refused(
    tmp_path: Path,
) -> None:
    """The bundles already on disk are gradeable, at the surface an operator runs.

    ``moonshotai/kimi-k3`` numbers each tool call within its turn, so calling one
    tool at the same position in two turns emits one id twice — across turns, never
    within one, which is why this bundle needs a message view that spans turns.
    The trial's join key is derived per occurrence rather than taken raw, so the
    bundle reconstructs and both constraints are decided from what it recorded.
    """
    source = tmp_path
    _write_cross_turn_bundle(
        source / "trials" / "refund_task" / "0",
        [
            [
                recorded_call(
                    "get_order",
                    sequence=0,
                    call_id="get_order:0",
                    arguments={"id": "O-1"},
                    output='{"t": 1}',
                )
            ],
            [
                recorded_call(
                    "get_order",
                    sequence=1,
                    call_id="get_order:0",
                    arguments={"id": "O-2"},
                    output='{"t": 2}',
                )
            ],
        ],
    )

    result = _retrace("--source", str(source), "--replay-id", "r1")

    assert result.exit_code == 0, result.output
    assert "the_order_was_read" in result.output
    assert "no_account_was_deleted" in result.output
    assert "evaluated 1, decided 1" in result.output
    assert (source / TRACE_REPLAY_DIRNAME / "r1" / "trace_replay_report.yaml").is_file()


def test_a_mis_authored_override_exits_one_before_any_trial_is_re_checked(
    tmp_path: Path,
) -> None:
    """One defect in one file, reported once, with nothing written.

    Two bundles, because with one "aborted before replaying" and "replayed and then
    aborted" are the same observation. The misspelling reaches the console rather
        than a traceback, and the output subtree is never created.
    """
    source = _corpus(tmp_path, [_read_the_order(), _read_the_order()], wire_tools=_WIRE_TOOLS)

    result = _retrace(
        "--source",
        str(source),
        "--constraints",
        str(_override(tmp_path / "supplied", _MISSPELLED_TOOL_CHECKS)),
    )

    assert result.exit_code == 1, result.output
    assert "get_ordr" in result.output
    assert not (source / TRACE_REPLAY_DIRNAME).exists()


def test_a_bundle_whose_tool_set_is_unknown_reports_the_skip_with_its_reason(
    tmp_path: Path,
) -> None:
    """A gate that could not run says so on the console, per bundle and once in full.

    The same override that aborts against a recorded wire list is admitted here,
    because these bundles recorded none — absent is not empty. So the run exits `0`
    and the console carries both halves: which bundles the gate could not answer for,
    and the reason it could not, printed once rather than per trial.
    """
    source = _corpus(tmp_path, [_read_the_order(), _read_the_order()])

    result = _retrace(
        "--source",
        str(source),
        "--constraints",
        str(_override(tmp_path / "supplied", _MISSPELLED_TOOL_CHECKS)),
    )

    assert result.exit_code == 0, result.output
    assert result.output.count("override unchecked") == 2
    assert result.output.count("the tool set of this task could not be resolved") == 1
    assert "always_false" in result.output


def test_a_route_that_won_no_trial_is_printed_unmeasured_beside_its_denominator(
    tmp_path: Path,
) -> None:
    """Standing single case: a losing route's constraints must not vanish, or read as passing.

    The evaluator emits the shared constraints and the winning route's only, so the
    losing route's constraint appears in no result at all. It still gets a console
    row, saying zero trials evaluated and naming the route it belongs to — and the
    route-scoping note prints, because ``always_true`` on a route that won twice out
    of twenty otherwise reads as a corpus-wide claim.
    """
    source = _corpus(tmp_path, [_read_the_order()], trace_checks=_ROUTED_CHECKS)

    result = _retrace("--source", str(source))

    assert result.exit_code == 0, result.output
    assert "nothing_was_read" in result.output
    assert "not_measured" in result.output
    assert "answered_from_memory" in result.output
    assert "evaluated 0" in result.output
    assert "trials its path won" in result.output


def test_a_dry_run_previews_the_batch_and_writes_nothing(tmp_path: Path) -> None:
    """The operator's no-write preview: what would be re-checked, and no artifact.

    Nothing is re-checked, so there is no verdict to report and the command says the
    table is empty rather than printing rows that would read as measurements. The
    reconstruction still happens — that is the thing worth checking for free — so a
    bundle that could not be rebuilt still fails here.
    """
    source = _corpus(tmp_path, [_read_the_order(), _read_the_order()])

    result = _retrace("--source", str(source), "--dry-run")

    assert result.exit_code == 0, result.output
    assert result.output.count("would re-check") == 2
    assert "no constraint was measured" in result.output
    assert "always_true" not in result.output
    assert not (source / TRACE_REPLAY_DIRNAME).exists()


def test_a_named_trial_replaces_discovery_on_the_command_line(tmp_path: Path) -> None:
    """``--trial`` measures the one bundle named, not every bundle beside it."""
    source = _corpus(tmp_path, [_read_the_order(), []])
    named = source / "trials" / "refund_task" / "1"

    result = _retrace("--source", str(source), "--trial", str(named))

    assert result.exit_code == 0, result.output
    assert "evaluated 1, decided 1" in result.output
    assert "always_false" in result.output
    assert "discriminating" not in result.output


_IMPORT_PROBE = """
import importlib
import sys

importlib.import_module("tolokaforge.core.grading.trace_replay")
print("\\n".join(sorted(m for m in sys.modules if m.split(".")[0] == "tolokaforge")))
"""


def test_the_replay_module_reaches_neither_an_llm_client_nor_the_judge() -> None:
    """ "Spends nothing" is a property of the imports, measured in a clean interpreter.

    A clean subprocess is the only honest measurement: pytest has already imported
    most of the tree, so an in-process footprint proves nothing.

    The two names are the assertion, not the ``tolokaforge.core.llm`` prefix: eleven
    ``core.llm`` *policy* modules (``cache_policy``, ``content_policy``,
    ``params_policy``, ``prompt_policy``, ``reasoning``, ``reasoning_codec``,
    ``response_policy``, ``schema_sanitizer``, ``usage``, ``dict_maps``, and the
    package) arrive transitively via ``core.models``, so a prefix assertion would be
    red on day one and would be asserting the wrong thing. ``client`` and ``judge``
    are what a wired-in judge would pull, and they are what spends money.

    The evaluator's presence is asserted beside them so a probe that imported
    nothing — a renamed module, a typo in the probe — fails instead of passing.
    """
    completed = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE], capture_output=True, text=True
    )

    assert completed.returncode == 0, completed.stderr
    footprint = set(completed.stdout.split())

    assert "tolokaforge.core.grading.trace_checks" in footprint
    assert "tolokaforge.core.llm" in footprint
    assert "tolokaforge.core.llm.client" not in footprint, (
        "trace replay now reaches an LLM client, so a command that must be incapable "
        "of spending has a client on its import path"
    )
    assert "tolokaforge.core.grading.judge" not in footprint, (
        "trace replay now reaches the judge, which spends tokens per trial — the "
        "boundary is what makes retrace free"
    )
