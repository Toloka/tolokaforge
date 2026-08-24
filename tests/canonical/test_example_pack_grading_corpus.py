"""The shipped example corpus grades what it configures, and its packs discriminate.

Fifteen claims over the packs an author reads as the reference:

1. **No pack in the repository and its weight map disagree about which components
   exist**, in either direction: nothing a pack configures goes unweighted, and no
   weight names a component the pack never configures. Both substrates refuse to fold
   a scored component whose share the map does not declare, and neither produces a
   component nothing configured, so either shape is ungradeable — and the authoring
   gate says so before the run pays for it. The guard reads the **effective** combine —
   what ``NativeAdapter.get_grading_config`` returns after the project layer merges —
   because five shipped packs declare no ``combine`` of their own and inherit
   ``llm_judge: 1.0`` from ``project.yaml``. Over raw ``grading.yaml`` the same guard
   is red on those five on day one. Both corpus roots are walked: the 83 project-less
   ``tests/data`` packs are where every stray weight was.
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
   inventory *and* its own effective combine, and produces no error, no advisory and
   nothing unchecked — the measured proof that the gate ships green rather than the
   claim that it does. The 49 authored packs outside those two roots — the two parity
   roots, the recorded projects and the migration fixtures — face the same whole gate
   through the same call site, against their own inventories and their own effective
   combines, so a fixture naming a tool its task never declares, or weighting a
   component it never configures, is refused before anyone runs ``validate``. Every
   schema those packs resolve closes its argument set, which is what keeps the
   argument rules refusing rather than advising. The rules a ``task.yaml`` holder can
   run without a tool set are held over the wider 112-pack walk on top of that, so no
   pack anywhere can declare a section that asserts nothing.
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
8. **A trial bundle re-grades to the verdict its live run produced.** ``lot_ops_01``'s
   correct run, written through the real artifact writer and read back off disk,
   scores the same ``1.0`` — because the bundle carries the tool-call record and not
   only the message trace. Without the record its flagship correlation cannot read
   ``status`` and the same trajectory scores ``0.5``, which is a replay blaming the
   author for evidence nobody wrote down.
9. **The replay engine reproduces a recorded verdict rather than re-deriving one.**
   Two ``cache_debug`` bundles written with their live grades re-check, through
   ``run_trace_replay_batch``, to the per-constraint verdicts and the winning route
   their own ``grade.yaml`` recorded — one bundle per route, one with the shared gate
   shut, so neither column is a constant.
10. **A corpus that decides everything separates a discriminating constraint from a
    degenerate one.** Both ``lot_ops_01`` correlations pass two of three trials and
    fail different ones; a supplied constraint nothing satisfies is ``ALWAYS_FALSE``
    and one everything satisfies is ``ALWAYS_TRUE``, both reported as findings.
11. **Missing evidence is reported as missing.** The same pack's flagship
    correlation over the three trajectories that need the tool-call record to decide
    is ``NEVER_DECIDED`` with nothing decided, not failed on every trial — and where
    one trial does decide it, ``UNDECIDED_IN_PART`` says so rather than condemning
    the corpus off one observation.
12. **A route that won no trial is reported unmeasured, not unanimous.** Three of
    ``cache_debug``'s eight declared constraints are emitted by no result on a
    mutating trial, and they keep a row saying zero trials evaluated.
13. **Agreement with the recorded pass is counted from two sources.** The verdict
    recomputed now against the ``binary_pass`` the live run wrote, over the corpus
    whose recorded column varies.
14. **``native_shared_domain``'s duplicate-check policy has two halves, and each is
    vetoed by the mechanism that can see it.** The ordering half is a shared
    ``severity: gate`` trace constraint and the warning half is the required rubric
    criterion, so a trial that listed but never warned fails the judge's gate alone
    while one that never listed fails the trace gate alone — an attribution the one
    conjoined criterion could not make.
15. **Every pack that replays a golden path is authored against a task that gives it a
    world to replay in.** An initial-state JSON file and an MCP server module are
    ``task.yaml`` facts, unreadable from ``grading.yaml``, and without them core hashes
    nothing and refuses to grade the trial at all. Each of the 112 packs is checked
    against its own resolved world, and again with that world's server module withheld
    and a golden action injected, which every pack has to be refused for.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from tests.canonical._factories import make_trajectory, make_trial_messages
from tests.utils.example_packs import (
    EXAMPLES_ROOT,
    REPO_ROOT,
    TEST_DATA_ROOT,
    enclosing_project,
    project_layer,
)
from tests.utils.recorded_calls import recorded_call
from tests.utils.timelines import Turn, build_turn_timeline
from tests.utils.trace_overrides import override_file
from tolokaforge.adapters._task_loader import (
    build_tool_inventory,
    hash_source_layer_under_adapter,
    load_task_yaml,
    replay_world_under_adapter,
    seeded_tables_under_adapter,
)
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.grading.combine import GradingEngine
from tolokaforge.core.grading.config_validation import (
    ArgumentSchema,
    AuthoringReport,
    HashSourceLayer,
    ReplayWorld,
    SeededTablesLayer,
    ToolInventory,
    inspect_grading_authoring,
    state_sources_as_a_run_reads_them,
)
from tolokaforge.core.grading.grade_components import (
    COMPONENT_BY_NAME,
    GRADE_COMPONENTS,
    component_requested,
)
from tolokaforge.core.grading.jsonpath_addressing import (
    block_addresses_the_database,
    unreachable_target,
)
from tolokaforge.core.grading.rubric import aggregate_rubric, parse_submit_report
from tolokaforge.core.grading.state_composition import HASH_SOURCE_KEYS
from tolokaforge.core.grading.trace_checks import evaluate_trace_checks
from tolokaforge.core.grading.trace_replay import (
    ConstraintDiscrimination,
    ConstraintDiscriminationRow,
    ConstraintProvenance,
    TraceChecksOverride,
    TraceReplayOutcomeStatus,
    TraceReplayReport,
    TrialTraceReplayOutcome,
    build_trace_replay_report,
    declared_trace_checks,
    read_trace_replay_inputs,
    run_trace_replay_batch,
)
from tolokaforge.core.grading.trace_timeline import build_trial_timeline
from tolokaforge.core.logging import StructuredLogger
from tolokaforge.core.models import (
    Grade,
    GradingCombineConfig,
    GradingConfig,
    RecordedToolCall,
    ToolExecutionStatus,
    TraceChecksConfig,
    TraceChecksResult,
    TraceConstraintKind,
    TraceConstraintResult,
    TraceConstraintSeverity,
    Trajectory,
)
from tolokaforge.core.output.artifacts import FileArtifactWriter, read_recorded_tool_log
from tolokaforge.core.project_loader import (
    load_project_config,
    project_grading_combine,
    resolve_effective_grading_combine,
)
from tolokaforge.dx.cli.main import cli
from tolokaforge.runner.grading import compose_runner_trial_verdict
from tolokaforge.runner.grading_ledger import audit_accounted_keys
from tolokaforge.runner.models import (
    RunnerInitialStateConfig,
    TableSchema,
    provisions_database,
)

pytestmark = [pytest.mark.canonical, pytest.mark.grading]

_REPO = REPO_ROOT
_EXAMPLES = EXAMPLES_ROOT
_TEST_DATA = TEST_DATA_ROOT

# Every task under ``examples/`` the corpus grades, so a guard that enumerated nothing
# fails instead of passing over the empty set. The two files outside it are the
# ``terminal_bench`` pair, which ship no enclosing project and are the corpus's two
# known-invalid tasks.
_GRADED_TASK_COUNT = 29
# Tool schemas the corpus puts on the wire, across the 24 tasks that declare any, so a
# parameter comparison that resolved nothing fails instead of passing over empty maps.
_CORPUS_TOOL_COUNT = 56
_TASKS_WITHOUT_A_PROJECT = (
    _EXAMPLES / "terminal_bench" / "fix-airline-segmentation" / "task.yaml",
    _EXAMPLES / "terminal_bench" / "fix-billing-holds" / "task.yaml",
)


def _pack_adapter(task_yaml: Path) -> tuple[str, NativeAdapter]:
    """The task's id and an adapter over it, wired the orchestrator's way.

    The adapter is pointed at this one task file rather than at the project's own
    discovery glob: several packs are run through a glob rooted at ``dataset/``
    while their ``project.yaml`` sits a level above, so enumerating by the declared
    glob silently measures a subset of the corpus.
    """
    project_yaml = enclosing_project(task_yaml)
    if project_yaml is None:
        root, environment = task_yaml.parent, None
    else:
        root = project_yaml.parent
        environment = load_project_config(project_yaml).default_environment
    adapter = NativeAdapter(
        {
            "tasks_glob": str(task_yaml.relative_to(root)),
            "task_packs": [str(root)],
            "project_task_defaults": project_layer(task_yaml),
            "project_default_environment": environment,
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


# Directories under ``tests/data`` holding recorded ``TaskDescription`` artifacts: a
# bundle's own copy of the config the trial was graded under. They are not authored
# packs, nothing may edit them, and a guard over authoring must not read them.
_RECORDED_ARTIFACT_DIRS = ("output/trials", "migration_corpora", "curation_runs")

# The authored task files that load no grading config, so a pack losing its grading
# block shows up as a guard failure rather than as a silent absence. The four
# ``terminal_bench`` files declare no ``task_id`` at all and ``actor_binding`` ships no
# grading; ``test_the_authored_walk_partitions_every_task_file_under_both_roots``
# holds each reason.
_TASKS_OUTSIDE_THE_GRADED_CORPUS = _TASKS_WITHOUT_A_PROJECT + (
    _TEST_DATA / "terminal_bench_tasks" / "echo-hello" / "task.yaml",
    _TEST_DATA / "terminal_bench_tasks" / "echo-hello-skills" / "task.yaml",
    _TEST_DATA / "actor_binding" / "task.yaml",
)

# Every authored pack in the repository whose grading config loads: 29 under
# ``examples/``, each beneath a ``project.yaml``, and 83 project-less packs under
# ``tests/data``. Reconciled by the partition guard rather than only counted here.
_AUTHORED_PACK_COUNT = 112


def _is_a_recorded_artifact(task_yaml: Path) -> bool:
    posix = task_yaml.as_posix()
    return any(f"/{directory}/" in posix for directory in _RECORDED_ARTIFACT_DIRS)


def _authored_packs() -> list[Path]:
    """Every authored task file under both roots that loads a grading config."""
    return [
        task_yaml
        for task_yaml in sorted(_EXAMPLES.rglob("task.yaml"))
        + sorted(_TEST_DATA.rglob("task.yaml"))
        if not _is_a_recorded_artifact(task_yaml)
        and task_yaml not in _TASKS_OUTSIDE_THE_GRADED_CORPUS
    ]


def _prompt_surfaces(task_yaml: Path) -> list[str]:
    """Everything the trial puts in front of the agent before it acts.

    A grounded-claim correlation is evidence of grounding only where the token it
    binds reached the agent through the substrate — so the prompt is a second oracle,
    and a token sitting in any of these is one a note can paraphrase without having
    observed anything.
    """
    task = yaml.safe_load(task_yaml.read_text())
    user = task["actors"]["user"]
    return [
        task["initial_user_message"],
        user["persona"],
        user["backstory"],
        *task["policies"]["guidance"],
    ]


def test_the_authored_walk_partitions_every_task_file_under_both_roots() -> None:
    """The two exclusions the widened walk makes, each one falsifiable.

    A guard is only as honest as its walk, and this one drops files on two grounds: a
    recorded ``TaskDescription`` under ``output/trials`` or ``migration_corpora`` is
    not authored, and five authored files load no grading config. Both are asserted
    against what the files actually do, so the exclusion list cannot be used to park
    a pack a guard reds on — a named file that *does* load a grading config fails
    here, and so does a task file the walk drops on neither ground.
    """
    every_file = set(_EXAMPLES.rglob("task.yaml")) | set(_TEST_DATA.rglob("task.yaml"))
    recorded = {task_yaml for task_yaml in every_file if _is_a_recorded_artifact(task_yaml)}
    authored = set(_authored_packs())

    assert authored | recorded | set(_TASKS_OUTSIDE_THE_GRADED_CORPUS) == every_file
    assert authored & recorded == set()
    assert len(authored) == _AUTHORED_PACK_COUNT, (
        f"the walk found {len(authored)} authored packs, not {_AUTHORED_PACK_COUNT}. A "
        "corpus guard over a subset proves nothing about the packs it skipped"
    )
    assert len(recorded) == len(every_file) - _AUTHORED_PACK_COUNT - len(
        _TASKS_OUTSIDE_THE_GRADED_CORPUS
    )
    for task_yaml in _TASKS_OUTSIDE_THE_GRADED_CORPUS:
        assert task_yaml.exists(), f"{task_yaml} is excluded by name and does not exist"
        assert _loads_no_grading_config(task_yaml), (
            f"{task_yaml} names a grading source, so excluding it hides a pack from "
            "every guard over this walk — including one whose source is not on disk, "
            "which belongs here as a failure the guards catch"
        )


def _loads_no_grading_config(task_yaml: Path) -> bool:
    """Whether this task file reaches no grading block, for either of the two reasons.

    It loads as no :class:`TaskConfig` at all, or it loads and names no grading source.
    A file naming a source that is not on disk is neither: ``tolokaforge validate``
    refuses such a pack under the native adapter, so it belongs in this walk as a
    failure the guards catch and never in the exclusion list as a pack nothing grades.
    """
    try:
        task, _ = load_task_yaml(task_yaml)
    except ValidationError:
        return True
    return task.grading is None


def test_a_pack_naming_a_grading_file_that_is_absent_cannot_be_excluded(tmp_path: Path) -> None:
    """The exclusion list may not become the place a refused pack is parked.

    ``tolokaforge validate`` fails such a pack under the native adapter, so listing one
    here would hide a hard failure behind a walk that reports nothing — the shape this
    helper's two legitimate reasons must not be widened to cover.
    """
    dangling = tmp_path / "task.yaml"
    dangling.write_text(
        yaml.safe_dump({"task_id": "dangling", "description": "d", "grading": "grading.yaml"})
    )

    assert not _loads_no_grading_config(dangling)


def test_every_authored_pack_and_its_weight_map_name_the_same_components() -> None:
    """Both directions of the membership question, over every pack in the repo.

    A component a pack configures that the effective map never weights is not gradeable
    at all: both folds refuse a component they scored and hold no share for, rather than
    inventing one, so such a pack fails at grade time on either substrate. A weight
    naming a component the pack never configures is the same defect from the other side
    — no substrate produces that component, so the weight weighs nothing, and it turns a
    pack that asks for nothing into one claiming a component it does not have.

    Both directions are asserted because a guard that asks one of two symmetric
    questions is how 21 ``wire_probes`` fixtures carrying a stray ``state_checks``
    weight stayed invisible while the requested-but-unweighted half read green. The
    map is the **effective** one — five shipped packs declare no ``combine`` of their
    own and inherit ``llm_judge: 1.0`` from ``project.yaml``, so over the authored
    block alone this guard is red on those five on day one.

    Keyed by path rather than by task id: two ``migration_packs`` fixtures reuse the
    task ids of the ``native_shared_domain`` packs they were narrowed from, so a
    corpus keyed by id silently measures 110 of the 112.
    """
    corpus = {task_yaml: _grading_config(task_yaml)[1] for task_yaml in _authored_packs()}
    assert len(corpus) == _AUTHORED_PACK_COUNT, (
        f"the guard measured {len(corpus)} packs, not {_AUTHORED_PACK_COUNT}. A corpus "
        "guard over a subset proves nothing about the packs it skipped"
    )

    unweighted: dict[str, list[str]] = {}
    unrequested: dict[str, list[str]] = {}
    for task_yaml, grading in corpus.items():
        requested = {
            spec.name
            for spec in GRADE_COMPONENTS
            if component_requested(spec, getattr(grading, spec.config_section))
        }
        weighted = set(grading.combine.weights or {})
        pack = str(task_yaml.relative_to(_REPO))
        if missing := sorted(requested - weighted):
            unweighted[pack] = missing
        if stray := sorted(weighted - requested):
            unrequested[pack] = stray

    assert unweighted == {}, (
        "these packs configure a component the effective combine never weights, so both "
        "folds refuse the trial rather than pick a share the author never declared"
    )
    assert unrequested == {}, (
        "these packs weight a component they never configure, so no substrate produces "
        "it and the weight folds nothing"
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
_GATED_PACK_COUNT = 63

# The one pack whose tool inventory cannot be built: it declares
# ``tools.agent.mobile: true``, a typo fixture whose whole point is that a non-mapping
# init block fails loud rather than reaching trial registration as a TypeError.
_PACK_WITH_NO_INVENTORY = "bad_mobile"

# The three packs that address a tool argument below a level whose schema stops
# declaring properties — the one thing the gate declines to check over this corpus,
# and the reason it gives (#765: the walk descends exactly as far as the schema
# declares). Pinned so a weight or tool-set skip, either of which would mean a rule
# proved nothing, cannot hide among them.
_PACKS_ADDRESSING_A_NESTED_ARGUMENT = ("helpdesk_01", "lot_ops_01", "nested_binding_grading")
_NESTED_ARGUMENT_SKIP = "stops declaring properties"


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


def _effective_combine(task_yaml: Path, grading: Mapping[str, Any]) -> GradingCombineConfig:
    """The combine a pack grades under: its own block over its project's defaults.

    The weight rules read the effective map because a task declaring no ``combine`` at
    all still inherits one, and ``weights`` merges key by key — so even a pack that
    writes a ``combine`` block can be missing weights its project supplies. Handing
    the gate the authored block instead refuses the five ``example-microservices-pack``
    tasks that inherit theirs.
    """
    return resolve_effective_grading_combine(
        project_grading_combine(project_layer(task_yaml)), grading.get("combine")
    )


# A weight key naming no component at all, injected into a pack's own effective map to
# prove the weight rules were live at this tier. Rule 3's "names no grading component"
# branch is the one shape no corpus edit can ever make legitimate.
_A_WEIGHT_NAMING_NO_COMPONENT = "a_component_no_pack_configures"


def _gate_reports(
    task_yaml: Path,
    grading: Mapping[str, Any],
    inventory: ToolInventory,
    world: ReplayWorld,
    hash_sources: HashSourceLayer,
    seeded_tables: SeededTablesLayer,
) -> tuple[AuthoringReport, AuthoringReport]:
    """A pack's own gate report, and the same pack's under one weight naming nothing.

    Both come out of one resolved combine and one call site, which is what makes the
    second a positive control for the first: supplying no combine leaves the weight
    rules out of the report entirely — the gate adds no skip, because only a caller
    gating a whole pack owes that — so a clean sweep would still read clean with those
    two rules never run. The probed report is empty in exactly that case.

    The replay world, the hash layer and the seeded tables are the pack's own too,
    resolved the way the gate's callers resolve each: an unresolvable world would
    report a skip for every pack replaying golden actions, an unresolvable hash layer
    one for every hash block whose flag and source disagree, an unresolvable
    seeded-tables layer one for every pack declaring ``id_fields``, and the
    ``unchecked`` assertion below would stop saying which rules were skipped in any of
    those cases.
    """
    combine = _effective_combine(task_yaml, grading)
    probed = combine.model_copy(
        update={"weights": {**combine.weights, _A_WEIGHT_NAMING_NO_COMPONENT: 1.0}}
    )
    return (
        inspect_grading_authoring(
            grading,
            inventory,
            replay_world=world,
            hash_sources=hash_sources,
            seeded_tables=seeded_tables,
            effective_combine=combine,
        ),
        inspect_grading_authoring(
            grading,
            inventory,
            replay_world=world,
            hash_sources=hash_sources,
            seeded_tables=seeded_tables,
            effective_combine=probed,
        ),
    )


_A_FILESYSTEM_ROOTED_PATH = "$.filesystem['/env/fs/agent-visible/x.py']"
_AN_AGENT_ROOTED_PATH = "$.agent.customers[0].balance"
_A_DATABASE_ROOTED_PATH = "$.db.orders[0].status"


def _states_a_pack_addresses_but_cannot_reach(
    grading: Mapping[str, Any], seeded_tables: SeededTablesLayer
) -> tuple[list[str], bool]:
    """The paths this pack writes that the runner cannot resolve, and its absent-DB read.

    An unresolvable ``seeded_tables`` answers ``False`` for the second reading: what a
    task seeds is the one input it needs, and no pack is held to a fact nobody could
    resolve. The first reading needs nothing but the block, so it answers for every
    pack.
    """
    state_checks = grading.get("state_checks")
    if not isinstance(state_checks, Mapping):
        return [], False
    beyond_the_runner = [
        assertion["path"]
        for assertion in state_checks.get("jsonpaths") or ()
        if isinstance(assertion, Mapping) and unreachable_target(assertion) is not None
    ]
    reads_a_database_it_does_not_seed = (
        bool(
            block_addresses_the_database(state_sources_as_a_run_reads_them(state_checks))
            and seeded_tables.known
        )
        and not seeded_tables.tables
    )
    return beyond_the_runner, reads_a_database_it_does_not_seed


def test_no_shipped_pack_addresses_a_state_its_substrate_cannot_reach() -> None:
    """Every authored ``state_checks`` block reads state the trial grading it has.

    Two readings over the whole authored corpus — both task roots and the parity,
    project and migration packs outside them — because a pack failing either cannot be
    graded at all: a ``path:`` rooted anywhere but ``db`` or ``tables`` — ``filesystem``,
    ``agent``, ``user`` — resolves on the core engine and not on the runner, and a block
    reading the database of a task that seeds none reaches a DB service ``RegisterTrial``
    never registered.

    The examined population is printed rather than counted silently, and asserted
    non-empty: both residues are lists that a walk selecting nothing would leave empty
    while reading clean. The two readings are then run again over blocks written here
    to fail them, through the same function the sweep calls — so a zero above is a fact
    about the corpus rather than about a predicate that stopped discriminating.
    """
    examined: list[str] = []
    beyond_the_runner: dict[str, list[str]] = {}
    reading_an_absent_database: list[str] = []

    for task_yaml in sorted({t for t, _ in _gated_packs()} | set(_packs_outside_the_gate_walk())):
        task, task_dir = load_task_yaml(task_yaml)
        if not task.grading or not (task_dir / task.grading).is_file():
            continue
        grading = yaml.safe_load((task_dir / task.grading).read_text()) or {}
        state_checks = grading.get("state_checks")
        if not isinstance(state_checks, Mapping) or not (
            state_checks.get("jsonpaths") or state_checks.get("hash")
        ):
            continue
        pack = str(task_yaml.relative_to(_REPO))
        examined.append(pack)
        paths, absent_database = _states_a_pack_addresses_but_cannot_reach(
            grading, seeded_tables_under_adapter(task, task_dir, task.adapter_type)
        )
        if paths:
            beyond_the_runner[pack] = paths
        if absent_database:
            reading_an_absent_database.append(pack)

    print(f"packs declaring a state_checks source ({len(examined)}):\n  " + "\n  ".join(examined))
    assert examined, "the walk selected no pack declaring a state_checks source"

    assert not beyond_the_runner, (
        "a state_checks.jsonpaths path addresses state the runner does not compose, so "
        "it resolves only on the core engine — root it at db or tables, or write a file "
        f"assertion as path_glob: + contains_ci:: {beyond_the_runner}"
    )
    assert not reading_an_absent_database, (
        "a state_checks block reads the trial's database on a task whose initial_state "
        f"seeds none, so GradeTrial refuses it before a score exists: "
        f"{reading_an_absent_database}"
    )

    # ``$.filesystem[…]`` is reachable on the runner (via
    # ``_read_agent_visible_filesystem``), so it is *not* in the negative-control
    # set. The residue is ``agent`` / ``user`` / ``mock_web_url`` /
    # ``rag_corpus_dir`` — roots the core engine composes from a run's live env
    # that the runner has no equivalent for.
    probed_paths, _ = _states_a_pack_addresses_but_cannot_reach(
        {
            "state_checks": {
                "jsonpaths": [
                    {"path": _AN_AGENT_ROOTED_PATH},
                ]
            }
        },
        SeededTablesLayer.unresolvable(),
    )
    assert probed_paths == [_AN_AGENT_ROOTED_PATH]
    _, probed_absent_database = _states_a_pack_addresses_but_cannot_reach(
        {"state_checks": {"jsonpaths": [{"path": _A_DATABASE_ROOTED_PATH}]}},
        SeededTablesLayer(tables={}),
    )
    assert probed_absent_database is True


def test_the_gate_and_the_runtime_read_one_fact_about_what_a_task_seeds() -> None:
    """The gate's answer and the runtime's answer are the same answer, per shipped task.

    The gate refuses a database-reading block against ``seeded_tables.tables``;
    ``RegisterTrial`` provisions the DB service against
    :func:`~tolokaforge.runner.models.provisions_database`. A task the first calls
    seeded and the second calls unprovisioned would pass the gate and fail the run.

    **What this cannot express, stated rather than left to be inferred.** Over the
    native corpus the two are equal by construction: ``NativeAdapter`` hard-codes
    ``schemas`` and ``unstable_fields`` empty, and the core ``InitialStateConfig``
    declares neither field, so no ``task.yaml`` can express a disagreement. The
    control row below writes out the shape that can — schemas seeded, tables empty —
    and asserts the two answers parting there. It is constructed rather than loaded,
    for the same reason: it pins that the predicates disagree on that shape, not that
    any resolver produces it. The corpus half therefore reds on exactly
    one future change: the day ``NativeAdapter`` populates either field without the
    gate being revisited. That is the drift it exists to catch, and the only one.
    """
    disagreed: list[str] = []
    examined: list[str] = []
    for task_yaml in _corpus_task_files():
        if task_yaml in _TASKS_WITHOUT_A_PROJECT:
            continue
        task, task_dir = load_task_yaml(task_yaml)
        if task.adapter_type != "native":
            continue
        task_id, adapter = _pack_adapter(task_yaml)
        runtime = provisions_database(adapter.to_task_description(task_id).initial_state)
        gate = bool(seeded_tables_under_adapter(task, task_dir, "native").tables)
        examined.append(str(task_yaml.relative_to(_REPO)))
        if runtime != gate:
            disagreed.append(f"{task_yaml}: provisions_database={runtime} seeded_tables={gate}")

    assert examined, "the walk selected no native task"
    print(f"native tasks holding both answers to one fact: {len(examined)}")
    assert not disagreed, (
        "the gate and RegisterTrial disagree about whether these tasks provision a "
        f"database, so a pack the gate passes fails its run: {disagreed}"
    )

    schemas_only = RunnerInitialStateConfig(
        tables={}, schemas=[TableSchema(table_name="orders", fields={"id": "string"})]
    )
    assert provisions_database(schemas_only) is True
    assert provisions_database(schemas_only) != bool(SeededTablesLayer(tables={}).tables)


def test_no_shipped_pack_fails_the_authoring_gate() -> None:
    """The corpus proof that the gate rejects nothing that grades today.

    Each block is checked against its own task's inventory *and* its own effective
    combine, so this is the whole severity table applied to real packs: an argument
    rule that descended past the first path segment, an advisory promoted to an error,
    or a section that declares nothing shows up here as a shipped pack that no longer
    loads.

    Two things stop a clean sweep from reading clean for the wrong reason. The
    ``unchecked`` channel is asserted rather than ignored, because a rule reported
    unchecked is not a rule that passed — the corpus produces exactly one documented
    skip, an argument addressed below its first segment (#765), and both which packs
    report one and what it says are pinned. And every pack is checked a second time
    with a weight naming no component, which has to be refused: the two weight rules
    are simply absent from a report built with no combine, so without that control a
    guard that stopped resolving the layer would go on passing.
    """
    findings: dict[str, list[str]] = {}
    unchecked: dict[str, list[str]] = {}
    unprobed: list[str] = []
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
        report, probed = _gate_reports(
            task_yaml,
            grading,
            inventory,
            replay_world_under_adapter(task, task.adapter_type),
            hash_source_layer_under_adapter(task, task_dir, task.adapter_type),
            seeded_tables_under_adapter(task, task_dir, task.adapter_type),
        )
        reported = [
            f"{finding.where}: {finding.message}" for finding in report.errors + report.advisories
        ]
        if reported:
            findings[task.task_id] = reported
        if report.unchecked:
            unchecked[task.task_id] = [skip.reason for skip in report.unchecked]
        if [finding.where for finding in probed.errors] != [
            f"combine.weights.{_A_WEIGHT_NAMING_NO_COMPONENT}"
        ]:
            unprobed.append(task.task_id)

    assert len(gated) == _GATED_PACK_COUNT, (
        f"the guard checked {len(gated)} packs, not {_GATED_PACK_COUNT}. A corpus "
        "proof over a subset says nothing about the packs it skipped"
    )
    assert without_an_inventory == [_PACK_WITH_NO_INVENTORY]
    assert findings == {}
    assert unprobed == [], (
        "the gate did not refuse a weight naming no component for these packs, so the "
        "weight rules never ran here and the clean sweep above proves nothing about them"
    )
    assert sorted(unchecked) == sorted(_PACKS_ADDRESSING_A_NESTED_ARGUMENT), (
        "these packs are the ones whose blocks the gate cannot check in full, and the "
        f"list moved: {unchecked}"
    )
    assert [
        reason
        for reasons in unchecked.values()
        for reason in reasons
        if _NESTED_ARGUMENT_SKIP not in reason
    ] == [], (
        "the gate skipped a rule for a reason other than the one nested-argument "
        "limitation this corpus has — a weight rule reported unchecked here proves "
        "nothing about the 60 packs it was supposed to gate"
    )


def test_no_authored_grading_block_asserts_nothing() -> None:
    """Rule 1 over all 112 authored packs, which is 49 more than the gate walk reaches.

    ``tests/data/grading_parity``, ``tests/data/transcript_parity``,
    ``tests/data/projects`` and ``tests/data/migration_packs`` sit outside
    :func:`_gated_packs`, so without this the rule's corpus proof stops at the packs
    that happen to live under the two task roots. The inventory is deliberately
    unresolvable: the rules that need a tool set are the two gate guards' business —
    the one above for the task roots, and
    :func:`test_the_packs_outside_the_gate_walk_are_held_to_the_whole_gate` for these —
    and what is wanted here is every rule a caller holding a ``task.yaml`` can run
    without one, over the widest walk in the file.

    The ``unchecked`` assertion is what stops that from reading as a clean bill of
    health. Every pack reports exactly the one tool-set skip; a rule moved behind
    ``inventory.known`` would show up here as a second skip rather than as a silent
    loss of coverage. Each pack's real replay world, hash layer and seeded tables are
    passed for that assertion's sake: all three are resolved off the ``task.yaml``
    every caller here holds, so leaving any of them unresolvable would add a second
    skip — to the four packs that replay golden actions, to any pack whose hash flag
    and source disagree, or to every pack declaring ``id_fields`` — and say nothing
    about any rule.
    """
    findings: dict[str, list[str]] = {}
    unchecked: dict[str, list[str]] = {}
    packs = _authored_packs()

    for task_yaml in packs:
        task, task_dir = load_task_yaml(task_yaml)
        assert task.grading is not None
        grading = yaml.safe_load((task_dir / task.grading).read_text()) or {}
        report = inspect_grading_authoring(
            grading,
            ToolInventory.unresolvable(),
            replay_world=replay_world_under_adapter(task, task.adapter_type),
            hash_sources=hash_source_layer_under_adapter(task, task_dir, task.adapter_type),
            seeded_tables=seeded_tables_under_adapter(task, task_dir, task.adapter_type),
        )
        pack = str(task_yaml.relative_to(_REPO))
        if report.errors or report.advisories:
            findings[pack] = [
                f"{finding.where}: {finding.message}"
                for finding in report.errors + report.advisories
            ]
        unchecked[pack] = [skip.where for skip in report.unchecked]

    assert len(packs) == _AUTHORED_PACK_COUNT, (
        f"the guard inspected {len(packs)} blocks, not {_AUTHORED_PACK_COUNT}. A corpus "
        "proof over a subset says nothing about the packs it skipped"
    )
    assert findings == {}, (
        "these packs declare a component section that asserts nothing, so the section "
        "scores nothing and the wire cannot tell it from one the author never wrote"
    )
    assert unchecked == {pack: ["grading"] for pack in unchecked}, (
        "a block-only rule was skipped for want of a tool set, so this guard stopped "
        "checking what it reports on"
    )


# The address the golden-action name rule reports under, and a name no pack declares.
# The name is every tool-set control's sentinel: injected into a hash block here, and
# written over a matcher or a tool_expectations entry — or injected as one where the
# pack authors neither — by the whole-gate guard.
_GOLDEN_ACTION_ADDRESS = "state_checks.hash.golden_actions["
_A_TOOL_NO_ACTOR_CAN_CALL = "a_tool_no_actor_can_call"


def _golden_action_findings(report: AuthoringReport) -> list[str]:
    """Only what the golden-action name rule reported, out of the whole gate's report.

    Scoped to one rule because the guard below is a single-rule instrument: it pairs its
    walk with a control provoking this rule and nothing else, so a finding another rule
    reported would leave the sweep saying nothing about whether this one still fires.
    The whole gate over the packs beyond :func:`_gated_packs` is
    :func:`test_the_packs_outside_the_gate_walk_are_held_to_the_whole_gate`'s business.
    """
    return [
        f"{finding.where}: {finding.message}"
        for finding in report.errors + report.advisories
        if finding.where.startswith(_GOLDEN_ACTION_ADDRESS)
    ]


def _naming_a_tool_no_actor_can_call(grading: Mapping[str, Any]) -> dict[str, Any]:
    """*grading* with one more golden action, naming a tool no pack in the corpus declares.

    Injected into every pack rather than one, because only 3 of the 112 declare a golden
    action at all: a rule that stopped firing would otherwise leave this guard reading
    green over the 106 that never provoke it. The flag is written on for the same reason
    the rule reads it — a source under a falsy flag is resolved by nobody.
    """
    state_checks = {**(grading.get("state_checks") or {})}
    hash_block = {**(state_checks.get("hash") or {})}
    state_checks["hash"] = {
        **hash_block,
        "enabled": True,
        "golden_actions": [
            *(hash_block.get("golden_actions") or []),
            {"name": _A_TOOL_NO_ACTOR_CAN_CALL},
        ],
    }
    return {**grading, "state_checks": state_checks}


def test_no_authored_golden_action_names_a_tool_no_actor_can_call() -> None:
    """Every golden action in the repo resolves against the tools its task declares.

    Over all 112 authored packs, which is where the golden-action packs live: two of
    the three sit under ``tests/data/projects``, outside :func:`_gated_packs` entirely,
    so a guard over that walk would reach one of them.

    Each inventory is the pack's own, built the way the gate's callers build it. An
    unresolvable one would skip the rule on every pack and prove nothing, which is why
    the packs whose inventory will not build are collected and pinned rather than
    passed over — and why every pack is checked a second time with an action naming a
    tool no actor can call, which has to be refused. Without that control a rule that
    stopped firing would read as a corpus with no defects in it.
    """
    findings: dict[str, list[str]] = {}
    unprobed: list[str] = []
    without_an_inventory: list[str] = []
    packs = _authored_packs()

    for task_yaml in packs:
        task, task_dir = load_task_yaml(task_yaml)
        assert task.grading is not None
        grading = yaml.safe_load((task_dir / task.grading).read_text()) or {}
        try:
            inventory = build_tool_inventory(task, task_dir)
        except ValueError:
            without_an_inventory.append(task.task_id)
            continue
        pack = str(task_yaml.relative_to(_REPO))
        if reported := _golden_action_findings(inspect_grading_authoring(grading, inventory)):
            findings[pack] = reported
        probed = inspect_grading_authoring(_naming_a_tool_no_actor_can_call(grading), inventory)
        if not _golden_action_findings(probed):
            unprobed.append(pack)

    assert len(packs) == _AUTHORED_PACK_COUNT, (
        f"the guard inspected {len(packs)} blocks, not {_AUTHORED_PACK_COUNT}. A corpus "
        "proof over a subset says nothing about the packs it skipped"
    )
    assert without_an_inventory == [_PACK_WITH_NO_INVENTORY]
    assert findings == {}, (
        "these packs replay a golden action no actor of the task can call, so both "
        "substrates refuse the replay and the trial is paid for and left with no "
        "state-hash verdict"
    )
    assert unprobed == [], (
        "the gate did not refuse a golden action naming a tool no actor can call for "
        "these packs, so the rule never ran here and the clean sweep above proves nothing"
    )


# The authored packs the gate walk does not reach: the two parity roots, the recorded
# projects and the migration fixtures. Pinned so a walk that stopped finding them fails
# rather than passing over the empty set, and computed as a difference so a fifth root
# is covered the day someone adds one.
_PACKS_OUTSIDE_THE_GATE_WALK = 49

# The two addresses a name no actor can call is reported under, one per producer.
_UNCALLABLE_TOOL_ADDRESSES = ("trace_checks.", "transcript_rules.tool_expectations.")


def _packs_outside_the_gate_walk() -> list[Path]:
    """Every authored pack :func:`_gated_packs` does not reach."""
    gated = {task_yaml for task_yaml, _ in _gated_packs()}
    return [task_yaml for task_yaml in _authored_packs() if task_yaml not in gated]


def _findings_naming_the_uncallable_tool(report: AuthoringReport) -> list[str]:
    """Only what the control provoked, filtered on the sentinel in the message.

    On the name rather than on an address, because the control reaches its packs by two
    branches reporting under different addresses: an address constant answers for one
    branch and passes over the other half of the walk. The name also keeps the filter
    honest should a pack ever carry a finding of its own at the injected address, which
    an address constant would read as the control firing. Both producers quote the
    offending tool, so one predicate reads both branches.
    """
    return [
        f"{finding.where}: {finding.message}"
        for finding in report.errors + report.advisories
        if _A_TOOL_NO_ACTOR_CAN_CALL in finding.message
    ]


def _retargeting_one_tool_named(grading: Any) -> bool:
    """Point the first tool-naming site in *grading* at a tool no actor can call.

    In place and depth-first, so the site is the first one an author reads. Returns
    whether the block held one at all: half the walk authors neither a matcher nor a
    ``tool_expectations`` entry, and those packs need the other branch.
    """
    if isinstance(grading, dict):
        tool = grading.get("tool")
        if isinstance(tool, dict):
            if isinstance(tool.get("equals"), str):
                tool["equals"] = _A_TOOL_NO_ACTOR_CAN_CALL
                return True
            named = tool.get("in_")
            if isinstance(named, list) and named and isinstance(named[0], str):
                named[0] = _A_TOOL_NO_ACTOR_CAN_CALL
                return True
        expectations = grading.get("tool_expectations")
        if isinstance(expectations, dict):
            for key in ("required_tools", "disallowed_tools"):
                named = expectations.get(key)
                if isinstance(named, list) and named and isinstance(named[0], str):
                    named[0] = _A_TOOL_NO_ACTOR_CAN_CALL
                    return True
        return any(_retargeting_one_tool_named(value) for value in grading.values())
    if isinstance(grading, list):
        return any(_retargeting_one_tool_named(value) for value in grading)
    return False


def _naming_a_tool_no_actor_can_call_where_the_pack_looks(
    grading: Mapping[str, Any],
) -> dict[str, Any]:
    """*grading* with one tool no actor can call, wherever this pack can hold one.

    A pack carrying a matcher or a ``tool_expectations`` entry has that site retargeted,
    which is what puts the trace rule under the control. The 23 carrying neither have a
    ``disallowed_tools`` entry injected instead — beside whatever the block already
    declares, the way :func:`_naming_a_tool_no_actor_can_call` injects a golden action.
    Mutating alone would leave half the walk with no control at all.
    """
    retargeted = deepcopy(dict(grading))
    if _retargeting_one_tool_named(retargeted):
        return retargeted
    rules = {**(grading.get("transcript_rules") or {})}
    expectations = {**(rules.get("tool_expectations") or {})}
    expectations["disallowed_tools"] = [
        *(expectations.get("disallowed_tools") or []),
        _A_TOOL_NO_ACTOR_CAN_CALL,
    ]
    rules["tool_expectations"] = expectations
    return {**grading, "transcript_rules": rules}


def test_the_packs_outside_the_gate_walk_are_held_to_the_whole_gate() -> None:
    """The parity roots and their neighbours face every rule, not only the block-only ones.

    :func:`_gated_packs` stops at the two task roots, which leaves the 49 packs under
    ``grading_parity``, ``transcript_parity``, ``tests/data/projects`` and
    ``tests/data/migration_packs`` reached only by guards scoped to a single rule
    apiece. Here each faces the whole gate through :func:`_gate_reports` — the same
    instrument the gated walk runs, over the half of the corpus that walk does not
    reach — against **its own** inventory, replay world, hash layer, seeded tables and
    effective combine, built the way ``tolokaforge validate``'s caller builds them. So a
    fixture whose grading names a tool its task never declares is refused here, before
    anyone runs ``validate`` and before a trial is paid for.

    The residue is asserted empty rather than pinned to whatever comes back: every one of
    these packs declares the schemas of the arguments it addresses, so a skip appearing
    here is a fixture that stopped doing so.

    Closure is asserted beside the residue because ``fixtures/tools.json`` is a cache: a
    pack whose committed file goes missing has it regenerated from the pack's own server,
    and the servers never emit ``additionalProperties``. The regenerated schema checks
    the same argument names at advisory tier instead of refusing them, which moves
    nothing in a report whose addressed arguments are all present — so neither the sweep
    nor the residue can see that happen, and this list can. A tool whose schema does not
    resolve read-only at all is a different state and stays legitimate here.

    Two controls, because the sweep answers for two families of rule. Every pack is
    checked again with a tool no actor can call, written wherever that pack can hold
    one — the 23 authoring neither a matcher nor a ``tool_expectations`` entry would
    otherwise sit inside a walk that proves nothing about them — and again with a weight
    naming no component, which :func:`_gate_reports` supplies from the same resolved
    combine: hand the gate no combine and the two weight rules are absent from the
    report entirely, so a sweep that stopped resolving the layer would go on reading
    clean over rules that never ran.
    """
    findings: dict[str, list[str]] = {}
    advisories: dict[str, list[str]] = {}
    unchecked: dict[str, list[str]] = {}
    opened: list[str] = []
    unprobed_tools: list[str] = []
    unprobed_weights: list[str] = []
    misaddressed: list[str] = []
    packs = _packs_outside_the_gate_walk()

    for task_yaml in packs:
        task, task_dir = load_task_yaml(task_yaml)
        assert task.grading is not None
        grading = yaml.safe_load((task_dir / task.grading).read_text()) or {}
        inventory = build_tool_inventory(task, task_dir)
        world = replay_world_under_adapter(task, task.adapter_type)
        layer = hash_source_layer_under_adapter(task, task_dir, task.adapter_type)
        tables = seeded_tables_under_adapter(task, task_dir, task.adapter_type)
        pack = str(task_yaml.relative_to(_REPO))

        report, weighted = _gate_reports(task_yaml, grading, inventory, world, layer, tables)
        if report.errors:
            findings[pack] = [f"{f.where}: {f.message}" for f in report.errors]
        if report.advisories:
            advisories[pack] = [f"{f.where}: {f.message}" for f in report.advisories]
        if report.unchecked:
            unchecked[pack] = [f"{skip.where}: {skip.reason}" for skip in report.unchecked]
        opened += [
            f"{pack}:{tool}"
            for tool in sorted(inventory.declared)
            if inventory.strictness(tool) is ArgumentSchema.OPEN
        ]
        if [finding.where for finding in weighted.errors] != [
            f"combine.weights.{_A_WEIGHT_NAMING_NO_COMPONENT}"
        ]:
            unprobed_weights.append(pack)

        probed = inspect_grading_authoring(
            _naming_a_tool_no_actor_can_call_where_the_pack_looks(grading),
            inventory,
            replay_world=world,
            hash_sources=layer,
            seeded_tables=tables,
        )
        reported = _findings_naming_the_uncallable_tool(probed)
        if not reported:
            unprobed_tools.append(pack)
        misaddressed += [
            reported_finding
            for reported_finding in reported
            if not reported_finding.startswith(_UNCALLABLE_TOOL_ADDRESSES)
        ]

    assert len(packs) == _PACKS_OUTSIDE_THE_GATE_WALK, (
        f"the guard inspected {len(packs)} packs, not {_PACKS_OUTSIDE_THE_GATE_WALK}. A "
        "corpus proof over a subset says nothing about the packs it skipped"
    )
    assert findings == {}, (
        "these packs cannot be graded as written: the gate refuses the block against the "
        "tool set the pack's own task declares, so every trial they grade is paid for "
        "and lost"
    )
    assert advisories == {}, (
        "the gate reported a probable defect in these packs, and a corpus that ships one "
        "teaches the shape it reports"
    )
    assert unchecked == {}, (
        "the gate could not check a rule for these packs — an addressed argument whose "
        "schema no longer resolves — so the sweep above passed over what it reports on"
    )
    assert opened == [], (
        "a schema in this walk stopped closing its argument set, so an argument the "
        "gate would refuse is now only advised about and the sweep above reads green"
    )
    assert unprobed_tools == [], (
        "the gate did not refuse a tool no actor can call for these packs, so the rule "
        "never ran here and the clean sweep above proves nothing about them"
    )
    assert unprobed_weights == [], (
        "the gate did not refuse a weight naming no component for these packs, so the "
        "weight rules never ran here and the clean sweep above proves nothing about them"
    )
    assert misaddressed == [], (
        "a control finding quoted the sentinel from an address neither the trace rule "
        f"nor the tool-expectation rule owns: {misaddressed}"
    )


# The address the replay-world rule reports the whole block under, distinct from the
# per-action ``…[i].name`` the name rule uses, so scoping to one is scoping to one rule.
_GOLDEN_REPLAY_WORLD_ADDRESS = "state_checks.hash.golden_actions"


def _replay_world_findings(report: AuthoringReport) -> list[str]:
    """Only what the replay-world rule reported, out of the whole gate's report.

    Scoped for the reason :func:`_golden_action_findings` gives: one rule, one control,
    so the sweep answers for the rule its control provokes.
    """
    return [
        f"{finding.where}: {finding.message}"
        for finding in report.errors + report.advisories
        if finding.where == _GOLDEN_REPLAY_WORLD_ADDRESS
    ]


def _a_golden_replay_with_no_world(grading: Mapping[str, Any]) -> dict[str, Any]:
    """*grading* with a golden path to replay, whatever else it declares.

    Injected into every pack rather than one, because only 3 of the 112 declare a golden
    action at all. ``golden_actions`` is the only hash source needing a world, and it is
    the source the rule reads, so a pack reaches the rule on this block alone — including
    ``tests/data/grading_parity/all_keys``, the one pack in the corpus whose world cannot
    be built.
    """
    state_checks = {**(grading.get("state_checks") or {})}
    hash_block = {**(state_checks.get("hash") or {})}
    state_checks["hash"] = {
        **hash_block,
        "enabled": True,
        "golden_actions": [*(hash_block.get("golden_actions") or []), {"name": "write_file"}],
    }
    return {**grading, "state_checks": state_checks}


def test_no_authored_pack_gives_its_golden_replay_no_world_to_be_built_in() -> None:
    """Every pack that replays a golden path is authored against a task supplying one.

    Over all 112 authored packs, each against **its own** replay world resolved the way
    the gate's callers resolve it, because the facts the rule reads live in ``task.yaml``
    rather than in the block. The tool inventory is deliberately unresolvable: this rule
    reads no tool, so resolving one would decide nothing here and would couple this
    sweep to a rule its control does not provoke.

    Every pack is checked a second time with its MCP server module withheld and a golden
    action injected, which has to be refused — without that control a rule that stopped
    firing would read as a corpus with no defects in it, since 109 of the 112 replay
    nothing.

    The unresolvable-world arm is **not** exercised here and cannot be: every authored
    pack in the repository is native, so every world resolved below is ``known``. That
    branch is carried by
    ``tests/unit/grading/test_grading_authoring_gate.py::test_a_world_no_caller_resolved_leaves_the_golden_replay_unchecked``
    alone.
    """
    findings: dict[str, list[str]] = {}
    unprobed: list[str] = []
    packs = _authored_packs()

    for task_yaml in packs:
        task, task_dir = load_task_yaml(task_yaml)
        assert task.grading is not None
        grading = yaml.safe_load((task_dir / task.grading).read_text()) or {}
        world = replay_world_under_adapter(task, task.adapter_type)
        pack = str(task_yaml.relative_to(_REPO))
        assert world.known, f"{pack} resolved no replay world, so it proves nothing here"
        report = inspect_grading_authoring(
            grading, ToolInventory.unresolvable(), replay_world=world
        )
        if reported := _replay_world_findings(report):
            findings[pack] = reported
        probed = inspect_grading_authoring(
            _a_golden_replay_with_no_world(grading),
            ToolInventory.unresolvable(),
            replay_world=replace(world, mcp_server=False),
        )
        if not _replay_world_findings(probed):
            unprobed.append(pack)

    assert len(packs) == _AUTHORED_PACK_COUNT, (
        f"the guard inspected {len(packs)} blocks, not {_AUTHORED_PACK_COUNT}. A corpus "
        "proof over a subset says nothing about the packs it skipped"
    )
    assert findings == {}, (
        "these packs replay a golden path their task gives no world to be built in, so "
        "core refuses to grade the trial at all once it is already paid for"
    )
    assert unprobed == [], (
        "the gate did not refuse a golden replay with no MCP server module to call for "
        "these packs, so the rule never ran here and the clean sweep above proves nothing"
    )


# The address the state-source exclusivity rule reports under, and a probe injected
# beside whatever each pack already declares as the positive control.
_PROBES_ADDRESS = "state_checks.db_probes"
_AN_INJECTED_PROBE = {
    "name": "a_probe_no_pack_declares",
    "dsn": "postgresql://grader:grader_pw@app-db:5432/probe",
    "query": "SELECT 1 AS present",
    "expect": [{"path": "$.row_count", "equals": 1}],
}

# How many of the 112 declare a state source the fold also scores, so the control's two
# arms cannot silently collapse into one: 26 packs where injecting a probe must be
# refused, and 86 where it must not, because the injection leaves them probe-only.
_PACKS_DECLARING_A_FOLD_SCORED_STATE_SOURCE = 26


def _probe_exclusivity_findings(report: AuthoringReport) -> list[str]:
    """Only what the state-source exclusivity rule reported, out of the whole report.

    Scoped for the reason :func:`_golden_action_findings` gives: one rule, one control,
    so the sweep answers for the rule its control provokes.
    """
    return [
        f"{finding.where}: {finding.message}"
        for finding in report.errors + report.advisories
        if finding.where == _PROBES_ADDRESS
    ]


def _declares_a_state_source_the_fold_scores(grading: Mapping[str, Any]) -> bool:
    """Whether the pack already declares a state source a probe could not share with.

    Written out here rather than read off the rule, so the control's expectation and the
    rule are two sources: a non-empty ``jsonpaths``, or a ``hash`` block that is enabled
    and names something to compare against.
    """
    state_checks = grading.get("state_checks")
    if not isinstance(state_checks, Mapping):
        return False
    hash_block = state_checks.get("hash")
    if not isinstance(hash_block, Mapping):
        hash_block = {}
    return bool(state_checks.get("jsonpaths")) or bool(
        hash_block.get("enabled") and any(hash_block.get(key) for key in HASH_SOURCE_KEYS)
    )


def _a_probe_beside_whatever_the_pack_declares(grading: Mapping[str, Any]) -> dict[str, Any]:
    """*grading* with one more ``db_probes`` entry and every other source left as written.

    Injected into every pack rather than one, because 3 of the 112 declare a probe at all.
    Nothing else is touched, which is what splits the walk: a pack already declaring a
    source the fold scores becomes the refused shape, and a pack declaring none — 83 of
    them — becomes a probe-only block, which is the shape this rule exists to leave alone.
    """
    state_checks = {**(grading.get("state_checks") or {})}
    state_checks["db_probes"] = [*(state_checks.get("db_probes") or []), _AN_INJECTED_PROBE]
    return {**grading, "state_checks": state_checks}


def test_no_authored_pack_declares_a_probe_beside_another_state_source() -> None:
    """No shipped pack declares a probe beside a state source the fold also scores.

    Over all 112 authored packs: the probe packs sit under ``examples/native`` and
    ``tests/data/tasks``, and the packs carrying the sources they may not join are spread
    across both roots and ``tests/data/grading_parity``, which is outside
    :func:`_gated_packs` entirely.

    The control **splits on what each pack already declares**, because the rule is a
    boundary rather than a refusal of the key: injecting a probe into a pack with a
    non-empty ``jsonpaths`` or an enabled hash over a source has to be refused, and
    injecting one into a pack declaring neither has to be admitted — that block is
    probe-only, which is exactly what a probe pack ships. A control that injected blindly
    and expected a finding everywhere would assert the opposite of the rule on 86 of the
    112. Both arms are collected, and the size of the refused arm is pinned so a corpus
    that stopped declaring hash and JSONPath sources could not leave the positive arm
    vacuous.

    The tool inventory is deliberately unresolvable: this rule reads no tool, so
    resolving one would decide nothing here and would couple this sweep to a rule its
    control does not provoke.
    """
    findings: dict[str, list[str]] = {}
    unprobed: list[str] = []
    refused_a_probe_only_block: list[str] = []
    with_a_source_of_their_own: list[str] = []
    packs = _authored_packs()

    for task_yaml in packs:
        task, task_dir = load_task_yaml(task_yaml)
        assert task.grading is not None
        grading = yaml.safe_load((task_dir / task.grading).read_text()) or {}
        world = replay_world_under_adapter(task, task.adapter_type)
        pack = str(task_yaml.relative_to(_REPO))
        report = inspect_grading_authoring(
            grading, ToolInventory.unresolvable(), replay_world=world
        )
        if reported := _probe_exclusivity_findings(report):
            findings[pack] = reported
        probed = _probe_exclusivity_findings(
            inspect_grading_authoring(
                _a_probe_beside_whatever_the_pack_declares(grading),
                ToolInventory.unresolvable(),
                replay_world=world,
            )
        )
        declares_another_source = _declares_a_state_source_the_fold_scores(grading)
        if declares_another_source:
            with_a_source_of_their_own.append(pack)
        if declares_another_source and not probed:
            unprobed.append(pack)
        if probed and not declares_another_source:
            refused_a_probe_only_block.append(pack)

    assert len(packs) == _AUTHORED_PACK_COUNT, (
        f"the guard inspected {len(packs)} blocks, not {_AUTHORED_PACK_COUNT}. A corpus "
        "proof over a subset says nothing about the packs it skipped"
    )
    assert findings == {}, (
        "these packs declare db_probes beside a state source the fold also scores, so one "
        "state_checks component holds two verdicts and each substrate discards a different "
        "one"
    )
    assert len(with_a_source_of_their_own) == _PACKS_DECLARING_A_FOLD_SCORED_STATE_SOURCE, (
        f"{len(with_a_source_of_their_own)} packs declare a state source a probe may not "
        f"join, not {_PACKS_DECLARING_A_FOLD_SCORED_STATE_SOURCE}, so the positive arm "
        "below covers a different corpus than the one measured"
    )
    assert unprobed == [], (
        "the gate did not refuse a probe injected beside these packs' own state source, so "
        "the rule never ran here and the clean sweep above proves nothing"
    )
    assert refused_a_probe_only_block == [], (
        "the gate refused a probe-only block on these packs, which is the shape every "
        "probe pack in the repository ships — the rule is refusing the key rather than its "
        "co-occurrence with another source"
    )


def test_the_two_project_less_task_files_are_the_terminal_bench_pair() -> None:
    """A native pack losing its project layer would otherwise drop out unnoticed."""
    orphans = tuple(
        task_yaml
        for task_yaml in sorted(_EXAMPLES.rglob("task.yaml"))
        if enclosing_project(task_yaml) is None
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


def _helpdesk_grade(calls: Sequence[RecordedToolCall]) -> Grade:
    """The whole fold over one trajectory, at the pack's own weights.

    No submission, because core reads none of this pack's state: ``jsonpaths`` is empty
    and ``db_probes`` is RUNNER_ONLY, so ``trace_checks`` is the only component core
    scores and the fold is that component alone.
    """
    return GradingEngine(_helpdesk_grading()).grade_trajectory(
        make_trajectory(
            task_id="helpdesk_01",
            messages=make_trial_messages(calls, _HELPDESK_TURNS),
            tool_log=list(calls),
        ),
        {},
    )


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

# The user turn paraphrases the task's own `initial_user_message`, which names no
# stale status value — the pack's grounded-claim checks rest on the prompt being a
# second oracle the note cannot copy the token out of.
_CACHE_DEBUG_TURNS = (
    "order 4021 still shows an out-of-date status to customers",
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

# Each route's own grounded-claim check, by the route that carries it. No single read
# is common to both routes, so the check is per route rather than shared — and a claim
# over both is a claim no trial decides.
_GROUNDED_CLAIM_CHECKS = {
    "divergence_between_the_api_layers": "the_note_quotes_the_value_the_served_read_returned",
    "divergence_against_the_cache": "the_note_quotes_the_value_the_cache_held",
}

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
# The realistic ungrounded note: the mechanism recited, and the symptom restated in
# the terms the on-call engineer reported it in. Nothing here is avoided by
# construction — the note is free to reuse every word of the prompt, and the prompt
# names no stale status value, so reproducing the token still takes having read it.
_UNGROUNDED_NOTE_TEXT = (
    "order 4021 is still showing an out-of-date status to customers even though our "
    "records say it shipped: order:4021 is never invalidated on a status update, so the "
    "cache-first read keeps serving the stale value"
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
    fold at the pack's old weights it scored ``(0.9333, True)`` on 2 of 3
    ``required_actions`` and ``(0.95, True)`` on 3 of 4 rule rows: docked for a route
    the task never required, which is what moved the weights rather than anything
    this pack says.
    """
    result = _cache_debug_result(calls)
    assert result.score == pytest.approx(1.0)
    assert result.winning_path == winning_path
    assert _failed(result) == []


@pytest.mark.parametrize(("calls", "winning_path"), _ROUTES_IN_FULL)
def test_only_the_winning_routes_grounded_claim_check_reaches_a_trials_verdicts(
    calls: Sequence[RecordedToolCall], winning_path: str
) -> None:
    """The two grounded-claim checks are never decided on one trial, which is why a
    ``migration.yaml`` naming both is refused at load (``_route_span_rejection``).

    ``tolokaforge reconcile`` recomputes a trial's constraint verdicts as
    ``{constraint.id: constraint for constraint in result.constraints}`` — the scored decision
    set, which is the shared constraints plus the winning route's — so a conjunction over one
    id from each route has no verdict for one of them whichever route the trial took.
    """
    verdicts = {constraint.id: constraint for constraint in _cache_debug_result(calls).constraints}

    assert [check for check in _GROUNDED_CLAIM_CHECKS.values() if check in verdicts] == [
        _GROUNDED_CLAIM_CHECKS[winning_path]
    ]


def test_the_cache_debug_prompt_names_no_status_its_grounded_claim_binds() -> None:
    """The prompt is the pack's second oracle, and it must not hold the answer.

    The on-call engineer reports an out-of-date status and does not know which one, so
    a note paraphrasing the symptom report cannot reproduce the token — which is the
    whole reason reproducing it is evidence the agent read a layer.

    The other three assertions are what stop this from passing vacuously on a pack
    whose reads do not carry the token either: the cached read the binder resolves
    over does return it, the reference-style note quotes it, and the ungrounded note
    does not. So there is something for the prompt to have leaked, and the check
    separates the two notes on it.
    """
    stale_status = _STALE_ORDER["status"]

    assert [text for text in _prompt_surfaces(_CACHE_DEBUG_TASK) if stale_status in text] == []
    assert stale_status in json.dumps(_CACHE_DEBUG_PAYLOADS[_CACHED])
    assert stale_status in _NOTE_TEXT
    assert stale_status not in _UNGROUNDED_NOTE_TEXT


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
            messages=make_trial_messages(_MUTATING_RUN, _CACHE_DEBUG_TURNS),
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


# The submission the pack's jsonpath check reads, so a fold sees the deterministic
# components a real grade was made of rather than a stub.
_LOT_OPS_SUBMISSION = {
    "filesystem": {"/env/fs/agent-visible/submissions/report.md": "CAPA-01 on LOT-1007"}
}


def _lot_ops_trajectory(calls: Sequence[RecordedToolCall]) -> Trajectory:
    return make_trajectory(
        task_id="lot_ops_01",
        messages=make_trial_messages(calls, _LOT_OPS_TURNS),
        tool_log=list(calls),
    )


def _lot_ops_grade(calls: Sequence[RecordedToolCall]) -> Grade:
    """The whole fold over one trajectory, at the pack's own weights."""
    return GradingEngine(_lot_ops_grading()).grade_trajectory(
        _lot_ops_trajectory(calls), _LOT_OPS_SUBMISSION
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


def test_the_lot_ops_prompt_names_no_reason_code_its_correlation_binds() -> None:
    """The other side of the same discipline: the catalog is the only place the code is.

    The operator persona is told not to volunteer the reason code and the guidance says
    not to guess it, so ``CAPA-01`` reaches the agent only out of a tool result. Were it
    in the prompt, an agent that posted it from the request would satisfy the
    correlation having read nothing — and the substrate probe, which grades the row
    that exists, would show nothing wrong.
    """
    code = _REASON_CODES[0]["code"]

    assert [text for text in _prompt_surfaces(_LOT_OPS_TASK) if code in text] == []
    assert code in json.dumps(_REASON_CODES)


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

    And the skip is filed only against kinds nothing scored. Both constraints here are
    flat ``before``s that took a verdict, and the component is ``0.0`` *because* they
    failed — a grade reporting ``before`` as skipped in the same breath is the
    accounting dishonesty pointing the other way, and it is what the ledger's
    "contributed nothing" contract rules out.
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
    assert result.score == 0.0
    audit = audit_accounted_keys(_lot_ops_grading(), result.accounted_keys)
    assert "trace_checks" not in (audit.error or "")
    assert audit.skip_notes == ()


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
    assert guessed.components.state_checks is None
    assert grounded.components.state_checks is None
    assert guessed.components.llm_judge is None
    assert grounded.components.llm_judge is None
    assert guessed.score < grounded.score


# Every core-side verdict the CHANGELOG's compatibility notice names for the two
# shipped ``examples/**`` packs, folded through the real ``GradingEngine`` at each
# pack's own weights and its own committed scenarios. The notice is a published
# surface and the numbers in it are what an operator compares a stored score
# against, so a fold that moved without the notice moving is a silent break.
#
# Both packs' core-side fold is carried entirely by ``trace_checks``:
# ``state_checks.db_probes`` is RUNNER_ONLY, so core evaluates none of it, and core
# assigns no ``llm_judge`` component at all — which is why each verdict below equals
# its trial's ``trace_checks`` score rather than a blend of three.
_PUBLISHED_CORE_VERDICTS = (
    pytest.param(_lot_ops_grade, _LOT_OPS_CORRECT_RUN, 1.0, True, id="lot_ops_grounded_process"),
    pytest.param(_lot_ops_grade, _GUESSED_CODE_RUN, 0.5, True, id="lot_ops_guessed_reason_code"),
    pytest.param(
        _lot_ops_grade, _FABRICATED_CODE_RUN, 0.5, True, id="lot_ops_code_outside_the_catalog"
    ),
    pytest.param(_lot_ops_grade, _UNREAD_LOT_RUN, 0.5, True, id="lot_ops_lot_never_read"),
    pytest.param(_lot_ops_grade, _DOUBLE_POST_RUN, 0.0, False, id="lot_ops_duplicate_gate_tripped"),
    pytest.param(_lot_ops_grade, _NO_ACTION_RUN, 0.0, False, id="lot_ops_no_action_opened"),
    pytest.param(_lot_ops_grade, (), 0.0, False, id="lot_ops_empty_trajectory"),
    pytest.param(_helpdesk_grade, _POLICY_CORRECT_RUN, 1.0, True, id="helpdesk_correct_process"),
    *(
        pytest.param(_helpdesk_grade, param.values[0], 2 / 3, True, id=f"helpdesk_{param.id}")
        for param in _WRONG_PROCESS_RUNS
    ),
    pytest.param(_helpdesk_grade, (), 0.0, False, id="helpdesk_empty_trajectory"),
)


@pytest.mark.parametrize(("grade_pack", "calls", "score", "binary_pass"), _PUBLISHED_CORE_VERDICTS)
def test_each_published_core_verdict_folds_to_the_number_the_notice_names(
    grade_pack: Callable[[Sequence[RecordedToolCall]], Grade],
    calls: Sequence[RecordedToolCall],
    score: float,
    binary_pass: bool,
) -> None:
    """The compatibility notice's own numbers, measured rather than asserted in prose.

    Both the score and the verdict, because the two move independently: the no-action
    run is the row where the verdict flipped, and the wrong-process rows are rows where
    the score moved and the verdict did not.
    """
    grade = grade_pack(calls)

    assert (grade.score, grade.binary_pass) == (pytest.approx(score), binary_pass)


def test_the_lot_ops_wrong_process_runs_pass_by_exactly_their_threshold() -> None:
    """No margin at all, which is the property that makes this pack's fold fragile.

    ``0.5`` is ``pass_threshold`` to the digit, admitted only because the comparison is
    ``>=``. Any reweighting, or one more constraint in the block, moves these three
    trials from pass to fail — so the equality is pinned here rather than left implicit
    in the rows above, where ``0.5`` beside ``True`` reads like a comfortable pass.
    """
    threshold = _lot_ops_grading().combine.pass_threshold
    wrong_process = (_GUESSED_CODE_RUN, _FABRICATED_CODE_RUN, _UNREAD_LOT_RUN)

    scores = [_lot_ops_grade(calls).score for calls in wrong_process]

    assert scores == [pytest.approx(threshold)] * len(wrong_process)
    assert all(_lot_ops_grade(calls).binary_pass for calls in wrong_process)


def _reload_from_bundle(trial_dir: Path) -> Trajectory:
    """The trajectory a grader gets from a bundle on disk, and nothing else.

    Both halves come off the filesystem — the message view from ``trajectory.yaml``,
    the tool-call record from ``tool_log.yaml`` — so what this returns is whatever
    the writer actually persisted. Modelling a bundle from a test helper's view of
    it instead is what made two earlier measurements of this wrong: the helper
    omitted the ``role: tool`` messages the writer keeps.
    """
    persisted = yaml.safe_load((trial_dir / "trajectory.yaml").read_text(encoding="utf-8"))
    record, _ = read_recorded_tool_log(trial_dir)
    return Trajectory.model_validate({**persisted, "tool_log": record})


def test_the_lot_ops_correct_run_regrades_from_its_own_bundle_to_the_live_verdict(
    tmp_path: Path,
) -> None:
    """A trial bundle carries the grader's view of the trial, not only the agent's.

    The pack's flagship correlation reads ``status: {equals: success}``, which no
    message can express — so with the record left out of the bundle this same
    trajectory scores ``0.5``, its correct process reported as *"the trial records
    no status at positions 4, 6"*. That is a replay blaming the author for evidence
    the harness declined to write down, on the one pack the milestone built to show
    the feature working.
    """
    calls = _LOT_OPS_CORRECT_RUN
    trial_dir = tmp_path / "trials" / "lot_ops_01" / "0"
    FileArtifactWriter().write_trial_bundle(
        trial_dir,
        _lot_ops_trajectory(calls),
        {"task_id": "lot_ops_01", "trial_index": 0},
        {},
        StructuredLogger("lot_ops_01-0"),
    )

    reloaded = _reload_from_bundle(trial_dir)
    timeline = build_trial_timeline(
        reloaded.messages, reloaded.tool_log, reloaded.termination_reason
    )
    trace_checks = _lot_ops_grading().trace_checks
    assert trace_checks is not None
    result = evaluate_trace_checks(timeline, trace_checks)

    assert result.score == pytest.approx(1.0)
    assert result.gate_failed is False
    assert _failed(result) == []
    assert timeline.records_present is True


_NOTES_TASK = (
    _EXAMPLES
    / "native"
    / "native_shared_domain"
    / "dataset"
    / "notes"
    / "testcases"
    / "add_note_duplicate_check_gated"
    / "task.yaml"
)

# What the pack is expected to declare, written out here so the block, the weight map
# and the criterion the rubric keeps are each compared against a second source. The
# weight map is a compatibility surface for this pack — a trial's score is not
# comparable across a change to it — so it is pinned rather than read.
_NOTES_GATE = "the_notes_were_listed_before_the_note_was_added"
_NOTES_WEIGHTS = {"llm_judge": 0.7, "trace_checks": 0.3}
_NOTES_VETO_CRITERION = "checked_duplicates_first"
_NOTES_SCORED_CRITERIA = ("note_saved", "clarity")

_NOTES_PROMPT = (
    'Please save a note titled "Team standup" with the body '
    '"Daily standup at 9:30 in the Maple room."'
)
_THE_EXISTING_NOTES = (
    '[{"id": "N-002", "title": "Team stand-up", "body": "Daily stand-up at 9:30"}]'
)


def _listed(sequence: int) -> RecordedToolCall:
    return recorded_call("list_notes", sequence=sequence, output=_THE_EXISTING_NOTES)


def _added(sequence: int) -> RecordedToolCall:
    return recorded_call(
        "add_note",
        sequence=sequence,
        arguments={"title": "Team standup", "body": "Daily standup at 9:30 in the Maple room."},
        output='{"id": "N-003"}',
    )


@dataclass(frozen=True)
class _NotesScenario:
    """One trial of the pack, and what each of its two vetoes says about it.

    ``warned`` is *supplied* rather than read out of ``reply``, because it is a judge's
    reading of the reply — which is precisely the half of the policy this pack keeps in
    its rubric. The trace gate's verdict is never supplied: it is the shipped
    evaluator's over ``calls``, which is the other half.
    """

    name: str
    calls: tuple[RecordedToolCall, ...]
    reply: str
    warned: bool
    judge_gate_failed: bool
    failed_trace_gates: tuple[str, ...]
    judge_component: float
    score: float
    binary_pass: bool


_NOTES_SCENARIOS = (
    _NotesScenario(
        name="listed_and_warned",
        calls=(_listed(0), _added(1)),
        reply='A similar note "Team stand-up" already exists; saving yours as asked.',
        warned=True,
        judge_gate_failed=False,
        failed_trace_gates=(),
        judge_component=1.0,
        score=1.0,
        binary_pass=True,
    ),
    _NotesScenario(
        name="listed_but_never_warned",
        calls=(_listed(0), _added(1)),
        reply="Saved your note.",
        warned=False,
        judge_gate_failed=True,
        failed_trace_gates=(),
        judge_component=0.0,
        score=0.3,
        binary_pass=False,
    ),
    _NotesScenario(
        name="never_listed",
        calls=(_added(0),),
        reply='A similar note "Team stand-up" already exists; saving yours as asked.',
        warned=True,
        judge_gate_failed=False,
        failed_trace_gates=(_NOTES_GATE,),
        judge_component=1.0,
        score=0.7,
        binary_pass=False,
    ),
)


def _notes_grading() -> GradingConfig:
    return _grading_config(_NOTES_TASK)[1]


def _notes_submission(warned: bool) -> dict[str, object]:
    """A judge ``submit_report`` over the pack's three criteria.

    Put through ``parse_submit_report`` rather than written as ``CriterionResult``s so
    the binary criterion's ``{0.0, 1.0}`` derivation and the marker-consistency check
    are production's rather than this table's.
    """
    submitted: dict[str, object] = {
        _NOTES_VETO_CRITERION: warned,
        f"{_NOTES_VETO_CRITERION}_justification": (
            f"What the reply told the user.\nVERDICT: {'MET' if warned else 'NOT MET'}"
        ),
    }
    for criterion_id in _NOTES_SCORED_CRITERIA:
        submitted[criterion_id] = 1.0
        submitted[f"{criterion_id}_justification"] = "In full.\nSCORE: 1.0"
    return submitted


def _notes_runner_config(grading: GradingConfig) -> dict[str, object]:
    """The pack's ``combine`` in the flat shape the runner's fold takes it in.

    Each component's section rides along as an empty mapping: the fold reads a section
    only to tell a *configured* component from a merely weighted one, and never a field
    of it.
    """
    return {
        "combine_method": grading.combine.method,
        "weights": dict(grading.combine.weights),
        "pass_threshold": grading.combine.pass_threshold,
        COMPONENT_BY_NAME["llm_judge"].config_section: {},
        COMPONENT_BY_NAME["trace_checks"].config_section: {},
    }


def test_the_notes_pack_declares_one_shared_gate_and_weights_both_components() -> None:
    """The gate is shared and gating, and the judge no longer carries the pack alone.

    Shared rather than route-scoped because a route gate is consulted only when its own
    route wins, and this veto has to hold whichever way the trial went. Neither
    component reaches ``pass_threshold`` by itself — measured here rather than asserted,
    since that is what makes a passing trial one that did both halves of the policy.
    """
    grading = _notes_grading()
    trace_checks = grading.trace_checks
    assert trace_checks is not None

    declared = [(constraint.id, constraint.severity) for constraint in trace_checks.constraints]
    assert declared == [(_NOTES_GATE, TraceConstraintSeverity.GATE)]
    assert trace_checks.alternatives is None
    assert grading.combine.weights == _NOTES_WEIGHTS
    assert max(grading.combine.weights.values()) < grading.combine.pass_threshold


@pytest.mark.parametrize(
    "scenario", _NOTES_SCENARIOS, ids=[scenario.name for scenario in _NOTES_SCENARIOS]
)
def test_each_half_of_the_notes_policy_is_vetoed_by_the_mechanism_that_can_see_it(
    scenario: _NotesScenario,
) -> None:
    """The attribution one conjoined criterion could not make.

    Three trials, and the two veto columns disagree on two of them: a trial that listed
    but never warned fails the judge's required gate with no trace gate down, and one
    that never listed fails the trace gate with the judge's own gate up. Both are a
    failed trial — so ``binary_pass`` alone would not tell them apart, and the columns
    are asserted separately for that reason.

    ``judge_component`` is the third column that matters: the graded criteria score in
    full on every row, so the ``0.0`` on the middle row is the veto zeroing the
    component and not a low weighted average. That zeroing is the runner fold's, which
    is why the fold is what this drives rather than the aggregate alone.
    """
    grading = _notes_grading()
    trace_checks = grading.trace_checks
    assert trace_checks is not None
    assert grading.llm_judge is not None
    rubric = grading.llm_judge.rubric
    assert rubric is not None

    trace = evaluate_trace_checks(
        _timeline(scenario.calls, (_NOTES_PROMPT, scenario.reply)), trace_checks
    )
    judge = aggregate_rubric(
        rubric, parse_submit_report(_notes_submission(scenario.warned), rubric)
    )
    verdict = compose_runner_trial_verdict(
        {
            COMPONENT_BY_NAME["llm_judge"].runner_score_field: judge.score,
            COMPONENT_BY_NAME["trace_checks"].runner_score_field: trace.score,
        },
        _notes_runner_config(grading),
        judge_gate_failed=judge.gate_failed,
        trace_gate_failed=trace.gate_failed,
    )

    assert judge.gate_failed is scenario.judge_gate_failed
    assert tuple(trace.failed_gate_ids) == scenario.failed_trace_gates
    assert verdict.judge_component == pytest.approx(scenario.judge_component)
    assert verdict.score == pytest.approx(scenario.score)
    assert verdict.binary_pass is scenario.binary_pass


# Which route each of the two bundles below was scored on, in the order they are
# written. The mutating run loses route A on the reads it never made, so the pair
# varies the winner rather than reproducing one constant.
_CACHE_DEBUG_REPLAY_RUNS = (
    (_ROUTE_A_IN_FULL, "divergence_between_the_api_layers"),
    (_MUTATING_RUN, "divergence_against_the_cache"),
)


def _verdicts(constraints: Sequence[TraceConstraintResult]) -> set[tuple[str, bool, bool]]:
    return {(item.id, item.passed, item.undecided) for item in constraints}


def _write_cache_debug_bundle(trial_dir: Path, calls: Sequence[RecordedToolCall]) -> None:
    """A bundle for one ``cache_debug`` trajectory, graded the way a real run grades it."""
    config = _grading_config(_CACHE_DEBUG_TASK)[1]
    trajectory = make_trajectory(
        task_id="cache_debug",
        messages=make_trial_messages(calls, _CACHE_DEBUG_TURNS),
        tool_log=list(calls),
    )
    grade = GradingEngine(config).grade_trajectory(trajectory, _NOTE_ON_DISK)
    FileArtifactWriter().write_trial_bundle(
        trial_dir,
        trajectory.model_copy(update={"grade": grade}),
        {"task_id": "cache_debug", "grading_config": config.model_dump(mode="json")},
        {},
        StructuredLogger(f"cache_debug-{trial_dir.name}"),
    )


def test_a_cache_debug_bundle_re_checks_to_the_verdict_its_own_grade_recorded(
    tmp_path: Path,
) -> None:
    """A recorded run is re-checkable against itself, and the two sides are independent.

    One side is the live fold, evaluated by ``GradingEngine`` and frozen into
    ``grade.yaml`` at write time; the other is the recomputation the replay engine
    performs now over the bundle it reads back. Both bundles are written from one
    pack, so the pair varies the two things a constant would fake: the winning route,
    and whether the shared gate shut.
    """
    for index, (calls, _) in enumerate(_CACHE_DEBUG_REPLAY_RUNS):
        _write_cache_debug_bundle(tmp_path / "trials" / "cache_debug" / str(index), calls)

    outcomes = run_trace_replay_batch(tmp_path, replay_id="parity")
    recorded = [read_trace_replay_inputs(outcome.bundle) for outcome in outcomes]

    assert [outcome.status for outcome in outcomes] == [TraceReplayOutcomeStatus.REPLAYED] * 2
    for outcome, inputs, (_, route) in zip(
        outcomes, recorded, _CACHE_DEBUG_REPLAY_RUNS, strict=True
    ):
        assert outcome.result is not None
        assert inputs.recorded_constraints is not None
        assert inputs.recorded_summary is not None
        assert _verdicts(outcome.result.constraints) == _verdicts(inputs.recorded_constraints)
        assert len(outcome.result.constraints) == 5
        assert outcome.result.winning_path == inputs.recorded_summary.winning_path == route

    assert [inputs.provenance for inputs in recorded] == [ConstraintProvenance.RECORDED] * 2
    assert [inputs.recorded_summary.gate_failed for inputs in recorded] == [False, True]
    assert [outcome.result.gate_failed for outcome in outcomes] == [False, True]


# A block no pack ships, supplied to reach the two degenerate verdicts a real pack's
# constraints do not produce over this corpus: one selecting a tool nothing called,
# one selecting any call at all. Neither bundle records a wire tool list, so the
# authoring gate cannot resolve ``recall_lot`` and reports the skip instead of
# refusing the block.
_DEGENERATE_OVERRIDE = {
    "constraints": [
        {
            "id": "a_tool_no_trial_called",
            "description": "a corrective action was recalled",
            "require": {
                "present": {"match": {"kind": "tool_call", "tool": {"equals": "recall_lot"}}}
            },
        },
        {
            "id": "any_tool_at_all_was_called",
            "description": "the agent called something",
            "require": {"present": {"match": {"kind": "tool_call"}}},
        },
    ]
}


def _write_lot_ops_bundle(
    trial_dir: Path, calls: Sequence[RecordedToolCall], *, with_tool_log: bool
) -> None:
    """One ``lot_ops_01`` bundle, graded live and written the way a real run writes it.

    ``with_tool_log=False`` removes the record sidecar from a bundle the writer has
    already stamped, which is the provision-failure shape: stamped current, carrying no
    record, because the trial body never ran. That is the shape on which the flagship
    correlation cannot read ``status``, and it is the one worth writing here — a bundle
    written before the record existed would also be *unstamped*, and the stamp is
    evidence rather than a gate, so the record's absence is what the re-check turns on.
    The recorded grade is the live one either way: it is the independent source the
    report counts agreement against, so it must not be recomputed from the degraded
    bundle.
    """
    FileArtifactWriter().write_trial_bundle(
        trial_dir,
        _lot_ops_trajectory(calls).model_copy(update={"grade": _lot_ops_grade(calls)}),
        {
            "task_id": "lot_ops_01",
            "grading_config": _lot_ops_grading().model_dump(mode="json"),
        },
        {},
        StructuredLogger(f"lot_ops_01-{trial_dir.name}"),
    )
    if not with_tool_log:
        (trial_dir / "tool_log.yaml").unlink()


def _lot_ops_corpus(
    root: Path, runs: Sequence[Sequence[RecordedToolCall]], *, with_tool_log: bool
) -> Path:
    for index, calls in enumerate(runs):
        _write_lot_ops_bundle(
            root / "trials" / "lot_ops_01" / str(index), calls, with_tool_log=with_tool_log
        )
    return root


def _replay_report(
    source: Path, *, replay_id: str = "discrimination", override: TraceChecksOverride | None = None
) -> tuple[list[TrialTraceReplayOutcome], TraceReplayReport]:
    """A batch over *source* and the report built off it, the way the command will.

    ``declared`` is read off the batch rather than by re-reading ``task.yaml``, so
    the constraint universe the report reports on is the one the trials were
    measured against — including an override's, which the pack files never carry.
    """
    outcomes = run_trace_replay_batch(source, replay_id=replay_id, override=override)
    report = build_trace_replay_report(
        outcomes,
        declared=declared_trace_checks(outcomes),
        source=source,
        replay_id=replay_id,
    )
    assert report is not None
    return outcomes, report


def _row(report: TraceReplayReport, constraint_id: str) -> ConstraintDiscriminationRow:
    (row,) = [item for item in report.discrimination if item.constraint_id == constraint_id]
    return row


def _per_trial_verdicts(outcomes: Sequence[TrialTraceReplayOutcome], constraint_id: str) -> str:
    """One mark per trial that evaluated the constraint, in discovery order.

    The aggregate counts cannot tell two constraints apart when both split the
    corpus the same way — which the two ``lot_ops_01`` correlations do, 2 passed and
    1 failed each — so this is what says *which* trial each verdict belongs to.
    """
    return " ".join(
        "U" if item.undecided else "P" if item.passed else "F"
        for outcome in outcomes
        for item in (outcome.result.constraints if outcome.result is not None else ())
        if item.id == constraint_id
    )


def test_both_lot_ops_correlations_discriminate_over_a_corpus_that_decides_everything(
    tmp_path: Path,
) -> None:
    """The report the feature exists to produce, over a corpus with nothing missing.

    Three trajectories the substrate oracle grades identically: each correlation
    passes two trials and fails one, and they fail *different* ones. Written with
    the tool-call record, so no verdict is undecided and ``DISCRIMINATING`` rests on
    complete evidence rather than on a gap.

    The same corpus re-checked against a supplied block reaches the two verdicts a
    working pack's constraints do not: a constraint selecting a tool nothing called
    is ``ALWAYS_FALSE`` on all three, and one selecting any call at all is
    ``ALWAYS_TRUE`` on all three. Both are findings, not failures — an author
    iterating on a candidate constraint needs to read them and keep working — and
    the gate's skip travels with them, because a block checked against a tool set
    nothing could resolve must not read as a block checked and found clean.

    The pack's own gate is ``P P P`` here and deliberately unasserted: the duplicate
    post lives in ``DOUBLE_POST``, which this corpus does not hold.
    """
    source = _lot_ops_corpus(
        tmp_path,
        [_LOT_OPS_CORRECT_RUN, _GUESSED_CODE_RUN, _UNREAD_LOT_RUN],
        with_tool_log=True,
    )
    outcomes, report = _replay_report(source)

    assert _per_trial_verdicts(outcomes, _LOT_OPS_CONSTRAINTS[0][0]) == "P F P"
    assert _per_trial_verdicts(outcomes, _LOT_OPS_CONSTRAINTS[1][0]) == "P P F"
    for constraint_id, _, _ in _LOT_OPS_CONSTRAINTS[:2]:
        row = _row(report, constraint_id)
        assert row.verdict is ConstraintDiscrimination.DISCRIMINATING
        assert (row.trials_evaluated, row.trials_decided, row.undecided_trials) == (3, 3, 0)
        assert (row.passed_trials, row.failed_trials) == (2, 1)
        assert row.decided_verdict is None
    assert [trial.tool_log_present for trial in report.trials] == [True] * 3
    assert report.evidence.bundles_with_tool_log == 3
    assert report.override_authoring is None

    supplied = override_file(tmp_path / "supplied", _DEGENERATE_OVERRIDE)
    _, overridden = _replay_report(source, replay_id="degenerate", override=supplied)

    assert [(row.constraint_id, row.verdict) for row in overridden.discrimination] == [
        ("a_tool_no_trial_called", ConstraintDiscrimination.ALWAYS_FALSE),
        ("any_tool_at_all_was_called", ConstraintDiscrimination.ALWAYS_TRUE),
    ]
    assert [row.trials_decided for row in overridden.discrimination] == [3, 3]
    assert overridden.override_authoring is not None
    assert overridden.override_authoring.advisories == []
    assert [skip.split(": ", 1)[0] for skip in overridden.override_authoring.unchecked] == [
        "grading"
    ]


def test_a_record_less_corpus_reports_the_flagship_correlation_as_never_decided(
    tmp_path: Path,
) -> None:
    """Missing evidence is reported as missing, never as the constraint's fault.

    These are exactly the three ``lot_ops_01`` trajectories on which the flagship
    correlation goes undecided without the record: its ``require.before.left``
    matcher reads ``status``, which no message can express. All three undecided is
    ``NEVER_DECIDED`` with nothing decided — the answer an author needs, where
    "failed on every trial" would be an accusation the corpus cannot support.

    The agreement counts are the other half of the claim. The two constraints that
    stay decided reproduce the live run's verdict on all three trials, and the
    undecided one is labelled on none — so the same corpus shows that a recomputation
    with no evidence drops out of the agreement denominator instead of being counted
    as a disagreement with the grade it cannot contradict. The doubled post is the
    trial the live run failed overall, and it changes none of these numbers: agreement
    is joined per constraint, not against the trial-level pass.
    (:func:`tests.unit.grading.test_trace_replay.test_agreement_is_counted_against_the_recorded_verdict_of_the_same_constraint`
    is where a count short of its denominator is locked; here every decided verdict
    agrees, because the recorded grade *is* this evaluator's own.)
    """
    source = _lot_ops_corpus(
        tmp_path,
        [_LOT_OPS_CORRECT_RUN, _UNREAD_LOT_RUN, _DOUBLE_POST_RUN],
        with_tool_log=False,
    )
    outcomes, report = _replay_report(source)
    reason_code = _row(report, _LOT_OPS_CONSTRAINTS[0][0])

    assert _per_trial_verdicts(outcomes, _LOT_OPS_CONSTRAINTS[0][0]) == "U U U"
    assert reason_code.verdict is ConstraintDiscrimination.NEVER_DECIDED
    assert (reason_code.trials_evaluated, reason_code.trials_decided) == (3, 0)
    assert reason_code.undecided_trials == 3
    assert (reason_code.passed_trials, reason_code.failed_trials) == (0, 0)
    assert (reason_code.trials_labelled, reason_code.agreed_with_recorded_pass) == (0, 0)

    assert [trial.gate_failed for trial in report.trials] == [False, False, True]
    assert report.evidence.bundles_with_tool_log == 0
    lot = _row(report, _LOT_OPS_CONSTRAINTS[1][0])
    gate = _row(report, _LOT_OPS_CONSTRAINTS[2][0])
    assert (lot.trials_labelled, lot.agreed_with_recorded_pass) == (3, 3)
    assert (gate.trials_labelled, gate.agreed_with_recorded_pass) == (3, 3)
    assert [trial.recorded_binary_pass for trial in report.trials] == [True, True, False]


def test_a_correlation_decided_on_one_trial_of_three_is_reported_undecided_in_part(
    tmp_path: Path,
) -> None:
    """The case the sixth member exists for, on the pack the milestone built.

    Standing single case. Two trials undecided and one decidably false: under a
    five-member set this read ``ALWAYS_FALSE`` — a corpus-wide condemnation resting
    on one observation, which is the exact misleading answer the feature exists to
    prevent. ``UNDECIDED_IN_PART`` says what was decided, how much of the corpus
    decided it, and which way.

    The one decided trial is also the one labelled: the guessed code is decidably
    false now and was recorded false by the live run, so the two sources agree on the
    only trial where both have an opinion. The two undecided trials recorded a *pass*
    on this constraint — the live run had the record — and counting that as a
    disagreement would blame the constraint for evidence the bundle no longer carries.
    """
    source = _lot_ops_corpus(
        tmp_path,
        [_LOT_OPS_CORRECT_RUN, _UNREAD_LOT_RUN, _GUESSED_CODE_RUN],
        with_tool_log=False,
    )
    outcomes, report = _replay_report(source)
    row = _row(report, _LOT_OPS_CONSTRAINTS[0][0])

    assert _per_trial_verdicts(outcomes, _LOT_OPS_CONSTRAINTS[0][0]) == "U U F"
    assert row.verdict is ConstraintDiscrimination.UNDECIDED_IN_PART
    assert row.decided_verdict is False
    assert (row.trials_evaluated, row.trials_decided, row.undecided_trials) == (3, 1, 2)
    assert (row.passed_trials, row.failed_trials) == (0, 1)
    assert (row.trials_labelled, row.agreed_with_recorded_pass) == (1, 1)


def test_a_cache_debug_route_that_won_no_trial_is_reported_unmeasured_not_unanimous(
    tmp_path: Path,
) -> None:
    """A route's constraints must not vanish, and must not read as passing either.

    Standing single case. ``evaluate_trace_checks`` emits the shared constraints and
    the winning route's only, so on this one mutating trial three of the pack's
    eight declared constraints appear in no result at all — route A lost on the
    reads it never made. A report built from the verdicts alone would simply not
    mention them; one that classified them in declaration order would call them
    ``ALWAYS_TRUE``, because over zero trials "every evaluated trial was decided and
    all passed" is vacuously true.

    So the row exists, says zero trials evaluated, and names the route it belongs
    to — and the report states the denominator, because ``ALWAYS_TRUE`` on a route
    that won twice out of twenty otherwise reads as a corpus-wide claim.
    """
    _write_cache_debug_bundle(tmp_path / "trials" / "cache_debug" / "0", _MUTATING_RUN)
    outcomes, report = _replay_report(tmp_path)
    rows = {row.constraint_id: row for row in report.discrimination}
    losing_route, losing_checks = _CACHE_DEBUG_PATHS[0]
    winning_route, winning_checks = _CACHE_DEBUG_PATHS[1]

    assert outcomes[0].result is not None
    assert len(outcomes[0].result.constraints) == 5
    assert len(rows) == len(_CACHE_DEBUG_SHARED) + sum(
        len(checks) for _, checks in _CACHE_DEBUG_PATHS
    )

    for constraint_id in losing_checks:
        assert (rows[constraint_id].route, rows[constraint_id].trials_evaluated) == (
            losing_route,
            0,
        )
        assert rows[constraint_id].verdict is ConstraintDiscrimination.NOT_MEASURED
        assert rows[constraint_id].decided_verdict is None
    for constraint_id in winning_checks:
        assert (rows[constraint_id].route, rows[constraint_id].trials_evaluated) == (
            winning_route,
            1,
        )
        assert rows[constraint_id].verdict is ConstraintDiscrimination.ALWAYS_TRUE

    assert rows["no_status_was_written"].route == ""
    assert rows["no_status_was_written"].verdict is ConstraintDiscrimination.ALWAYS_FALSE
    assert [trial.winning_path for trial in report.trials] == [winning_route]
    assert "trials its path won" in report.route_scoping
