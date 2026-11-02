"""The evidence a transcript-only judge reads (``customization.evidence: transcript``).

A transcript judge never sees the final state. It grades from three things this
module derives from the transcript alone:

* the transcript with every tool result numbered ``[call N]`` (in the order the
  results arrive) and cut at :attr:`TranscriptEvidence.tool_result_chars`
  characters — :func:`number_tool_results`;
* the agent's calls that can change state, in the order it made them, each with
  how it ended — :func:`state_changing_calls`;
* ``show_call`` — :class:`ShowCallTool`, which returns the full results the cut
  hides, read from the same transcript.

All three are pure functions of the transcript and :class:`TranscriptEvidence`, so
an offline replay that rebuilds the transcript and the options rebuilds the judge's
whole view: ``show_call`` needs no live backend and is never shimmed.

A call can change state unless its tool is in
:attr:`TranscriptEvidence.read_only_tools` (the agent tools that declare
``mutates_state: false``, ADR-0061) or it matches one of
:attr:`TranscriptEvidence.read_only_calls` (the task's
:class:`~tolokaforge.runner.models.ReadOnlyCallRule` list). An undeclared tool
reads as one that can change state, as ADR-0061 prescribes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tolokaforge.tools.registry import Tool, ToolCategory, ToolPolicy, ToolResult

if TYPE_CHECKING:
    from tolokaforge.runner.models import ReadOnlyCallRule

__all__ = [
    "DEFAULT_TRANSCRIPT_TOOL_CHARS",
    "SHOW_CALL_MAX_CHARS",
    "SHOW_CALL_TOOL_NAME",
    "NumberedTranscript",
    "RecordedCall",
    "ShowCallTool",
    "TranscriptEvidence",
    "message_text",
    "number_tool_results",
    "state_changing_calls",
]

#: How much of each tool result a transcript judge reads inline, by default.
DEFAULT_TRANSCRIPT_TOOL_CHARS = 2000

#: Name of the transcript judge's tool that returns full tool results.
SHOW_CALL_TOOL_NAME = "show_call"

#: Characters of tool results one ``show_call`` answer returns, over all the calls it names.
SHOW_CALL_MAX_CHARS = 12_000

#: The status a call shows when its result carries none (a transcript from an
#: engine that did not record it on the transcript wire).
UNKNOWN_STATUS = "status unknown"

#: The status of a call no result answers.
NO_RESULT_STATUS = "no result"


@dataclass(frozen=True)
class TranscriptEvidence:
    """How a transcript judge reads one trial.

    * ``tool_result_chars`` — how much of each tool result the transcript shows.
    * ``read_only_tools`` — agent tools that declare ``mutates_state: false``.
    * ``read_only_calls`` — the task's rules marking calls that cannot change state.
    """

    tool_result_chars: int = DEFAULT_TRANSCRIPT_TOOL_CHARS
    read_only_tools: frozenset[str] = frozenset()
    read_only_calls: tuple[ReadOnlyCallRule, ...] = ()

    def is_read_only(self, call: RecordedCall) -> bool:
        if call.name in self.read_only_tools:
            return True
        return any(_rule_matches(rule, call) for rule in self.read_only_calls)


@dataclass(frozen=True)
class RecordedCall:
    """One agent tool call as the transcript records it.

    ``number`` is the ``[call N]`` of the result that answers it, ``None`` when no
    result does; ``result`` is that result in full and ``status`` how the call ended.
    """

    name: str
    arguments: str
    number: int | None
    result: str
    status: str


@dataclass(frozen=True)
class NumberedTranscript:
    """The transcript as a transcript judge reads it, and its tool results by number.

    ``messages`` is the input transcript with each tool result's content replaced by
    ``[call N] <the result, cut>``. ``results`` maps each number to the call it
    answers and the full result; ``agent_calls`` are the agent's calls in order.
    """

    messages: list[dict[str, Any]]
    results: dict[int, RecordedCall]
    agent_calls: tuple[RecordedCall, ...] = ()


def message_text(content: Any) -> str:
    """A transcript message's content as text: the joined text blocks of a block list."""
    if isinstance(content, list):
        content = " ".join(block.get("text", "") for block in content if isinstance(block, dict))
    return (content or "").strip()


def number_tool_results(
    transcript: Sequence[Mapping[str, Any]], *, tool_result_chars: int
) -> NumberedTranscript:
    """Number every tool result ``[call N]`` and cut it at ``tool_result_chars``.

    A cut result says how many characters it left out and that ``show_call``
    returns them. Results are numbered in the order they arrive, the user's own
    tool steps included, so every result the transcript shows can be read in full.
    """
    calls_by_id = _calls_by_id(transcript)
    messages: list[dict[str, Any]] = []
    results: dict[int, RecordedCall] = {}
    number_by_id: dict[Any, int] = {}
    for message in transcript:
        if message.get("role") != "tool":
            messages.append(dict(message))
            continue
        number = len(results) + 1
        number_by_id.setdefault(message.get("tool_call_id"), number)
        text = message_text(message.get("content"))
        name, arguments = calls_by_id.get(message.get("tool_call_id"), ("?", ""))
        results[number] = RecordedCall(
            name=name,
            arguments=arguments,
            number=number,
            result=text,
            status=str(message.get("tool_status") or UNKNOWN_STATUS),
        )
        messages.append({**message, "content": f"[call {number}] {_cut(text, tool_result_chars)}"})
    return NumberedTranscript(
        messages=messages,
        results=results,
        agent_calls=_agent_calls(transcript, results, number_by_id),
    )


