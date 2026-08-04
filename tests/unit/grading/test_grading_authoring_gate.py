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

Every inventory is built from a real task pack, and every schema is the tool's own —
a mocked registry would let the severity table drift from what the tools declare.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from tolokaforge.adapters._task_loader import (
    build_tool_inventory,
    load_task_yaml,
    validate_grading_yaml,
)
from tolokaforge.core.grading import config_validation
from tolokaforge.core.grading.config_validation import (
    _A_NON_EMPTY_SECTION_STILL_DECLARES,
    _TEXTUAL_MATCHER_FIELDS,
    _TOOL_EXPECTATION_HAZARDS,
    _TRANSCRIPT_RULE_KEYS,
    _WHAT_EACH_SECTION_MUST_DECLARE,
    UNRESOLVED_COMBINE_REASON,
    AuthoringReport,
    CombineLayer,
    Finding,
    ToolInventory,
    inspect_grading_authoring,
)
from tolokaforge.core.grading.grade_components import (
    COMPONENT_BY_NAME,
    GRADE_COMPONENTS,
    component_requested,
)
from tolokaforge.core.grading.trace_timeline import TraceEvent
from tolokaforge.core.models import (
    GradingCombineConfig,
    GradingConfig,
    GradingFindingSeverity,
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


def _quotes(operator: str, name: str) -> dict[str, Any]:
    """A ``present`` over an assistant message whose text reads the bound *name*."""
    return {"present": {"match": {"kind": "assistant_message", "text": {operator: name}}}}


@dataclass(frozen=True)
class _Rule:
    """One authored defect, the channel it lands in, and the fix it must name.

    ``combine`` is the effective map the row is checked under. ``None`` leaves the
    weight rules out, which is what every tool-aware row wants: they are about a
    matcher, and a weight finding beside one would report two defects for one typo.
    """

    label: str
    task: Path
    grading: dict[str, Any]
    checker: str
    channel: str
    message: str
    combine: GradingCombineConfig | None = None


_RULES: tuple[_Rule, ...] = (
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
        grading={"state_checks": {"hash": {"enabled": False, "expected_state_hash": "aaaa"}}},
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
        message="Declare expected_state_hash or golden_actions",
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
        rule.grading, _inventory(rule.task), effective_combine=rule.combine
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
            rule.grading, _inventory(rule.task), effective_combine=rule.combine
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
        "state_checks": {"hash": {"enabled": False, "expected_state_hash": "aaaa"}},
    }

    with pytest.raises(ValueError) as excinfo:
        validate_grading_yaml(
            _write_grading(tmp_path, grading), inventory=ToolInventory.unresolvable()
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
    grading = {"state_checks": {"hash": {"enabled": enabled, "expected_state_hash": "aaaa"}}}

    assert inspect_grading_authoring(grading, _inventory(_HELPDESK)) == AuthoringReport()


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

    report = inspect_grading_authoring(grading, _inventory(_HELPDESK))

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

    report = inspect_grading_authoring(grading, _inventory(_HELPDESK))

    assert [finding.where for finding in report.errors] == ["state_checks.hash.enabled"]


def test_an_empty_golden_action_list_is_not_a_hash_source() -> None:
    """Standing single case: a declared source that replays nothing is no source.

    An empty list is the shape a pack reaches by deleting the actions and leaving the
    key, and both substrates already read it as absent — core names it as the missing
    source in ``grade.reasons`` and the runner replays nothing. A gate keying on the
    key's *presence* would accept it and leave the divergence reachable.
    """
    grading = {"state_checks": {"hash": {"enabled": True, "golden_actions": []}}}

    report = inspect_grading_authoring(grading, _inventory(_HELPDESK))

    assert [finding.where for finding in report.errors] == ["state_checks.hash.enabled"]


def test_golden_actions_alone_are_a_hash_source() -> None:
    """Standing single case: the source shape both substrates are proven to share.

    The replay is what every shipped golden-action pack grades by, so a rule reading
    only ``expected_state_hash`` as a source would refuse the one hash shape whose
    verdict is the same on both substrates. The action names a tool the task declares,
    which is the other thing a replayable source needs.
    """
    grading = _golden_actions({"name": "write_file"})

    assert inspect_grading_authoring(grading, _inventory(_HELPDESK)) == AuthoringReport()


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
    report = inspect_grading_authoring(_golden_actions(action), _inventory(_HELPDESK))

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

    report = inspect_grading_authoring(grading, _inventory(_HELPDESK))

    assert [finding.where for finding in report.errors] == [
        "state_checks.hash.golden_actions[1].name",
        "state_checks.hash.golden_actions[2].name",
    ]


def test_a_golden_action_under_a_disabled_flag_is_refused_by_no_rule_at_all() -> None:
    """The one hash shape the gate accepts and no substrate grades (#832).

    Deliberate, and asserted so it cannot change by accident in either direction. The
    name rule may not fire, because neither substrate resolves a source under a falsy
    flag and refusing it would be stricter than the grade. The block still declares a
    source, so the section rule sees something declared, and the flag agrees with no
    ``expected_state_hash``, so the source rule sees nothing to disagree with — the
    whole block grades nothing and draws no finding. #832 owns closing it, at the flag
    rather than at the name.
    """
    grading = _golden_actions({"name": "close_widget"}, enabled=False)

    assert inspect_grading_authoring(grading, _inventory(_HELPDESK)) == AuthoringReport()


def test_an_unresolvable_inventory_leaves_a_golden_action_name_unchecked() -> None:
    """The name rule needs the task's tools, so it is skipped with the rest of that group.

    One skip for the whole group, not one per rule: the block below is unreplayable
    against any tool set that resolves, and against an inventory that cannot answer it
    is simply not knowable — a gate that raised here would reject every pack whose
    adapter cannot report a tool set.
    """
    grading = _golden_actions({"name": "close_widget"})

    report = inspect_grading_authoring(grading, ToolInventory.unresolvable())

    assert report.errors == ()
    assert [skip.where for skip in report.unchecked] == ["grading"]


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
        {"hash": {"enabled": False, "expected_state_hash": "aaaa"}},
        "state_checks.hash.expected_state_hash",
        id="a_source_the_flag_never_reads",
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
    report = inspect_grading_authoring({"state_checks": state_checks}, _inventory(_HELPDESK))

    assert [finding.where for finding in report.errors] == [address]


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
        ToolInventory(declared=frozenset({"http_request"}), parameters={}, known=False)

    with pytest.raises(ValueError, match="unresolvable inventory carries tools"):
        ToolInventory(declared=frozenset(), parameters={"http_request": {}}, known=False)

    assert ToolInventory.unresolvable().known is False


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
    """A regex capture is a string whatever field it was taken off.

    Scoped to the type rule's own address rather than to an empty report: the gate
    answers nothing else about this block, and asserting that would pin the absence
    of every rule rather than the presence of this exemption.
    """
    grading = _bound_block(
        _tool_call("read_file"),
        {"start": {"field": "args.offset", "pattern": "([0-9]+)"}},
        _quotes("contains_binding", "start"),
    )

    report = inspect_grading_authoring(grading, _inventory(_CODING))

    flagged = [finding.where for finding in report.errors + report.advisories]
    assert _EXTRACTION_ADDRESS not in flagged, flagged


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
)


@pytest.mark.parametrize(("require", "field"), _REFERENCES_THAT_COMPARE_TEXT)
def test_every_event_field_holding_text_flags_a_binding_of_another_type(
    require: dict[str, Any], field: str
) -> None:
    """``TraceEvent`` declares three fields as ``str | None``, and all three compare text.

    A rule naming only the field a cell happened to use would let the identical
    never-true check through on the other two.
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


# Which attribute of ``TraceEvent`` each matchable field reads, written out here so
# the type check below compares the gate's set against the dataclass rather than
# against itself. ``args`` addresses ``arguments``, whose members are typed by the
# tool schema and not by the event.
_MATCHER_FIELD_ATTRIBUTES = {
    "tool": "tool_name",
    "text": "text",
    "result": "result",
    "executor": "executor",
    "status": "status",
    "args": "arguments",
}


def test_the_textual_matcher_fields_are_the_events_string_fields() -> None:
    """A field the event types as text is one every reference compares correctly.

    The gate flags a binding reference sitting on one of these against a schema-typed
    non-string extraction, and exempts the rest. So a field retyped to ``str | None``
    on ``TraceEvent`` and left out of the gate's set is a never-true check the gate
    stops reporting — the same class it exists to catch — and one typed as anything
    else but listed here is a correct native comparison the gate starts rejecting.
    """
    matchable = {field for fields in TRACE_MATCHABLE_FIELDS_BY_KIND.values() for field in fields}
    assert set(_MATCHER_FIELD_ATTRIBUTES) == matchable

    annotations = TraceEvent.__annotations__
    textual = {
        field
        for field, attribute in _MATCHER_FIELD_ATTRIBUTES.items()
        if annotations[attribute] == "str | None"
    }

    assert textual == _TEXTUAL_MATCHER_FIELDS
