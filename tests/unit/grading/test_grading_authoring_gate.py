"""What a task's tools say about the grading block it is graded by.

Every defect here is otherwise charged to the agent or to nobody. A misspelled tool
name in a ``present`` matcher scores the component 0.0 with the message a real agent
failure carries; the same typo under ``absent`` passes every trial; an uncompilable
``regex`` raises inside the evaluator once the trial is paid for. So the gate has to
answer before the run, and it has to answer without a false-reject mode — which is
what the ``unchecked`` channel is: reported, never fatal.

Each row of :data:`_RULES` is one authored defect and the channel it must be
reported in. :func:`test_every_checker_the_module_declares_is_provoked_by_a_rule`
holds the table against the module by running it: a checker no row provokes is a
rule nothing exercises, and a row naming a checker the module lost fails there too.

Every schema is the tool's own — a mocked registry would let the severity table
drift from what the tools declare. Where one pack cannot express a shape, several
are unioned rather than a schema invented. The one exception is written out and
says so: no pack types a property outside the six JSON type names, and the rule
that answers for those has to be given one.

**A finding is asserted by its message as well as its address.** The address says
which rule answered; only the message says what the author is told, and the two
fail independently. A dispatch that renders one sentence for every case leaves
every address right and every finding wrong — a shape an address-only suite reads
as green, and the content assertions here catch on the first run.
"""

from __future__ import annotations

import ast
import json
import logging
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, get_type_hints

import pytest
import yaml
from pydantic import ValidationError

from tests.utils.golden_source_shapes import sources_no_replay_can_iterate
from tolokaforge.adapters._task_loader import (
    build_tool_inventory,
    load_task_yaml,
    seeded_tables_under_adapter,
    validate_grading_yaml,
)
from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.core.grading import config_validation
from tolokaforge.core.grading.config_validation import (
    _A_NON_EMPTY_SECTION_STILL_DECLARES,
    _HOW_TO_CORRELATE,
    _MATCHER_FIELD_ATTRIBUTES,
    _NO_INITIAL_STATE_FILE,
    _NO_MCP_SERVER_MODULE,
    _TEXTUAL_MATCHER_FIELDS,
    _TOOL_EXPECTATION_HAZARDS,
    _TRANSCRIPT_RULE_KEYS,
    _UNCORRELATABLE_JSON_TYPES,
    _WHAT_EACH_SECTION_MUST_DECLARE,
    UNRESOLVED_COMBINE_REASON,
    AdapterHashSource,
    AuthoringReport,
    CombineLayer,
    Finding,
    HashSourceLayer,
    ReplayWorld,
    SeededTablesLayer,
    SuppliedSourceState,
    ToolInventory,
    _authored_hash_is_a_state_source,
    _BoundTypeSource,
    _is_a_string_at_runtime,
    inspect_grading_authoring,
)
from tolokaforge.core.grading.golden_replay import (
    _ABSENT_INITIAL_STATE,
    _NO_MCP_SERVER,
    InitialStateSource,
    UnreplayableGoldenSource,
    refuse_unreplayable_golden_source,
)
from tolokaforge.core.grading.grade_components import (
    COMPONENT_BY_NAME,
    GRADE_COMPONENTS,
    component_requested,
)
from tolokaforge.core.grading.state_composition import (
    CONFLICTING_STATE_SOURCES_MESSAGE,
    HASH_SOURCE_KEYS,
    StateHashConfig,
    hash_block_is_a_state_source,
)
from tolokaforge.core.grading.trace_replay import tool_inventory_from_bundle
from tolokaforge.core.grading.trace_timeline import TraceEvent
from tolokaforge.core.models import (
    GradingCombineConfig,
    GradingConfig,
    GradingFindingSeverity,
    ToolExecutorIdentity,
    ToolExpectations,
    TranscriptRulesConfig,
)
from tolokaforge.runner.models import TRACE_MATCHABLE_FIELDS_BY_KIND

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[3]
_MODULE_SOURCE = _REPO / "tolokaforge" / "core" / "grading" / "config_validation.py"
_EXAMPLES = _REPO / "examples" / "native"

# Two packs, because the severity of an unknown argument name is the difference
# between their schemas: ``http_request`` is a builtin carrying
# ``additionalProperties: false``, while an MCP tool's schema declares its
# properties and permits others.
_HELPDESK = _EXAMPLES / "multi_service_helpdesk_workflow/dataset/tasks/helpdesk_01/task.yaml"
_NOTES = _EXAMPLES / "native_shared_domain/dataset/notes/testcases/add_first_note/task.yaml"
_NO_TOOLS = _EXAMPLES / "example-microservices-pack/tasks/api_endpoint_add/task.yaml"

# Four more, because a binder's extraction is checked against the *declared type* of
# an argument and the two packs above type none but strings. Between them these carry
# every JSON type a correlation against text cannot hold for, plus the property that
# writes no type at all: ``read_file`` types ``offset`` integer and
# ``with_line_numbers`` boolean under ``additionalProperties: false``, ``place_order``
# types ``items`` array under a schema permitting extras, ``search_kb.alpha`` is the
# corpus's only ``number``, and ``mobile.actions`` is an ``anyOf`` with no ``type``.
_CODING = _EXAMPLES / "coding/dataset/tasks/coding/coding_public_example_01/task.yaml"
_SHOP_ORDERS = _REPO / "tests/data/tasks/shop_orders_02/task.yaml"
_MOBILE = _REPO / "tests/data/tasks/synth_mobile_01/task.yaml"
_RAG = _EXAMPLES / "rag_search/dataset/tasks/kb_lookup_01/task.yaml"


@cache
def _inventory(task_yaml: Path) -> ToolInventory:
    task, task_dir = load_task_yaml(task_yaml)
    return build_tool_inventory(task, task_dir)


def _trace_block(match: dict[str, Any], kind: str = "present") -> dict[str, Any]:
    """One constraint over *match*, in the shape an author writes it."""
    return {
        "trace_checks": {
            "constraints": [
                {
                    "id": "probe",
                    "description": "a probe constraint",
                    "require": {kind: {"match": match}},
                }
            ]
        }
    }


def _tool_call(tool: str, **args: dict[str, Any]) -> dict[str, Any]:
    match: dict[str, Any] = {"kind": "tool_call", "tool": {"equals": tool}}
    if args:
        match["args"] = args
    return match


def _required_action(name: str, requestor: str) -> dict[str, Any]:
    """One ``required_actions`` entry, in the shape an author writes it."""
    return {
        "transcript_rules": {
            "required_actions": [
                {"action_id": "the_declared_call", "requestor": requestor, "name": name}
            ]
        }
    }


def _bound_block(
    binder: dict[str, Any], values: dict[str, Any], require: dict[str, Any]
) -> dict[str, Any]:
    """One constraint drawing *values* out of *binder* and correlating them in *require*."""
    return {
        "trace_checks": {
            "constraints": [
                {
                    "id": "probe",
                    "description": "a probe constraint",
                    "bind": {"match": binder, "values": values},
                    "require": require,
                }
            ]
        }
    }


def _golden_actions(*actions: dict[str, Any], enabled: bool = True) -> dict[str, Any]:
    """A hash block replaying *actions*, in the shape an author writes it."""
    return {"state_checks": {"hash": {"enabled": enabled, "golden_actions": list(actions)}}}


# One probe and one assertion, as an author writes them. Both are read for truthiness
# alone by the rule that refuses their co-occurrence, so the contents are only realistic
# enough that the block loads.
_A_DB_PROBE = {
    "name": "the_corrective_action_landed",
    "dsn": "postgresql://grader:grader_pw@app-db:5432/mfg",
    "query": "SELECT status FROM corrective_actions WHERE lot_id = 7",
    "expect": [{"path": "$.row_count", "equals": 1}],
}
_A_JSONPATH_ASSERTION = {"path": "$.db.widgets[0].status", "equals": "closed"}


def _probes_beside(**state_checks: Any) -> dict[str, Any]:
    """A ``state_checks`` block declaring one probe and whatever else *state_checks* holds."""
    return {"state_checks": {"db_probes": [_A_DB_PROBE], **state_checks}}


# Every world a task can give a golden replay, written out rather than resolved from a
# pack: the one shape this issue is about — an initial-state file beside no MCP server
# module — is authorable and ships nowhere, so no pack can supply it. The corpus guard
# in ``tests/canonical/test_example_pack_grading_corpus.py`` resolves the real ones.
_A_BUILDABLE_WORLD = ReplayWorld(initial_state=InitialStateSource.JSON_FILE, mcp_server=True)
_NO_SERVER_MODULE = ReplayWorld(initial_state=InitialStateSource.JSON_FILE, mcp_server=False)
_NO_INITIAL_STATE = ReplayWorld(initial_state=InitialStateSource.ABSENT, mcp_server=True)
_AN_INLINE_INITIAL_STATE = ReplayWorld(initial_state=InitialStateSource.INLINE, mcp_server=True)
_A_TASK_SUPPLYING_NEITHER = ReplayWorld(initial_state=InitialStateSource.ABSENT, mcp_server=False)

# The answers a caller can give the hash-source rule. The resolved layer is what both
# pack gates report for a native task — the authored block is the only place a source can
# come from — and what every row here means by grading natively. The unresolvable one is
# the answer for a task an external adapter grades, whose source may live in fixtures the
# block never names. The last three are that adapter naming the fixture it reads and the
# state it found it in, which is the only thing that separates its convention from a pack
# that costs a trial and grades nothing.
_A_FIXTURE_AN_ADAPTER_READS = "fixtures/golden_actions.json"
_THE_BLOCK_IS_THE_WHOLE_LAYER = HashSourceLayer()
_AN_ADAPTER_MAY_SUPPLY_THE_SOURCE = HashSourceLayer.unresolvable()
_AN_ADAPTER_SUPPLIES_A_USABLE_SOURCE = HashSourceLayer(
    supplied=AdapterHashSource(where=_A_FIXTURE_AN_ADAPTER_READS, state=SuppliedSourceState.USABLE)
)
_AN_ADAPTER_SUPPLIES_A_MISSING_SOURCE = HashSourceLayer(
    supplied=AdapterHashSource(where=_A_FIXTURE_AN_ADAPTER_READS, state=SuppliedSourceState.MISSING)
)
_AN_ADAPTER_SUPPLIES_AN_EMPTY_SOURCE = HashSourceLayer(
    supplied=AdapterHashSource(where=_A_FIXTURE_AN_ADAPTER_READS, state=SuppliedSourceState.EMPTY)
)

# What a caller can say about the tables a task seeds, which its ``id_fields``
# declaration keys. The resolved layer seeds two rows one component alone cannot tell
# apart, so a declaration is held against real records rather than against an empty
# view every declaration would be wrong about. The unresolvable one is the answer for a
# caller holding no task.yaml, and the default every row that is not about the seeded
# tables carries.
_TWO_ROWS_ONE_COMPONENT_CANNOT_KEY = {
    "positions": [
        {"account_id": "A1", "symbol": "AAPL"},
        {"account_id": "A1", "symbol": "MSFT"},
    ]
}
_NO_CALLER_READ_WHAT_THE_TASK_SEEDS = SeededTablesLayer.unresolvable()
_THE_TASK_SEEDS_THESE_TABLES = SeededTablesLayer(tables=_TWO_ROWS_ONE_COMPONENT_CANNOT_KEY)
_THE_TASK_SEEDS_NO_TABLES = SeededTablesLayer(tables={})

_A_FILESYSTEM_ROOTED_ASSERTION = {
    "path": "$.filesystem['/env/fs/agent-visible/x.py']",
    "contains": "def divide",
}
_A_FILE_ASSERTION = {"path_glob": "/env/fs/agent-visible/x.py", "contains_ci": "def divide"}
# ``$.agent[…]`` roots at state only the core engine composes (from a run's live
# env). The runner has no equivalent, so this remains an unreachable-path defect
# after filesystem paths were promoted to runner-graded.
_AN_UNREACHABLE_PATH_ASSERTION = {
    "path": "$.agent.customers[0].balance",
    "equals": "0",
}


def _keyed_state(id_fields: dict[str, Any]) -> dict[str, Any]:
    """A state block declaring *id_fields* beside a source that makes it evaluable."""
    return {"state_checks": {"jsonpaths": [_A_JSONPATH_ASSERTION], "id_fields": id_fields}}


def _quotes(operator: str, name: str) -> dict[str, Any]:
    """A ``present`` over an assistant message whose text reads the bound *name*."""
    return {"present": {"match": {"kind": "assistant_message", "text": {operator: name}}}}


@dataclass(frozen=True)
class _Rule:
    """One authored defect, the channel it lands in, and the fix it must name.

    ``combine`` is the effective map the row is checked under. ``None`` leaves the
    weight rules out, which is what every tool-aware row wants: they are about a
    matcher, and a weight finding beside one would report two defects for one typo.
    ``world`` reads the same way: unresolvable leaves the replay-world rule reporting
    nothing in either finding channel, so only the row that is about the world says
    what the task supplies. ``hash_sources`` defaults to the native reading — the
    authored block is the whole layer — so only the rows about the layer say who else
    may supply the source. ``seeded_tables`` defaults the other way, to unresolvable:
    no other row's fixture declares ``id_fields``, so the rows about the seeded tables
    are the only ones that name what the task seeds.
    """

    label: str
    task: Path
    grading: dict[str, Any]
    checker: str
    channel: str
    message: str
    combine: GradingCombineConfig | None = None
    world: ReplayWorld = ReplayWorld.unresolvable()
    hash_sources: HashSourceLayer = _THE_BLOCK_IS_THE_WHOLE_LAYER
    seeded_tables: SeededTablesLayer = _NO_CALLER_READ_WHAT_THE_TASK_SEEDS


