"""Rejected tool arguments: useful feedback, never the submitted values.

Schema validation runs before a tool's own code, so a tool cannot sanitize what
the executor says about the arguments it refused. The feedback names the argument
and the rule from the schema alone: a value the model submitted (here a synthetic
private marker) must not come back in the reply, and the tool must not run.
"""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.tools.registry import (
    Tool,
    ToolExecutionStatus,
    ToolExecutor,
    ToolPolicy,
    ToolRegistry,
    ToolResult,
)

pytestmark = pytest.mark.unit

MARKER = "SYNTHETIC_PRIVATE_VALUE_7f3c"


class _Quantity(Tool):
    def __init__(self, parameters: dict[str, Any] | None) -> None:
        super().__init__(name="set_quantity", description="Set a quantity", policy=ToolPolicy())
        self.parameters = parameters
        self.executions = 0

    def get_schema(self) -> dict[str, Any]:
        function: dict[str, Any] = {"name": self.name, "description": self.description}
        if self.parameters is not None:
            function["parameters"] = self.parameters
        return {"type": "function", "function": function}

    def execute(self, **kwargs: Any) -> ToolResult:
        self.executions += 1
        return ToolResult(success=True, output="ok", status=ToolExecutionStatus.SUCCESS)


SCHEMA = {
    "type": "object",
    "properties": {
        "quantity": {"type": "integer", "minimum": 1},
        "note": {"type": "string", "maxLength": 40},
        "unit": {"type": "string", "enum": ["box", "pallet"]},
        "lines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"sku": {"type": "string"}},
                "required": ["sku"],
            },
        },
    },
    "required": ["quantity"],
    "additionalProperties": False,
}


def _run(arguments: dict[str, Any], parameters: dict[str, Any] | None = SCHEMA):
    tool = _Quantity(parameters)
    registry = ToolRegistry()
    registry.register(tool)
    return ToolExecutor(registry).execute(tool.name, arguments, call_id="c1"), tool


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"quantity": MARKER}, "quantity: expected type integer"),
        ({"note": MARKER}, "missing required argument(s) 'quantity'"),
        ({"quantity": 1, "extra": MARKER}, "unexpected argument(s)"),
        ({"quantity": 1, MARKER: 1}, "declared: lines, note, quantity, unit"),
        ({"quantity": 0}, "quantity: violates minimum 1"),
        ({"quantity": 1, "note": MARKER * 3}, "note: violates maxLength 40"),
        ({"quantity": 1, "unit": MARKER}, 'unit: must be one of ["box", "pallet"]'),
        ({"quantity": 1, "lines": [{"sku": MARKER}, {"qty": MARKER}]}, "lines[]: missing required"),
    ],
)
def test_a_rejection_names_the_rule_and_never_the_value(arguments, expected):
    result, tool = _run(arguments)
    assert result.status is ToolExecutionStatus.INVALID_ARGUMENTS and not result.success
    assert expected in result.error
    assert MARKER not in result.error and MARKER not in result.output
    assert tool.executions == 0


def test_a_tool_without_arguments_does_not_echo_what_it_received():
    result, tool = _run({"anything": MARKER}, parameters=None)
    assert result.status is ToolExecutionStatus.INVALID_ARGUMENTS
    assert "takes no arguments" in result.error and MARKER not in result.error
    assert tool.executions == 0


def test_corrected_arguments_run_the_tool_once():
    rejected, tool = _run({"quantity": MARKER})
    assert rejected.status is ToolExecutionStatus.INVALID_ARGUMENTS
    registry = ToolRegistry()
    registry.register(tool)
    accepted = ToolExecutor(registry).execute(tool.name, {"quantity": 2}, call_id="c2")
    assert accepted.status is ToolExecutionStatus.SUCCESS and tool.executions == 1
