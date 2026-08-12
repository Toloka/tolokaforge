"""Stage 8 capability test — :attr:`Capability.SIMPLE_TOOL_CALL`.

Every registered model with this capability must emit a structured tool
call when offered a calculator tool plus a distracting sibling tool. The
sibling (``log_note``) forces the model to actively choose the
calculator rather than regurgitate the single tool in the list.

Migrated from legacy
``tests/integration/test_llm_client_models.py::TestSimpleToolCalling``
and ``TestMultiToolSelection``, folded into one capability file because
both assertions target the same underlying contract (a well-formed
structured tool call with valid argument parsing).
"""

from __future__ import annotations

import json

import pytest

from tolokaforge.core.models import Message, MessageRole
from tolokaforge.testing.certify import ALL_MODELS, Capability, ModelCertificate

_CALCULATE_TOOL = {
    "type": "function",
    "function": {
        "name": "calculate",
        "description": (
            "Evaluate a mathematical expression. Only basic arithmetic "
            "operators (+, -, *, /, parentheses) and numeric literals."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Arithmetic expression. Example: '(200*1.5)+50'.",
                }
            },
            "required": ["expression"],
        },
    },
}

_LOG_NOTE_TOOL = {
    "type": "function",
    "function": {
        "name": "log_note",
        "description": "Log a free-form note.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
}


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
def test_simple_tool_call(
    cert: ModelCertificate,
    live_client,
    skip_unless_capability_declared,
) -> None:
    """Model selects the calculator tool and emits valid arguments.

    Assertions:
      1. ``result.tool_calls`` is non-empty.
      2. The selected tool is ``calculate`` (not the ``log_note``
         distractor).
      3. Arguments parse as a ``dict`` carrying the ``expression`` key.
    """
    skip_unless_capability_declared(cert, Capability.SIMPLE_TOOL_CALL)
    client = live_client(cert)
    result = client.generate(
        system=(
            "You are a calculator agent. When asked to calculate, you MUST "
            "use the calculate tool. Ignore unrelated logging suggestions."
        ),
        messages=[
            Message(
                role=MessageRole.USER,
                content="Calculate 250 * 12.50 + 500. Use the calculate tool.",
            )
        ],
        tools=[_CALCULATE_TOOL, _LOG_NOTE_TOOL],
        tool_choice="auto",
    )
    assert result.tool_calls, f"{cert.model_id}: no tool call returned (text={result.text[:120]!r})"
    tc = result.tool_calls[0]
    assert tc.name == "calculate", f"{cert.model_id}: wrong tool {tc.name!r}"

    args = tc.arguments
    if isinstance(args, str):
        args = json.loads(args)
    _msg = f"{cert.model_id}: arguments must be a dict, got {type(args).__name__}: {args!r}"
    assert isinstance(args, dict), _msg
    assert "expression" in args, f"{cert.model_id}: missing 'expression' in {args!r}"