_RULES: tuple[_Rule, ...] = (
    _Rule(
        label="state_read_on_a_task_that_seeds_no_database",
        task=_HELPDESK,
        grading={"state_checks": {"jsonpaths": [_A_JSONPATH_ASSERTION]}},
        checker="_check_state_reads_a_database_the_task_seeds",
        channel="errors",
        message="seeds no tables",
        seeded_tables=_THE_TASK_SEEDS_NO_TABLES,
    ),
    _Rule(
        # ``$.filesystem[…]`` is *reachable* on the runner (via
        # ``_read_agent_visible_filesystem``), so the authoring gate should not
        # refuse it — this rule now exercises the residual unreachable set
        # (``agent`` / ``user`` / ``mock_web_url`` / ``rag_corpus_dir``), which
        # the core engine composes from a run's live env and the runner does not.
        label="a_path_addressing_beyond_the_runners_state",
        task=_HELPDESK,
        grading={
            "state_checks": {"jsonpaths": [{"path": "$.agent.customers[0].balance", "equals": "0"}]}
        },
        checker="_check_jsonpaths_address_a_reachable_state",
        channel="errors",
        message="addresses state the runner's JSONPath grading does not carry",
    ),
    _Rule(
        label="a_path_glob_the_runner_cannot_read",
        task=_HELPDESK,
        grading={
            "state_checks": {
                "jsonpaths": [{"path_glob": "/env/fs/agent-visible/x.py", "contains": "def divide"}]
            }
        },
        checker="_check_path_glob_is_compared_the_way_the_runner_reads_it",
        channel="errors",
        message="which the runner's file-content evaluator does not read",
    ),
    _Rule(
        label="matcher_names_an_undeclared_tool",
        task=_HELPDESK,
        grading=_trace_block(_tool_call("http_reqest")),
        checker="_check_tool_names",
        channel="errors",
        message="is not declared by this task",
    ),
    _Rule(
        label="tool_expectation_names_an_undeclared_tool",
        task=_HELPDESK,
        grading={"transcript_rules": {"tool_expectations": {"required_tools": ["http_reqest"]}}},
        checker="_check_tool_expectation_names",
        channel="errors",
        message="short a required tool on every trial",
    ),
    _Rule(
        label="required_action_naming_an_undeclared_tool",
        task=_HELPDESK,
        grading=_required_action("http_reqest", "assistant"),
        checker="_check_required_action_names",
        channel="errors",
        # The tool, the set the task does declare, and what the action costs: an
        # author shown only the hazard has nothing to correct the name against.
        message="tool 'http_reqest' is not declared by this task, which gives its actors "
        "['http_request', 'write_file']: no actor can call it, so the transcript component "
        "is short a required action on every trial",
    ),
    _Rule(
        label="required_action_whose_requestor_declares_no_such_tool",
        task=_HELPDESK,
        grading=_required_action("http_request", "user"),
        checker="_check_required_action_names",
        channel="errors",
        message="tools.user.enabled declares []: ['tools.agent.enabled'] declares the tool "
        "instead, so the executor filter never matches",
    ),
    _Rule(
        label="argument_outside_a_closed_schema",
        task=_HELPDESK,
        grading=_trace_block(_tool_call("http_request", urll={"exists": True})),
        checker="_check_argument_paths",
        channel="errors",
        message="its schema admits no other",
    ),
    _Rule(
        label="argument_outside_an_open_schema",
        task=_NOTES,
        grading=_trace_block(_tool_call("add_note", titel={"exists": True})),
        checker="_check_argument_paths",
        channel="advisories",
        message="probable typo rather than a certainty",
    ),
    _Rule(
        label="extraction_outside_a_closed_schema",
        task=_CODING,
        grading=_bound_block(
            _tool_call("read_file"),
            {"start": {"field": "args.ofset"}},
            _quotes("contains_binding", "start"),
        ),
        checker="_check_bound_extractions",
        channel="errors",
        message="its schema admits no other",
    ),
    _Rule(
        label="extraction_outside_an_open_schema",
        task=_SHOP_ORDERS,
        grading=_bound_block(
            _tool_call("place_order"),
            {"ordered": {"field": "args.itms"}},
            _quotes("contains_binding", "ordered"),
        ),
        checker="_check_bound_extractions",
        channel="advisories",
        message="probable typo rather than a certainty",
    ),
    _Rule(
        label="capture_pattern_over_an_argument_the_schema_types_non_string",
        task=_CODING,
        grading=_bound_block(
            _tool_call("read_file"),
            {"start": {"field": "args.offset", "pattern": "([0-9]+)"}},
            _quotes("contains_binding", "start"),
        ),
        checker="_check_bound_extractions",
        channel="errors",
        message="A capture is taken off text alone",
    ),
    _Rule(
        label="args_correlation_neither_type_can_satisfy_on_closed_schemas",
        task=_CODING,
        grading=_bound_block(
            _tool_call("read_file"),
            {"start": {"field": "args.offset"}},
            {"present": {"match": _tool_call("read_file", path={"equals_binding": "start"})}},
        ),
        checker="_check_bound_comparisons",
        channel="errors",
        message="Correlate two arguments the tools type the same way",
    ),
    _Rule(
        label="args_correlation_neither_type_can_satisfy_on_an_open_schema",
        task=_SHOP_ORDERS,
        grading=_bound_block(
            _tool_call("place_order"),
            {"ordered": {"field": "args.items"}},
            {
                "present": {
                    "match": _tool_call("place_order", customer_id={"equals_binding": "ordered"})
                }
            },
        ),
        checker="_check_bound_comparisons",
        channel="advisories",
        message="Correlate two arguments the tools type the same way",
    ),
    _Rule(
        label="matcher_regex_that_does_not_compile",
        task=_HELPDESK,
        grading=_trace_block({"kind": "tool_call", "tool": {"regex": "http_(request"}}),
        checker="_check_regex_compiles",
        channel="errors",
        message="does not compile",
    ),
    _Rule(
        label="transcript_regex_that_does_not_compile",
        task=_HELPDESK,
        grading={"transcript_rules": {"disallow_regex": ["unterminated(["]}},
        checker="_check_regex_compiles",
        channel="errors",
        message="does not compile",
    ),
    _Rule(
        label="hash_source_without_the_flag",
        task=_HELPDESK,
        grading={"state_checks": {"hash": {"enabled": False, "expect_initial_state": True}}},
        checker="_check_hash_source_declared",
        channel="errors",
        message="the comparison never runs",
    ),
    _Rule(
        label="enabled_hash_with_no_source",
        task=_HELPDESK,
        grading={"state_checks": {"hash": {"enabled": True}}},
        checker="_check_hash_source_declared",
        channel="errors",
        message="Declare golden_actions or expect_initial_state",
    ),
    _Rule(
        label="enabled_hash_whose_source_an_adapter_may_supply",
        task=_HELPDESK,
        grading={"state_checks": {"hash": {"enabled": True}}},
        checker="_check_hash_source_declared",
        channel="unchecked",
        message="an external adapter may compute the source",
        hash_sources=_AN_ADAPTER_MAY_SUPPLY_THE_SOURCE,
    ),
    _Rule(
        label="hash_source_without_the_flag_under_an_unresolved_layer",
        task=_HELPDESK,
        grading={"state_checks": {"hash": {"enabled": False, "expect_initial_state": True}}},
        checker="_check_hash_source_declared",
        channel="unchecked",
        message="an external adapter may compute the source",
        hash_sources=_AN_ADAPTER_MAY_SUPPLY_THE_SOURCE,
    ),
    _Rule(
        label="enabled_hash_whose_supplied_source_is_missing",
        task=_HELPDESK,
        grading={"state_checks": {"hash": {"enabled": True}}},
        checker="_check_hash_source_declared",
        channel="errors",
        # The fixture and its state together: a refusal naming neither leaves the author
        # with the generic "declare a source" fix, which is not the one that repairs this.
        message=f"{_A_FIXTURE_AN_ADAPTER_READS}, which is missing",
        hash_sources=_AN_ADAPTER_SUPPLIES_A_MISSING_SOURCE,
    ),
    _Rule(
        label="enabled_hash_whose_supplied_source_is_empty",
        task=_HELPDESK,
        grading={"state_checks": {"hash": {"enabled": True}}},
        checker="_check_hash_source_declared",
        channel="errors",
        message=f"{_A_FIXTURE_AN_ADAPTER_READS}, which is empty",
        hash_sources=_AN_ADAPTER_SUPPLIES_AN_EMPTY_SOURCE,
    ),
    _Rule(
        label="hash_source_without_the_flag_beside_a_supplied_source",
        task=_HELPDESK,
        grading={"state_checks": {"hash": {"enabled": False, "expect_initial_state": True}}},
        checker="_check_hash_source_declared",
        channel="errors",
        message="the comparison never runs",
        hash_sources=_AN_ADAPTER_SUPPLIES_A_USABLE_SOURCE,
    ),
    _Rule(
        label="probes_beside_a_source_that_scores_the_same_component",
        task=_HELPDESK,
        grading=_probes_beside(jsonpaths=[_A_JSONPATH_ASSERTION]),
        checker="_check_probes_are_the_only_state_source",
        channel="errors",
        # The report-only tail rather than the shared sentence other tests already lock:
        # this row is the only assertion that the finding says which substrate would have
        # discarded which verdict.
        message="only the runner evaluates a probe",
    ),
    _Rule(
        label="golden_action_naming_an_undeclared_tool",
        task=_HELPDESK,
        grading=_golden_actions({"name": "close_widget"}),
        checker="_check_golden_action_names",
        channel="errors",
        message="golden action 'close_widget' is not declared by this task",
    ),
    _Rule(
        label="golden_action_naming_nothing_at_all",
        task=_HELPDESK,
        grading=_golden_actions({"kwargs": {"widget_id": "W1"}}),
        checker="_check_golden_action_names",
        channel="errors",
        message="names no tool to replay",
    ),
    _Rule(
        label="a_golden_source_that_is_no_list_of_actions",
        task=_HELPDESK,
        grading={"state_checks": {"hash": {"enabled": True, "golden_actions": "write_file"}}},
        checker="_check_golden_actions_are_a_list",
        channel="errors",
        message="the list of actions a golden replay executes",
    ),
    _Rule(
        label="golden_actions_the_task_gives_no_world_to_replay_in",
        task=_HELPDESK,
        grading=_golden_actions({"name": "write_file"}),
        checker="_check_golden_replay_world",
        channel="errors",
        message="declares no tools.agent.mcp_server",
        world=_NO_SERVER_MODULE,
    ),
    _Rule(
        label="a_section_that_declares_nothing",
        task=_HELPDESK,
        grading={"transcript_rules": {}},
        checker="_check_sections_declare_something",
        channel="errors",
        message="declares nothing at all",
    ),
    _Rule(
        label="a_state_checks_block_with_no_source",
        task=_HELPDESK,
        grading={"state_checks": {"jsonpaths": [], "relaxed_validation": True}},
        checker="_check_sections_declare_something",
        channel="errors",
        message="declares no source any substrate can read",
    ),
    _Rule(
        label="a_transcript_rules_block_with_no_rule",
        task=_HELPDESK,
        grading={"transcript_rules": {"required_actions": []}},
        checker="_check_sections_declare_something",
        channel="errors",
        message="declares no rule any substrate can evaluate",
    ),
    _Rule(
        label="a_custom_checks_block_deciding_no_opt_in",
        task=_HELPDESK,
        grading={"custom_checks": {"file": "checks.py"}},
        checker="_check_sections_declare_something",
        channel="errors",
        message="the component's own default reads as off",
    ),
    _Rule(
        label="configured_component_with_no_weight",
        task=_HELPDESK,
        grading={"transcript_rules": {"max_turns": 10}},
        checker="_check_requested_components_are_weighted",
        channel="errors",
        message="Declare combine.weights.transcript_rules",
        combine=GradingCombineConfig(weights={}),
    ),
    _Rule(
        label="an_id_fields_declaration_nothing_resolved_the_seeded_tables_for",
        task=_HELPDESK,
        grading=_keyed_state({"positions": ["account_id", "symbol"]}),
        checker="_check_id_fields_against_seeded_tables",
        channel="unchecked",
        message="no caller resolved the tables this task seeds",
    ),
    _Rule(
        label="an_id_fields_component_no_seeded_record_carries",
        task=_HELPDESK,
        grading=_keyed_state({"positions": ["account_id", "ticker"]}),
        checker="_check_id_fields_against_seeded_tables",
        channel="errors",
        # The table and the component together: the author has to know which key of
        # which table to go and fix.
        message="declares key component(s) ['ticker'] absent from every seeded record "
        "of table 'positions'",
        seeded_tables=_THE_TASK_SEEDS_THESE_TABLES,
    ),
    _Rule(
        label="weight_naming_a_component_the_pack_never_configures",
        task=_HELPDESK,
        grading={},
        checker="_check_weights_name_requested_components",
        channel="errors",
        message="the weight weighs nothing",
        combine=GradingCombineConfig(weights={"state_checks": 1.0}),
    ),
)

_FINDING_CHANNELS = ("errors", "advisories")


def _texts(report: AuthoringReport, channel: str) -> list[str]:
    return [
        entry.message if isinstance(entry, Finding) else entry.reason
        for entry in getattr(report, channel)
    ]


@pytest.mark.parametrize("rule", _RULES, ids=[row.label for row in _RULES])
def test_each_rule_is_reported_in_its_own_channel_naming_the_fix(rule: _Rule) -> None:
    """The severity of a finding is the whole content of the rule.

    An unknown argument name on a closed schema is provably wrong and on an open one
    is only suspect; reporting either in the other's channel would hard-fail a pack
    the schema does not condemn, or wave through a matcher that selects nothing.
    """
    report = inspect_grading_authoring(
        rule.grading,
        _inventory(rule.task),
        effective_combine=rule.combine,
        replay_world=rule.world,
        hash_sources=rule.hash_sources,
        seeded_tables=rule.seeded_tables,
    )

    reported = _texts(report, rule.channel)
    assert any(rule.message in text for text in reported), reported
    for other in (channel for channel in _FINDING_CHANNELS if channel != rule.channel):
        assert getattr(report, other) == (), _texts(report, other)


def _checker_names_in_source() -> frozenset[str]:
    """Every ``_check_*`` the module declares, read out of its source."""
    found = frozenset(
        node.name
        for node in ast.walk(ast.parse(_MODULE_SOURCE.read_text()))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("_check_")
    )
    assert found, f"{_MODULE_SOURCE.name} declares no checkers, so this audit reads nothing"
    return found


