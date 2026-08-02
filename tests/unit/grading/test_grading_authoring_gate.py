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


@pytest.mark.parametrize("advisory", [True, False], ids=["advisory_on", "advisory_off"])
def test_an_unresolvable_inventory_fails_nothing_and_says_why(
    tmp_path: Path, advisory: bool
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
        advisory=advisory,
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
