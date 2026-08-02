"""The shipped example corpus grades what it configures, and its packs discriminate.

Seven claims over the packs an author reads as the reference:

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
5. **No shipped pack fails the authoring gate.** Every ``grading.yaml`` under
   ``examples/`` and ``tests/data/tasks/`` is checked against its own task's tool
   inventory and produces no error and no advisory, which is the measured proof that
   the gate ships green rather than the claim that it does.
6. **``cache_debug`` grades two genuinely alternative diagnostic routes and cannot be
   passed by mutating.** Either comparison its rubric reference names scores in full
   and records itself as the winner; completing neither scores below completing
   either; and the shared gate sinks a trial whose winning route scored ``1.0``. Each
   route additionally requires the note to quote the stale status token that route's
   own read returned, so a note that recites the mechanism without the observation
   fails that route's grounded-claim check and nothing else.
7. **``lot_ops_01`` grades how the posted values were obtained, which its substrate
   oracle cannot see.** The reason code has to appear in a successful API result
   before the action is opened and the lot has to have been read first, both bound out
   of the POST rather than written as literals — so a guessed code, a fabricated code,
   an action against an unread lot, and a doubled post each fail exactly the check
   that names them, on trajectories the db_probe grades identically.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from tests.canonical._factories import make_trajectory
from tests.utils.recorded_calls import recorded_call
from tests.utils.timelines import Turn, build_turn_timeline
from tolokaforge.adapters._task_loader import build_tool_inventory, load_task_yaml
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.grading.config_validation import inspect_grading_authoring
from tolokaforge.core.grading.grade_components import GRADE_COMPONENTS
from tolokaforge.core.grading.trace_checks import evaluate_trace_checks
from tolokaforge.core.models import (
    Grade,
    GradingConfig,
    Message,
    MessageRole,
    RecordedToolCall,
    ToolCall,
    ToolExecutionStatus,
    TraceChecksConfig,
    TraceChecksResult,
    TraceConstraintKind,
    TraceConstraintSeverity,
)
from tolokaforge.core.project_loader import load_project_config
from tolokaforge.dx.cli.main import cli
from tolokaforge.runner.grading_ledger import audit_accounted_keys

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


_TEST_DATA_TASKS = Path(__file__).resolve().parents[1] / "data" / "tasks"

# Every pack under the two roots that ships a grading.yaml, so a guard that
# enumerated nothing fails instead of passing over the empty set.
_GATED_PACK_COUNT = 57

# The one pack whose tool inventory cannot be built: it declares
# ``tools.agent.mobile: true``, a typo fixture whose whole point is that a non-mapping
# init block fails loud rather than reaching trial registration as a TypeError.
_PACK_WITH_NO_INVENTORY = "bad_mobile"


def _gated_packs() -> list[tuple[Path, Path]]:
    """Each shipped task file that references a grading file, with that file."""
    gated: list[tuple[Path, Path]] = []
    for task_yaml in sorted(_EXAMPLES.rglob("task.yaml")) + sorted(
        _TEST_DATA_TASKS.rglob("task.yaml")
    ):
        if task_yaml in _TASKS_WITHOUT_A_PROJECT:
            continue
        task, task_dir = load_task_yaml(task_yaml)
        grading_path = task_dir / task.grading if task.grading else None
        if grading_path is not None and grading_path.exists():
            gated.append((task_yaml, grading_path))
    return gated


def test_no_shipped_pack_fails_the_authoring_gate() -> None:
    """The corpus proof that the gate rejects nothing that grades today.

    Each block is checked against its own task's inventory, so this is the whole
    severity table applied to real packs: an argument rule that descended past the
    first path segment, or an advisory promoted to an error, shows up here as a
    shipped pack that no longer loads.
    """
    findings: dict[str, list[str]] = {}
    without_an_inventory: list[str] = []
    gated = _gated_packs()

    for task_yaml, grading_path in gated:
        task, task_dir = load_task_yaml(task_yaml)
        grading = yaml.safe_load(grading_path.read_text()) or {}
        try:
            inventory = build_tool_inventory(task, task_dir)
        except ValueError:
            without_an_inventory.append(task.task_id)
            continue
        report = inspect_grading_authoring(grading, inventory)
        reported = [
            f"{finding.where}: {finding.message}" for finding in report.errors + report.advisories
        ]
        if reported:
            findings[task.task_id] = reported

    assert len(gated) == _GATED_PACK_COUNT, (
        f"the guard checked {len(gated)} packs, not {_GATED_PACK_COUNT}. A corpus "
        "proof over a subset says nothing about the packs it skipped"
    )
    assert without_an_inventory == [_PACK_WITH_NO_INVENTORY]
    assert findings == {}


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


def _http_call(
    sequence: int,
    url: str,
    method: str,
    *,
    body: dict[str, object] | None = None,
    output: str = "",
    status: ToolExecutionStatus = ToolExecutionStatus.SUCCESS,
) -> RecordedToolCall:
    """One recorded ``http_request``.

    ``body`` is a mapping rather than a kwargs splat: a request body carrying a
    ``status`` key is ordinary here (``POST /orders/4021``) and would otherwise
    shadow this function's own parameter.
    """
    arguments: dict[str, object] = {"url": url, "method": method}
    if body:
        arguments["json"] = body
    return recorded_call(
        "http_request", sequence=sequence, arguments=arguments, output=output, status=status
    )


def _json_response(payload: object, status_code: int = 200) -> str:
    """A response as ``HTTPRequestTool`` renders it — the parsed object's ``repr``.

    Load-bearing for every constraint that binds a value out of a result: the tool
    formats a JSON body with ``f"{response.json()}"``, so a served object shows
    single-quoted keys and a JSON string nested inside one shows the double quotes
    it was serialised with. The two capture patterns in ``cache_debug`` differ for
    exactly that reason.
    """
    return f"Status: {status_code}\nResponse (JSON):\n{payload!r}"


def _search(sequence: int, **body: object) -> RecordedToolCall:
    return _http_call(sequence, _SEARCH, "POST", body=body)


def _create_case(sequence: int) -> RecordedToolCall:
    return _http_call(
        sequence, _CASES, "POST", body={"delivery_id": 4021, "resolution_path": "reschedule"}
    )


def _annotate_delivery(sequence: int) -> RecordedToolCall:
    return _http_call(sequence, _DELIVERY, "PATCH", body={"resolution_path": "reschedule"})


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


_HELPDESK_TURNS = (
    "chasing DLV-4021, it lands after our dock closes",
    "reconciling the delivery, the site and the policy",
)


def _helpdesk_grading() -> GradingConfig:
    return _grading_config(_HELPDESK_TASK)[1]


def _timeline(calls: Sequence[RecordedToolCall], turns: tuple[str, str]):
    user, assistant = turns
    return build_turn_timeline([Turn("user", user), Turn("assistant", assistant, recorded=calls)])


def _failed(result: TraceChecksResult) -> list[str]:
    """The ids of the checks the scored decision set says did not hold."""
    return [constraint.id for constraint in result.constraints if not constraint.passed]


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
    result = evaluate_trace_checks(_timeline(_POLICY_CORRECT_RUN, _HELPDESK_TURNS), trace_checks)
    assert result.score == pytest.approx(1.0)
    assert _failed(result) == []


@pytest.mark.parametrize(("calls", "broken_constraint"), _WRONG_PROCESS_RUNS)
def test_each_trace_constraint_fails_the_process_it_names(
    calls: Sequence[RecordedToolCall], broken_constraint: str
) -> None:
    """No constraint is satisfied by every trajectory the task admits."""
    trace_checks = _helpdesk_grading().trace_checks
    assert trace_checks is not None
    result = evaluate_trace_checks(_timeline(calls, _HELPDESK_TURNS), trace_checks)
    assert _failed(result) == [broken_constraint]


def test_every_declared_trace_constraint_is_broken_by_one_of_the_wrong_runs() -> None:
    """So a constraint no scenario can fail cannot be added without a red test."""
    named = {param.values[1] for param in _WRONG_PROCESS_RUNS}
    assert named == {constraint_id for constraint_id, _ in _HELPDESK_CONSTRAINTS}


_CACHE_DEBUG_TASK = (
    _EXAMPLES
    / "native"
    / "multi_service_cache_debug"
    / "dataset"
    / "tasks"
    / "cache_debug"
    / "task.yaml"
)

_SERVED = "http://orders-api:8000/orders/4021"
_SOURCE = "http://orders-api:8000/orders/4021/source"
_CACHED = "http://cache-admin:8000/cache/order:4021"
_CACHE_KEYS = "http://cache-admin:8000/keys"

_CACHE_DEBUG_TURNS = (
    "order 4021 still shows processing to customers",
    "reading the layers and writing up the root cause",
)

# The shared half of the block, written out here so the assertion compares two
# sources. Exactly one check is a gate, and it is shared rather than sitting inside
# a route: "do not mutate on a diagnose-only task" holds whichever route was taken.
_CACHE_DEBUG_SHARED = (
    ("no_status_was_written", TraceConstraintKind.ABSENT, TraceConstraintSeverity.GATE),
    ("the_note_was_written", TraceConstraintKind.PRESENT, TraceConstraintSeverity.SCORED),
)

# The two routes and the checks each declares, in declaration order — which is also
# the tie-break order, so a run walking both routes is scored on the first.
_CACHE_DEBUG_PATHS = (
    (
        "divergence_between_the_api_layers",
        (
            "both_api_layer_reads_happened",
            "both_api_layer_reads_precede_the_note",
            "the_note_quotes_the_value_the_served_read_returned",
        ),
    ),
    (
        "divergence_against_the_cache",
        (
            "the_cached_value_and_an_api_read_happened",
            "the_cache_comparison_precedes_the_note",
            "the_note_quotes_the_value_the_cache_held",
        ),
    ),
)

# The two order views the pack's bug is the divergence between: the poisoned redis
# blob (``assets/build_seed.py``) and the postgres row (``shared/app-db/init.sql``).
_STALE_ORDER = {
    "order_id": 4021,
    "customer_id": "ACME",
    "product": "Widget crate",
    "status": "processing",
    "updated_at": "2026-07-10T09:00:00+00:00",
}
_FRESH_ORDER = dict(_STALE_ORDER, status="shipped", updated_at="2026-07-28T14:12:00+00:00")

# What each of the three reads answers with. ``/cache/order:4021`` returns the cached
# blob as a JSON *string* inside a JSON field (``shared/cache-admin/main.py``), which
# is why the cached read carries double-quoted keys where the served read does not.
_CACHE_DEBUG_PAYLOADS = {
    _SERVED: _STALE_ORDER,
    _SOURCE: _FRESH_ORDER,
    _CACHED: {"key": "order:4021", "value": json.dumps(_STALE_ORDER)},
    _CACHE_KEYS: {"keys": ["order:4021"]},
}

# A note the way the pack's own rubric reference writes one: it names the mechanism
# *and* quotes the two status values the agent read, which is what makes the
# grounded-claim check pass on a correct run rather than only on a verbose one.
_NOTE_TEXT = (
    "order:4021 is never invalidated on a status update, so the cache-first read keeps "
    "serving the stale processing value while the source of truth already reads shipped"
)
# The same diagnosis with the observation removed: a plausible note that explains
# cache invalidation without quoting anything the agent saw.
_UNGROUNDED_NOTE_TEXT = (
    "order:4021 is never invalidated on a status update, so reads serve the stale value"
)

# The note as the pack's own jsonpath check reads it, so a whole-grade fold sees the
# deterministic components the gate has to override rather than a stub.
_NOTE_ON_DISK = {"filesystem": {"/env/fs/agent-visible/submissions/rootcause.md": _NOTE_TEXT}}


def _read(sequence: int, url: str) -> RecordedToolCall:
    return _http_call(sequence, url, "GET", output=_json_response(_CACHE_DEBUG_PAYLOADS[url]))


def _post_status(sequence: int) -> RecordedToolCall:
    return _http_call(
        sequence, _SERVED, "POST", body={"status": "shipped"}, output=_json_response(_FRESH_ORDER)
    )


def _root_cause_note(sequence: int, text: str = _NOTE_TEXT) -> RecordedToolCall:
    return recorded_call(
        "write_file",
        sequence=sequence,
        arguments={"path": "submissions/rootcause.md", "content": text},
    )


def _cache_debug_messages(calls: Sequence[RecordedToolCall]) -> list[Message]:
    """The message view declaring every recorded call, as the trial would carry it."""
    user, assistant = _CACHE_DEBUG_TURNS
    return [
        Message(role=MessageRole.USER, content=user),
        Message(
            role=MessageRole.ASSISTANT,
            content=assistant,
            tool_calls=[
                ToolCall(id=call.call_id, name=call.tool_name, arguments=call.arguments)
                for call in calls
            ],
        ),
    ]


_ROUTE_A_IN_FULL = (_read(0, _SERVED), _read(1, _SOURCE), _root_cause_note(2))
_ROUTE_B_IN_FULL = (
    _read(0, _SERVED),
    _read(1, _CACHE_KEYS),
    _read(2, _CACHED),
    _root_cause_note(3),
)
# The cache route reads either orders-api endpoint, because the rubric reference
# names the source-vs-cache divergence as locating the bug just as the served-vs-cache
# one does. Without this row nothing holds the route to accepting both.
_ROUTE_B_FROM_THE_SOURCE_READ = (_read(0, _SOURCE), _read(1, _CACHED), _root_cause_note(2))
_ROUTES_IN_FULL = (
    pytest.param(_ROUTE_A_IN_FULL, "divergence_between_the_api_layers", id="served_vs_source"),
    pytest.param(_ROUTE_B_IN_FULL, "divergence_against_the_cache", id="served_vs_cache"),
    pytest.param(
        _ROUTE_B_FROM_THE_SOURCE_READ, "divergence_against_the_cache", id="source_vs_cache"
    ),
)

# Reads both layers, writes a correct note, and posts a status update on the way —
# the trajectory the shipped pack awarded full marks for a forbidden action.
_MUTATING_RUN = (_read(0, _SERVED), _read(1, _CACHED), _post_status(2), _root_cause_note(3))

# Starts down both routes and completes neither: the served read plus a key listing
# observes no divergence, so nothing was derived.
_CHERRY_PICKED_RUN = (_read(0, _SERVED), _read(1, _CACHE_KEYS), _root_cause_note(2))

# Each row is a trajectory that breaks exactly one declared check and no other. The
# route the agent walked decides which checks are scored, so the rows that break a
# route's own check are the rows on which that route wins.
_CACHE_DEBUG_WRONG_PROCESS_RUNS = (
    pytest.param(_MUTATING_RUN, "no_status_was_written", id="the_order_was_mutated"),
    pytest.param(
        (_read(0, _SERVED), _read(1, _SOURCE)),
        "the_note_was_written",
        id="both_layers_read_but_nothing_written",
    ),
    pytest.param(
        _CHERRY_PICKED_RUN,
        "both_api_layer_reads_happened",
        id="the_key_listing_stands_in_for_the_source_read",
    ),
    pytest.param(
        (_read(0, _SERVED), _root_cause_note(1), _read(2, _SOURCE)),
        "both_api_layer_reads_precede_the_note",
        id="the_source_was_read_after_the_note",
    ),
    pytest.param(
        (_root_cause_note(0), _read(1, _SOURCE)),
        "the_cached_value_and_an_api_read_happened",
        id="the_cache_was_never_read",
    ),
    pytest.param(
        (_root_cause_note(0), _read(1, _CACHED), _read(2, _SERVED)),
        "the_cache_comparison_precedes_the_note",
        id="the_cache_was_read_after_the_note",
    ),
    # Both routes walked in full, with a note that recites the mechanism and quotes
    # nothing the agent observed. Each row is the run on which its route wins, so the
    # grounded-claim check reached is that route's own.
    pytest.param(
        (_read(0, _SERVED), _read(1, _SOURCE), _root_cause_note(2, _UNGROUNDED_NOTE_TEXT)),
        "the_note_quotes_the_value_the_served_read_returned",
        id="the_note_names_no_value_the_served_read_returned",
    ),
    pytest.param(
        (
            _read(0, _SERVED),
            _read(1, _CACHE_KEYS),
            _read(2, _CACHED),
            _root_cause_note(3, _UNGROUNDED_NOTE_TEXT),
        ),
        "the_note_quotes_the_value_the_cache_held",
        id="the_note_names_no_value_the_cache_held",
    ),
)


def _cache_debug_trace_checks() -> TraceChecksConfig:
    trace_checks = _grading_config(_CACHE_DEBUG_TASK)[1].trace_checks
    assert trace_checks is not None
    return trace_checks


def _cache_debug_result(calls: Sequence[RecordedToolCall]) -> TraceChecksResult:
    return evaluate_trace_checks(_timeline(calls, _CACHE_DEBUG_TURNS), _cache_debug_trace_checks())


def test_the_cache_debug_pack_declares_two_routes_behind_one_shared_gate() -> None:
    trace_checks = _cache_debug_trace_checks()
    shared = tuple(
        (constraint.id, constraint.require.declared_kind(), constraint.severity)
        for constraint in trace_checks.constraints
    )
    paths = tuple(
        (path.id, tuple(constraint.id for constraint in path.constraints))
        for path in trace_checks.alternatives or ()
    )
    assert shared == _CACHE_DEBUG_SHARED
    assert paths == _CACHE_DEBUG_PATHS


@pytest.mark.parametrize(("calls", "winning_path"), _ROUTES_IN_FULL)
def test_each_cache_debug_route_scores_in_full_and_records_itself_the_winner(
    calls: Sequence[RecordedToolCall], winning_path: str
) -> None:
    """Both diagnostic routes the pack's rubric reference names are worth full marks.

    The served-vs-source run is the one the shipped pack docked. Driven through the
    fold at the pack's old weights it scored CORE ``(0.9333, True)`` on 2 of 3
    ``required_actions`` and RUNNER ``(0.95, True)`` on 3 of 4 rule rows: docked on
    both substrates for a route the task never required. The two numbers differ only
    by the aggregation divergence #685 already owns — core multiplies action x comm x
    legacy, the runner takes the fraction of rows — not by anything this pack says.
    """
    result = _cache_debug_result(calls)
    assert result.score == pytest.approx(1.0)
    assert result.winning_path == winning_path
    assert _failed(result) == []


@pytest.mark.parametrize(("calls", "broken_check"), _CACHE_DEBUG_WRONG_PROCESS_RUNS)
def test_each_cache_debug_check_fails_the_process_it_names(
    calls: Sequence[RecordedToolCall], broken_check: str
) -> None:
    """No check is satisfied by every trajectory, and each can fail on its own.

    Each row asserts the whole failing set, not membership in it, so a check its
    route's other check already implies shows up here as a row naming two: the
    ordering checks carry ``on_missing: pass`` precisely so a read that never
    happened is charged to the presence check alone.
    """
    assert _failed(_cache_debug_result(calls)) == [broken_check]


def test_every_declared_cache_debug_check_is_broken_by_one_of_the_wrong_runs() -> None:
    """So a check no scenario can fail cannot be added to the pack without a red test."""
    declared = {check for check, _, _ in _CACHE_DEBUG_SHARED} | {
        check for _, checks in _CACHE_DEBUG_PATHS for check in checks
    }
    assert {param.values[1] for param in _CACHE_DEBUG_WRONG_PROCESS_RUNS} == declared


def test_completing_neither_cache_debug_route_scores_below_completing_either() -> None:
    """The hazard alternatives exist for: half of one route plus half of another.

    Asserted against the two routes' own measured scores rather than a literal, so a
    rebalance that moved every number in step would still have to keep the ordering.
    """
    in_full = [_cache_debug_result(param.values[0]).score for param in _ROUTES_IN_FULL]
    cherry_picked = _cache_debug_result(_CHERRY_PICKED_RUN)

    assert cherry_picked.score < min(in_full)
    assert [path.score for path in cherry_picked.paths] == [
        pytest.approx(cherry_picked.score)
    ] * len(_CACHE_DEBUG_PATHS), (
        "the cherry-picked run completed neither route, so no route may score above "
        "the component the max-over-routes fold returned"
    )


def test_the_cache_debug_gate_fails_a_trial_whose_winning_route_scored_in_full() -> None:
    """A mutation on a diagnose-only task sinks the trial the route would have passed.

    The shipped pack scored this trajectory ``(1.0, True)`` on both substrates: the
    agent read both layers, wrote a correct note, and satisfied every required
    action, so the ``POST`` cost it nothing. The route it took still scores in full —
    ``paths[winner].score`` is untouched by the gate — and the component is ``0.0``
    with the state check at full marks, so no weighting rescues the trial.
    """
    result = _cache_debug_result(_MUTATING_RUN)
    winner = next(path for path in result.paths if path.id == result.winning_path)

    assert winner.score == pytest.approx(1.0)
    assert result.score == pytest.approx(0.0)
    assert result.gate_failed is True
    assert result.failed_gate_ids == ["no_status_was_written"]

    grade = GradingEngine(_grading_config(_CACHE_DEBUG_TASK)[1]).grade_trajectory(
        make_trajectory(
            task_id="cache_debug",
            messages=_cache_debug_messages(_MUTATING_RUN),
            tool_log=list(_MUTATING_RUN),
        ),
        _NOTE_ON_DISK,
    )
    assert grade.components.state_checks == pytest.approx(1.0)
    assert grade.components.trace_checks == pytest.approx(0.0)
    assert grade.binary_pass is False


_LOT_OPS_TASK = (
    _EXAMPLES
    / "native"
    / "multi_service_lot_ops"
    / "dataset"
    / "tasks"
    / "lot_ops_01"
    / "task.yaml"
)

# The block the pack is expected to ship, written out here so the assertion compares
# two sources. Both correlations are `before`, and the duplicate-post check is the
# pack's only gate.
_LOT_OPS_CONSTRAINTS = (
    (
        "the_reason_code_posted_was_read_from_the_catalog",
        TraceConstraintKind.BEFORE,
        TraceConstraintSeverity.SCORED,
    ),
    (
        "the_lot_was_read_before_the_action_was_opened",
        TraceConstraintKind.BEFORE,
        TraceConstraintSeverity.SCORED,
    ),
    (
        "exactly_one_corrective_action_was_opened",
        TraceConstraintKind.COUNT,
        TraceConstraintSeverity.GATE,
    ),
)

_APP = "http://app-service:8000"

# The three responses the lot-ops API really answers with, from `shared/app/main.py`
# and the `reason_codes` / `lots` seeds in `shared/app-db/init.sql`. The catalog read
# is the only place `CAPA-01` reaches the transcript before the POST, which is what
# the reason-code correlation is about.
_REASON_CODES = [
    {"code": "CAPA-01", "title": "Contamination", "category": "quality"},
    {"code": "CAPA-02", "title": "Dimensional nonconformance", "category": "quality"},
    {"code": "CAPA-03", "title": "Documentation error", "category": "process"},
]
_LOT_7 = {
    "lot_id": 7,
    "lot_code": "LOT-1007",
    "product": "Sterile vial C",
    "status": "released",
    "quantity": 980,
    "created_at": "2026-06-16",
}

_LOT_OPS_TURNS = (
    "LOT-1007 (lot_id 7) came back from QC with a contamination hit",
    "looking the lot and the reason code up, then opening the action",
)


def _lot_ops_get(
    sequence: int, path: str, payload: object, status_code: int = 200
) -> RecordedToolCall:
    return _http_call(sequence, f"{_APP}{path}", "GET", output=_json_response(payload, status_code))


def _open_action(sequence: int, lot: int, code: str, *, accepted: bool = True) -> RecordedToolCall:
    """A ``POST`` opening a corrective action, as the service answers it.

    ``reason_code`` carries a foreign key to ``reason_codes(code)``, so a code the
    catalog does not hold is rejected by postgres and the tool records a failure. The
    binder reads ``args.json.reason_code`` rather than the result, so it binds the
    attempted code either way — which is what lets a fabricated code be caught.
    """
    created = {
        "ca_id": 1,
        "lot_id": lot,
        "reason_code": code,
        "note": "QC contamination hit",
        "status": "open",
    }
    return _http_call(
        sequence,
        f"{_APP}/lots/{lot}/corrective-actions",
        "POST",
        body={"reason_code": code, "note": "QC contamination hit"},
        output=_json_response(created, 201) if accepted else "",
        status=ToolExecutionStatus.SUCCESS if accepted else ToolExecutionStatus.ERROR,
    )


def _completion_report(sequence: int) -> RecordedToolCall:
    return recorded_call(
        "write_file",
        sequence=sequence,
        arguments={
            "path": "submissions/report.md",
            "content": "Opened a contamination corrective action (CAPA-01) on lot LOT-1007.",
        },
    )


def _catalog(sequence: int) -> RecordedToolCall:
    return _lot_ops_get(sequence, "/reason-codes", _REASON_CODES)


def _lot(sequence: int) -> RecordedToolCall:
    return _lot_ops_get(sequence, "/lots/7", _LOT_7)


_LOT_OPS_CORRECT_RUN = (
    _lot(0),
    _catalog(1),
    _open_action(2, 7, "CAPA-01"),
    _completion_report(3),
)

# The trajectory that motivates the pack's whole trace block: the agent skips the
# catalog, writes `CAPA-01` from memory, and lands the identical substrate row. The
# db_probe cannot tell it from the correct run.
_GUESSED_CODE_RUN = (_lot(0), _open_action(1, 7, "CAPA-01"), _completion_report(2))

# The run that separates the binding from a hard-coded `contains: CAPA-01`: the agent
# does read the catalog and then posts a code the catalog does not hold. Under the
# literal the catalog result matches ahead of the POST and the check passes; under the
# binding the candidate is `CAPA-99`, nothing successful carries it, and it fails.
_FABRICATED_CODE_RUN = (
    _lot(0),
    _catalog(1),
    _open_action(2, 7, "CAPA-99", accepted=False),
    _completion_report(3),
)

# Reads the lot *code* as though it were the id — the confusion `task.yaml`'s own
# prompt invites ("LOT-1007 (that's lot_id 7)") — and opens the action against a lot
# it never read. `/lots/1007` is also why the correlation binds the whole URL: a bound
# `"7"` is a substring of `.../lots/1007`.
_UNREAD_LOT_RUN = (
    _lot_ops_get(0, "/lots/1007", {"detail": "not found"}, 404),
    _catalog(1),
    _open_action(2, 7, "CAPA-01"),
    _completion_report(3),
)

# #773: the action is posted twice. The db_probe does see the duplicate — its third
# assertion reads `row_count` — but `evaluate_db_probes` passes a probe only when every
# assertion does, so a duplicate took `state_checks` to `0.0` and the remaining
# `0.2 + 0.3` landed on `pass_threshold` exactly, which `>=` admits. A rebalance alone
# would not close that, which is why the check is a gate.
_DOUBLE_POST_RUN = (
    _lot(0),
    _catalog(1),
    _open_action(2, 7, "CAPA-01"),
    _open_action(3, 7, "CAPA-01"),
    _completion_report(4),
)

# The trajectory that justified deleting the pack's `required_actions`: the agent
# researches and reports but never opens the action. Both binders bind *from* the
# POST, so this is the zero-candidate case, and it is a standing test rather than a
# wrong-process row because it fails both correlations rather than one.
_NO_ACTION_RUN = (_lot(0), _catalog(1), _completion_report(2))

_LOT_OPS_WRONG_PROCESS_RUNS = (
    pytest.param(
        _GUESSED_CODE_RUN,
        "the_reason_code_posted_was_read_from_the_catalog",
        id="the_reason_code_was_never_looked_up",
    ),
    pytest.param(
        _FABRICATED_CODE_RUN,
        "the_reason_code_posted_was_read_from_the_catalog",
        id="the_posted_code_is_not_one_the_catalog_holds",
    ),
    pytest.param(
        _UNREAD_LOT_RUN,
        "the_lot_was_read_before_the_action_was_opened",
        id="the_action_is_opened_against_a_lot_never_read",
    ),
    pytest.param(
        _DOUBLE_POST_RUN,
        "exactly_one_corrective_action_was_opened",
        id="the_corrective_action_is_posted_twice",
    ),
)


def _lot_ops_grading() -> GradingConfig:
    return _grading_config(_LOT_OPS_TASK)[1]


def _lot_ops_result(calls: Sequence[RecordedToolCall]) -> TraceChecksResult:
    trace_checks = _lot_ops_grading().trace_checks
    assert trace_checks is not None
    return evaluate_trace_checks(_timeline(calls, _LOT_OPS_TURNS), trace_checks)


def _lot_ops_grade(calls: Sequence[RecordedToolCall]) -> Grade:
    """The whole fold over one trajectory, at the pack's own weights."""
    return GradingEngine(_lot_ops_grading()).grade_trajectory(
        make_trajectory(
            task_id="lot_ops_01",
            messages=_cache_debug_messages(calls),
            tool_log=list(calls),
        ),
        {"filesystem": {"/env/fs/agent-visible/submissions/report.md": "CAPA-01 on LOT-1007"}},
    )