def test_every_checker_the_module_declares_is_provoked_by_a_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rule with no row is a severity no author is ever shown.

    Provocation, not naming: each checker is wrapped and the whole table is run, so a
    row that stops reaching the checker it claims — because the dispatcher no longer
    calls it, say — fails here even though the row's own assertion still passes.
    Two rows share ``_check_argument_paths``, one per severity, so the audit is
    at-least-one rather than exactly-one.
    """
    declared = _checker_names_in_source()
    assert {rule.checker for rule in _RULES} == declared

    answered: set[str] = set()
    for name in declared:
        monkeypatch.setattr(config_validation, name, _recording(name, answered))
    for rule in _RULES:
        inspect_grading_authoring(
            rule.grading,
            _inventory(rule.task),
            effective_combine=rule.combine,
            replay_world=rule.world,
            hash_sources=rule.hash_sources,
            seeded_tables=rule.seeded_tables,
        )

    assert answered == declared


def _recording(name: str, answered: set[str]):
    """The checker, wrapped so a report it fills marks it as reached."""
    original = getattr(config_validation, name)

    def wrapper(*args: Any, **kwargs: Any) -> AuthoringReport:
        report = original(*args, **kwargs)
        if report.errors or report.advisories or report.unchecked:
            answered.add(name)
        return report

    return wrapper


# ---------------------------------------------------------------------------
# The three reproduced hazards, at the gate an author meets
# ---------------------------------------------------------------------------


def _write_grading(tmp_path: Path, grading: dict[str, Any]) -> Path:
    grading_path = tmp_path / "grading.yaml"
    grading_path.write_text(yaml.safe_dump(grading))
    return grading_path


def test_a_path_with_no_file_at_it_is_not_a_clean_bill_of_health(tmp_path: Path) -> None:
    """This gate reads a file, and a path with none behind it is nobody's silent pass.

    Whether a task has a block to read at all is resolved upstream, by
    :func:`grading_source_under_adapter` off the adapter the task declares, so a path
    reaching here has already been stat'd and one that is gone by the time this opens it
    is a vanished file rather than a task naming no source. Both callers turn the raise
    into a named per-task failure, where an empty report would have read as a pack that
    passed every rule.
    """
    absent = tmp_path / "grading.yaml"

    with pytest.raises(FileNotFoundError) as excinfo:
        validate_grading_yaml(absent, inventory=ToolInventory.unresolvable())

    assert str(absent) in str(excinfo.value)


_HAZARDS = (
    pytest.param(_trace_block(_tool_call("http_reqest")), "http_reqest", id="present_typo"),
    pytest.param(
        _trace_block(_tool_call("htp_request"), kind="absent"), "htp_request", id="absent_typo"
    ),
    pytest.param(
        _trace_block(_tool_call("http_request", urll={"exists": True})),
        "urll",
        id="argument_typo",
    ),
)


@pytest.mark.parametrize(("grading", "typo"), _HAZARDS)
def test_the_typo_is_the_authors_error_not_the_agents(
    tmp_path: Path, grading: dict[str, Any], typo: str
) -> None:
    """All three shapes score the component 0.0 or 1.0 at grade time and blame nobody.

    The ``absent`` row is the strongest: it scores 1.0, so a rule scoped to the
    present-family kinds would look correct on the failing case and be missing on the
    passing one.
    """
    with pytest.raises(ValueError) as excinfo:
        validate_grading_yaml(_write_grading(tmp_path, grading), inventory=_inventory(_HELPDESK))

    message = str(excinfo.value)
    assert typo in message, message
    assert "http_request" in message, message


def test_an_uncompilable_regex_is_caught_before_the_tokens_are_spent(tmp_path: Path) -> None:
    """``re.error`` out of the evaluator loses the trial, not the constraint.

    Core lets it propagate out of the grader and the runner folds it into a failed
    grade response, so neither substrate reports it as the check that failed.
    """
    grading = _trace_block({"kind": "tool_call", "tool": {"regex": "http_(request"}})

    with pytest.raises(ValueError, match="unterminated subpattern"):
        validate_grading_yaml(_write_grading(tmp_path, grading), inventory=_inventory(_HELPDESK))


# A second defect, carried alongside the probe below so which surface answered is
# readable off the raise: the gate batches every finding it has, and a load error raised
# before the gate runs cannot know about this one.
_A_PATTERN_THAT_DOES_NOT_COMPILE = {"transcript_rules": {"disallow_regex": ["unterminated(["]}}


@pytest.mark.parametrize(
    "beside",
    [
        {"jsonpaths": [_A_JSONPATH_ASSERTION]},
        {"hash": {"enabled": True, "expect_initial_state": True}},
    ],
    ids=["jsonpaths", "hash"],
)
def test_probes_beside_another_state_source_are_refused_before_the_gate_is_reached(
    tmp_path: Path, beside: dict[str, Any]
) -> None:
    """One surface answers both halves of the rule: the model, not the gate.

    ``validate`` constructs the core ``StateChecksConfig`` on every declared
    ``state_checks`` block, so this rule is a load error whichever source the probe was
    written beside, and the uncompilable pattern beside it is never reported — the author
    fixes the probe, runs ``validate`` again, and hears about the pattern then. Asserting
    the absence of the second finding is what pins which surface answered, so a reorder
    of the loader cannot move this silently.
    """
    grading = {**_probes_beside(**beside), **_A_PATTERN_THAT_DOES_NOT_COMPILE}

    with pytest.raises(ValidationError) as excinfo:
        validate_grading_yaml(
            _write_grading(tmp_path, grading), inventory=ToolInventory.unresolvable()
        )

    message = str(excinfo.value)
    assert CONFLICTING_STATE_SOURCES_MESSAGE in message, message
    assert "does not compile" not in message, message


@pytest.mark.parametrize(
    "require",
    [
        pytest.param({"present": {"match": _tool_call("http_request")}}, id="top_level"),
        pytest.param(
            {"all_of": [{"present": {"match": _tool_call("http_request")}}]}, id="nested_in_all_of"
        ),
        pytest.param(
            {"any_of": [{"present": {"match": _tool_call("http_request")}}]}, id="nested_in_any_of"
        ),
    ],
)
def test_an_anchorless_on_missing_is_refused_before_the_gate_at_any_depth(
    tmp_path: Path, require: dict[str, Any]
) -> None:
    """The gate needs no ``on_missing`` rule of its own, at the top or nested.

    ``validate`` constructs the whole ``TraceChecksConfig``, so the constraint's own
    rule is what answers here — a second copy in this module would be a rule that can
    disagree with the one the runner enforces. The uncompilable pattern beside it goes
    unreported for the reason the rule above names, which pins which surface answered.
    """
    grading = {
        "trace_checks": {
            "constraints": [
                {
                    "id": "probe",
                    "description": "a probe constraint",
                    "on_missing": "pass",
                    "require": require,
                }
            ]
        },
        **_A_PATTERN_THAT_DOES_NOT_COMPILE,
    }

    with pytest.raises(ValidationError) as excinfo:
        validate_grading_yaml(_write_grading(tmp_path, grading), inventory=_inventory(_HELPDESK))

    message = str(excinfo.value)
    assert "on_missing has nothing to decide over ['present']" in message, message
    assert "does not compile" not in message, message


# ---------------------------------------------------------------------------
# unchecked: reported, never fatal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fail_on", list(GradingFindingSeverity), ids=[member.value for member in GradingFindingSeverity]
)
def test_an_unresolvable_inventory_fails_nothing_and_says_why(
    tmp_path: Path, fail_on: GradingFindingSeverity
) -> None:
    """ "Could not check" must be structurally incapable of failing a pack.

    The block below is wrong in every tool-aware way there is; against an inventory
    that cannot answer, none of it is knowable, and a gate that raised here would
    reject every pack whose adapter cannot report a tool set.
    """
    grading = _trace_block(_tool_call("nothing_declares_this", urll={"exists": True}))

    report = validate_grading_yaml(
        _write_grading(tmp_path, grading),
        inventory=ToolInventory.unresolvable(),
        fail_on=fail_on,
    )

    assert report.errors == ()
    assert report.advisories == ()
    assert [skip.reason for skip in report.unchecked] == [
        "the tool set of this task could not be resolved, so no tool name and no "
        "argument name in this block is checkable",
        UNRESOLVED_COMBINE_REASON,
    ]


def test_an_unresolvable_inventory_still_runs_the_rules_that_need_no_tools(
    tmp_path: Path,
) -> None:
    """Skipping the schema rules must not skip the block-only ones.

    Otherwise an unresolvable inventory reads as a clean bill of health for a pack
    carrying an uncompilable pattern, which raises at grade time regardless of what
    tools the task has.
    """
    grading = {
        **_trace_block({"kind": "tool_call", "tool": {"regex": "http_(request"}}),
        "state_checks": {"hash": {"enabled": False, "expect_initial_state": True}},
    }

    with pytest.raises(ValueError) as excinfo:
        validate_grading_yaml(
            _write_grading(tmp_path, grading),
            inventory=ToolInventory.unresolvable(),
            hash_sources=_THE_BLOCK_IS_THE_WHOLE_LAYER,
        )

    message = str(excinfo.value)
    assert "does not compile" in message, message
    assert "the comparison never runs" in message, message


_UNCHECKED_ARGUMENTS = (
    pytest.param(
        _HELPDESK,
        _tool_call("http_request", **{"json.q": {"len_gt": 0}}),
        "first segment only",
        id="nested_path_the_pack_ships",
    ),
    pytest.param(
        _HELPDESK,
        _tool_call("http_request", **{"json.qq": {"len_gt": 0}}),
        "first segment only",
        id="nested_path_below_a_declared_head",
    ),
    pytest.param(
        _HELPDESK,
        _tool_call("http_request", **{"data.anything": {"exists": True}}),
        "first segment only",
        id="nested_path_under_another_head",
    ),
    pytest.param(
        _NOTES,
        {"kind": "tool_call", "args": {"title": {"exists": True}}},
        "does not name one tool",
        id="arguments_with_no_tool_named",
    ),
)


@pytest.mark.parametrize(("task", "match", "reason"), _UNCHECKED_ARGUMENTS)
def test_an_argument_the_schema_cannot_answer_for_is_unchecked(
    task: Path, match: dict[str, Any], reason: str
) -> None:
    """Descending past the first segment would reject the flagship pack.

    ``http_request``'s ``json`` property is ``{"type": "object"}`` with no
    ``properties``, so ``json.q`` — which helpdesk_01 ships and grades on — is
    answerable at ``json`` and nowhere below it (#765).
    """
    report = inspect_grading_authoring(_trace_block(match), _inventory(task))

    assert report.errors == ()
    assert report.advisories == ()
    assert [skip.reason for skip in report.unchecked] != []
    assert any(reason in skip.reason for skip in report.unchecked), report.unchecked


def test_a_matcher_over_a_tool_with_no_resolved_schema_is_unchecked() -> None:
    """An MCP task that commits no fixture resolves no schema, and says so.

    Boundary case, standing lock: the tool is declared, so its *name* is checked;
    only its arguments are not.
    """
    task_yaml = (
        _REPO / "tests/data/projects/food_delivery_2/tasks/order_modify_with_checks/task.yaml"
    )
    grading = _trace_block(_tool_call("modify_order", not_an_argument={"exists": True}))

    report = inspect_grading_authoring(grading, _inventory(task_yaml))

    assert report.errors == ()
    assert report.advisories == ()
    assert any("no schema resolved" in skip.reason for skip in report.unchecked), report.unchecked


# ---------------------------------------------------------------------------
# Boundary cases no sweep over real packs produces
# ---------------------------------------------------------------------------


def test_a_task_that_declares_no_tools_makes_every_tool_matcher_an_error() -> None:
    """Declaring nothing and being unable to report anything decide opposite things.

    Boundary case, standing lock: short-circuiting on an empty declared set would
    leave this pack shape — which ships in the example corpus — unvalidatable.
    """
    inventory = _inventory(_NO_TOOLS)
    assert inventory.declared == frozenset()
    assert inventory.known is True

    report = inspect_grading_authoring(_trace_block(_tool_call("http_request")), inventory)

    assert [finding.where for finding in report.errors] == ["trace_checks.probe.present.match.tool"]
    assert "no tools at all" in report.errors[0].message


def test_a_typo_inside_an_alternative_route_is_reported_under_that_route() -> None:
    """A route's constraints are checked, and a finding there names the route.

    Boundary case, standing lock: a walk over ``constraints`` alone reaches nothing
    inside ``alternatives``, so a misspelled tool on one route escapes the gate
    entirely and the route it sits on can never be walked by any agent. The
    ``<path>.<constraint>`` address is what tells such a finding apart from one on a
    shared constraint of the same name — the block's single id space is what makes
    it unambiguous.
    """
    grading = {
        "trace_checks": {
            "constraints": [
                {
                    "id": "the_ticket_was_read",
                    "description": "the agent read the ticket",
                    "require": {"present": {"match": _tool_call("http_reqest")}},
                }
            ],
            "alternatives": [
                {
                    "id": "by_the_ticket_api",
                    "description": "the answer came from the ticket API",
                    "constraints": [
                        {
                            "id": "the_api_answered",
                            "description": "the ticket API answered",
                            "require": {"present": {"match": _tool_call("http_requst")}},
                        }
                    ],
                },
                {
                    "id": "by_the_knowledge_base",
                    "description": "the answer came from the knowledge base",
                    "constraints": [
                        {
                            "id": "the_article_was_read",
                            "description": "the knowledge-base article was read",
                            "require": {"present": {"match": _tool_call("http_request")}},
                        }
                    ],
                },
            ],
        }
    }

    report = inspect_grading_authoring(grading, _inventory(_HELPDESK))

    assert [finding.where for finding in report.errors] == [
        "trace_checks.the_ticket_was_read.present.match.tool",
        "trace_checks.by_the_ticket_api.the_api_answered.present.match.tool",
    ]
    assert all("is not declared by this task" in finding.message for finding in report.errors)


_HASH_FLAGS_BOTH_SUBSTRATES_GRADE_ON = (
    pytest.param(True, id="written_true"),
    pytest.param(1, id="written_one"),
    pytest.param("yes", id="written_yes"),
)


@pytest.mark.parametrize("enabled", _HASH_FLAGS_BOTH_SUBSTRATES_GRADE_ON)
def test_a_truthy_hash_flag_is_not_a_finding(enabled: Any) -> None:
    """The gate may not be stricter than either substrate it speaks for.

    Boundary case, standing lock: core branches on the flag's truthiness and the
    runner coerces it, so a pack written ``enabled: 1`` does read the hash and
    grades on it. Testing the flag for ``True`` rather than for truth rejects a
    pack that works — the opposite failure to the one the rule exists to catch,
    and one no sweep over shipped packs finds because they all write ``true``.
    """
    grading = {"state_checks": {"hash": {"enabled": enabled, "expect_initial_state": True}}}

    assert (
        inspect_grading_authoring(
            grading, _inventory(_HELPDESK), seeded_tables=_THE_TASK_SEEDS_THESE_TABLES
        )
        == AuthoringReport()
    )


@pytest.mark.parametrize("enabled", _HASH_FLAGS_BOTH_SUBSTRATES_GRADE_ON)
def test_a_truthy_hash_flag_with_no_source_is_refused(enabled: Any) -> None:
    """Every flag spelling that grades needs something to grade against.

    The same three spellings the rule above may not refuse *with* a source it must
    refuse *without* one, and for the same reason read the other way: a flag core
    branches on and the runner coerces is a flag that reaches the hash evaluator, so
    testing it for ``True`` here would leave ``enabled: 1`` free to carry the
    divergence the rule exists to close.
    """
    grading = {"state_checks": {"hash": {"enabled": enabled}}}

    report = inspect_grading_authoring(
        grading, _inventory(_HELPDESK), hash_sources=_THE_BLOCK_IS_THE_WHOLE_LAYER
    )

    assert [finding.where for finding in report.errors] == ["state_checks.hash.enabled"]


def test_an_enabled_hash_beside_an_empty_jsonpath_list_is_refused() -> None:
    """Standing single case: the shape whose two substrates decide it differently.

    Core evaluates neither source here — no hash to compare and no assertion to run —
    so its ``state_checks`` component rests on no evidence at all, while the runner's
    refusal semantics compare the trial against its initial state and hand the fold a
    real binary verdict. Refusing the shape is what keeps that cell out of the
    authorable set; a gate that accepted it would hand both substrates the same pack
    to disagree over.
    """
    grading = {"state_checks": {"hash": {"enabled": True}, "jsonpaths": []}}

    report = inspect_grading_authoring(
        grading, _inventory(_HELPDESK), hash_sources=_THE_BLOCK_IS_THE_WHOLE_LAYER
    )

    assert [finding.where for finding in report.errors] == ["state_checks.hash.enabled"]


def test_an_empty_golden_action_list_is_not_a_hash_source() -> None:
    """Standing single case: a declared source that replays nothing is no source.

    An empty list is the shape a pack reaches by deleting the actions and leaving the
    key, and both substrates already read it as absent — core names it as the missing
    source in ``grade.reasons`` and the runner replays nothing. A gate keying on the
    key's *presence* would accept it and leave the divergence reachable.
    """
    grading = {"state_checks": {"hash": {"enabled": True, "golden_actions": []}}}

    report = inspect_grading_authoring(
        grading, _inventory(_HELPDESK), hash_sources=_THE_BLOCK_IS_THE_WHOLE_LAYER
    )

    assert [finding.where for finding in report.errors] == ["state_checks.hash.enabled"]


def test_the_frozen_adapter_convention_is_unchecked_rather_than_refused() -> None:
    """The shape #911 was filed for: hash on, nothing declared, the adapter supplies it.

    ``enabled: true`` beside only a ``weight`` is the frozen-core adapters' own
    convention — the source lives in a golden-actions fixture the block never names —
    so under a layer no caller resolved, the rule reports the shape it cannot check
    instead of refusing packs that grade fine. The skip is addressed where the native
    refusal would have been, so an author reading either finds the same key.
    """
    grading = {"state_checks": {"hash": {"enabled": True, "weight": 1.0}}}

    report = inspect_grading_authoring(
        grading,
        ToolInventory.unresolvable(),
        hash_sources=_AN_ADAPTER_MAY_SUPPLY_THE_SOURCE,
        seeded_tables=_THE_TASK_SEEDS_THESE_TABLES,
    )

    assert report.errors == ()
    assert report.advisories == ()
    assert [skip.where for skip in report.unchecked if skip.where != "grading"] == [
        "state_checks.hash.enabled"
    ]


def test_a_block_the_hash_rule_accepts_reports_nothing_on_an_unresolved_layer() -> None:
    """A clean block is clean on either layer, so the skip names only refusable shapes.

    Reporting every external pack's hash block as unchecked would bury the one skip that
    means something under one per healthy pack: the rule found nothing to refuse, so
    there is nothing it failed to check.
    """
    grading = {"state_checks": {"hash": {"enabled": True, "expect_initial_state": True}}}

    report = inspect_grading_authoring(
        grading,
        _inventory(_HELPDESK),
        hash_sources=_AN_ADAPTER_MAY_SUPPLY_THE_SOURCE,
        seeded_tables=_THE_TASK_SEEDS_THESE_TABLES,
    )

    assert report == AuthoringReport()


def test_an_adapter_supplying_a_usable_source_leaves_the_bare_block_clean() -> None:
    """An answered layer makes the frozen-core convention a plain pass.

    The same bare block an unresolved layer leaves unchecked, under an adapter that says
    which fixture it reads and that the fixture is usable: the pack is checked, not
    merely unrefused, so it draws nothing on any channel. A skip here would report every
    healthy external pack as something a caller failed to check, which is exactly what
    an answer removes.
    """
    grading = {"state_checks": {"hash": {"enabled": True, "weight": 1.0}}}

    report = inspect_grading_authoring(
        grading,
        _inventory(_HELPDESK),
        hash_sources=_AN_ADAPTER_SUPPLIES_A_USABLE_SOURCE,
        seeded_tables=_THE_TASK_SEEDS_THESE_TABLES,
    )

    assert report == AuthoringReport()


def test_an_unresolvable_hash_source_layer_may_not_carry_a_supplied_source() -> None:
    """The two states decide opposite things, so a hybrid decides neither.

    ``known=False`` skips the rule that reads the layer, so a fixture reported beside it
    is resolved and then ignored — a lost source read as merely unchecked, which is the
    silence this rule exists to break. The same guard the tool inventory, the replay
    world and the combine layer carry, for the same reason.
    """
    with pytest.raises(ValueError, match="unresolvable hash-source layer carries facts"):
        HashSourceLayer(
            known=False,
            supplied=AdapterHashSource(
                where=_A_FIXTURE_AN_ADAPTER_READS, state=SuppliedSourceState.MISSING
            ),
        )

    assert HashSourceLayer.unresolvable().supplied is None


# ---------------------------------------------------------------------------
# The id_fields declaration, held against what the task seeds
# ---------------------------------------------------------------------------


def test_a_relaxed_declaration_is_downgraded_to_a_log_line_and_nothing_else(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The escape hatch has to leave every report channel empty, advisories included.

    The default ``fail_on`` is ADVISORY, so reporting the downgrade as an advisory
    would fail precisely the packs ``relaxed_validation`` exists to pass — the gentler
    channel is the harsher answer here. A logged warning is the whole observable, which
    is what the run path already does with the same findings.
    """
    grading = {
        "state_checks": {
            "jsonpaths": [_A_JSONPATH_ASSERTION],
            "id_fields": {"positions": ["account_id", "ticker"]},
            "relaxed_validation": True,
        }
    }

    with caplog.at_level(logging.WARNING):
        report = inspect_grading_authoring(
            grading, _inventory(_HELPDESK), seeded_tables=_THE_TASK_SEEDS_THESE_TABLES
        )

    assert (report.errors, report.advisories, report.unchecked) == ((), (), ())
    warned = [record.getMessage() for record in caplog.records if record.levelno >= logging.WARNING]
    assert any("'ticker'" in message for message in warned), warned


def test_a_declaration_the_seeded_records_answer_reports_nothing() -> None:
    """The positive half: a key that does address the seeded rows draws no line at all.

    Without it the rows above would pass against a checker that reported every
    declaration, and the ``?`` line for an unresolvable layer would read as the only
    outcome an author ever sees.
    """
    report = inspect_grading_authoring(
        _keyed_state({"positions": ["account_id", "symbol"]}),
        _inventory(_HELPDESK),
        seeded_tables=_THE_TASK_SEEDS_THESE_TABLES,
    )

    assert (report.errors, report.advisories, report.unchecked) == ((), (), ())


@pytest.mark.parametrize(
    "seeded_tables",
    [
        pytest.param(_NO_CALLER_READ_WHAT_THE_TASK_SEEDS, id="nothing_resolved_the_tables"),
        pytest.param(_THE_TASK_SEEDS_THESE_TABLES, id="the_tables_are_resolved"),
    ],
)
@pytest.mark.parametrize(
    "state_checks",
    [
        pytest.param({"jsonpaths": [_A_JSONPATH_ASSERTION]}, id="no_id_fields_key"),
        pytest.param({"jsonpaths": [_A_JSONPATH_ASSERTION], "id_fields": {}}, id="an_empty_map"),
    ],
)
def test_a_pack_declaring_no_key_draws_nothing_under_either_layer(
    state_checks: dict[str, Any], seeded_tables: SeededTablesLayer
) -> None:
    """A pack that declares no key is not owed a ``?`` line about one.

    The unresolvable layer is every caller's default, so a skip for a declaration
    nobody wrote would print an unchecked line beside every task in the corpus.

    The claim is about the ``id_fields`` address alone. These blocks assert over the
    trial's database, so under an unresolvable layer the sibling rule asking whether
    the task seeds one reports its own skip — at ``state_checks``, about a declaration
    the author did write.
    """
    report = inspect_grading_authoring(
        {"state_checks": state_checks}, _inventory(_HELPDESK), seeded_tables=seeded_tables
    )

    assert (report.errors, report.advisories) == ((), ())
    assert [skip for skip in report.unchecked if skip.where == "state_checks.id_fields"] == []


def test_a_database_reading_block_is_clean_on_a_task_that_seeds_the_tables() -> None:
    """The accepting half: the rule refuses an absent database, not a present one."""
    report = inspect_grading_authoring(
        {"state_checks": {"jsonpaths": [_A_JSONPATH_ASSERTION]}},
        _inventory(_HELPDESK),
        seeded_tables=_THE_TASK_SEEDS_THESE_TABLES,
    )

    assert report == AuthoringReport()


def test_an_unresolvable_layer_skips_the_seeded_read_and_still_refuses_the_path() -> None:
    """One unresolvable input silences one rule, not the block.

    The two rules read different things: what the task seeds is a fact about the
    ``task.yaml``, and where a path is rooted is a fact about the block. So a caller
    that cannot resolve the first still gets the second — which matters because the
    gate is skipped wholesale for exactly the tasks whose seeded tables no caller can
    read, and a silent pass there would be the shape this rule exists to close.
    """
    report = inspect_grading_authoring(
        {"state_checks": {"jsonpaths": [_A_JSONPATH_ASSERTION, _AN_UNREACHABLE_PATH_ASSERTION]}},
        _inventory(_HELPDESK),
        seeded_tables=_NO_CALLER_READ_WHAT_THE_TASK_SEEDS,
    )

    assert [skip.where for skip in report.unchecked] == ["state_checks"]
    assert "not checkable here" in report.unchecked[0].reason
    assert [finding.where for finding in report.errors] == ["state_checks.jsonpaths"]
    assert "does not carry" in report.errors[0].message


def test_a_source_less_hash_block_on_a_task_that_seeds_nothing_is_refused() -> None:
    """The hash half of the same rule, in the shape that declares no source at all.

    ``_execute_hash_grading`` reads the trial's stable hash before it consults either
    source, so this block reaches the database exactly as one declaring both does. It
    is addressed to the flag, which is the key that put it there.
    """
    report = inspect_grading_authoring(
        {"state_checks": {"hash": {"enabled": True}}},
        _inventory(_HELPDESK),
        hash_sources=_AN_ADAPTER_SUPPLIES_A_USABLE_SOURCE,
        seeded_tables=_THE_TASK_SEEDS_NO_TABLES,
    )

    assert [finding.where for finding in report.errors] == ["state_checks.hash.enabled"]
    assert "seeds no tables" in report.errors[0].message


def test_a_path_glob_compared_with_contains_ci_is_the_shape_the_rule_accepts() -> None:
    """The operator both substrates read draws nothing — the pairing the docs prescribe."""
    report = inspect_grading_authoring(
        {"state_checks": {"jsonpaths": [_A_FILE_ASSERTION]}},
        _inventory(_HELPDESK),
        seeded_tables=_THE_TASK_SEEDS_THESE_TABLES,
    )

    assert report == AuthoringReport()


def test_relaxed_validation_does_not_downgrade_a_block_that_cannot_grade() -> None:
    """The escape hatch that passes an ``id_fields`` pack does not pass this one.

    ``relaxed_validation`` exists for a declaration whose keys no longer resolve
    against seeded records — a pack that still grades. A block reading a database its
    task does not provision grades on neither substrate, so downgrading it would hand
    the author a green gate and a failed run.
    """
    report = inspect_grading_authoring(
        {"state_checks": {"jsonpaths": [_A_JSONPATH_ASSERTION], "relaxed_validation": True}},
        _inventory(_HELPDESK),
        seeded_tables=_THE_TASK_SEEDS_NO_TABLES,
    )

    assert [finding.where for finding in report.errors] == ["state_checks.jsonpaths"]


def test_a_probe_expectation_is_not_addressed_against_the_trials_jsonpath_state() -> None:
    """The new rules read ``jsonpaths`` and nothing else — a check on their domain.

    A ``db_probes`` expectation writes ``path:`` too, but against the probe's own query
    result — ``{rows, row_count}``, fetched over the probe's ``dsn`` with no trial id.
    Ten such assertions ship in two packs that grade correctly, and every one of them is
    rooted where the trial's JSONPath state carries nothing. A rule helpfully widened to
    walk expectations would refuse them all.
    """
    report = inspect_grading_authoring(
        {
            "state_checks": {
                "db_probes": [
                    {
                        "name": "orders_shipped",
                        "dsn": "postgresql://grader@app-db:5432/app",
                        "query": "SELECT status FROM orders",
                        "expect": [{"path": "$.rows[0].status", "equals": "shipped"}],
                    }
                ]
            }
        },
        _inventory(_HELPDESK),
        seeded_tables=_THE_TASK_SEEDS_NO_TABLES,
    )

    assert report.errors == ()
    assert report.advisories == ()


def test_a_seeded_tables_layer_is_resolved_or_it_is_not() -> None:
    """Both halves of the invariant, because both are incoherent in the same way.

    A resolved layer with no view would hold every declaration against nothing and
    refuse every table as unknown; an unresolvable one carrying tables has them
    resolved and then ignored, since the rule that reads them is skipped.
    """
    with pytest.raises(ValueError, match="resolved seeded-tables layer carries no view"):
        SeededTablesLayer(tables=None)
    with pytest.raises(ValueError, match="unresolvable seeded-tables layer carries facts"):
        SeededTablesLayer(tables={}, known=False)

    assert SeededTablesLayer.unresolvable().tables is None
    assert SeededTablesLayer(tables={}).known is True


def test_the_same_defect_reads_the_same_at_the_adapter_and_at_validate(tmp_path: Path) -> None:
    """One pack, both gates, one sentence — the guard against re-divergence.

    ``tolokaforge validate`` and ``NativeAdapter.to_task_description`` refuse the same
    declaration, and an author who fixes what one of them said has fixed what the other
    would have said. Locking the sentence rather than the fact of refusal is the point:
    two implementations agreeing that a pack is broken while disagreeing about which
    table and which component is the state this check was written to end.
    """
    task_dir = tmp_path / "tasks" / "positions"
    task_dir.mkdir(parents=True)
    (task_dir / "initial_state.json").write_text(json.dumps(_TWO_ROWS_ONE_COMPONENT_CANNOT_KEY))
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "task_id": "positions",
                "description": "one defective declaration, read by both gates",
                "initial_state": {"json_db": "initial_state.json"},
                "tools": {"agent": {"enabled": []}},
                "grading": "grading.yaml",
            }
        )
    )
    (task_dir / "grading.yaml").write_text(
        yaml.safe_dump(
            {
                "combine": {"method": "weighted", "weights": {"state_checks": 1.0}},
                "state_checks": {
                    "jsonpaths": [_A_JSONPATH_ASSERTION],
                    "id_fields": {"positions": ["account_id", "ticker"]},
                },
            }
        )
    )
    task, effective_dir = load_task_yaml(task_dir / "task.yaml")

    with pytest.raises(ValueError) as at_the_adapter:
        NativeAdapter(
            {"base_dir": str(tmp_path), "tasks_glob": "tasks/**/task.yaml"}
        ).to_task_description("positions")
    with pytest.raises(ValueError) as at_validate:
        validate_grading_yaml(
            task_dir / "grading.yaml",
            inventory=ToolInventory.unresolvable(),
            seeded_tables=seeded_tables_under_adapter(task, effective_dir, task.adapter_type),
        )

    sentence = (
        "state_checks.id_fields['positions'] declares key component(s) ['ticker'] absent "
        "from every seeded record of table 'positions'"
    )
    assert sentence in str(at_the_adapter.value)
    assert sentence in str(at_validate.value)