def state_changing_calls(
    numbered: NumberedTranscript, evidence: TranscriptEvidence
) -> list[RecordedCall]:
    """The agent's calls that can change state, in the order it made them."""
    return [call for call in numbered.agent_calls if not evidence.is_read_only(call)]


class ShowCallTool(Tool):
    """Returns the full tool results a transcript judge reads cut, by ``[call N]``."""

    def __init__(self, numbered: NumberedTranscript, *, tool_result_chars: int):
        super().__init__(
            name=SHOW_CALL_TOOL_NAME,
            description=(
                "Every tool result in the transcript starts with its number, [call N], "
                f"and is cut at {tool_result_chars} characters. This returns the full "
                "results of the calls you name, as the agent received them, at most "
                f"{SHOW_CALL_MAX_CHARS} characters in all; pass offset to read further "
                "into a long one. It shows what the agent saw, not the final state. "
                "Read-only."
            ),
            policy=ToolPolicy(timeout_s=15.0, category=ToolCategory.READ),
        )
        self._results = numbered.results

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "calls": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "Call numbers: the N of [call N].",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "The character each result starts at (default 0).",
                        },
                    },
                    "required": ["calls"],
                },
            },
        }

    def execute(self, calls: list[int] | None = None, offset: int = 0, **_: Any) -> ToolResult:
        if not calls:
            return ToolResult(success=False, output="", error="show_call needs 'calls', e.g. [7]")
        if offset < 0:
            return ToolResult(success=False, output="", error="show_call 'offset' must be >= 0")
        budget = SHOW_CALL_MAX_CHARS
        parts: list[str] = []
        for number in calls:
            call = self._results.get(number)
            if call is None:
                parts.append(f"call {number}: no such call (1-{len(self._results)})")
                continue
            if budget <= 0:
                parts.append(
                    f"call {number}: not shown, the answer is over {SHOW_CALL_MAX_CHARS} "
                    "characters; ask for it again"
                )
                continue
            piece = call.result[offset : offset + budget]
            budget -= len(piece)
            parts.append(
                f"call {number} {call.name}: characters {offset}-{offset + len(piece)} "
                f"of {len(call.result)}\n{piece}"
            )
        return ToolResult(success=True, output="\n\n".join(parts))


def _cut(text: str, limit: int) -> str:
    if not text:
        return "(empty result)"
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[... {len(text) - limit} more characters; show_call returns them]"


def _calls_by_id(transcript: Sequence[Mapping[str, Any]]) -> dict[Any, tuple[str, str]]:
    calls: dict[Any, tuple[str, str]] = {}
    for message in transcript:
        for call in message.get("tool_calls") or []:
            function = call.get("function", call)
            calls[call.get("id")] = (str(function.get("name", "?")), _arguments_text(function))
    return calls


def _arguments_text(function: Mapping[str, Any]) -> str:
    arguments = function.get("arguments", "")
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments, ensure_ascii=False)


def _agent_calls(
    transcript: Sequence[Mapping[str, Any]],
    results: Mapping[int, RecordedCall],
    number_by_id: Mapping[Any, int],
) -> tuple[RecordedCall, ...]:
    calls: list[RecordedCall] = []
    for message in transcript:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            function = call.get("function", call)
            number = number_by_id.get(call.get("id"))
            answered = results.get(number) if number is not None else None
            calls.append(
                RecordedCall(
                    name=str(function.get("name", "?")),
                    arguments=_arguments_text(function),
                    number=number,
                    result=answered.result if answered else "",
                    status=answered.status if answered else NO_RESULT_STATUS,
                )
            )
    return tuple(calls)


def _rule_matches(rule: ReadOnlyCallRule, call: RecordedCall) -> bool:
    if rule.tool != call.name:
        return False
    if not rule.arguments:
        return True
    arguments = _parsed_arguments(call.arguments)
    for path, allowed in rule.arguments.items():
        found, value = _value_at(arguments, path)
        if not found or value not in allowed:
            return False
    return True


def _parsed_arguments(arguments: str) -> Any:
    try:
        return json.loads(arguments) if arguments else {}
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"a transcript tool call carries unparseable arguments JSON ({exc}); the "
            "transcript wire never produces one"
        ) from exc


def _value_at(arguments: Any, path: str) -> tuple[bool, Any]:
    value = arguments
    for segment in path.split("."):
        if not isinstance(value, Mapping) or segment not in value:
            return False, None
        value = value[segment]
    return True, value