def test_the_lot_ops_pack_declares_the_two_correlations_and_the_duplicate_gate() -> None:
    trace_checks = _lot_ops_grading().trace_checks
    assert trace_checks is not None
    declared = tuple(
        (constraint.id, constraint.require.declared_kind(), constraint.severity)
        for constraint in trace_checks.constraints
    )
    assert declared == _LOT_OPS_CONSTRAINTS
    assert trace_checks.alternatives is None


def test_the_lot_ops_pack_scores_the_grounded_process_in_full() -> None:
    result = _lot_ops_result(_LOT_OPS_CORRECT_RUN)
    assert result.score == pytest.approx(1.0)
    assert result.gate_failed is False
    assert _failed(result) == []


@pytest.mark.parametrize(("calls", "broken_constraint"), _LOT_OPS_WRONG_PROCESS_RUNS)
def test_each_lot_ops_constraint_fails_the_process_it_names(
    calls: Sequence[RecordedToolCall], broken_constraint: str
) -> None:
    """Every row reaches the substrate state the db_probe grades, by a wrong process."""
    assert _failed(_lot_ops_result(calls)) == [broken_constraint]


def test_every_declared_lot_ops_constraint_is_broken_by_one_of_the_wrong_runs() -> None:
    """So a constraint no scenario can fail cannot be added without a red test."""
    assert {param.values[1] for param in _LOT_OPS_WRONG_PROCESS_RUNS} == {
        constraint_id for constraint_id, _, _ in _LOT_OPS_CONSTRAINTS
    }


