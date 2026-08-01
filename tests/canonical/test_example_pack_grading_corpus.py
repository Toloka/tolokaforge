"""The shipped example corpus grades what it configures, and the flagship pack discriminates.

Four claims over the packs an author reads as the reference:

1. **No example pack configures a component it never weights.** Core drops a scored
   component carrying no declared weight and the runner folds it in at an invented
   ``1.0`` (#744), so the two substrates disagree on any pack of that shape. The guard
   reads the **effective** combine — what ``NativeAdapter.get_grading_config`` returns
   after the project layer merges — because five shipped packs declare no ``combine``
   of their own and inherit ``llm_judge: 1.0`` from ``project.yaml``. Over raw
   ``grading.yaml`` the same guard is red on those five on day one.
2. **``helpdesk_01``'s ``trace_checks`` block asserts the process its README calls
   ungradeable by any other rule**, and each of its three constraints can fail on
   its own. A trajectory that reaches the right database state by a wrong process
   fails the constraint that names that process and no other.
3. **``tolokaforge validate`` is a gate over that corpus**: it partitions the same 30
   task files into the 28 it loads under their project's ``task_defaults`` and the two
   it rejects, and exits non-zero because of them.
4. **The tool inventory a gate reads answers for exactly the tools the wire carries.**
   The inventory resolves schemas read-only while ``to_task_description`` may spawn the
   task's MCP server; both go through one producer, and this is where a second copy of
   the resolution would show up.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from click.testing import CliRunner

from tests.utils.recorded_calls import recorded_call
from tests.utils.timelines import Turn, build_turn_timeline
from tolokaforge.adapters._task_loader import build_tool_inventory
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.grading.grade_components import GRADE_COMPONENTS
from tolokaforge.core.grading.trace_checks import evaluate_trace_checks
from tolokaforge.core.models import GradingConfig, RecordedToolCall
from tolokaforge.core.project_loader import load_project_config
from tolokaforge.dx.cli.main import cli

pytestmark = [pytest.mark.canonical, pytest.mark.grading]

_EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

# Every task the corpus grades, so a guard that enumerated nothing fails instead of
# passing over the empty set. The two files outside it are the ``terminal_bench``
# pair, which ship no enclosing project and are the corpus's two known-invalid tasks.
_GRADED_TASK_COUNT = 28
# Tool schemas the corpus puts on the wire, across the 23 tasks that declare any, so a
# parameter comparison that resolved nothing fails instead of passing over empty maps.
_CORPUS_TOOL_COUNT = 54
_TASKS_WITHOUT_A_PROJECT = (
    _EXAMPLES / "terminal_bench" / "fix-airline-segmentation" / "task.yaml",
    _EXAMPLES / "terminal_bench" / "fix-billing-holds" / "task.yaml",
)


def _enclosing_project(task_yaml: Path) -> Path | None:
    """The ``project.yaml`` whose layer this task loads under, or ``None``."""
    for directory in task_yaml.parents:
        candidate = directory / "project.yaml"
        if candidate.exists():
            return candidate
        if directory == _EXAMPLES:
            return None
    return None


def _pack_adapter(task_yaml: Path) -> tuple[str, NativeAdapter]:
    """The task's id and an adapter over it, wired the orchestrator's way.

    The adapter is pointed at this one task file rather than at the project's own
    discovery glob: several packs are run through a glob rooted at ``dataset/``
    while their ``project.yaml`` sits a level above, so enumerating by the declared
    glob silently measures a subset of the corpus.
    """
    project_yaml = _enclosing_project(task_yaml)
    assert project_yaml is not None, f"{task_yaml} is under no project"
    project = load_project_config(project_yaml)
    root = project_yaml.parent
    adapter = NativeAdapter(
        {
            "tasks_glob": str(task_yaml.relative_to(root)),
            "task_packs": [str(root)],
            "project_task_defaults": project.task_defaults.model_dump(exclude_defaults=True)
            or None,
            "project_default_environment": project.default_environment,
        }
    )
    task_ids = adapter.get_task_ids()
    assert len(task_ids) == 1, f"{task_yaml} resolved to {task_ids}, not one task"
    return task_ids[0], adapter


def _corpus_task_files() -> list[Path]:
    return [
        task_yaml
        for task_yaml in sorted(_EXAMPLES.rglob("task.yaml"))
        if task_yaml not in _TASKS_WITHOUT_A_PROJECT
    ]


def _grading_config(task_yaml: Path) -> tuple[str, GradingConfig]:
    """The task's id and its effective grading config."""
    task_id, adapter = _pack_adapter(task_yaml)
    return task_id, adapter.get_grading_config(task_id)


