"""Capability test — :attr:`Capability.PROGRESS_AFTER_SUCCESS`.

Mirror of an eval failure surface where the model called
``salesforce_create_case`` successfully on turn 7, then re-called it
with identical arguments 16 more times before the stuck detector
terminated the trial. Tool response was a clean success on every
repetition; the user gave no signal that another case was needed.

Design — fully synthesised history for determinism. We hand-craft:

1. A user request that needs ONE case opened.
2. A synthesised assistant turn that emits a clean
   ``create_support_case`` tool call.
3. A synthesised tool response carrying a SUCCESS payload — a
   non-null ``case_id``, ``status: created``, and the echoed subject.
4. A follow-up user message that asks a question NOT requiring a
   new case (e.g. "thanks, when will I get an update?"). The
   ``get_case_status`` tool is also offered so the model has a
   legitimate non-loop tool path to take.

A passing model responds with either (a) text, (b) a
``get_case_status`` call, or (c) a meaningfully different
``create_support_case`` call. A failing model re-emits
``create_support_case`` with arguments that match the prior
successful call.

Why a synthesised turn-1 rather than a real two-pass loop: a real
loop's first call might happen to fail or to produce different
arguments, making the test vacuous. Synthesising forces the exact
"prior call succeeded with these args" scenario.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tolokaforge.core.models import Message, MessageRole, ToolCall

from ._capability import Capability, ModelCertificate
from .registry import ALL_MODELS

_USER_EMAIL = "samira.patel@example.com"

_USER_REQUEST = (
    f"Please open a support case for our customer {_USER_EMAIL}. "
    f"Their office printer (asset MX-44) jammed and Engineering needs "
    f"to come out today. Subject should be 'Printer MX-44 jammed — "
    f"Engineering dispatch needed'."
)

# The follow-up turn that arrives AFTER the synthesised success.
# Phrased as an information question, not a duplicate request, so a
# well-behaved model has no reason to re-call create_support_case.
_FOLLOWUP = "Got it, thanks. Will you let me know when an engineer is on the way?"

_CASE_ID = "500Z00000000000777"

_BROKEN_TOOL_CALL_ID = "call_progress_probe_01"

_SUCCESSFUL_ARGS: dict[str, Any] = {
    "subject": "Printer MX-44 jammed — Engineering dispatch needed",
    "contact_email": _USER_EMAIL,
    "priority": "high",
}

_TOOL_SUCCESS_PAYLOAD = json.dumps(
    {
        "case_id": _CASE_ID,
        "status": "created",
        "subject": _SUCCESSFUL_ARGS["subject"],
        "contact_email": _SUCCESSFUL_ARGS["contact_email"],
        "priority": _SUCCESSFUL_ARGS["priority"],
        "next_action": "Engineering team will be dispatched and contact the requester directly.",
    }
)


_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "create_support_case",
            "description": (
                "Open a NEW support case. Use only when no existing case "
                "covers the request. Returns the new case_id on success."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {
                        "type": "string",
                        "description": "Short title of the case.",
                    },
                    "contact_email": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "default": None,
                        "description": "Contact email for the requester.",
                    },
                    "priority": {
                        "anyOf": [
                            {"enum": ["low", "normal", "high", "urgent"], "type": "string"},
                            {"type": "null"},
                        ],
                        "default": None,
                        "description": "Ticket priority bucket.",
                    },
                },
                "required": ["subject"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_case_status",
            "description": (
                "Fetch the current status of an existing case by case_id. "
                "Use this to answer follow-up questions about a case you "
                "already opened."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "case_id": {
                        "type": "string",
                        "description": "The case_id returned by create_support_case.",
                    },
                },
                "required": ["case_id"],
            },
        },
    },
]


_SYSTEM = (
    "You are a support-desk assistant. Use create_support_case to open "
    "a new case when needed, and get_case_status to look up an existing "
    "case. NEVER open a duplicate case — if a case has already been "
    "successfully created for the user's issue, acknowledge the existing "
    "case_id instead of opening another one."
)


def _decoded_args(raw: Any) -> dict[str, Any]:
    """Return tool-call arguments as a dict, decoding JSON-strings."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            pytest.fail(f"tool-call arguments arrived as a non-JSON string: {raw[:300]!r}")
    assert isinstance(
        raw, dict
    ), f"tool-call arguments must be a dict, got {type(raw).__name__}: {raw!r}"
    return raw


