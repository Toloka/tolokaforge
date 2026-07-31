"""Rejection contract for :func:`decode_transcript_wire`.

A wire payload that cannot be reconstructed into a linkable trace is rejected,
never degraded: a ``tool_calls`` entry without an ``id`` names the version skew
that produced it, unparseable ``arguments`` name the tool, and a missing ``role``,
``content``, ``function``, ``function.name`` or ``function.arguments`` names the
message. Defaulting any of these would hand grading a trace whose calls cannot be
joined to their results, silently.

Every rejection is a ``ValueError``, because that is what the runner's
``GradeTrial`` catches to turn a bad payload into a named RPC failure. A bare
``KeyError`` escapes it and reaches the operator as ``KeyError: 'function'``,
naming nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from tolokaforge.core.grading.transcript_wire import decode_transcript_wire

pytestmark = pytest.mark.unit


def _call(**overrides: Any) -> dict[str, Any]:
    entry = {
        "id": "call_A",
        "function": {"name": "refund", "arguments": '{"payment_id": "PAY-1"}'},
    }
    entry.update(overrides)
    return entry


def _messages(call: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "refund it"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
    ]


@pytest.mark.parametrize("absent_id", [None, ""], ids=["key_missing", "empty_string"])
def test_tool_call_without_an_id_is_rejected_naming_the_skew(absent_id: str | None) -> None:
    call = _call()
    if absent_id is None:
        del call["id"]
    else:
        call["id"] = absent_id

    with pytest.raises(ValueError) as excinfo:
        decode_transcript_wire(_messages(call))

    message = str(excinfo.value)
    assert "wire message 1" in message
    assert "'refund'" in message
    assert "predates the tool-call id on the transcript wire" in message


def test_unparseable_arguments_are_rejected_rather_than_defaulted() -> None:
    call = _call(function={"name": "refund", "arguments": "{not json"})

    with pytest.raises(ValueError, match="unparseable"):
        decode_transcript_wire(_messages(call))


def test_a_tool_call_without_a_function_is_rejected_as_a_value_error() -> None:
    """A bare ``KeyError`` escapes the ``except (ValueError, TimelineInconsistencyError)``
    that turns a bad payload into a named `GradeTrial` failure, so the operator sees
    ``KeyError: 'function'`` — naming neither the message nor what was wrong with it."""
    call = _call()
    del call["function"]

    with pytest.raises(ValueError) as excinfo:
        decode_transcript_wire(_messages(call))

    message = str(excinfo.value)
    assert "wire message 1" in message
    assert "no 'function'" in message
    assert "keys present: ['id']" in message


@pytest.mark.parametrize("missing", ["name", "arguments"])
def test_a_function_missing_a_key_is_rejected_naming_the_keys_present(missing: str) -> None:
    function = {"name": "refund", "arguments": "{}"}
    del function[missing]

    with pytest.raises(ValueError) as excinfo:
        decode_transcript_wire(_messages(_call(function=function)))

    message = str(excinfo.value)
    assert "wire message 1" in message
    assert f"has no {missing!r}" in message
    assert f"keys present: {sorted(function)}" in message


@pytest.mark.parametrize("missing", ["role", "content"])
def test_missing_role_or_content_is_rejected_naming_the_message(missing: str) -> None:
    entry = {"role": "tool", "content": "{}", "tool_call_id": "call_A"}
    del entry[missing]

    with pytest.raises(ValueError, match=f"wire message 0 has no {missing!r}"):
        decode_transcript_wire([entry])


def test_a_well_formed_payload_decodes_to_linkable_calls() -> None:
    decoded = decode_transcript_wire(
        _messages(_call()) + [{"role": "tool", "content": "ok", "tool_call_id": "call_A"}]
    )

    assert decoded[0].tool_calls is None
    assert decoded[1].tool_calls is not None
    assert decoded[1].tool_calls[0].id == decoded[2].tool_call_id == "call_A"
    assert decoded[1].tool_calls[0].arguments == {"payment_id": "PAY-1"}