def test_golden_actions_alone_are_a_hash_source() -> None:
    """Standing single case: the source shape both substrates are proven to share.

    The replay is what every shipped golden-action pack grades by, so a rule reading
    only its sibling source would refuse the hash shape most in-tree packs are
    authored in. The action names a tool the task declares
    and the task gives the replay a world to be built in, which are the other two
    things a replayable source needs.
    """
    grading = _golden_actions({"name": "write_file"})

    report = inspect_grading_authoring(
        grading,
        _inventory(_HELPDESK),
        replay_world=_A_BUILDABLE_WORLD,
        seeded_tables=_THE_TASK_SEEDS_THESE_TABLES,
    )

    assert report == AuthoringReport()


_GOLDEN_ACTIONS_NO_REPLAY_CAN_RUN = (
    pytest.param({"name": "close_widget"}, id="a_tool_no_actor_is_given"),
    pytest.param({"kwargs": {"widget_id": "W1"}}, id="no_name_key"),
    pytest.param({"name": ""}, id="name_written_empty"),
    pytest.param({"name": None}, id="name_written_null"),
    pytest.param({"name": ["close_widget"]}, id="name_written_as_a_list"),
)


@pytest.mark.parametrize("action", _GOLDEN_ACTIONS_NO_REPLAY_CAN_RUN)
def test_every_golden_action_shape_no_replay_can_run_is_refused(action: dict[str, Any]) -> None:
    """Every shape that leaves a trial paid for and unscored draws one finding each.

    All of them resolve to nothing wherever a replay reads them, and both substrates
    refuse the replay outright rather than skipping the action — so the pack costs a full
    trial and takes no state-hash verdict. ``name: null`` never reaches a replay at all:
    it fails ``GoldenAction`` construction inside the adapter with a Pydantic message
    about a string, so the gate naming the action is the only reading an author can act
    on. The ``hash`` block is untyped, so a name written as a list reaches the rule as an
    unhashable value and is refused rather than tested for membership: the gate answers
    with findings and raises for nothing, and a bare ``TypeError`` reaches
    ``tolokaforge validate`` carrying no address and passes the pre-run preflight's
    ``(ValueError, RuntimeError, OSError)`` arm as a harness fault.
    """
    report = inspect_grading_authoring(
        _golden_actions(action), _inventory(_HELPDESK), replay_world=_A_BUILDABLE_WORLD
    )

    assert [finding.where for finding in report.errors] == [
        "state_checks.hash.golden_actions[0].name"
    ]
    assert report.advisories == ()
    assert "['http_request', 'write_file']" in report.errors[0].message


def test_each_unreplayable_golden_action_is_addressed_by_its_own_index() -> None:
    """An author correcting a golden path is shown every offending action, not the first.

    The index is the only thing that tells two actions apart — a name may repeat, and a
    nameless one carries nothing at all — so a rule reporting the block rather than the
    action leaves the author to find them by bisection.
    """
    grading = _golden_actions(
        {"name": "write_file"}, {"name": "close_widget"}, {"kwargs": {"widget_id": "W1"}}
    )

    report = inspect_grading_authoring(
        grading, _inventory(_HELPDESK), replay_world=_A_BUILDABLE_WORLD
    )

    assert [finding.where for finding in report.errors] == [
        "state_checks.hash.golden_actions[1].name",
        "state_checks.hash.golden_actions[2].name",
    ]


_WORLDS_A_DISABLED_FLAG_IS_READ_AGAINST = (
    pytest.param(_A_BUILDABLE_WORLD, id="a_world_the_replay_could_be_built_in"),
    pytest.param(_A_TASK_SUPPLYING_NEITHER, id="a_task_supplying_neither_fact"),
    pytest.param(ReplayWorld.unresolvable(), id="a_world_no_caller_resolved"),
)


@pytest.mark.parametrize("world", _WORLDS_A_DISABLED_FLAG_IS_READ_AGAINST)
def test_a_golden_action_under_a_disabled_flag_is_refused_at_the_source(
    world: ReplayWorld,
) -> None:
    """A replay nobody runs is refused at the source key, not at the action.

    Both substrates test the flag before they read any source, so the block grades the
    state with no hash at all — core takes no verdict and the runner builds a
    description carrying no golden actions — and the whole golden path is dead weight.
    The name rule stays out of it for the reason it stays out of every falsy-flag block:
    refusing a name nobody resolves would be stricter than the grade. So the one finding
    is the flag's, addressed at the source the pack wrote, and the action names a tool
    the task gives no actor precisely to prove the name rule does not join in.

    Held against every world a task can give a replay, because the replay-world rule
    reads the flag before it reads anything else: a task supplying neither fact draws no
    second finding for a replay nobody runs, and one no caller resolved draws no skip.
    """
    grading = _golden_actions({"name": "close_widget"}, enabled=False)

    report = inspect_grading_authoring(
        grading,
        _inventory(_HELPDESK),
        replay_world=world,
        hash_sources=_THE_BLOCK_IS_THE_WHOLE_LAYER,
    )

    assert [finding.where for finding in report.errors] == ["state_checks.hash.golden_actions"]
    assert report.advisories == ()
    assert report.unchecked == ()


#: One authorable value per hash source, so the totality lock below can write whichever
#: key the table names. A source added to ``HASH_SOURCE_KEYS`` with no value here fails
#: that lock with a ``KeyError`` naming it.
_A_TRUTHY_HASH_SOURCE: dict[str, Any] = {
    "golden_actions": [{"name": "write_file"}],
    "expect_initial_state": True,
}


@pytest.mark.parametrize("key", HASH_SOURCE_KEYS)
def test_every_hash_source_under_a_disabled_flag_is_refused_at_its_own_key(key: str) -> None:
    """The rule reads the source table rather than one member of it by name.

    Each source alone under a falsy flag is the same defect — a comparison nothing runs,
    on either substrate — so each draws one finding at the key its author wrote. Read off
    ``HASH_SOURCE_KEYS`` rather than listed here, because the defect this closes *is* a
    rule naming one source by hand: a third source joining the tuple fails here until
    the rule reads it too.
    """
    grading = {"state_checks": {"hash": {"enabled": False, key: _A_TRUTHY_HASH_SOURCE[key]}}}

    report = inspect_grading_authoring(
        grading,
        _inventory(_HELPDESK),
        replay_world=_A_BUILDABLE_WORLD,
        hash_sources=_THE_BLOCK_IS_THE_WHOLE_LAYER,
    )

    assert [finding.where for finding in report.errors] == [f"state_checks.hash.{key}"]
    assert key in report.errors[0].message


_HASH_FLAGS_NEITHER_SUBSTRATE_GRADES_ON = (
    pytest.param({"enabled": False}, id="written_false"),
    pytest.param({"enabled": 0}, id="written_zero"),
    pytest.param({"enabled": None}, id="written_null"),
    pytest.param({}, id="no_enabled_key_at_all"),
    # The four YAML spellings a raw truthiness read gets backwards: every one of them is
    # a non-empty string, and every one of them is the ``False`` Pydantic hands both
    # substrates before either evaluator branches on the flag.
    pytest.param({"enabled": "false"}, id="written_false_quoted"),
    pytest.param({"enabled": "no"}, id="written_no"),
    pytest.param({"enabled": "off"}, id="written_off"),
    pytest.param({"enabled": "0"}, id="written_zero_quoted"),
)


@pytest.mark.parametrize("flag", _HASH_FLAGS_NEITHER_SUBSTRATE_GRADES_ON)
def test_every_flag_spelling_that_reads_no_source_refuses_the_one_declared(
    flag: dict[str, Any],
) -> None:
    """The mirror of the truthy spellings, over the source that used to escape.

    However the flag is written, neither substrate reads a source behind one a run
    switches off, so every spelling is the same defect and draws the same finding — a
    block omitting ``enabled`` altogether included, which is what an author reaches by
    deleting the flag rather than the source. The message quotes the author's own text
    rather than the coerced flag the rule branched on, because ``enabled: 0`` and
    ``enabled: null`` are fixed by writing ``true`` where a reader told "the flag is
    off" would go looking for a ``false`` that is not there.
    """
    grading = {"state_checks": {"hash": {**flag, "golden_actions": [{"name": "write_file"}]}}}

    report = inspect_grading_authoring(
        grading,
        _inventory(_HELPDESK),
        replay_world=_A_BUILDABLE_WORLD,
        hash_sources=_THE_BLOCK_IS_THE_WHOLE_LAYER,
    )

    assert [finding.where for finding in report.errors] == ["state_checks.hash.golden_actions"]
    assert f"hash.enabled is {flag.get('enabled')!r}" in report.errors[0].message


@pytest.mark.parametrize("flag", _HASH_FLAGS_NEITHER_SUBSTRATE_GRADES_ON)
def test_a_flag_no_run_switches_on_reads_no_database_the_task_must_seed(
    flag: dict[str, Any],
) -> None:
    """The falsy mirror of the truthy standing lock, over the rule that costs a pack.

    A block a run reads as off grades no hash, so it reaches no DB service and the task
    beneath it needs to seed nothing. Refusing it would reject a pack that loads and
    grades cleanly — the failure this rule's own remedy cannot repair, since seeding
    tables for a hash nobody computes buys the author nothing.

    The block declares a source so it is the shape an author writes, and the hash-source
    layer is left unresolved so the sibling rule above skips rather than answering here:
    this cell speaks for the seeded-tables rule alone.
    """
    grading = {"state_checks": {"hash": {**flag, "expect_initial_state": True}}}

    report = inspect_grading_authoring(
        grading, _inventory(_HELPDESK), seeded_tables=_THE_TASK_SEEDS_NO_TABLES
    )

    assert report.errors == ()
    assert [skip.where for skip in report.unchecked] == ["state_checks.hash.expect_initial_state"]


#: Every row's verdict is written here rather than derived from either side, so a row
#: pins what "the hash is a state source" *means* instead of asserting the two readings
#: agree — two readings that drifted together would still fail. ``model_accepts`` is
#: ``False`` where ``StateHashConfig`` refuses the block outright; ``is_a_state_source``
#: is then ``False`` too, because a pack that cannot load is not a pack the probe
#: exclusivity rule reports a second finding over.
_HASH_BLOCK_STATE_SOURCE_VERDICTS = (
    pytest.param({}, True, False, id="empty_block"),
    pytest.param({"enabled": True}, True, False, id="flag_with_no_source"),
    pytest.param({"enabled": True, "golden_actions": []}, True, False, id="empty_replay"),
    pytest.param(
        {"enabled": False, "golden_actions": [{"name": "write_file"}]},
        True,
        False,
        id="replay_under_a_false_flag",
    ),
    pytest.param(
        {"enabled": True, "expect_initial_state": True}, True, True, id="flag_and_an_initial_state"
    ),
    pytest.param(
        {"enabled": True, "expect_initial_state": False},
        True,
        False,
        id="an_initial_state_written_off",
    ),
    pytest.param(
        {"enabled": True, "expect_initial_state": True, "golden_actions": [{"name": "write_file"}]},
        False,
        False,
        id="two_expected_states",
    ),
    pytest.param(
        {"enabled": "false", "expect_initial_state": True},
        True,
        False,
        id="yaml_string_false",
    ),
    pytest.param({"enabled": "no"}, True, False, id="yaml_string_no_alone"),
    pytest.param({"enabled": "no", "expect_initial_state": True}, True, False, id="yaml_string_no"),
    pytest.param(
        {"enabled": "off", "golden_actions": [{"name": "write_file"}]},
        True,
        False,
        id="yaml_string_off",
    ),
    pytest.param({"enabled": 1}, True, False, id="one_with_no_source"),
    pytest.param(
        {"enabled": 1, "expect_initial_state": True}, True, True, id="one_and_an_initial_state"
    ),
    pytest.param(
        {"enabled": "maybe", "expect_initial_state": True}, False, False, id="unparsable_flag"
    ),
    pytest.param(
        {"enabled": True, "expect_initial_state": 123}, False, False, id="source_not_a_bool"
    ),
    pytest.param(
        {"enabled": True, "expect_initial_state": True, "weight": 2.0},
        False,
        False,
        id="weight_out_of_domain",
    ),
    pytest.param(
        {"enabled": True, "expect_initial_state": True, "enalbed": True},
        False,
        False,
        id="undeclared_key",
    ),
)


@pytest.mark.parametrize(
    ("block", "model_accepts", "is_a_state_source"), _HASH_BLOCK_STATE_SOURCE_VERDICTS
)
def test_the_gate_and_the_model_answer_one_state_source_rule(
    block: dict[str, Any], model_accepts: bool, is_a_state_source: bool
) -> None:
    """The gate reads the block the way a run does, coercion included.

    ``StateHashConfig`` is what a run grades on, and Pydantic coerces the YAML string
    ``"false"`` — and ``"no"`` and ``"off"`` — to ``False``. A gate re-reading
    ``enabled`` off the raw mapping would read those as truthy and refuse a pack that
    loads and grades cleanly, so the two would disagree on which packs declare a hash
    source at all. Both sides are pinned to the table's own verdict, which is what
    stops the pair drifting together onto a rule neither should have.
    """
    if model_accepts:
        hash_config = StateHashConfig.model_validate(block)
        assert hash_block_is_a_state_source(hash_config) is is_a_state_source
    else:
        assert is_a_state_source is False, "a refused block declares nothing"
        with pytest.raises(ValidationError):
            StateHashConfig.model_validate(block)

    assert _authored_hash_is_a_state_source({"state_checks": {"hash": block}}) is is_a_state_source


def test_the_state_source_table_holds_all_three_answers() -> None:
    """A table of one answer would pass against a rule that always gives it.

    The lock above compares nothing across rows, so it is only as strong as the answers
    the table asks for: without a source row a rule returning ``False`` outright passes
    it, and without a refusal row the coercion the fix turns on is never reached.
    """
    answers = {(param.values[1], param.values[2]) for param in _HASH_BLOCK_STATE_SOURCE_VERDICTS}

    assert answers == {(True, True), (True, False), (False, False)}


def test_an_unresolvable_inventory_leaves_a_golden_action_name_unchecked() -> None:
    """The name rule needs the task's tools, so it is skipped with the rest of that group.

    One skip for the whole group, not one per rule: the block below is unreplayable
    against any tool set that resolves, and against an inventory that cannot answer it
    is simply not knowable — a gate that raised here would reject every pack whose
    adapter cannot report a tool set. The world is resolved and buildable, so the only
    skip in the report is the tool set's.
    """
    grading = _golden_actions({"name": "close_widget"})

    report = inspect_grading_authoring(
        grading,
        ToolInventory.unresolvable(),
        replay_world=_A_BUILDABLE_WORLD,
        seeded_tables=_THE_TASK_SEEDS_THESE_TABLES,
    )

    assert report.errors == ()
    assert [skip.where for skip in report.unchecked] == ["grading"]


_WORLDS_A_GOLDEN_REPLAY_CANNOT_BE_BUILT_IN = (
    pytest.param(_A_BUILDABLE_WORLD, (), id="every_fact_the_replay_needs"),
    pytest.param(
        _NO_INITIAL_STATE,
        ("this task declares no initial_state.json_db",),
        id="no_initial_state_at_all",
    ),
    pytest.param(
        _AN_INLINE_INITIAL_STATE,
        ("this task declares initial_state.json_db inline",),
        id="an_initial_state_written_inline",
    ),
    pytest.param(
        _NO_SERVER_MODULE,
        ("this task declares no tools.agent.mcp_server",),
        id="no_mcp_server_module",
    ),
    pytest.param(
        _A_TASK_SUPPLYING_NEITHER,
        (
            "this task declares no initial_state.json_db",
            "this task declares no tools.agent.mcp_server",
        ),
        id="neither_fact",
    ),
)


@pytest.mark.parametrize(("world", "expected"), _WORLDS_A_GOLDEN_REPLAY_CANNOT_BE_BUILT_IN)
def test_every_replay_fact_a_task_withholds_from_its_golden_path_is_its_own_error(
    world: ReplayWorld, expected: tuple[str, ...]
) -> None:
    """A golden path is authorable only against a task that gives it a world.

    Core hashes nothing without one: it raises out of the grading engine and the trial is
    left unscored, once every token is already spent — where before it produced a
    ``state_checks`` component its JSONPath assertions alone earned, indistinguishable
    from one whose hash matched. Each withheld fact is its own finding naming its own
    ``task.yaml`` key, so an author supplying two does not pay a grading pass per
    omission; an inline ``json_db`` is withheld rather than supplied, because the replay
    loads a file under the task directory.
    """
    report = inspect_grading_authoring(
        _golden_actions({"name": "write_file"}),
        _inventory(_HELPDESK),
        replay_world=world,
        seeded_tables=_THE_TASK_SEEDS_THESE_TABLES,
    )

    assert [finding.where for finding in report.errors] == [
        "state_checks.hash.golden_actions"
    ] * len(expected)
    for finding, because in zip(report.errors, expected, strict=True):
        assert finding.message.startswith(because), finding.message
    assert report.advisories == ()
    assert report.unchecked == ()


#: The two ``task.yaml`` keys a withheld world sends its reader to write.
_THE_KEYS_A_WITHHELD_WORLD_NAMES = frozenset({"initial_state.json_db", "tools.agent.mcp_server"})


