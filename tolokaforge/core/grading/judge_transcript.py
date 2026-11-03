"""The evidence a transcript-only judge reads (``customization.evidence: transcript``).

A transcript judge never sees the final state. It grades from three things this
module derives from the transcript alone:

* the transcript with every tool result numbered ``[call N]`` (in the order the
  results arrive) and cut at :attr:`TranscriptEvidence.tool_result_chars`
  characters — :func:`number_tool_results`;
* the agent's calls that can change state, in the order it made them, each with
  the first line of the result the agent received — :func:`state_changing_calls`,
  :func:`call_result_head`;
* ``show_call`` — :class:`ShowCallTool`, which returns the full results the cut
  hides, read from the same transcript.

All three are pure functions of the transcript, :class:`TranscriptEvidence` and
the agent tools' ``mutates_state`` declarations
(:class:`~tolokaforge.core.tool_effects.ToolEffects`), so an offline replay that
rebuilds the transcript, the options and the declarations rebuilds the judge's
whole view: ``show_call`` needs no live backend and is never shimmed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tolokaforge.core.grading.judge_tools import read_only_policy
from tolokaforge.core.tool_call_ids import EpisodeUniqueCallIds
from tolokaforge.tools.registry import Tool, ToolResult

if TYPE_CHECKING:
    from tolokaforge.core.tool_effects import ToolEffects

__all__ = [
    "DEFAULT_TRANSCRIPT_TOOL_CHARS",
    "SHOW_CALL_MAX_CHARS",
    "SHOW_CALL_TOOL_NAME",
    "NumberedTranscript",
    "RecordedCall",
    "ShowCallTool",
    "TranscriptEvidence",
    "call_arguments_brief",
    "call_result_head",
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

#: Characters of a call's arguments the state-changing block shows; the transcript
#: shows them in full on the call itself.
CALL_ARGUMENT_CHARS = 300

#: Characters of the first line of a call's result the state-changing block shows.
RESULT_HEAD_CHARS = 120

#: What the judge reads in place of a result with no text.
EMPTY_RESULT = "(empty result)"

#: What the state-changing block shows for a call no result answers.
NO_RESULT = "no result"


@dataclass(frozen=True)
class TranscriptEvidence:
    """How a transcript judge reads one trial.

    * ``tool_result_chars`` — how much of each tool result the transcript shows.
    """

    tool_result_chars: int = DEFAULT_TRANSCRIPT_TOOL_CHARS


@dataclass(frozen=True)
class RecordedCall:
    """One agent tool call as the transcript records it.

    ``number`` is the ``[call N]`` of the result that answers it, ``None`` when no
    result does; ``result`` is that result in full, as the agent received it.
    """

    name: str
    arguments: str
    number: int | None
    result: str


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
    A call and its result are joined by the episode-unique key of their id
    (:mod:`tolokaforge.core.tool_call_ids`), so a provider that repeats an id
    across turns still joins each call to its own result.
    """
    declared = _declared_calls(transcript)
    result_keys = EpisodeUniqueCallIds()
    messages: list[dict[str, Any]] = []
    results: dict[int, RecordedCall] = {}
    number_by_key: dict[str, int] = {}
    for message in transcript:
        if message.get("role") != "tool":
            messages.append(dict(message))
            continue
        number = len(results) + 1
        key = result_keys.assign(str(message.get("tool_call_id")))
        number_by_key.setdefault(key, number)
        call = declared.get(key)
        text = message_text(message.get("content"))
        results[number] = RecordedCall(
            name=call.name if call else "?",
            arguments=call.arguments if call else "",
            number=number,
            result=text,
        )
        messages.append({**message, "content": f"[call {number}] {_cut(text, tool_result_chars)}"})
    return NumberedTranscript(
        messages=messages,
        results=results,
        agent_calls=_agent_calls(declared, results, number_by_key),
    )


def call_result_head(call: RecordedCall) -> str:
    """The first non-blank line of a call's result, cut at :data:`RESULT_HEAD_CHARS`.

    The result is the text the agent received, so a failed call reads with the
    :data:`~tolokaforge.core.tool_message_format.TOOL_ERROR_MESSAGE_PREFIX` the
    loop wrote onto it (``Error: Status: 403``).
    """
    if call.number is None:
        return NO_RESULT
    head = next((line.strip() for line in call.result.splitlines() if line.strip()), "")
    return _brief(head, RESULT_HEAD_CHARS) if head else EMPTY_RESULT


def call_arguments_brief(call: RecordedCall) -> str:
    """A call's arguments for the state-changing block, cut at :data:`CALL_ARGUMENT_CHARS`.

    The transcript shows them in full on the call itself.
    """
    return _brief(call.arguments, CALL_ARGUMENT_CHARS)


def state_changing_calls(
    numbered: NumberedTranscript, tool_effects: ToolEffects
) -> list[RecordedCall]:
    """The agent's calls that can change state, in the order it made them."""
    return [
        call
        for call in numbered.agent_calls
        if tool_effects.can_change_state(call.name, call.arguments)
    ]


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
            policy=read_only_policy(),
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


def _brief(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[:limit]}…"


def _cut(text: str, limit: int) -> str:
    if not text:
        return EMPTY_RESULT
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[... {len(text) - limit} more characters; show_call returns them]"


@dataclass(frozen=True)
class _DeclaredCall:
    role: str
    name: str
    arguments: str


def _declared_calls(transcript: Sequence[Mapping[str, Any]]) -> dict[str, _DeclaredCall]:
    """Every declared call by its episode-unique key, in declaration order."""
    keys = EpisodeUniqueCallIds()
    calls: dict[str, _DeclaredCall] = {}
    for message in transcript:
        for call in message.get("tool_calls") or []:
            function = call.get("function", call)
            calls[keys.assign(str(call.get("id")))] = _DeclaredCall(
                role=str(message.get("role")),
                name=str(function.get("name", "?")),
                arguments=_arguments_text(function),
            )
    return calls


def _arguments_text(function: Mapping[str, Any]) -> str:
    arguments = function.get("arguments", "")
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments, ensure_ascii=False)


def _agent_calls(
    declared: Mapping[str, _DeclaredCall],
    results: Mapping[int, RecordedCall],
    number_by_key: Mapping[str, int],
) -> tuple[RecordedCall, ...]:
    calls: list[RecordedCall] = []
    for key, call in declared.items():
        if call.role != "assistant":
            continue
        number = number_by_key.get(key)
        answered = results.get(number) if number is not None else None
        calls.append(
            RecordedCall(
                name=call.name,
                arguments=call.arguments,
                number=number,
                result=answered.result if answered else "",
            )
        )
    return tuple(calls)