def _subjects_match(a: str | None, b: str | None) -> bool:
    """Case-insensitive whitespace-tolerant subject comparison.

    Matches "Printer MX-44 jammed — Engineering dispatch needed"
    against "printer mx-44 jammed - engineering dispatch needed",
    catching trivial re-statements that a model might use to
    circumvent a byte-exact check.
    """
    if not (isinstance(a, str) and isinstance(b, str)):
        return False
    return "".join(a.lower().split()) == "".join(b.lower().split())


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
def test_progress_after_success(
    cert: ModelCertificate,
    live_client,
    skip_unless_capability_declared,
) -> None:
    """Model advances past a successful tool call instead of re-issuing it.

    Assertions (in order — any single passing branch satisfies the contract):

    1. PASS if the model returns text only (no tool calls) — the
       follow-up question is informational and can be answered
       conversationally. ``"###STOP###"`` / acknowledgment is fine.
    2. PASS if the model calls ``get_case_status`` (with ``case_id``
       matching the synthesised success or any plausible variant) —
       legitimate advancement to the case-status path.
    3. PASS if the model calls ``create_support_case`` with a subject
       that is *substantively different* from the prior successful
       call AND a different contact_email or priority — interpreted
       as the model believing the follow-up implies a second,
       different case.
    4. FAIL if the model re-emits ``create_support_case`` with a
       subject matching the prior call (case-insensitive
       whitespace-tolerant) — that's the eval loop pattern.
    """
    skip_unless_capability_declared(cert, Capability.PROGRESS_AFTER_SUCCESS)
    client = live_client(cert)

    synthesised_assistant = Message(
        role=MessageRole.ASSISTANT,
        content="",
        tool_calls=[
            ToolCall(
                id=_BROKEN_TOOL_CALL_ID,
                name="create_support_case",
                arguments=dict(_SUCCESSFUL_ARGS),
            )
        ],
    )
    tool_success = Message(
        role=MessageRole.TOOL,
        content=_TOOL_SUCCESS_PAYLOAD,
        tool_call_id=_BROKEN_TOOL_CALL_ID,
    )

    result = client.generate(
        system=_SYSTEM,
        messages=[
            Message(role=MessageRole.USER, content=_USER_REQUEST),
            synthesised_assistant,
            tool_success,
            Message(role=MessageRole.USER, content=_FOLLOWUP),
        ],
        tools=_TOOLS,
        tool_choice="auto",
    )

    # Branch 1: text-only response is the cleanest pass.
    if not result.tool_calls:
        assert result.text and result.text.strip(), (
            f"{cert.model_id}: model returned neither tool calls nor text. "
            f"Empty response is a separate failure mode (probably an "
            f"upstream timeout); not the progress-after-success contract."
        )
        return

    # Some tool call was made; classify it.
    tc = result.tool_calls[0]
    if tc.name == "get_case_status":
        # Branch 2: legitimate advancement to the status-check path.
        args = _decoded_args(tc.arguments)
        assert args.get(
            "case_id"
        ), f"{cert.model_id}: get_case_status call missing case_id. Args: {args!r}"
        return

    if tc.name == "create_support_case":
        # Branch 3 vs 4: re-call of the same tool. Check arguments.
        args = _decoded_args(tc.arguments)
        if _subjects_match(args.get("subject"), _SUCCESSFUL_ARGS["subject"]):
            pytest.fail(
                f"{cert.model_id}: model re-called create_support_case with "
                f"the same subject as the prior successful call — the eval "
                f"loop pattern. Prior call: {_SUCCESSFUL_ARGS!r}. "
                f"This call: {args!r}. Tool response carried "
                f"case_id={_CASE_ID!r} so the model had clear evidence the "
                f"case already exists."
            )
        # Different subject — legitimate refinement.
        return

    pytest.fail(
        f"{cert.model_id}: model called an unknown tool {tc.name!r}. "
        f"Only create_support_case and get_case_status were offered."
    )