@pytest.mark.parametrize(
    "sentences",
    [
        pytest.param(
            (*_NO_INITIAL_STATE_FILE.values(), _NO_MCP_SERVER_MODULE),
            id="the_gate_refusing_a_pack",
        ),
        pytest.param(
            (*_ABSENT_INITIAL_STATE.values(), _NO_MCP_SERVER),
            id="the_engine_refusing_a_grade",
        ),
    ],
)
def test_both_withheld_world_vocabularies_name_the_same_task_yaml_keys(
    sentences: tuple[str | None, ...],
) -> None:
    """Two message sets, two audiences, one set of keys to write.

    The gate addresses the pack's author before a trial is paid for and the engine
    addresses whoever holds it once one is, so the two sets of sentences are deliberately
    not shared. What a reader *acts on* is the same either way — a ``task.yaml`` key — and
    a set that stopped naming one would leave half of them nothing to fix.
    """
    named = {
        key
        for key in _THE_KEYS_A_WITHHELD_WORLD_NAMES
        if any(key in sentence for sentence in sentences if sentence is not None)
    }

    assert named == _THE_KEYS_A_WITHHELD_WORLD_NAMES


def test_a_world_no_caller_resolved_leaves_the_golden_replay_unchecked() -> None:
    """The unresolvable arm, which no corpus walk can reach.

    Every one of the 94 authored packs in the repository is native, so every world the
    canonical corpus guard resolves is ``known`` — this case carries the branch alone.
    A caller holding no ``task.yaml`` — the trace-replay batch and the rubric migration
    both check a ``trace_checks`` fragment against a bundle's recorded tools — must not
    have a pack refused for facts it never claimed to read, and must not read as a clean
    bill of health either.
    """
    report = inspect_grading_authoring(
        _golden_actions({"name": "write_file"}),
        _inventory(_HELPDESK),
        replay_world=ReplayWorld.unresolvable(),
        seeded_tables=_THE_TASK_SEEDS_THESE_TABLES,
    )

    assert report.errors == ()
    assert report.advisories == ()
    assert [(skip.where, skip.reason) for skip in report.unchecked] == [
        (
            "state_checks.hash.golden_actions",
            "no caller resolved what this task gives a golden replay, so whether the "
            "replay has a world to be built in is not checkable",
        )
    ]


def test_a_pack_that_replays_nothing_draws_no_skip_for_a_world() -> None:
    """A skip is emitted where the rule would have run, and nowhere else.

    90 of the repository's 94 authored packs replay no golden path, and the block below is
    what they look like. A skip reported for every unresolvable world would put an entry
    beside each of them for a rule that had nothing to check — noise in ``validate``'s
    output, and a corpus guard whose ``unchecked`` assertion can no longer say which
    rules were skipped.
    """
    grading = {"state_checks": {"jsonpaths": [{"path": "$.widgets[0].status", "equals": "closed"}]}}

    report = inspect_grading_authoring(
        grading,
        _inventory(_HELPDESK),
        replay_world=ReplayWorld.unresolvable(),
        seeded_tables=_THE_TASK_SEEDS_THESE_TABLES,
    )

    assert report == AuthoringReport()


def test_golden_actions_written_as_a_bare_string_draw_the_shape_and_the_missing_world() -> None:
    """Two rules, two true statements, two fixes — reported together at one address.

    The world rule reads the source for truth and never for shape, so a bare string with
    no world to replay in is refused for the world it lacks; the shape rule refuses the
    same value for being no list of actions. Neither suppresses the other: an author whose
    pack is wrong twice fixes both in one pass, which is the principle the world rule
    already applies to the two facts a task can withhold. ``_check_golden_action_names``
    stays silent, having no element to address.

    The shape is named first, which is the order core answers in — the refusal sits above
    the world it would otherwise need — so an author reading the gate's list and an author
    reading a grade-time raise meet the same sentence first.
    """
    grading = {"state_checks": {"hash": {"enabled": True, "golden_actions": "write_file"}}}

    report = inspect_grading_authoring(
        grading, _inventory(_HELPDESK), replay_world=_NO_SERVER_MODULE
    )

    assert [finding.where for finding in report.errors] == [
        "state_checks.hash.golden_actions",
        "state_checks.hash.golden_actions",
    ]
    assert "the list of actions a golden replay executes" in report.errors[0].message
    assert "tools.agent.mcp_server" in report.errors[1].message


#: Shared with the two substrate read sites, over a tool ``_HELPDESK`` declares.
_GOLDEN_SOURCES_NO_REPLAY_CAN_ITERATE = sources_no_replay_can_iterate("write_file")

#: Every falsy spelling of the source, which is no replay rather than a malformed one.
#: ``null`` is what an author reaches by commenting their actions out and leaving the key.
#: The empty list is :func:`test_an_empty_golden_action_list_is_not_a_hash_source`'s row.
_GOLDEN_SOURCES_THAT_REPLAY_NOTHING = (
    pytest.param(None, id="the_key_carrying_nothing"),
    pytest.param({}, id="an_empty_mapping"),
    pytest.param("", id="an_empty_string"),
    pytest.param(0, id="zero"),
    pytest.param(False, id="false"),
)

_TOOL_SETS_A_SHAPE_IS_KNOWABLE_WITHOUT = (
    pytest.param(False, id="a_resolved_tool_set"),
    pytest.param(True, id="a_tool_set_no_adapter_could_report"),
)


@pytest.mark.parametrize("unresolvable", _TOOL_SETS_A_SHAPE_IS_KNOWABLE_WITHOUT)
@pytest.mark.parametrize(("golden_actions", "kind"), _GOLDEN_SOURCES_NO_REPLAY_CAN_ITERATE)
def test_a_golden_source_no_replay_can_iterate_is_refused_at_the_source(
    golden_actions: Any, kind: str, unresolvable: bool
) -> None:
    """A source that is no list of actions costs the whole trial on either substrate.

    Core hands the authored value to the replay loop and the runner iterates it onto the
    wire, so each fails on it once the trial is paid for and neither names the key. The
    type received is named because it is what tells an author which line to look at: a
    mapping is one action that lost its ``-``, a string is a tool name written beside the
    key.

    Held against a tool set no adapter could report as well, where it must stay an error:
    whether a value is a list needs no tool set, so a shape defect skipped with the
    tool-aware rules would be lost for every pack whose inventory is unresolvable — and
    those are the packs no other surface answers either.
    """
    grading = {"state_checks": {"hash": {"enabled": True, "golden_actions": golden_actions}}}
    inventory = ToolInventory.unresolvable() if unresolvable else _inventory(_HELPDESK)

    report = inspect_grading_authoring(grading, inventory, replay_world=_A_BUILDABLE_WORLD)

    assert [finding.where for finding in report.errors] == ["state_checks.hash.golden_actions"]
    assert f"got {kind} ({golden_actions!r})" in report.errors[0].message
    assert report.advisories == ()
    assert "state_checks.hash.golden_actions" not in [skip.where for skip in report.unchecked]


def test_a_golden_source_no_replay_can_iterate_under_a_falsy_flag_is_the_flags_finding() -> None:
    """The two hash rules partition the flag, so the pack draws one finding either way.

    Nothing reads a source under a flag that is not truthy — the runner builds a
    description carrying no golden actions and core takes no verdict — so its shape is
    beside the point and the fix is the flag or the source. Reporting the shape here as
    well would charge one edit twice.
    """
    grading = {"state_checks": {"hash": {"enabled": False, "golden_actions": "write_file"}}}

    report = inspect_grading_authoring(
        grading,
        _inventory(_HELPDESK),
        replay_world=_A_BUILDABLE_WORLD,
        hash_sources=_THE_BLOCK_IS_THE_WHOLE_LAYER,
    )

    assert [finding.where for finding in report.errors] == ["state_checks.hash.golden_actions"]
    assert "hash.enabled is False" in report.errors[0].message
    assert "the list of actions a golden replay executes" not in report.errors[0].message


@pytest.mark.parametrize("golden_actions", _GOLDEN_SOURCES_THAT_REPLAY_NOTHING)
def test_a_falsy_golden_source_beside_the_other_source_is_no_finding(golden_actions: Any) -> None:
    """The domain's edge: a falsy source is no replay, not a malformed one.

    Every read site loads a falsy value as no actions to replay, which is what the
    no-source rule and every other rule in this family already read it as — so the shape
    rule may not widen from *truthy* to *declared*. The sibling source beside it is what
    keeps the no-source rule out of the way, leaving an empty report the only answer left.
    """
    grading = {
        "state_checks": {
            "hash": {
                "enabled": True,
                "expect_initial_state": True,
                "golden_actions": golden_actions,
            }
        }
    }

    report = inspect_grading_authoring(
        grading,
        _inventory(_HELPDESK),
        replay_world=_A_BUILDABLE_WORLD,
        seeded_tables=_THE_TASK_SEEDS_THESE_TABLES,
    )

    assert report == AuthoringReport()


@pytest.mark.parametrize("golden_actions", _GOLDEN_SOURCES_THAT_REPLAY_NOTHING)
def test_a_falsy_golden_source_alone_is_a_block_declaring_no_source(golden_actions: Any) -> None:
    """The other half of the domain's edge, and the partition it must not disturb.

    With nothing else declared the block asks for a hash it gives nothing to compare
    against, which is one finding at the flag from the rule that owns that shape. A shape
    rule reading the source for *presence* would report a second finding here for a value
    that is simply no source.
    """
    grading = {"state_checks": {"hash": {"enabled": True, "golden_actions": golden_actions}}}

    report = inspect_grading_authoring(
        grading,
        _inventory(_HELPDESK),
        replay_world=_A_BUILDABLE_WORLD,
        hash_sources=_THE_BLOCK_IS_THE_WHOLE_LAYER,
    )

    assert [finding.where for finding in report.errors] == ["state_checks.hash.enabled"]


def test_the_gate_and_a_substrate_refuse_a_shapeless_source_in_one_sentence() -> None:
    """One definition, two audiences — the pattern the withheld-world vocabularies use.

    Here the two audiences share the *whole* sentence rather than only the keys it names:
    the gate reports the shape before a trial is paid for and each substrate raises on it
    at its own read, and the fix is identical, so a second wording would be a second
    definition to drift.
    """
    grading = {"state_checks": {"hash": {"enabled": True, "golden_actions": "write_file"}}}

    report = inspect_grading_authoring(
        grading, _inventory(_HELPDESK), replay_world=_A_BUILDABLE_WORLD
    )
    with pytest.raises(UnreplayableGoldenSource) as raised:
        refuse_unreplayable_golden_source("write_file", context="grading.yaml")

    assert "state_checks.hash.golden_actions" in report.errors[0].message
    assert report.errors[0].message in str(raised.value)


def test_an_unresolvable_replay_world_may_not_carry_task_facts() -> None:
    """The two states cannot share one value, so the shape that conflates them is refused.

    A world reporting ``known=False`` is skipped wherever it is read, so a fact carried
    beside it is resolved and then ignored — and the fact that would have refused the
    pack is silently dropped instead. The same guard the tool inventory and the combine
    layer carry, for the same reason.
    """
    with pytest.raises(ValueError, match="carries task facts"):
        ReplayWorld(initial_state=InitialStateSource.JSON_FILE, mcp_server=True, known=False)


# The rest of a loadable config, because ``combine`` is required: without it every
# section below reads as refused, for a reason that has nothing to do with its block.
_A_MINIMAL_COMBINE = {"combine": {"method": "weighted", "weights": {}, "pass_threshold": 1.0}}


def _refuses_its_own_empty_block(section: str) -> bool:
    """Whether ``GradingConfig`` refuses *section* written ``{}``, on the block's account.

    The error has to name the section. A required field added elsewhere in the model
    would otherwise read as "no empty block loads", which silently empties the gate
    rule's scope instead of failing.
    """
    try:
        GradingConfig.model_validate({**_A_MINIMAL_COMBINE, section: {}})
    except ValidationError as error:
        return any(str(item["loc"][0]) == section for item in error.errors())
    return False


def test_the_sections_a_gate_rule_reaches_are_the_ones_the_models_still_admit() -> None:
    """Rule 1's scope is measured against the models, not listed beside them.

    Two of the five components already refuse their empty block at construction — a
    ``trace_checks`` block declaring neither constraints nor alternatives, and an
    ``llm_judge`` block with no rubric — so the gate rule finishes a call the project
    made twice rather than introducing one, and it reaches exactly the three that are
    left. Both columns are asserted rather than only their agreement: a component that
    grew its own refusal has to leave the gate rule's table, and one that lost it has
    to join it.

    The two tables the rule reads are held to the same key set, because the deeper
    one is looked up totally: a section joining the first without an answer in the
    second raises ``KeyError`` on every block the author writes.
    """
    sections = {spec.config_section for spec in GRADE_COMPONENTS}
    refused = {section for section in sections if _refuses_its_own_empty_block(section)}

    assert refused == {"trace_checks", "llm_judge"}
    assert sections - refused == {"state_checks", "transcript_rules", "custom_checks"}
    assert set(_WHAT_EACH_SECTION_MUST_DECLARE) == sections - refused
    assert set(_A_NON_EMPTY_SECTION_STILL_DECLARES) == set(_WHAT_EACH_SECTION_MUST_DECLARE)


_SOURCELESS_STATE_CHECKS = (
    pytest.param({}, "state_checks", id="an_empty_block"),
    pytest.param({"jsonpaths": []}, "state_checks", id="an_empty_assertion_list"),
    pytest.param(
        {"jsonpaths": [], "id_fields": {"widgets": "widget_id"}},
        "state_checks",
        id="only_keys_that_configure_how_a_source_is_read",
    ),
    pytest.param({"hash": {}}, "state_checks", id="a_hash_block_declaring_neither_half"),
    pytest.param(
        {"hash": {"enabled": False}}, "state_checks", id="a_hash_block_with_only_the_flag_off"
    ),
    pytest.param(
        {"hash": {"enabled": True}}, "state_checks.hash.enabled", id="the_flag_on_with_no_source"
    ),
    pytest.param(
        {"hash": {"enabled": True, "golden_actions": []}},
        "state_checks.hash.enabled",
        id="the_flag_on_with_an_empty_replay",
    ),
    pytest.param(
        {"hash": {"enabled": False, "golden_actions": []}},
        "state_checks",
        id="the_flag_off_over_an_empty_replay",
    ),
    pytest.param(
        {"hash": {"enabled": False, "expect_initial_state": True}},
        "state_checks.hash.expect_initial_state",
        id="an_initial_state_the_flag_never_reads",
    ),
    pytest.param(
        {"hash": {"enabled": False, "golden_actions": [{"name": "write_file"}]}},
        "state_checks.hash.golden_actions",
        id="a_replay_the_flag_never_runs",
    ),
)


@pytest.mark.parametrize(("state_checks", "address"), _SOURCELESS_STATE_CHECKS)
def test_every_state_block_that_evaluates_nothing_draws_exactly_one_finding(
    state_checks: dict[str, Any], address: str
) -> None:
    """The two rules over ``state_checks`` partition its unevaluable shapes.

    Every shape here scores nothing, and each must be refused once and addressed at
    the key its own fix belongs to: a block that declares no source at all is the
    section's finding, and a hash block whose flag and source disagree is the hash
    rule's. Asserting the *whole* list rather than membership is what holds the
    partition — a rule widened to cover a shape the other already owns shows up here
    as two findings for one defect, and one narrowed shows up as none.
    """
    report = inspect_grading_authoring(
        {"state_checks": state_checks},
        _inventory(_HELPDESK),
        hash_sources=_THE_BLOCK_IS_THE_WHOLE_LAYER,
    )

    assert [finding.where for finding in report.errors] == [address]


# Every source shape a probe can be declared beside, and the address each draws its one
# finding at. The refused rows are the ones where two sources score the same component;
# the admitted rows are the smallest edits to them that discard nothing, and the last
# three belong to the hash rule instead — which is where the partition is held.
_A_PROBE_BESIDE = (
    pytest.param({}, [], id="nothing_else_at_all"),
    pytest.param({"jsonpaths": []}, [], id="an_empty_assertion_list"),
    pytest.param({"hash": {}}, [], id="a_hash_block_declaring_neither_half"),
    pytest.param({"hash": {"enabled": False}}, [], id="a_hash_block_with_the_flag_off"),
    pytest.param(
        {"jsonpaths": [_A_JSONPATH_ASSERTION]}, ["state_checks.db_probes"], id="one_assertion"
    ),
    pytest.param(
        {"hash": {"enabled": True, "expect_initial_state": True}},
        ["state_checks.db_probes"],
        id="an_enabled_hash_over_an_initial_state",
    ),
    pytest.param(
        {"hash": {"enabled": True, "golden_actions": [{"name": "write_file"}]}},
        ["state_checks.db_probes"],
        id="an_enabled_hash_over_a_replay",
    ),
    pytest.param(
        {"hash": {"enabled": 1, "expect_initial_state": True}},
        ["state_checks.db_probes"],
        id="a_flag_written_one_rather_than_true",
    ),
    pytest.param(
        {"hash": {"enabled": True}},
        ["state_checks.hash.enabled"],
        id="an_enabled_hash_with_nothing_to_compare",
    ),
    pytest.param(
        {"hash": {"enabled": True, "golden_actions": []}},
        ["state_checks.hash.enabled"],
        id="an_enabled_hash_over_an_empty_replay",
    ),
    pytest.param(
        {"hash": {"enabled": False, "expect_initial_state": True}},
        ["state_checks.hash.expect_initial_state"],
        id="a_source_the_flag_never_reads",
    ),
)


@pytest.mark.parametrize(("beside", "addresses"), _A_PROBE_BESIDE)
def test_the_state_sources_a_probe_may_be_declared_beside(
    beside: dict[str, Any], addresses: list[str]
) -> None:
    """Two rules over ``state_checks`` partition its *over*-declared shapes too.

    Only the runner evaluates a probe, so a probe beside a source the fold also scores
    leaves one component holding two verdicts and each substrate discarding a different
    one. The admitted rows are what keeps that from being a blanket refusal of the key: a
    disabled hash, a hash block declaring neither half, and an empty assertion list each
    produce no second verdict, so nothing is discarded beside the probe.

    The last three rows are the partition against :func:`_check_hash_source_declared`,
    which owns every hash block whose flag and source disagree — a probe does not move
    that ownership, and asserting the whole list rather than membership is what shows the
    two rules never report one defect twice.

    The inventory is unresolvable and the replay world is left so, which makes every row
    the proof that this rule reads the authored block alone: no tool name, no world, no
    fold. The hash layer is resolved, because the last three rows are the hash rule's and
    that rule does read it.
    """
    report = inspect_grading_authoring(
        _probes_beside(**beside),
        ToolInventory.unresolvable(),
        hash_sources=_THE_BLOCK_IS_THE_WHOLE_LAYER,
    )

    assert [finding.where for finding in report.errors] == addresses
    assert report.advisories == ()