def _graded_corpus() -> dict[str, GradingConfig]:
    return dict(_grading_config(task_yaml) for task_yaml in _corpus_task_files())


def test_every_component_an_example_pack_configures_carries_a_weight() -> None:
    """A configured-but-unweighted component is #744's authoring-side exposure."""
    corpus = _graded_corpus()
    assert len(corpus) == _GRADED_TASK_COUNT, (
        f"the guard measured {len(corpus)} example tasks, not {_GRADED_TASK_COUNT}. A "
        "corpus guard over a subset proves nothing about the packs it skipped"
    )
    unweighted = {
        task_id: sorted(
            spec.name
            for spec in GRADE_COMPONENTS
            if getattr(grading, spec.config_section, None)
            and spec.name not in (grading.combine.weights or {})
        )
        for task_id, grading in corpus.items()
    }
    assert {task_id: names for task_id, names in unweighted.items() if names} == {}, (
        "these packs configure a component the effective combine never weights, so core "
        "drops it from the fold and the runner invents a 1.0 for it (#744)"
    )


def test_the_tool_inventory_answers_for_the_tools_the_wire_actually_carries() -> None:
    """One producer serves both the run and the pre-run gate, so neither can drift.

    The two sides are not the same source: the inventory reads the producer in
    read-only mode, while ``to_task_description`` assembles ``ToolSchema`` objects
    around it in subprocess mode. A second copy of the schema lookup inlined into
    the adapter fails here as soon as the two copies disagree.
    """
    divergent_names: dict[str, tuple[list[str], list[str]]] = {}
    divergent_parameters: dict[str, list[str]] = {}
    compared = 0

    for task_yaml in _corpus_task_files():
        task_id, adapter = _pack_adapter(task_yaml)
        wire = {
            tool.name: tool.parameters for tool in adapter.to_task_description(task_id).agent_tools
        }
        inventory = build_tool_inventory(adapter.get_task(task_id), adapter.get_task_dir(task_id))

        if inventory.declared != set(wire):
            divergent_names[task_id] = (sorted(inventory.declared), sorted(wire))
        drifted = sorted(
            name
            for name, parameters in wire.items()
            if name in inventory.parameters and inventory.parameters[name] != parameters
        )
        if drifted:
            divergent_parameters[task_id] = drifted
        compared += sum(1 for name in wire if name in inventory.parameters)

    assert compared == _CORPUS_TOOL_COUNT, (
        f"the guard compared {compared} tool schemas, not {_CORPUS_TOOL_COUNT}. Every tool "
        "the corpus puts on the wire resolves in the inventory too, so a shortfall means "
        "the read-only mode stopped answering for tools the run still ships"
    )
    assert divergent_names == {}, "the inventory and the wire disagree on which tools exist"
    assert divergent_parameters == {}, "the two modes resolved different schemas for one tool"


def test_the_two_project_less_task_files_are_the_terminal_bench_pair() -> None:
    """A native pack losing its project layer would otherwise drop out unnoticed."""
    orphans = tuple(
        task_yaml
        for task_yaml in sorted(_EXAMPLES.rglob("task.yaml"))
        if _enclosing_project(task_yaml) is None
    )
    assert orphans == _TASKS_WITHOUT_A_PROJECT


def test_validate_gates_the_example_corpus_on_its_two_invalid_tasks() -> None:
    """The corpus proof that layering the project defaults rejects nothing new.

    ``COLUMNS`` is set wide so the per-task lines carry a whole path each and the
    partition can be read off the output rather than inferred from the counts.
    """
    result = CliRunner(mix_stderr=False).invoke(
        cli,
        ["validate", "--tasks", str(_EXAMPLES / "**" / "task.yaml")],
        env={"COLUMNS": "400"},
    )
    lines = result.stderr.splitlines()
    valid = {Path(line.removeprefix("✓ ")) for line in lines if line.startswith("✓ ")}
    invalid = {
        Path(line.removeprefix("✗ ").split(":", 1)[0]) for line in lines if line.startswith("✗ ")
    }

    assert result.exit_code == 1, result.stderr
    assert invalid == set(_TASKS_WITHOUT_A_PROJECT)
    assert valid == set(_EXAMPLES.rglob("task.yaml")) - invalid
    assert len(valid) == _GRADED_TASK_COUNT
    assert f"{_GRADED_TASK_COUNT} valid, {len(_TASKS_WITHOUT_A_PROJECT)} invalid" in result.stderr


_HELPDESK_TASK = (
    _EXAMPLES
    / "native"
    / "multi_service_helpdesk_workflow"
    / "dataset"
    / "tasks"
    / "helpdesk_01"
    / "task.yaml"
)

