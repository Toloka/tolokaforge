"""Schema-executor parity — the sanitized schema IS the accepted-args schema.

The invariant this module locks: for every (provider-shaped tool schema,
model-emitted arguments) pair,
``ToolExecutor.execute(name, args, call_id=..., validation_schema=S)``
accepts ``args`` iff ``args`` conforms to ``S``, where ``S`` is the parameters
schema the model was shown for that tool (the output of
``capabilities.schema_sanitizer.sanitize(tools)``). When ``validation_schema``
is omitted or ``None``, the executor validates against
``tool.get_schema()["function"]["parameters"]``.
"""

from __future__ import annotations

from typing import Any

import pytest
from tolokaforge_models.policies.gemini import GeminiSchema

from tolokaforge.core.docker_adapter import DockerRunnerAdapter
from tolokaforge.tools.registry import (
    Tool,
    ToolExecutionStatus,
    ToolExecutor,
    ToolRegistry,
    ToolResult,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# Tool fixtures
# --------------------------------------------------------------------------


class _FlatTool(Tool):
    """A plain flat-schema tool: ``{a: str, b: int}``, both required."""

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "flat_tool",
                "description": "flat",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "a": {"type": "string"},
                        "b": {"type": "integer"},
                    },
                    "required": ["a", "b"],
                },
            },
        }

    def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(success=True, output="flat-ok")


class _OneOfTool(Tool):
    """A tool whose ``item`` parameter is a Pydantic discriminated-union shape.

    Mirrors the discovery repro: ``Annotated[Ticket | Task,
    Field(discriminator='kind')]`` with per-branch ``additionalProperties:
    false`` and branch-specific ``required``. The sanitiser's flatten pass
    is lossy — the tight branch shape cannot be reconstructed from the flat
    surface — which is what makes the parity invariant load-bearing.
    """

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "oneof_tool",
                "description": "oneof",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "item": {
                            "oneOf": [
                                {
                                    "type": "object",
                                    "properties": {
                                        "kind": {"type": "string", "const": "ticket"},
                                        "ticket_id": {"type": "string"},
                                        "subject": {"type": "string"},
                                    },
                                    "required": ["kind", "ticket_id", "subject"],
                                    "additionalProperties": False,
                                },
                                {
                                    "type": "object",
                                    "properties": {
                                        "kind": {"type": "string", "const": "task"},
                                        "task_id": {"type": "string"},
                                        "title": {"type": "string"},
                                    },
                                    "required": ["kind", "task_id", "title"],
                                    "additionalProperties": False,
                                },
                            ],
                            "discriminator": {"propertyName": "kind"},
                        }
                    },
                    "required": ["item"],
                },
            },
        }

    def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(success=True, output="oneof-ok")


def _make_flat_executor() -> ToolExecutor:
    registry = ToolRegistry()
    registry.register(_FlatTool("flat_tool", "flat"))
    return ToolExecutor(registry)


def _make_oneof_executor() -> tuple[ToolExecutor, dict[str, Any]]:
    """Return an executor with the oneOf tool registered plus the sanitised
    parameters schema Gemini would show the model."""
    registry = ToolRegistry()
    tool = _OneOfTool("oneof_tool", "oneof")
    registry.register(tool)
    executor = ToolExecutor(registry)
    sanitised = GeminiSchema().sanitize([tool.get_schema()])
    return executor, sanitised[0]["function"]["parameters"]


# --------------------------------------------------------------------------
# The six parity cases
# --------------------------------------------------------------------------


def test_case_1_baseline_pass_plain_schema_no_override() -> None:
    """Case 1: plain schema, valid args, no override → SUCCESS.

    Locks that omitting ``validation_schema`` preserves the executor's
    current behaviour verbatim.
    """
    executor = _make_flat_executor()

    result = executor.execute("flat_tool", {"a": "x", "b": 1}, call_id="c1")

    assert result.success is True
    assert result.status is None  # SUCCESS is expressed as ``status=None`` here


def test_case_2_baseline_reject_plain_schema_no_override() -> None:
    """Case 2: plain schema, missing required, no override → INVALID_ARGUMENTS.

    Locks that jsonschema validation still fires when no override is passed.
    """
    executor = _make_flat_executor()

    result = executor.execute("flat_tool", {"a": "x"}, call_id="c2")

    assert result.success is False
    assert result.status is ToolExecutionStatus.INVALID_ARGUMENTS


def test_case_3_gemini_flatten_case_a_well_formed_branch_with_override() -> None:
    """Case 3 (Case A): a well-formed ticket branch conforms to both the
    original and the sanitised surface. With override → SUCCESS.
    """
    executor, sanitised_params = _make_oneof_executor()

    result = executor.execute(
        "oneof_tool",
        {"item": {"kind": "ticket", "ticket_id": "T-42", "subject": "hi"}},
        call_id="c3",
        validation_schema=sanitised_params,
    )

    assert result.success is True
    assert result.status is None


def test_case_4a_gemini_flatten_case_b_stray_sibling_with_override() -> None:
    """Case 4a (Case B with override): ticket branch plus a stray ``task_id``
    conforms to the sanitised surface (both siblings are unioned into one
    object schema). With override → SUCCESS.
    """
    executor, sanitised_params = _make_oneof_executor()

    result = executor.execute(
        "oneof_tool",
        {
            "item": {
                "kind": "ticket",
                "ticket_id": "T-42",
                "subject": "hi",
                "task_id": "ignore",
            }
        },
        call_id="c4a",
        validation_schema=sanitised_params,
    )

    assert result.success is True
    assert result.status is None