# The other two sections rule 1 reaches, refused and surviving shapes in one table
# because the boundary between them is the whole content of each rule. Every refused
# row scored a vacuous ``1.0`` — the block reads as configured, is evaluated against
# nothing, and averaging an empty sub-check set produces a perfect score — and every
# row is the smallest edit to a neighbour that crosses the line, so a rule widened
# past what it owns shows up here as a survivor that stopped loading.
_SECTIONS_WITH_KEYS_THAT_CONFIGURE_RATHER_THAN_ASSERT = (
    pytest.param({"transcript_rules": {}}, ["transcript_rules"], id="an_empty_transcript_block"),
    pytest.param(
        {"transcript_rules": {"required_actions": []}},
        ["transcript_rules"],
        id="an_empty_required_action_list",
    ),
    pytest.param(
        {"transcript_rules": {"must_contain": [], "disallow_regex": [], "communicate_info": []}},
        ["transcript_rules"],
        id="every_phrase_list_empty_at_once",
    ),
    pytest.param(
        {"transcript_rules": {"tool_expectations": {"required_tools": [], "disallowed_tools": []}}},
        ["transcript_rules"],
        id="a_tool_expectations_block_expecting_neither",
    ),
    pytest.param(
        {"transcript_rules": {"must_contain": ["cannot"]}}, [], id="one_phrase_to_search_for"
    ),
    pytest.param({"transcript_rules": {"max_turns": 10}}, [], id="a_bound_on_the_turn_counter"),
    pytest.param(
        {"transcript_rules": {"min_assistant_turns": 1}}, [], id="an_activity_floor_alone"
    ),
    pytest.param(
        {"transcript_rules": {"tool_expectations": {"required_tools": ["http_request"]}}},
        [],
        id="one_required_tool",
    ),
    pytest.param(
        {"transcript_rules": {"tool_expectations": {"disallowed_tools": ["http_request"]}}},
        [],
        id="one_forbidden_tool",
    ),
    pytest.param({"custom_checks": {}}, ["custom_checks"], id="an_empty_custom_checks_block"),
    pytest.param(
        {"custom_checks": {"file": "checks.py"}},
        ["custom_checks"],
        id="a_checks_file_under_no_flag",
    ),
    pytest.param({"custom_checks": {"enabled": False}}, [], id="the_opt_out_written_down"),
    pytest.param(
        {"custom_checks": {"enabled": True, "file": "checks.py"}}, [], id="the_suite_switched_on"
    ),
)


@pytest.mark.parametrize(
    ("grading", "addresses"), _SECTIONS_WITH_KEYS_THAT_CONFIGURE_RATHER_THAN_ASSERT
)
def test_a_block_whose_every_key_only_configures_how_it_runs_is_refused(
    grading: dict[str, Any], addresses: list[str]
) -> None:
    """Rule 1 reaches past the empty mapping for all three sections it owns.

    A ``transcript_rules`` whose every rule list is empty and a ``custom_checks``
    naming a file under no ``enabled`` flag are both non-empty mappings that assert
    nothing: the first is scored against no sub-check, and the second never runs
    because ``CustomChecksConfig.enabled`` defaults to ``False``. Both took a free
    ``1.0`` while reading as configured, which is what makes them the same defect as
    the empty block rather than a milder one.

    The surviving rows are asserted in the same table so the refusal is a boundary
    and not a blanket: a bound on the turn counter, one phrase, one tool on either
    expectation list, and the opt-out written down all still load.
    """
    report = inspect_grading_authoring(grading, _inventory(_HELPDESK))

    assert [finding.where for finding in report.errors] == addresses


def test_the_transcript_rules_the_gate_reads_are_the_ones_the_model_declares() -> None:
    """The predicate's scope is measured against ``TranscriptRulesConfig``, not listed.

    A rule key added to the model and not here would declare something the gate reads
    as nothing, refusing a pack that grades — the failure direction a list beside the
    model cannot catch. ``tool_expectations`` is the one field read a level down,
    through the two keys the tool-name rule already addresses, so its own key set is
    held against ``ToolExpectations`` in the same breath.

    The author-facing sentence is checked against the same sets, since a message that
    named a key the predicate stopped reading would send the author to declare
    something the gate goes on refusing.
    """
    assert set(_TRANSCRIPT_RULE_KEYS) | {"tool_expectations"} == set(
        TranscriptRulesConfig.model_fields
    )
    assert set(_TOOL_EXPECTATION_HAZARDS) == set(ToolExpectations.model_fields)

    what_to_declare = _WHAT_EACH_SECTION_MUST_DECLARE["transcript_rules"]
    for key in (*_TRANSCRIPT_RULE_KEYS, *_TOOL_EXPECTATION_HAZARDS, "tool_expectations"):
        assert key in what_to_declare, key


def test_an_unresolvable_inventory_may_not_carry_tools() -> None:
    """The two states decide opposite things, so a hybrid decides neither.

    ``known=False`` skips every tool-aware rule, so tools reported beside it are
    resolved and then ignored — a name checked against nothing, reading as clean.
    """
    with pytest.raises(ValueError, match="unresolvable inventory carries tools"):
        ToolInventory(
            declared=frozenset({"http_request"}),
            agent_declared=frozenset({"http_request"}),
            user_declared=frozenset(),
            actor_split_known=False,
            parameters={},
            known=False,
        )

    with pytest.raises(ValueError, match="unresolvable inventory carries tools"):
        ToolInventory(
            declared=frozenset(),
            agent_declared=frozenset(),
            user_declared=frozenset(),
            actor_split_known=False,
            parameters={"http_request": {}},
            known=False,
        )

    assert ToolInventory.unresolvable().known is False


def test_the_declared_set_is_the_union_of_the_two_actors() -> None:
    """Three sets that can disagree are three sets a rule can read differently.

    ``declared`` answers "does the task give anyone this tool"; the two actor sets
    answer "may *this* actor call it". A producer reporting a union that is not one
    would make an undeclared-tool rule and an actor rule contradict each other over
    the same name, each reading as clean.
    """
    with pytest.raises(ValueError, match="not the union of the actors"):
        ToolInventory(
            declared=frozenset({"calculator"}),
            agent_declared=frozenset(),
            user_declared=frozenset(),
            actor_split_known=True,
            parameters={},
            known=True,
        )

    with pytest.raises(ValueError, match="not the union of the actors"):
        ToolInventory(
            declared=frozenset({"read_file"}),
            agent_declared=frozenset({"read_file"}),
            user_declared=frozenset({"calculator"}),
            actor_split_known=True,
            parameters={},
            known=True,
        )

    assert ToolInventory(
        declared=frozenset({"read_file", "calculator"}),
        agent_declared=frozenset({"read_file"}),
        user_declared=frozenset({"calculator"}),
        actor_split_known=True,
        parameters={},
        known=True,
    ).declared == frozenset({"read_file", "calculator"})


# ---------------------------------------------------------------------------
# A required action names an actor, and that actor has to have the tool
# ---------------------------------------------------------------------------


def _two_actor_pack(tmp_path: Path, *, agent: list[str], user: list[str]) -> ToolInventory:
    """The inventory of a pack giving each actor its own builtin tools.

    Written to disk and read back through the loader and the inventory producer,
    rather than constructed, so what separates the two actors here is what the
    adapter separates them by at run time.
    """
    task_path = tmp_path / "task.yaml"
    task_path.write_text(
        yaml.safe_dump(
            {
                "task_id": "two_actors",
                "description": "a task whose two actors each declare a tool",
                "tools": {"agent": {"enabled": agent}, "user": {"enabled": user}},
            }
        )
    )
    task, task_dir = load_task_yaml(task_path)
    return build_tool_inventory(task, task_dir)


_THE_REQUESTORS_ADDRESS = "transcript_rules.required_actions[0].requestor"

_AN_ACTION_PER_ACTOR = (
    pytest.param("calculator", "user", None, id="the_users_tool_asked_of_the_user"),
    pytest.param("write_file", "assistant", None, id="the_agents_tool_asked_of_the_agent"),
    pytest.param(
        "calculator", "assistant", _THE_REQUESTORS_ADDRESS, id="the_users_tool_asked_of_the_agent"
    ),
    pytest.param(
        "write_file", "user", _THE_REQUESTORS_ADDRESS, id="the_agents_tool_asked_of_the_user"
    ),
)


@pytest.mark.parametrize(("name", "requestor", "address"), _AN_ACTION_PER_ACTOR)
def test_a_required_action_is_read_against_the_actor_its_requestor_names(
    tmp_path: Path, name: str, requestor: str, address: str | None
) -> None:
    """Both columns of the same pack, because either half alone reads as clean.

    ``requestor`` is matched against the recorded executor at grade time, so an
    action naming the other actor's tool selects nothing however the trial went —
    the cost a misspelling carries, from a name that is spelled right. A rule
    reading only the union would pass both offending rows; one reading only the
    user's tools would refuse the agent's own action.
    """
    inventory = _two_actor_pack(tmp_path, agent=["write_file"], user=["calculator"])

    report = inspect_grading_authoring(_required_action(name, requestor), inventory)

    if address is None:
        assert report.errors == (), _texts(report, "errors")
        return
    assert [finding.where for finding in report.errors] == [address]
    message = report.errors[0].message
    assert name in message, message
    assert "tools.agent.enabled" in message and "tools.user.enabled" in message, message


def test_a_requestor_is_unchecked_where_the_tool_set_came_off_a_recorded_trial(
    tmp_path: Path,
) -> None:
    """A replayed trial's tool list says *what* was offered, never *to whom*.

    ``tools_schemas.yaml`` is one list, so the inventory built from it files every
    tool under the agent because a set has to go somewhere. Reading that placement
    as a fact would refuse every ``requestor: user`` action a replay re-checks — an
    authoring that may well be right, failed on evidence the bundle does not hold.
    The half the bundle *can* answer keeps its teeth: a name no recorded tool
    carries is still an error.
    """
    bundle = tmp_path / "trial0"
    bundle.mkdir()
    (bundle / "tools_schemas.yaml").write_text(
        yaml.safe_dump(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "calculator",
                        "description": "Perform safe arithmetic calculations",
                        "parameters": {
                            "type": "object",
                            "properties": {"expression": {"type": "string"}},
                            "additionalProperties": False,
                        },
                    },
                }
            ]
        )
    )
    inventory = tool_inventory_from_bundle(bundle)

    on_the_recorded_tool = inspect_grading_authoring(
        _required_action("calculator", "user"), inventory
    )
    assert on_the_recorded_tool.errors == (), _texts(on_the_recorded_tool, "errors")
    assert [skip.where for skip in on_the_recorded_tool.unchecked] == [_THE_REQUESTORS_ADDRESS]

    on_a_tool_it_never_recorded = inspect_grading_authoring(
        _required_action("totally_absent", "user"), inventory
    )
    assert [finding.where for finding in on_a_tool_it_never_recorded.errors] == [
        "transcript_rules.required_actions[0].name"
    ]


def test_an_actor_blind_inventory_refuses_to_say_what_an_actor_may_call() -> None:
    """The method behind the routing above, asked directly.

    ``declared_by`` answers off two sets that exist whatever the producer knew, so
    an inventory that cannot tell the actors apart has to refuse rather than hand
    back the agent's whole set as the agent's own.
    """
    inventory = ToolInventory(
        declared=frozenset({"calculator"}),
        agent_declared=frozenset({"calculator"}),
        user_declared=frozenset(),
        actor_split_known=False,
        parameters={},
        known=True,
    )

    with pytest.raises(ValueError, match="does not know which actor declared what"):
        inventory.declared_by(ToolExecutorIdentity.USER)


def test_an_absent_user_side_call_is_no_finding_on_a_pack_with_no_user_tools() -> None:
    """A ``trace_checks`` matcher may name an actor the task gives no tools.

    ``absent`` over ``executor: user`` asserts that no user-side call happened,
    which a pack declaring no user tools satisfies — and is true of. Extending the
    actor rule to trace matchers would refuse packs that grade correctly, so it
    stops at ``required_actions``, where the declaration is a positive claim.

    The typo beside it is the positive control: the same matcher, one letter wrong
    in the tool, is an error — so this rule reading nothing is a decision about the
    executor rather than a site the gate never walked.
    """
    user_side = {
        "kind": "tool_call",
        "tool": {"equals": "http_request"},
        "executor": {"equals": "user"},
    }

    clean = inspect_grading_authoring(_trace_block(user_side, kind="absent"), _inventory(_HELPDESK))
    assert clean.errors == (), _texts(clean, "errors")
    assert clean.advisories == (), _texts(clean, "advisories")

    typo = inspect_grading_authoring(
        _trace_block({**user_side, "tool": {"equals": "http_reqest"}}, kind="absent"),
        _inventory(_HELPDESK),
    )
    assert [finding.where for finding in typo.errors] == ["trace_checks.probe.absent.match.tool"]


# ---------------------------------------------------------------------------
# A component and its weight name each other, in both directions
# ---------------------------------------------------------------------------


def test_an_explicit_opt_out_loads_and_needs_no_weight() -> None:
    """Standing single case: ``custom_checks: {enabled: false}`` declares something.

    An explicit opt-out is not "declares nothing", and it survives the wire intact
    where an empty block does not — so both substrates read it the same, and neither
    scores it. Weighting a component nobody scores is what the second half refuses,
    which is what makes the first half a decision rather than an omission.
    """
    opted_out = {"custom_checks": {"enabled": False}}
    no_weights = GradingCombineConfig(weights={})

    assert (
        inspect_grading_authoring(opted_out, _inventory(_HELPDESK), effective_combine=no_weights)
        == AuthoringReport()
    )

    weighted_anyway = inspect_grading_authoring(
        opted_out,
        _inventory(_HELPDESK),
        effective_combine=GradingCombineConfig(weights={"custom_checks": 1.0}),
    )
    assert [finding.where for finding in weighted_anyway.errors] == [
        "combine.weights.custom_checks"
    ]
    assert "absent or opted out" in weighted_anyway.errors[0].message


def test_an_unflagged_custom_checks_block_is_not_requested_by_the_components_own_default() -> None:
    """Standing single case: ``CustomChecksConfig.enabled`` defaults to ``False``.

    A caller reading the key generically would default it the other way and demand a
    weight for a component no substrate runs. The predicate asks the component, which
    is why "not requested" is the answer for a block that names a checks file — so
    neither weight rule fires here, and the only finding is rule 1's.

    That the block escapes both weight rules is exactly why rule 1 has to reach it: an
    unrequested component with no weight is invisible to the fold, and a pack whose
    ``custom_checks`` is its only section then lands the free pass a pack asking for
    nothing has earned, scoring ``1.0`` on a suite that never ran.
    """
    grading = {"custom_checks": {"file": "checks.py"}}

    report = inspect_grading_authoring(
        grading, _inventory(_HELPDESK), effective_combine=GradingCombineConfig(weights={})
    )

    assert [finding.where for finding in report.errors] == ["custom_checks"]
    assert not component_requested(COMPONENT_BY_NAME["custom_checks"], grading["custom_checks"])


def test_a_mistyped_custom_checks_key_raises_rather_than_disabling_the_component() -> None:
    """Standing single case: the predicate inherits the block's own validation.

    Reading ``enabled`` off a block with a misspelled key would answer "not
    requested" — silently unscoring a component the author asked for and demanding no
    weight for it, so the pack would validate clean and grade without it.
    """
    with pytest.raises(ValidationError, match="enabld"):
        inspect_grading_authoring(
            {"custom_checks": {"enabld": True}},
            _inventory(_HELPDESK),
            effective_combine=GradingCombineConfig(weights={}),
        )


def test_a_component_written_null_is_not_requested_and_a_declared_one_is() -> None:
    """Standing single case: ``llm_judge: null`` is absent, not configured.

    Both halves, because the negative alone passes under a predicate that never
    reports anything: the same block with a rubric must demand its weight.
    """
    no_weights = GradingCombineConfig(weights={})

    assert (
        inspect_grading_authoring(
            {"llm_judge": None}, _inventory(_HELPDESK), effective_combine=no_weights
        )
        == AuthoringReport()
    )

    declared = inspect_grading_authoring(
        {"llm_judge": {"rubric": {"criteria": []}}},
        _inventory(_HELPDESK),
        effective_combine=no_weights,
    )
    assert [finding.where for finding in declared.errors] == ["combine.weights.llm_judge"]


def test_a_deliberately_non_scoring_pack_loads_clean() -> None:
    """Standing single case: nothing configured and nothing weighted asks for nothing.

    The shape the wire-probe fixtures exist in — they record wire behaviour and score
    nothing, and say so in their own comment. What the rules forbid is the stray
    weight beside it, not the free pass itself.
    """
    report = inspect_grading_authoring(
        {}, _inventory(_HELPDESK), effective_combine=GradingCombineConfig(weights={})
    )

    assert report == AuthoringReport()


def test_a_weight_naming_no_component_at_all_is_refused_with_the_other_fix() -> None:
    """``combine.weights`` validates no key, so a typo there reaches both folds unread.

    Boundary case, standing lock: the fix is not the one a weight naming a real but
    unconfigured component takes — there is no section to configure — so a single
    message would send the author to write a block that does not exist.
    """
    report = inspect_grading_authoring(
        {"transcript_rules": {"max_turns": 10}},
        _inventory(_HELPDESK),
        effective_combine=GradingCombineConfig(
            weights={"transcript_rules": 1.0, "transcript_rule": 1.0}
        ),
    )

    assert [finding.where for finding in report.errors] == ["combine.weights.transcript_rule"]
    assert "names no grading component" in report.errors[0].message
    assert "Correct the name, or drop the weight" in report.errors[0].message


# ---------------------------------------------------------------------------
# The effective combine: inherited weights pass, an unresolved fold is unchecked
# ---------------------------------------------------------------------------


def test_a_task_inheriting_its_weights_from_its_project_passes(tmp_path: Path) -> None:
    """B1's regression cell: the rule reads the effective combine, not the authored block.

    Five shipped ``example-microservices-pack`` tasks are this shape — four declare no
    ``combine`` at all and one declares ``pass_threshold`` alone, with a comment saying
    the rest inherits. A rule reading the file would refuse all five.
    """
    grading = _write_grading(tmp_path, {"transcript_rules": {"max_turns": 10}})

    report = validate_grading_yaml(
        grading,
        inventory=ToolInventory.unresolvable(),
        combine_layer=CombineLayer({"weights": {"transcript_rules": 1.0}}),
    )

    assert report.errors == ()
    assert [skip.reason for skip in report.unchecked] == [config_validation._UNRESOLVABLE_REASON]


def test_the_same_task_with_no_resolvable_project_reports_unchecked(tmp_path: Path) -> None:
    """A fold nobody resolved is reported, never passed.

    The same pack as the case above, whose weight is supplied by a layer this caller
    cannot see. Answering "unweighted" would refuse a pack that grades correctly;
    answering "clean" would report a rule that ran on nothing as a rule that held.
    """
    grading = _write_grading(tmp_path, {"transcript_rules": {"max_turns": 10}})

    report = validate_grading_yaml(grading, inventory=ToolInventory.unresolvable())

    assert report.errors == ()
    assert UNRESOLVED_COMBINE_REASON in [skip.reason for skip in report.unchecked]


def test_an_unresolvable_combine_layer_may_not_carry_project_defaults() -> None:
    """The two states decide opposite things, so a hybrid decides neither.

    ``known=False`` skips both weight rules, so defaults reported beside it are
    resolved and then ignored — a fold checked against nothing, reading as unchecked.
    """
    with pytest.raises(ValueError, match="unresolvable combine layer carries project defaults"):
        CombineLayer(project_combine={"weights": {"state_checks": 1.0}}, known=False)

    assert CombineLayer.unresolvable().known is False


# ---------------------------------------------------------------------------
# Inside a binding
# ---------------------------------------------------------------------------