# The block the pack is expected to ship, written out here so the assertion compares
# two sources rather than the pack against itself. A constraint dropped from the pack
# fails against this list, and one added without a scenario below fails too.
_HELPDESK_CONSTRAINTS = (
    ("policy_query_rides_in_the_body", "present"),
    ("policy_read_before_the_case_is_written", "before"),
    ("delivery_not_annotated_before_the_policy_read", "absent_before"),
)

_SEARCH = "http://policy-search:8000/search"
_CASES = "http://crm:8000/cases"
_DELIVERY = "http://delivery-tracker:8000/deliveries/4021"


def _http_call(sequence: int, url: str, method: str, **body: object) -> RecordedToolCall:
    arguments: dict[str, object] = {"url": url, "method": method}
    if body:
        arguments["json"] = body
    return recorded_call("http_request", sequence=sequence, arguments=arguments)


def _search(sequence: int, **body: object) -> RecordedToolCall:
    return _http_call(sequence, _SEARCH, "POST", **body)


def _create_case(sequence: int) -> RecordedToolCall:
    return _http_call(sequence, _CASES, "POST", delivery_id=4021, resolution_path="reschedule")


def _annotate_delivery(sequence: int) -> RecordedToolCall:
    return _http_call(sequence, _DELIVERY, "PATCH", resolution_path="reschedule")


_POLICY_CORRECT_RUN = (
    _search(0, q="after-hours cold chain no on-site storage"),
    _create_case(1),
    _annotate_delivery(2),
)

# Each row is a trajectory that reaches the same database state by a different
# process, paired with the one constraint that process breaks. Every constraint is
# named by exactly one row, which is what makes none of them an always-pass check.
_WRONG_PROCESS_RUNS = (
    pytest.param(
        (_search(0, query="after-hours cold chain"), _create_case(1), _annotate_delivery(2)),
        "policy_query_rides_in_the_body",
        id="query_under_the_wrong_body_key",
    ),
    pytest.param(
        (_create_case(0), _search(1, q="after-hours cold chain"), _annotate_delivery(2)),
        "policy_read_before_the_case_is_written",
        id="case_written_before_the_policy_is_read",
    ),
    pytest.param(
        (_annotate_delivery(0), _search(1, q="after-hours cold chain"), _create_case(2)),
        "delivery_not_annotated_before_the_policy_read",
        id="delivery_annotated_before_the_policy_is_read",
    ),
)


def _helpdesk_grading() -> GradingConfig:
    return _grading_config(_HELPDESK_TASK)[1]


def _timeline(calls: Sequence[RecordedToolCall]):
    return build_turn_timeline(
        [
            Turn("user", "chasing DLV-4021, it lands after our dock closes"),
            Turn("assistant", "reconciling the delivery, the site and the policy", recorded=calls),
        ]
    )


def test_the_flagship_pack_declares_the_three_documented_trace_constraints() -> None:
    trace_checks = _helpdesk_grading().trace_checks
    assert trace_checks is not None
    declared = tuple(
        (constraint.id, constraint.require.declared_kind())
        for constraint in trace_checks.constraints
    )
    assert declared == _HELPDESK_CONSTRAINTS


def test_the_flagship_pack_scores_the_policy_correct_process_in_full() -> None:
    trace_checks = _helpdesk_grading().trace_checks
    assert trace_checks is not None
    result = evaluate_trace_checks(_timeline(_POLICY_CORRECT_RUN), trace_checks)
    assert result.score == pytest.approx(1.0)
    assert [constraint.id for constraint in result.constraints if not constraint.passed] == []


@pytest.mark.parametrize(("calls", "broken_constraint"), _WRONG_PROCESS_RUNS)
def test_each_trace_constraint_fails_the_process_it_names(
    calls: Sequence[RecordedToolCall], broken_constraint: str
) -> None:
    """No constraint is satisfied by every trajectory the task admits."""
    trace_checks = _helpdesk_grading().trace_checks
    assert trace_checks is not None
    result = evaluate_trace_checks(_timeline(calls), trace_checks)
    failed = [constraint.id for constraint in result.constraints if not constraint.passed]
    assert failed == [broken_constraint]


def test_every_declared_trace_constraint_is_broken_by_one_of_the_wrong_runs() -> None:
    """So a constraint no scenario can fail cannot be added without a red test."""
    named = {param.values[1] for param in _WRONG_PROCESS_RUNS}
    assert named == {constraint_id for constraint_id, _ in _HELPDESK_CONSTRAINTS}
