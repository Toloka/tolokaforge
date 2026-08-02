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

from tolokaforge.adapters._task_loader import (
    build_tool_inventory,
    load_task_yaml,
    validate_grading_yaml,
)
from tolokaforge.core.grading import config_validation
from tolokaforge.core.grading.config_validation import (
    AuthoringReport,
    Finding,
    ToolInventory,
    inspect_grading_authoring,
)
from tolokaforge.core.models import GradingFindingSeverity

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


def _quotes(operator: str, name: str) -> dict[str, Any]:
    """A ``present`` over an assistant message whose text reads the bound *name*."""
    return {"present": {"match": {"kind": "assistant_message", "text": {operator: name}}}}


@dataclass(frozen=True)
class _Rule:
    """One authored defect, the channel it lands in, and the fix it must name."""

    label: str
    task: Path
    grading: dict[str, Any]
    checker: str
    channel: str
    message: str


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
    report = inspect_grading_authoring(rule.grading, _inventory(rule.task))

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
        inspect_grading_authoring(rule.grading, _inventory(rule.task))

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
        "argument name in this block is checkable"
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
        "declares no type",
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