def test_a_typo_inside_a_binder_is_reported_at_the_binders_own_matcher() -> None:
    """A binder's matcher decides which events supply candidates, and can be wrong.

    Boundary case, standing lock: a walk over ``require`` alone reaches no binder,
    so a misspelled tool there selects no event, the binding yields no assignment,
    and the default ``on_unbound`` charges that to the agent on every trial. The
    route form carries the route id for the same reason a matcher finding does.
    """
    grading = {
        "trace_checks": {
            "constraints": [
                {
                    "id": "the_quote_came_from_a_read",
                    "description": "the note quotes a path the agent read",
                    "bind": {
                        "match": _tool_call("read_fil"),
                        "values": {"read_path": {"field": "args.path"}},
                    },
                    "require": _quotes("contains_binding", "read_path"),
                }
            ],
            "alternatives": [
                {
                    "id": "by_reading_the_file",
                    "description": "the answer came from the file",
                    "constraints": [
                        {
                            "id": "the_route_quote_came_from_a_read",
                            "description": "the note quotes a path this route read",
                            "bind": {
                                "match": _tool_call("read_fle"),
                                "values": {"route_path": {"field": "args.path"}},
                            },
                            "require": _quotes("contains_binding", "route_path"),
                        }
                    ],
                },
                {
                    "id": "by_running_a_command",
                    "description": "the answer came from a command",
                    "constraints": [
                        {
                            "id": "the_command_output_was_quoted",
                            "description": "the note quotes a command the agent ran",
                            "bind": {
                                "match": _tool_call("bash"),
                                "values": {"ran": {"field": "args.command"}},
                            },
                            "require": _quotes("contains_binding", "ran"),
                        }
                    ],
                },
            ],
        }
    }

    report = inspect_grading_authoring(grading, _inventory(_CODING))

    assert [finding.where for finding in report.errors] == [
        "trace_checks.the_quote_came_from_a_read.bind.match.tool",
        "trace_checks.by_reading_the_file.the_route_quote_came_from_a_read.bind.match.tool",
    ]
    assert all("is not declared by this task" in finding.message for finding in report.errors)


def test_an_uncompilable_capture_pattern_is_reported_at_its_own_address() -> None:
    """A binder's pattern is compiled by the same evaluator a matcher's ``regex`` is.

    Boundary case, standing lock: it is the one authored pattern that lives outside
    a ``ValuePredicate``, so a rule walking predicates alone lets it through to
    grade time, where ``re.error`` costs the trial rather than the constraint.
    """
    grading = _bound_block(
        {"kind": "assistant_message"},
        {"figure": {"field": "text", "pattern": "([0-9]+"}},
        {"present": {"match": _tool_call("write_file", content={"contains_binding": "figure"})}},
    )

    report = inspect_grading_authoring(grading, _inventory(_CODING))

    assert [finding.where for finding in report.errors] == [
        "trace_checks.probe.bind.values.figure.pattern"
    ]
    assert "does not compile" in report.errors[0].message


_UNCHECKED_EXTRACTIONS = (
    pytest.param(
        _HELPDESK,
        _tool_call("http_request"),
        {"query": {"field": "args.json.q"}},
        "first segment only",
        id="extraction_below_a_declared_head",
    ),
    pytest.param(
        _MOBILE,
        _tool_call("mobile"),
        {"acts": {"field": "args.actions"}},
        "declares no single type",
        id="extraction_the_schema_gives_no_type",
    ),
)


@pytest.mark.parametrize(("task", "binder", "values", "reason"), _UNCHECKED_EXTRACTIONS)
def test_an_extraction_the_schema_cannot_answer_for_is_unchecked(
    task: Path, binder: dict[str, Any], values: dict[str, Any], reason: str
) -> None:
    """The two residues the tool schema leaves, both reported and neither fatal.

    ``json.q`` on ``http_request`` bottoms out at ``json`` (#765), and ``mobile``
    writes ``actions`` as an ``anyOf`` with no ``type``, so neither the argument
    below the first segment nor the type of the value bound out of it is knowable.
    Both are where the evaluation-time failure is the only backstop, so a gate that
    stayed silent here would read as a clean bill of health.
    """
    name = next(iter(values))
    grading = _bound_block(binder, values, _quotes("contains_binding", name))

    report = inspect_grading_authoring(grading, _inventory(task))

    assert report.errors == ()
    assert report.advisories == ()
    assert [skip.where for skip in report.unchecked] == [
        f"trace_checks.probe.bind.values.{name}.field"
    ]
    assert reason in report.unchecked[0].reason, report.unchecked


# The type rule, one case each: what is flagged turns on the type the schema gives
# the extraction, never on which operator reads it, and each cell below is the
# smallest edit to its neighbour that changes the answer.

_EXTRACTION_ADDRESS = "trace_checks.probe.bind.values.start.field"
_ARGUMENT_ADDRESS = "trace_checks.probe.present.match.args.path"
_INTEGER_ARGUMENT = {"start": {"field": "args.offset"}}


def test_a_bound_integer_read_as_text_is_an_error_on_a_closed_schema() -> None:
    """``contains`` falls back to equality for a non-string pair, which is never true.

    The whole hazard is that the author cannot tell this from the agent failing:
    the check is red on every trajectory and the message is the one a genuine miss
    carries. ``read_file`` types ``offset`` as an integer and admits no other
    argument, so the schema settles it before the run.
    """
    grading = _bound_block(
        _tool_call("read_file"), _INTEGER_ARGUMENT, _quotes("contains_binding", "start")
    )

    report = inspect_grading_authoring(grading, _inventory(_CODING))

    assert [finding.where for finding in report.errors] == [_EXTRACTION_ADDRESS]
    assert "declares as type 'integer'" in report.errors[0].message
    assert "trace_checks.probe.present.match.text" in report.errors[0].message
    assert report.advisories == ()


def test_a_bound_array_read_as_text_is_an_advisory_on_an_open_schema() -> None:
    """A schema permitting arguments it does not declare describes its tool loosely.

    The severity is the whole content of the cell: hard-failing here would enforce
    against an MCP pack a claim its schema does not make, which is the policy the
    unknown-argument rule already follows.
    """
    grading = _bound_block(
        _tool_call("place_order"),
        {"ordered": {"field": "args.items"}},
        _quotes("contains_binding", "ordered"),
    )

    report = inspect_grading_authoring(grading, _inventory(_SHOP_ORDERS))

    assert report.errors == ()
    assert [finding.where for finding in report.advisories] == [
        "trace_checks.probe.bind.values.ordered.field"
    ]
    assert "declares as type 'array'" in report.advisories[0].message


def test_a_capture_pattern_on_the_extraction_is_not_flagged_for_its_type() -> None:
    """The type rule does not fire at the extraction's own address under a pattern.

    A capture is not the value the schema typed, so what the reference compares is
    settled at ``.pattern`` and not here — where the rule that owns the shape does
    report this block. Scoped to the type rule's own address rather than to an
    empty report, which this block is not: asserting emptiness would pin the
    absence of every rule rather than the presence of this exemption.
    """
    grading = _bound_block(
        _tool_call("read_file"),
        {"start": {"field": "args.offset", "pattern": "([0-9]+)"}},
        _quotes("contains_binding", "start"),
    )

    report = inspect_grading_authoring(grading, _inventory(_CODING))

    flagged = [finding.where for finding in report.errors + report.advisories]
    assert _EXTRACTION_ADDRESS not in flagged, flagged


_PATTERN_ADDRESS = "trace_checks.probe.bind.values.start.pattern"


def test_a_capture_pattern_over_a_non_string_argument_is_an_error_on_a_closed_schema() -> None:
    """A pattern binds a capture off text alone, and nothing off an integer.

    ``_extracted`` narrows by pattern only where the value is a ``str`` and yields
    nothing otherwise, so the name binds on no event however the agent behaved and
    the default ``on_unbound`` charges the miss to it — the message a genuine agent
    failure carries. Which predicate reads the name does not enter into it.
    """
    grading = _bound_block(
        _tool_call("read_file"),
        {"start": {"field": "args.offset", "pattern": "([0-9]+)"}},
        _quotes("contains_binding", "start"),
    )

    report = inspect_grading_authoring(grading, _inventory(_CODING))

    assert [finding.where for finding in report.errors] == [_PATTERN_ADDRESS]
    assert "type 'integer'" in report.errors[0].message
    assert report.advisories == ()


def test_a_capture_pattern_over_a_non_string_argument_is_an_advisory_on_an_open_schema() -> None:
    """The severity rests on the one schema the rule reads, as its neighbour's does.

    A schema permitting arguments it does not declare describes its tool loosely,
    and hard-failing on it would enforce a claim the schema does not make.
    """
    grading = _bound_block(
        _tool_call("place_order"),
        {"ordered": {"field": "args.items", "pattern": "(W[0-9]+)"}},
        _quotes("contains_binding", "ordered"),
    )

    report = inspect_grading_authoring(grading, _inventory(_SHOP_ORDERS))

    assert report.errors == ()
    assert [finding.where for finding in report.advisories] == [
        "trace_checks.probe.bind.values.ordered.pattern"
    ]
    assert "type 'array'" in report.advisories[0].message


def test_a_capture_pattern_over_a_string_argument_is_not_flagged() -> None:
    """The shape the feature exists for, one token from the flagged one.

    "Bind the directory out of the path the agent read" is a capture over an
    argument the schema types ``string``, separated from the error above only by
    which argument ``read_file`` was asked about.
    """
    grading = _bound_block(
        _tool_call("read_file"),
        {"start": {"field": "args.path", "pattern": "([a-z]+)"}},
        _quotes("contains_binding", "start"),
    )

    assert inspect_grading_authoring(grading, _inventory(_CODING)) == AuthoringReport()


def test_a_capture_pattern_over_the_whole_argument_mapping_is_flagged() -> None:
    """``field: args`` binds the mapping itself, which no pattern reads either.

    The one extraction whose type no schema declares and the gate knows anyway:
    ``TraceEvent`` types ``arguments`` as a mapping, so the capture yields nothing
    for the same reason it yields nothing off an integer.
    """
    grading = _bound_block(
        _tool_call("read_file"),
        {"start": {"field": "args", "pattern": "([0-9]+)"}},
        _quotes("contains_binding", "start"),
    )

    report = inspect_grading_authoring(grading, _inventory(_CODING))

    assert [finding.where for finding in report.errors] == [_PATTERN_ADDRESS]
    assert "type 'object'" in report.errors[0].message


def test_an_uncompilable_pattern_over_a_non_string_argument_names_both_repairs() -> None:
    """Two findings at one key, because they are two mistakes with two fixes.

    The rule does not read whether the pattern compiles, and it must not: making
    the regex valid does not make an integer capturable, so an author shown only
    the compile error would fix it and still bind nothing.
    """
    grading = _bound_block(
        _tool_call("read_file"),
        {"start": {"field": "args.offset", "pattern": "([0-9]+"}},
        _quotes("contains_binding", "start"),
    )

    report = inspect_grading_authoring(grading, _inventory(_CODING))

    assert [finding.where for finding in report.errors] == [_PATTERN_ADDRESS, _PATTERN_ADDRESS]
    assert "does not compile" in report.errors[0].message
    assert "type 'integer'" in report.errors[1].message


def test_a_bound_integer_correlated_with_another_argument_is_not_flagged() -> None:
    """Arguments correlate by native equality, which is the point of the feature.

    ``equals_binding`` on an ``args`` predicate compares two values the tool typed
    the same way, so the integer that cannot be found inside prose is exactly right
    here — and a rule reading the operator instead of the schema would reject it.
    """
    grading = _bound_block(
        _tool_call("read_file"),
        _INTEGER_ARGUMENT,
        {"present": {"match": _tool_call("read_file", limit={"equals_binding": "start"})}},
    )

    assert inspect_grading_authoring(grading, _inventory(_CODING)) == AuthoringReport()


def _correlated(task: Path, binder: str, values: dict[str, Any], match: dict[str, Any]) -> Any:
    """One binder over *binder*'s calls, correlated into *match*, against *task*."""
    grading = _bound_block(_tool_call(binder), values, {"present": {"match": match}})
    return inspect_grading_authoring(grading, _inventory(task))


def test_a_string_argument_correlated_with_an_integer_binding_is_an_error() -> None:
    """The wholesale ``args`` exemption was as wide as "both sides are arguments".

    Two arguments correlate natively only where the tools type them the same way.
    ``read_file`` types ``path`` a string and ``offset`` an integer, and no string
    equals an integer, so this is red on every trajectory — and the message it
    fails with is the one a genuine agent miss carries.
    """
    report = _correlated(
        _CODING,
        "read_file",
        {"n": {"field": "args.offset"}},
        _tool_call("read_file", path={"equals_binding": "n"}),
    )

    assert [finding.where for finding in report.errors] == [_ARGUMENT_ADDRESS]
    assert "type 'string'" in report.errors[0].message
    assert "type 'integer'" in report.errors[0].message
    assert "'read_file'" in report.errors[0].message
    assert report.advisories == ()


def test_an_integer_argument_correlated_with_a_string_binding_is_an_error() -> None:
    """The reverse direction, answered at both tiers.

    The gate refuses it before the run wherever both schemas type the arguments;
    the evaluator's backstop reads both operands' runtime JSON types over the
    residue the gate cannot type. This test locks the pre-run half.
    """
    report = _correlated(
        _CODING,
        "read_file",
        {"p": {"field": "args.path"}},
        _tool_call("read_file", offset={"equals_binding": "p"}),
    )

    assert [finding.where for finding in report.errors] == [
        "trace_checks.probe.present.match.args.offset"
    ]
    assert "type 'integer'" in report.errors[0].message


def test_the_same_correlation_on_open_schemas_is_an_advisory() -> None:
    """The finding rests on two schemas' claims, so the weaker one decides.

    A schema permitting arguments it does not declare describes its tool loosely,
    and this is the one rule reading two of them — hard-failing here would enforce
    against both packs a claim neither makes.
    """
    report = _correlated(
        _SHOP_ORDERS,
        "place_order",
        {"it": {"field": "args.items"}},
        _tool_call("place_order", customer_id={"equals_binding": "it"}),
    )

    assert report.errors == ()
    assert [finding.where for finding in report.advisories] == [
        "trace_checks.probe.present.match.args.customer_id"
    ]
    assert "type 'array'" in report.advisories[0].message


def _one_task_worth_of_tools(*tasks: Path) -> ToolInventory:
    """Every tool of several packs in one inventory, each keeping its own schema.

    No task in this repository declares a closed schema beside an open one — each
    pack's tools are strict together or loose together — so the pairing that
    separates "both schemas forbid extras" from "either does" cannot be resolved
    from a single pack. Unioning two real inventories reaches it without inventing
    a schema: every claim still comes from the tool that made it.
    """
    resolved = [_inventory(task) for task in tasks]
    return ToolInventory(
        declared=frozenset(name for one in resolved for name in one.declared),
        agent_declared=frozenset(name for one in resolved for name in one.agent_declared),
        user_declared=frozenset(name for one in resolved for name in one.user_declared),
        actor_split_known=True,
        parameters={name: schema for one in resolved for name, schema in one.parameters.items()},
        known=True,
    )


_A_CLOSED_TOOL_BESIDE_AN_OPEN_ONE = _one_task_worth_of_tools(_CODING, _SHOP_ORDERS)

_MIXED_STRICTNESS_PAIRS = (
    pytest.param(
        "read_file",
        {"n": {"field": "args.offset"}},
        _tool_call("place_order", customer_id={"equals_binding": "n"}),
        "trace_checks.probe.present.match.args.customer_id",
        id="the_loose_schema_types_the_argument",
    ),
    pytest.param(
        "place_order",
        {"it": {"field": "args.items"}},
        _tool_call("read_file", path={"equals_binding": "it"}),
        "trace_checks.probe.present.match.args.path",
        id="the_loose_schema_types_the_binding",
    ),
)


@pytest.mark.parametrize(("binder", "values", "match", "address"), _MIXED_STRICTNESS_PAIRS)
def test_a_correlation_resting_on_one_loose_schema_is_an_advisory(
    binder: str, values: dict[str, Any], match: dict[str, Any], address: str
) -> None:
    """The finding rests on two claims, so the weaker one decides — from either side.

    A schema permitting arguments it does not declare describes its tool loosely,
    and it is loose about the *type* it wrote for the same reason. Hard-failing on
    a pair where either half is that loose would enforce a claim only one of them
    makes, and the two rows here are that reading in both directions.
    """
    grading = _bound_block(_tool_call(binder), values, {"present": {"match": match}})

    report = inspect_grading_authoring(grading, _A_CLOSED_TOOL_BESIDE_AN_OPEN_ONE)

    assert report.errors == ()
    assert [finding.where for finding in report.advisories] == [address]


def test_an_argument_correlated_with_a_bare_result_extraction_is_an_error() -> None:
    """An extraction no schema describes still has a type, and the event gives it.

    ``field: result`` binds text — ``TraceEvent`` types it that way and no tool
    schema mentions it — so correlating it against an integer argument is never
    true. Calling that side unresolved would lose the finding and add an
    ``unchecked`` line saying nothing.
    """
    report = _correlated(
        _CODING,
        "read_file",
        {"r": {"field": "result"}},
        _tool_call("read_file", offset={"equals_binding": "r"}),
    )

    assert [finding.where for finding in report.errors] == [
        "trace_checks.probe.present.match.args.offset"
    ]
    assert "the event types it" in report.errors[0].message


def test_a_capture_correlated_with_a_natively_typed_argument_names_the_capture() -> None:
    """A capture is text, and the author is owed the repair they have not taken.

    Three things settle a bound type — a schema, the event, and a ``pattern`` — and
    the finding must not collapse them: an author who already wrote a capture and
    is told to write a capture is given no repair at all, and the event did not
    type this one, the pattern did. The verdict is unchanged either way, since
    ``"123" == 123`` is false on every trajectory.
    """
    report = _correlated(
        _CODING,
        "read_file",
        {"digits": {"field": "args.path", "pattern": "([0-9]+)"}},
        _tool_call("read_file", offset={"equals_binding": "digits"}),
    )

    assert [finding.where for finding in report.errors] == [
        "trace_checks.probe.present.match.args.offset"
    ]
    message = report.errors[0].message
    assert "the capture pattern makes it text" in message
    assert "the event types it" not in message
    assert "compare a regex capture" not in message
    assert "drop the pattern" in message


def test_every_rule_naming_a_repair_names_the_same_one() -> None:
    """A repair one rule recommends and another refuses is worse than no repair.

    Two rules answer for a binding whose type cannot hold against what reads it —
    the extraction's own type rule and the correlation rule — and both close by
    naming what the author should write instead. Narrowing one and not the other
    leaves the branch recommending, in one release, a shape it refuses in another.
    """
    read_as_text = _correlated(
        _CODING,
        "read_file",
        _INTEGER_ARGUMENT,
        {"kind": "assistant_message", "text": {"contains_binding": "start"}},
    )
    correlated = _correlated(
        _CODING,
        "read_file",
        _INTEGER_ARGUMENT,
        _tool_call("read_file", path={"equals_binding": "start"}),
    )

    repair = _HOW_TO_CORRELATE[_BoundTypeSource.SCHEMA]
    assert read_as_text.errors[0].message.endswith(repair)
    assert correlated.errors[0].message.endswith(repair)