def test_case_4b_gemini_flatten_case_b_without_override_still_rejects() -> None:
    """Case 4b (Case B without override): the original nested oneOf schema
    rejects the flat args on the stray sibling — the fallback path (no
    override) validates against the tool's declared schema.
    """
    executor, _ = _make_oneof_executor()

    result = executor.execute(
        "oneof_tool",
        {
            "item": {
                "kind": "ticket",
                "ticket_id": "T-42",
                "subject": "hi",
                "task_id": "ignore",
            }
        },
        call_id="c4b",
    )

    assert result.success is False
    assert result.status is ToolExecutionStatus.INVALID_ARGUMENTS


def test_case_5a_gemini_flatten_case_c_branch_required_missing_with_override() -> None:
    """Case 5a (Case C with override): only the discriminator plus one branch
    field, no branch-specific required. Conforms to the sanitised surface
    (which reduces ``required`` to the discriminator intersection). With
    override → SUCCESS.
    """
    executor, sanitised_params = _make_oneof_executor()

    result = executor.execute(
        "oneof_tool",
        {"item": {"kind": "ticket", "ticket_id": "T-42"}},
        call_id="c5a",
        validation_schema=sanitised_params,
    )

    assert result.success is True
    assert result.status is None


def test_case_5b_gemini_flatten_case_c_without_override_still_rejects() -> None:
    """Case 5b (Case C without override): the original nested oneOf schema
    rejects the args on branch-required — the fallback path validates against
    the tool's declared schema.
    """
    executor, _ = _make_oneof_executor()

    result = executor.execute(
        "oneof_tool",
        {"item": {"kind": "ticket", "ticket_id": "T-42"}},
        call_id="c5b",
    )

    assert result.success is False
    assert result.status is ToolExecutionStatus.INVALID_ARGUMENTS


def test_case_6_sanitized_schema_still_enforces_its_own_invariants() -> None:
    """Case 6: ``kind`` outside the sanitised enum (``["task", "ticket"]``).
    With override → INVALID_ARGUMENTS. Locks that the override *replaces*
    the schema, not the gate — validation still runs.
    """
    executor, sanitised_params = _make_oneof_executor()

    result = executor.execute(
        "oneof_tool",
        {"item": {"kind": "other"}},
        call_id="c6",
        validation_schema=sanitised_params,
    )

    assert result.success is False
    assert result.status is ToolExecutionStatus.INVALID_ARGUMENTS


def test_case_7a_validation_schema_none_falls_back_to_tool_schema_pass() -> None:
    """Case 7a: ``validation_schema=None`` explicit — the executor falls back
    to the tool's own ``get_schema()["function"]["parameters"]``. Models the
    :class:`ToolCallingLoop` state where ``validation_schemas_by_tool`` is set
    but the current tool's name is missing from the map (``.get`` returns
    ``None``); the fallback is intentional and this case documents it.
    """
    executor = _make_flat_executor()

    result = executor.execute(
        "flat_tool",
        {"a": "x", "b": 1},
        call_id="c7a",
        validation_schema=None,
    )

    assert result.success is True
    assert result.status is None


def test_case_7b_validation_schema_none_falls_back_to_tool_schema_reject() -> None:
    """Case 7b: same fallback path as 7a, but missing-required lands at the
    tool's own schema — INVALID_ARGUMENTS. Locks that the fallback is not a
    "no-op" — the tool's schema still gates the call.
    """
    executor = _make_flat_executor()

    result = executor.execute(
        "flat_tool",
        {"a": "x"},
        call_id="c7b",
        validation_schema=None,
    )

    assert result.success is False
    assert result.status is ToolExecutionStatus.INVALID_ARGUMENTS


# --------------------------------------------------------------------------
# Docker-adapter kwarg refusal
# --------------------------------------------------------------------------


class _RecordingRuntime:
    """A minimal ``RuntimeBackend`` stub that records the ``execute_tool``
    call kwargs. Used to prove ``DockerRunnerAdapter.execute`` does not smuggle
    ``validation_schema`` into the tool's ``arguments`` dict.
    """

    def __init__(self) -> None:
        self.last_call: dict[str, Any] | None = None

    def execute_tool(
        self,
        trial_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        executor: str = "agent",
        *,
        call_id: str,
    ) -> ToolResult:
        self.last_call = {
            "trial_id": trial_id,
            "tool_name": tool_name,
            "arguments": dict(arguments),
            "executor": executor,
            "call_id": call_id,
        }
        return ToolResult(success=True, output="runtime-ok")


def test_docker_adapter_accepts_validation_schema_without_smuggling_it_into_arguments() -> None:
    """``DockerRunnerAdapter.execute`` accepts the ``validation_schema`` kwarg
    but must NOT fold it into the tool's ``arguments`` dict — the runner-side
    gRPC path does no jsonschema validation, so the schema has nowhere to go
    and letting it leak into ``arguments`` would send it to the tool as a
    parameter.
    """
    runtime = _RecordingRuntime()
    adapter = DockerRunnerAdapter(runtime, trial_id="trial-1", executor="agent")

    result = adapter.execute(
        "some_tool",
        {"a": "x"},
        call_id="call-1",
        validation_schema={"type": "object"},
    )

    assert result.success is True
    assert runtime.last_call is not None
    assert runtime.last_call["arguments"] == {"a": "x"}
    assert "validation_schema" not in runtime.last_call["arguments"]