def test_a_run_that_never_opened_the_action_fails_both_correlations_and_stays_gradeable() -> None:
    """The zero-candidate case on a shipped pack, and it must not be an ungradeable trial.

    Both binders draw from the POST, so an agent that never posts binds nothing and the
    default ``on_unbound`` charges it — strictly stronger than the ``required_actions``
    row this replaced, which asked only that *a* POST happened. The ledger audit is the
    other half: a constraint whose ``require`` tree was never evaluated has to be filed
    as a skip, or the runner reports scored keys it neither evaluated nor skipped and
    ``GradeTrialResponse`` comes back unsuccessful.
    """
    result = _lot_ops_result(_NO_ACTION_RUN)
    failed = {constraint.id: constraint.message for constraint in result.constraints}

    assert _failed(result) == [
        "the_reason_code_posted_was_read_from_the_catalog",
        "the_lot_was_read_before_the_action_was_opened",
    ]
    assert failed["the_reason_code_posted_was_read_from_the_catalog"] == (
        "before is unbound: the binding selected no event"
    )
    audit = audit_accounted_keys(_lot_ops_grading(), result.accounted_keys)
    assert "trace_checks" not in (audit.error or "")


def test_the_guessed_reason_code_is_caught_by_the_correlation_and_by_nothing_else() -> None:
    """The correlation earns its weight over the fold rather than restating the probe.

    Driven through the real ``GradingEngine`` at the pack's weights, the guessed run
    and the grounded run differ in ``trace_checks`` and in no other component. The
    substrate oracle agrees with them both twice over: ``db_probes`` is RUNNER_ONLY, so
    core evaluates none of it here, and on a real run a guess that happens to be right
    lands the identical ``corrective_actions`` row — ``reason_code``, ``status`` and
    ``row_count`` all read the same. The judge is unscored in a deterministic fold, so
    ``llm_judge`` is ``None`` on both.
    """
    grounded = _lot_ops_grade(_LOT_OPS_CORRECT_RUN)
    guessed = _lot_ops_grade(_GUESSED_CODE_RUN)

    assert grounded.components.trace_checks == pytest.approx(1.0)
    assert guessed.components.trace_checks == pytest.approx(0.5)
    assert guessed.components.state_checks == grounded.components.state_checks
    assert guessed.components.llm_judge == grounded.components.llm_judge
    assert guessed.score < grounded.score