def test_a_bare_args_binding_is_typed_without_resolving_the_binders_tool() -> None:
    """``TraceEvent`` types ``arguments`` a mapping whatever tool the binder selected.

    So the answer never rested on a schema, and reaching for one first would lose
    the finding on every binder whose matcher names no single tool — a shape that
    still draws its own ``unchecked`` line at the extraction, beside this error.
    """
    grading = _bound_block(
        {"kind": "tool_call"},
        {"a": {"field": "args"}},
        {"present": {"match": _tool_call("read_file", offset={"equals_binding": "a"})}},
    )

    report = inspect_grading_authoring(grading, _inventory(_CODING))

    assert [finding.where for finding in report.errors] == [
        "trace_checks.probe.present.match.args.offset"
    ]
    assert "type 'object'" in report.errors[0].message
    assert [skip.where for skip in report.unchecked] == ["trace_checks.probe.bind.values.a.field"]


def test_a_number_argument_correlated_with_an_integer_binding_is_not_flagged() -> None:
    """Two types that differ and correlate anyway, which a difference rule refuses.

    ``search_kb`` types ``alpha`` a number and ``top_k`` an integer, and Python
    equates ``1`` with ``1.0``, so this holds on any trajectory where the agent
    passes the same figure to both.
    """
    report = _correlated(
        _RAG,
        "search_kb",
        {"k": {"field": "args.top_k"}},
        _tool_call("search_kb", alpha={"equals_binding": "k"}),
    )

    assert report == AuthoringReport()


def test_a_container_needle_is_flagged_against_a_container_haystack() -> None:
    """``contains`` descends into a haystack and never compares it to the needle.

    Both sides are the same declared type here, so a rule reading "do the types
    differ" would pass it — and the descent reaches only the scalars inside, so an
    array is found in nothing at all.
    """
    report = _correlated(
        _SHOP_ORDERS,
        "place_order",
        {"it": {"field": "args.items"}},
        _tool_call("place_order", items={"contains_binding": "it"}),
    )

    assert [finding.where for finding in report.advisories] == [
        "trace_checks.probe.present.match.args.items"
    ]
    assert "contains_binding" in report.advisories[0].message


def test_a_scalar_needle_against_a_container_haystack_is_not_flagged() -> None:
    """The asymmetry the table exists for, one token from its flagged neighbour.

    The same ``array`` argument, the same operator, and only the binding's type
    changed: a string is found inside a list of strings by descent, which is the
    correlation "the order carried the customer the lookup returned" is written as.
    """
    report = _correlated(
        _SHOP_ORDERS,
        "place_order",
        {"c": {"field": "args.customer_id"}},
        _tool_call("place_order", items={"contains_binding": "c"}),
    )

    assert report == AuthoringReport()


def test_two_binding_operators_on_one_predicate_draw_two_findings() -> None:
    """A predicate is a conjunction, so two operators are two comparisons.

    Both must hold for the predicate to, so an author who wrote two never-true
    comparisons made two mistakes and is owed the address of each operator.
    """
    report = _correlated(
        _SHOP_ORDERS,
        "place_order",
        {"it": {"field": "args.items"}},
        _tool_call("place_order", customer_id={"equals_binding": "it", "contains_binding": "it"}),
    )

    assert len(report.advisories) == 2
    named = sorted(
        operator
        for operator in ("equals_binding", "contains_binding")
        for finding in report.advisories
        if finding.message.startswith(operator)
    )
    assert named == ["contains_binding", "equals_binding"]


_REGEX_BESIDE_A_REFERENCE = (
    pytest.param(
        {"r": {"field": "result"}},
        "trace_checks.probe.present.match.args.offset",
        id="a_non_args_extraction_the_shipped_rule_exits_on",
    ),
    pytest.param(
        {"r": {"field": "args.path", "pattern": "([0-9]+)"}},
        "trace_checks.probe.present.match.args.offset",
        id="a_capture_the_shipped_rule_exits_on",
    ),
    pytest.param(
        {"r": {"field": "args.offset"}},
        "trace_checks.probe.bind.values.r.field",
        id="an_args_extraction_the_shipped_rule_reaches",
    ),
    pytest.param(
        {"r": {"field": "args"}},
        "trace_checks.probe.bind.values.r.field",
        id="a_bare_args_extraction_the_shipped_rule_reaches",
    ),
)


@pytest.mark.parametrize(("values", "address"), _REGEX_BESIDE_A_REFERENCE)
def test_a_regex_beside_the_reference_draws_exactly_one_finding(
    values: dict[str, Any], address: str
) -> None:
    """One mistake, one report — and standing down is scoped to where the other rule reports.

    A ``regex`` on the predicate says the argument holds text whatever the schema
    types it, which is what makes the reference textual to the extraction rule. But
    that rule exits on a non-``args`` field and on a ``pattern`` before it resolves
    anything, so deferring wherever a ``regex`` appears would leave the first two
    rows here unreported at both tiers rather than reported once. The address is
    what says which rule answered.
    """
    grading = _bound_block(
        _tool_call("read_file"),
        values,
        {
            "present": {
                "match": _tool_call("read_file", offset={"regex": "[0-9]+", "equals_binding": "r"})
            }
        },
    )

    report = inspect_grading_authoring(grading, _inventory(_CODING))

    assert [finding.where for finding in report.errors + report.advisories] == [address]


def test_a_path_below_its_first_segment_draws_exactly_one_skip() -> None:
    """The shipped skip already lands at this address, so this rule stands down.

    A second ``unchecked`` row at one address reports one gap twice and reads as
    two unanswered questions.
    """
    grading = _bound_block(
        _tool_call("read_file"),
        _INTEGER_ARGUMENT,
        {
            "present": {
                "match": {
                    "kind": "tool_call",
                    "tool": {"equals": "mobile"},
                    "args": {"actions.deep": {"equals_binding": "start"}},
                }
            }
        },
    )

    report = inspect_grading_authoring(grading, _inventory(_MOBILE))

    assert [skip.where for skip in report.unchecked] == [
        "trace_checks.probe.present.match.args.actions.deep"
    ]
    assert report.errors + report.advisories == ()


def test_an_argument_path_is_read_as_authored_rather_than_split_out_of_its_address() -> None:
    """``args: {"path.args.offset": …}`` addresses one path, and it is below a head.

    Recovering the path from the finding's address by its last ``.args.`` would
    read ``'offset'`` — a real ``read_file`` argument of another type — and report
    a correlation the author never wrote. The path travels with the predicate, so
    this is one skip and no finding.
    """
    grading = _bound_block(
        _tool_call("read_file"),
        {"p": {"field": "args.path"}},
        {
            "present": {
                "match": {
                    "kind": "tool_call",
                    "tool": {"equals": "read_file"},
                    "args": {"path.args.offset": {"equals_binding": "p"}},
                }
            }
        },
    )

    report = inspect_grading_authoring(grading, _inventory(_CODING))

    assert [skip.where for skip in report.unchecked] == [
        "trace_checks.probe.present.match.args.path.args.offset"
    ]
    assert report.errors + report.advisories == ()


def test_a_matcher_naming_no_tool_draws_exactly_one_skip() -> None:
    """Which schema types the argument is already unanswered one address up.

    ``_one_matchers_argument_paths`` reports it for the matcher as a whole, and a
    second row per predicate would multiply one gap by however many arguments the
    matcher happens to carry.
    """
    grading = _bound_block(
        _tool_call("read_file"),
        _INTEGER_ARGUMENT,
        {
            "present": {
                "match": {"kind": "tool_call", "args": {"path": {"equals_binding": "start"}}}
            }
        },
    )

    report = inspect_grading_authoring(grading, _inventory(_CODING))

    assert [skip.where for skip in report.unchecked] == ["trace_checks.probe.present.match.args"]
    assert report.errors + report.advisories == ()


def test_an_argument_the_schema_gives_no_type_leaves_the_comparison_unchecked() -> None:
    """A property writing an ``anyOf`` and no ``type`` settles nothing either way.

    Reported rather than passed over: the comparison may well be never-true, and
    the author is owed the difference between "checked and fine" and "not
    checkable". ``mobile.actions`` is the corpus's only such property.
    """
    grading = _bound_block(
        _tool_call("read_file"),
        _INTEGER_ARGUMENT,
        {"present": {"match": _tool_call("mobile", actions={"equals_binding": "start"})}},
    )

    report = inspect_grading_authoring(grading, _inventory(_MOBILE))

    assert [skip.where for skip in report.unchecked] == [
        "trace_checks.probe.present.match.args.actions"
    ]
    assert "no single type for 'actions'" in report.unchecked[0].reason
    assert report.errors + report.advisories == ()


def test_an_argument_typed_outside_the_json_type_names_leaves_it_unchecked() -> None:
    """A schema may write a ``type`` the table has no answer for, and must not crash.

    ``type: "null"`` is legal JSON Schema and a typo is legal YAML. Either reaches
    ``ever_satisfiable`` as an author-supplied string, and the gate answers without
    a false-reject mode, so the comparison is unchecked rather than refused — and
    rather than a ``KeyError`` inside ``tolokaforge validate``. Written out because
    no in-repo schema types a property outside the six names; the packs this
    answers for are maintained outside this repository.
    """
    inventory = ToolInventory(
        declared=frozenset({"read_file"}),
        agent_declared=frozenset({"read_file"}),
        user_declared=frozenset(),
        actor_split_known=True,
        parameters={
            "read_file": {
                "additionalProperties": False,
                "properties": {"path": {"type": "null"}, "offset": {"type": "integer"}},
            }
        },
        known=True,
    )
    grading = _bound_block(
        _tool_call("read_file"),
        _INTEGER_ARGUMENT,
        {"present": {"match": _tool_call("read_file", path={"equals_binding": "start"})}},
    )

    report = inspect_grading_authoring(grading, inventory)

    assert [skip.where for skip in report.unchecked] == [_ARGUMENT_ADDRESS]
    assert report.errors + report.advisories == ()


def test_a_bound_integer_equated_against_text_is_flagged_like_a_containment() -> None:
    """``equals_binding`` escapes the rule by where it sits, not by being itself.

    ``operator.eq("…offset 40…", 40)`` is False on every trajectory exactly as
    ``contains`` is, so an ``equals_binding`` on a text field is the same never-true
    check — and the containment message names ``equals_binding`` as the fix, which
    steers authors straight here.
    """
    grading = _bound_block(
        _tool_call("read_file"), _INTEGER_ARGUMENT, _quotes("equals_binding", "start")
    )

    report = inspect_grading_authoring(grading, _inventory(_CODING))

    assert [finding.where for finding in report.errors] == [_EXTRACTION_ADDRESS]
    assert "trace_checks.probe.present.match.text" in report.errors[0].message


def test_a_bound_integer_read_beside_a_regex_on_an_argument_is_flagged() -> None:
    """A ``regex`` beside the reference says the argument holds text.

    An argument is exempt because the comparison is native on both sides; a pattern
    written on the same predicate withdraws that, since a regex only ever holds
    against a string.
    """
    grading = _bound_block(
        _tool_call("read_file"),
        _INTEGER_ARGUMENT,
        {
            "present": {
                "match": _tool_call(
                    "write_file", content={"regex": "line [0-9]+", "equals_binding": "start"}
                )
            }
        },
    )

    report = inspect_grading_authoring(grading, _inventory(_CODING))

    assert [finding.where for finding in report.errors] == [_EXTRACTION_ADDRESS]
    assert "trace_checks.probe.present.match.args.content" in report.errors[0].message


_DECLARED_TYPES_THAT_ARE_NEVER_TEXT = (
    pytest.param(_CODING, "read_file", "args.offset", "integer", id="integer"),
    pytest.param(_CODING, "read_file", "args.with_line_numbers", "boolean", id="boolean"),
    pytest.param(_RAG, "search_kb", "args.alpha", "number", id="number"),
    pytest.param(_SHOP_ORDERS, "place_order", "args.items", "array", id="array"),
    pytest.param(_HELPDESK, "http_request", "args.headers", "object", id="object"),
)


@pytest.mark.parametrize(("task", "tool", "field", "declared"), _DECLARED_TYPES_THAT_ARE_NEVER_TEXT)
def test_every_json_type_that_is_never_text_is_reported(
    task: Path, tool: str, field: str, declared: str
) -> None:
    """The rule spans every non-string JSON type, not the two the cells above used.

    Each row is a real property of a real tool, so a type dropped from the table is
    a correlation that silently stops being caught rather than a constant nothing
    reads. Channels are merged here because the severity is the subject of its own
    two cells and this one asks only whether the type is answered for at all.
    """
    grading = _bound_block(
        _tool_call(tool), {"start": {"field": field}}, _quotes("contains_binding", "start")
    )

    report = inspect_grading_authoring(grading, _inventory(task))

    reported = report.errors + report.advisories
    assert [finding.where for finding in reported] == [_EXTRACTION_ADDRESS]
    assert f"type {declared!r}" in reported[0].message


def test_an_argument_typed_outside_the_json_type_names_is_not_flagged() -> None:
    """A schema is free to write a ``type`` the table has no answer for.

    ``type: "null"`` is legal JSON Schema and a typo is legal YAML, and the gate
    answers without a false-reject mode, so a name it cannot read is no evidence
    rather than a finding — and rather than a raise inside ``tolokaforge validate``.
    The inventory is written out rather than resolved from a pack because no in-repo
    schema types a property outside the six names; the packs this answers for are
    maintained outside this repository.
    """
    inventory = ToolInventory(
        declared=frozenset({"read_file"}),
        agent_declared=frozenset({"read_file"}),
        user_declared=frozenset(),
        actor_split_known=True,
        parameters={
            "read_file": {
                "additionalProperties": False,
                "properties": {"offset": {"type": "null"}},
            }
        },
        known=True,
    )
    grading = _bound_block(
        _tool_call("read_file"), _INTEGER_ARGUMENT, _quotes("contains_binding", "start")
    )

    assert inspect_grading_authoring(grading, inventory) == AuthoringReport()


_REFERENCES_THAT_COMPARE_TEXT = (
    pytest.param(_quotes("contains_binding", "start"), "text", id="text"),
    pytest.param(
        {"present": {"match": {"kind": "tool_call", "tool": {"equals_binding": "start"}}}},
        "tool",
        id="tool",
    ),
    pytest.param(
        {
            "present": {
                "match": {
                    "kind": "tool_call",
                    "tool": {"equals": "read_file"},
                    "status": {"equals": "success"},
                    "result": {"contains_binding": "start"},
                }
            }
        },
        "result",
        id="result",
    ),
    pytest.param(
        {
            "present": {
                "match": {
                    "kind": "tool_call",
                    "tool": {"equals": "read_file"},
                    "status": {"equals_binding": "start"},
                }
            }
        },
        "status",
        id="status",
    ),
    pytest.param(
        {
            "present": {
                "match": {
                    "kind": "tool_call",
                    "tool": {"equals": "read_file"},
                    "executor": {"equals_binding": "start"},
                }
            }
        },
        "executor",
        id="executor",
    ),
)


@pytest.mark.parametrize(("require", "field"), _REFERENCES_THAT_COMPARE_TEXT)
def test_every_event_field_holding_text_flags_a_binding_of_another_type(
    require: dict[str, Any], field: str
) -> None:
    """Five ``TraceEvent`` fields hold a ``str`` at runtime, and all five compare text.

    ``status`` and ``executor`` reach a predicate as members of a closed vocabulary
    typed ``str``, so the value compared is text exactly as ``result``'s is. A rule
    naming only the field a cell happened to use would let the identical never-true
    check through on the other four.
    """
    grading = _bound_block(_tool_call("read_file"), _INTEGER_ARGUMENT, require)

    report = inspect_grading_authoring(grading, _inventory(_CODING))

    assert [finding.where for finding in report.errors] == [_EXTRACTION_ADDRESS]
    assert f"present.match.{field}" in report.errors[0].message


def test_a_bound_string_argument_read_as_text_is_not_flagged() -> None:
    """The correlation the feature exists for, and the one a coarser rule would reject.

    "The note quotes the path the agent read" is a ``contains_binding`` on a text
    field over an ``args`` extraction — the exact shape flagged above, separated
    only by the type ``read_file`` gives ``path``.
    """
    grading = _bound_block(
        _tool_call("read_file"),
        {"read_path": {"field": "args.path"}},
        _quotes("contains_binding", "read_path"),
    )

    assert inspect_grading_authoring(grading, _inventory(_CODING)) == AuthoringReport()


def test_a_matcher_carrying_no_predicate_at_all_is_not_a_finding() -> None:
    """ "At most ten tool calls" names no tool and is not vacuous.

    Pinned so a later rule against match-everything matchers has to delete an
    assertion rather than slip in: ``absent`` over this matcher is "the agent called
    no tool", and ``before`` over it orders the first call against something.
    """
    grading = {
        "trace_checks": {
            "constraints": [
                {
                    "id": "at_most_ten_calls",
                    "description": "the agent made at most ten tool calls",
                    "require": {"count": {"match": {"kind": "tool_call"}, "max": 10}},
                }
            ]
        }
    }

    report = inspect_grading_authoring(grading, _inventory(_HELPDESK))

    assert report == AuthoringReport()


def test_the_textual_matcher_fields_are_the_fields_whose_value_is_a_string() -> None:
    """A field whose runtime value is a ``str`` is one every reference compares correctly.

    The gate flags a binding reference sitting on one of these against a schema-typed
    non-string extraction, and exempts the rest. So a field the event types as text
    and the gate leaves out is a never-true check the gate stops reporting — the same
    class it exists to catch — and one typed as anything else but held here is a
    correct native comparison the gate starts rejecting.

    Three assertions, each falsifiable on its own. The mapping is held against the
    matchable union, so a field the model gains cannot go untyped. The membership is
    written out, so retyping an attribute on ``TraceEvent`` reds here rather than
    moving the gate's set and the derivation together in silence. And the gate's set
    is held against one computed here through the shipped predicate, so a constant
    that stops being derived at all — the shape that let ``status`` and ``executor``
    stay out while the comment claimed they were the event's string fields — cannot
    pass beside the written-out membership.

    The predicate itself is called rather than re-implemented: a second walk here
    would move with the first under a retype and take the independence with it.
    """
    matchable = {field for fields in TRACE_MATCHABLE_FIELDS_BY_KIND.values() for field in fields}
    assert set(_MATCHER_FIELD_ATTRIBUTES) == matchable

    annotations = get_type_hints(TraceEvent)
    textual = {
        field
        for field, attribute in _MATCHER_FIELD_ATTRIBUTES.items()
        if _is_a_string_at_runtime(annotations[attribute])
    }

    assert textual == {"tool", "text", "result", "status", "executor"}
    assert textual == _TEXTUAL_MATCHER_FIELDS


def test_the_types_no_reference_can_correlate_with_text_are_read_off_the_table() -> None:
    """The five types the rule refuses, derived rather than listed beside the rule.

    The membership is what this holds, and it would hold just as well against a
    hand-written set — the derivation itself is carried by
    ``test_each_table_cell_agrees_with_the_shipped_operator_over_real_values``,
    which measures every cell against the operators. What this adds is the answer
    the rule must give whatever the derivation is: a type is uncorrelatable with
    text exactly when neither binding operator can ever hold between a string the
    event yields and a value of that type.
    """
    never_text = frozenset({"integer", "number", "boolean", "array", "object"})

    assert never_text == _UNCORRELATABLE_JSON_TYPES
