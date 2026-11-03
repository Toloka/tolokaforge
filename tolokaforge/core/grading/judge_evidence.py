"""What the rubric judge is shown, composed from the parts of ``customization.evidence``.

Each part of :class:`~tolokaforge.runner.models.JudgeEvidence` is an
:class:`EvidencePart`: it contributes a section of the judge's opening message, a
closing instruction, the tools it offers, the recorded tools an offline replay shims
for it, and whether it reads the transcript numbered. :func:`compose_evidence` is the
one composition over them; the judge registers ``submit_report`` and what it returns.

The transcript-reading parts derive everything from the transcript and the agent
tools' ``mutates_state`` declarations
(:class:`~tolokaforge.core.tool_effects.ToolEffects`):

* the transcript with every tool result numbered ``[call N]`` (in the order the
  results arrive), cut when ``tool_results`` is on — :func:`number_tool_results`;
* the agent's calls that can change state, in the order it made them, each with the
  first line of the result the agent received — :func:`state_changing_calls`;
* ``show_call`` — :class:`ShowCallTool`, which returns the full results the cut
  hides, read from the same transcript, so a replay rebuilds it and never shims it.

An extra read tool a caller hands the judge names the part that owns it in its
``evidence_part`` attribute, and is offered only while that part is on.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol

from tolokaforge.core.actors.tool_steps import user_tool_step_positions_of
from tolokaforge.core.grading.judge_tools import (
    GetDbStateTool,
    QueryDbTool,
    ReadFileTool,
    SearchKbTool,
    read_only_policy,
)
from tolokaforge.core.tool_call_ids import EpisodeUniqueCallIds
from tolokaforge.runner.models import EvidencePartName, JudgeEvidence
from tolokaforge.tools.registry import Tool, ToolResult

if TYPE_CHECKING:
    from tolokaforge.core.grading.judge import DBReader
    from tolokaforge.core.grading.kb_search import KnowledgeSearch
    from tolokaforge.core.tool_effects import ToolEffects

__all__ = [
    "EXTRA_TOOL_OWNERS",
    "REPLAYED_TOOL_OWNERS",
    "SHOW_CALL_MAX_CHARS",
    "SHOW_CALL_TOOL_NAME",
    "ComposedEvidence",
    "EvidencePart",
    "NumberedTranscript",
    "RecordedCall",
    "ShowCallTool",
    "TrialEvidence",
    "call_arguments_brief",
    "call_result_head",
    "compose_evidence",
    "evidence_parts",
    "format_transcript",
    "message_text",
    "neutralise_judge_delimiters",
    "number_tool_results",
    "state_changing_calls",
]

#: Name of the ``tool_results`` part's tool that returns full tool results.
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

#: Delimiter strings the opening message fences untrusted evidence with. Anything in
#: a model-controlled slot that replays one exactly would let the judge read what
#: follows as the harness's own instructions; :func:`neutralise_judge_delimiters`
#: breaks them.
_JUDGE_UNTRUSTED_DELIMITERS: tuple[str, ...] = (
    "===== TRANSCRIPT =====",
    "===== END TRANSCRIPT =====",
    "===== STATE-CHANGING CALLS =====",
    "===== END STATE-CHANGING CALLS =====",
)


def neutralise_judge_delimiters(text: str) -> str:
    """Break exact matches of the opening message's own fence strings inside untrusted text.

    Applied to every model-controlled slot (the agent's policy, each transcript
    message's content, each tool call's name and arguments). A zero-width space keeps
    the fence readable to an operator while it no longer matches the delimiter the
    judge anchors on. Only the exact fence strings are touched: free-form Markdown is
    common in honest agent output.
    """
    for delimiter in _JUDGE_UNTRUSTED_DELIMITERS:
        text = text.replace(delimiter, delimiter.replace("=====", "===​==="))
    return text


def message_text(content: Any) -> str:
    """A transcript message's content as text: the joined text blocks of a block list."""
    if isinstance(content, list):
        content = " ".join(block.get("text", "") for block in content if isinstance(block, dict))
    return (content or "").strip()


def format_transcript(transcript: list[dict[str, Any]]) -> str:
    """Render the agent transcript to a compact, judge-readable string.

    Tool calls are summarised inline so the judge sees what the agent *did*, not just
    what it said, and every model-controlled slot passes through
    :func:`neutralise_judge_delimiters`. A user's tool step
    (``actors.user.tool_turns: isolated``) and its results are labelled as such:
    nobody in the dialogue read them, and a step carries no text, so without its own
    line its calls would read as the agent's.
    """
    steps = user_tool_step_positions_of(transcript)
    lines: list[str] = []
    for index, msg in enumerate(transcript):
        role = str(msg.get("role", "?")).upper()
        content = message_text(msg.get("content"))
        if index in steps:
            lines.append(_user_tool_step_line(role, neutralise_judge_delimiters(content)))
            content = content or "(tool result)"
        elif content:
            lines.append(f"{role}: {neutralise_judge_delimiters(content)}")
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", tc) if isinstance(tc, dict) else {}
            name = fn.get("name", "?")
            args = fn.get("arguments", "")
            lines.append(
                f"  -> tool_call {neutralise_judge_delimiters(str(name))}"
                f"({neutralise_judge_delimiters(str(args))})"
            )
        if msg.get("tool_call_id") and not content:
            lines.append(f"{role}: (tool result)")
    return "\n".join(lines) if lines else "(empty transcript)"


def _user_tool_step_line(role: str, content: str) -> str:
    """The line a message of a user tool step renders as: the step, or one of its results."""
    if role == "USER":
        label = "USER (tool step, not shown to the agent)"
        return f"{label}: {content}" if content else label
    return f"TOOL (result for the user): {content or '(tool result)'}"


# ---------------------------------------------------------------------------
# The numbered transcript
# ---------------------------------------------------------------------------


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
    """The transcript with its tool results numbered, and those results by number.

    ``messages`` is the input transcript with each tool result's content replaced by
    ``[call N] <the result, cut where a cut applies>``. ``results`` maps each number to
    the call it answers and the full result; ``agent_calls`` are the agent's calls in
    order.
    """

    messages: list[dict[str, Any]]
    results: dict[int, RecordedCall]
    agent_calls: tuple[RecordedCall, ...] = ()


def number_tool_results(
    transcript: Sequence[Mapping[str, Any]], *, tool_result_chars: int | None
) -> NumberedTranscript:
    """Number every tool result ``[call N]``, cut at ``tool_result_chars`` (``None``: uncut).

    A cut result says how many characters it left out and that ``show_call`` returns
    them. Results are numbered in the order they arrive, the user's own tool steps
    included. A call and its result are joined by the episode-unique key of their id
    (:mod:`tolokaforge.core.tool_call_ids`), so a provider that repeats an id across
    turns still joins each call to its own result.
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
    :data:`~tolokaforge.core.tool_message_format.TOOL_ERROR_MESSAGE_PREFIX` the loop
    wrote onto it (``Error: Status: 403``).
    """
    if call.number is None:
        return NO_RESULT
    head = next((line.strip() for line in call.result.splitlines() if line.strip()), "")
    return _brief(head, RESULT_HEAD_CHARS) if head else EMPTY_RESULT


def call_arguments_brief(call: RecordedCall) -> str:
    """A call's arguments for the state-changing block, cut at :data:`CALL_ARGUMENT_CHARS`."""
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
    """Returns the full tool results the judge reads cut, by ``[call N]``."""

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


def _cut(text: str, limit: int | None) -> str:
    if not text:
        return EMPTY_RESULT
    if limit is None or len(text) <= limit:
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


# ---------------------------------------------------------------------------
# The parts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrialEvidence:
    """What one trial hands the judge, with the transcript numbered where a part reads it so."""

    agent_system_prompt: str
    transcript: list[dict[str, Any]]
    numbered: NumberedTranscript | None
    state_diff: str | None
    db_reader: DBReader | None
    kb_search: KnowledgeSearch | None
    extra_read_tools: tuple[Tool, ...]
    workspace_dir: Path | None
    tool_effects: ToolEffects

    def extras_owned_by(self, part: EvidencePartName) -> tuple[Tool, ...]:
        return tuple(
            tool for tool in self.extra_read_tools if getattr(tool, "evidence_part", None) == part
        )

    def shown_diff(self) -> str:
        """The state diff the judge is shown, ``""`` for none."""
        return (self.state_diff or "").strip()


ToolRecord = Literal["read_tools", "kb_tools"]
"""Which surface list a part's tools are recorded in: the final-state read tools a
replay shims, or the knowledge-search tools (offered or withheld)."""


class EvidencePart(Protocol):
    """One part of the judge's evidence.

    ``tools`` are the tools the part would offer for this trial; the composition
    offers them while the part is on, and records a ``kb_tools`` part's as withheld
    while it is off. ``replayed_tools`` are the recorded tool names an offline replay
    shims for the part.
    """

    @property
    def name(self) -> EvidencePartName: ...

    @property
    def enabled(self) -> bool: ...

    @property
    def numbers_transcript(self) -> bool: ...

    @property
    def recorded_as(self) -> ToolRecord | None: ...

    @property
    def replayed_tools(self) -> frozenset[str]: ...

    def tools(self, trial: TrialEvidence) -> tuple[Tool, ...]: ...

    def section(self, trial: TrialEvidence) -> str: ...

    def closing(self, trial: TrialEvidence, offered: frozenset[str]) -> str | None: ...


_GRADE_AND_SUBMIT = "grade each criterion and call submit_report."

_INSPECT_THE_FINAL_STATE = (
    f"Use your read-only tools to inspect the final state, then {_GRADE_AND_SUBMIT}"
)

_DB_READS = frozenset({"get_db_state", "query_db"})


@dataclass(frozen=True)
class AgentPolicyPart:
    """The agent's policy / system prompt, shown unless it is blank."""

    enabled: bool
    name: ClassVar[EvidencePartName] = EvidencePartName.AGENT_POLICY
    numbers_transcript: ClassVar[bool] = False
    recorded_as: ClassVar[ToolRecord | None] = None
    replayed_tools: ClassVar[frozenset[str]] = frozenset()

    def tools(self, trial: TrialEvidence) -> tuple[Tool, ...]:
        return ()

    def section(self, trial: TrialEvidence) -> str:
        policy = trial.agent_system_prompt.strip()
        if not policy:
            return ""
        return (
            "The agent under evaluation operated under this policy / system prompt:\n"
            f"---\n{neutralise_judge_delimiters(policy)}\n---\n\n"
        )

    def closing(self, trial: TrialEvidence, offered: frozenset[str]) -> str | None:
        return None


@dataclass(frozen=True)
class KnowledgeSearchPart:
    """The knowledge-search tools the agent had: ``search_kb`` and the extras this part owns."""

    enabled: bool
    snippet_chars: int | None
    name: ClassVar[EvidencePartName] = EvidencePartName.KNOWLEDGE_SEARCH
    numbers_transcript: ClassVar[bool] = False
    recorded_as: ClassVar[ToolRecord | None] = "kb_tools"
    replayed_tools: ClassVar[frozenset[str]] = frozenset()

    def tools(self, trial: TrialEvidence) -> tuple[Tool, ...]:
        search: tuple[Tool, ...] = (
            (SearchKbTool(trial.kb_search, snippet_chars=self.snippet_chars),)
            if trial.kb_search is not None
            else ()
        )
        return (*search, *trial.extras_owned_by(self.name))

    def section(self, trial: TrialEvidence) -> str:
        return ""

    def closing(self, trial: TrialEvidence, offered: frozenset[str]) -> str | None:
        return None


@dataclass(frozen=True)
class StateDiffPart:
    """The ``initial → final`` diff of the database state: the agent's own edits."""

    enabled: bool
    name: ClassVar[EvidencePartName] = EvidencePartName.STATE_DIFF
    numbers_transcript: ClassVar[bool] = False
    recorded_as: ClassVar[ToolRecord | None] = None
    replayed_tools: ClassVar[frozenset[str]] = frozenset()

    def tools(self, trial: TrialEvidence) -> tuple[Tool, ...]:
        return ()

    def section(self, trial: TrialEvidence) -> str:
        diff = trial.shown_diff()
        if not diff:
            return ""
        return (
            "Below is a diff of the database state the agent changed "
            "(initial → final): the rows it added, removed, or modified, plus "
            "which tables it left untouched. Use it as your starting point for "
            "what the agent did.\n"
            f"{diff}\n\n"
        )

    def closing(self, trial: TrialEvidence, offered: frozenset[str]) -> str | None:
        if not trial.shown_diff():
            return None
        if not offered >= _DB_READS:
            return f"The diff shows changes, not the complete final state. Then {_GRADE_AND_SUBMIT}"
        return (
            "The diff shows changes, not the complete final state. For criteria "
            "about what must NOT change, invariants, absence, or the quality of a "
            "full final value, inspect the relevant state directly with query_db "
            f"(or get_db_state as a last resort). Then {_GRADE_AND_SUBMIT}"
        )


@dataclass(frozen=True)
class DbPart:
    """``get_db_state`` / ``query_db`` over the final database state, and the extras it owns."""

    enabled: bool
    name: ClassVar[EvidencePartName] = EvidencePartName.DB
    numbers_transcript: ClassVar[bool] = False
    recorded_as: ClassVar[ToolRecord | None] = "read_tools"
    replayed_tools: ClassVar[frozenset[str]] = _DB_READS

    def tools(self, trial: TrialEvidence) -> tuple[Tool, ...]:
        reads: tuple[Tool, ...] = (
            (GetDbStateTool(trial.db_reader), QueryDbTool(trial.db_reader))
            if trial.db_reader is not None
            else ()
        )
        return (*reads, *trial.extras_owned_by(self.name))

    def section(self, trial: TrialEvidence) -> str:
        return ""

    def closing(self, trial: TrialEvidence, offered: frozenset[str]) -> str | None:
        return _INSPECT_THE_FINAL_STATE


@dataclass(frozen=True)
class WorkspacePart:
    """``read_file`` over the agent's workspace when one exists, and the extras it owns."""

    enabled: bool
    name: ClassVar[EvidencePartName] = EvidencePartName.WORKSPACE
    numbers_transcript: ClassVar[bool] = False
    recorded_as: ClassVar[ToolRecord | None] = "read_tools"
    replayed_tools: ClassVar[frozenset[str]] = frozenset({"read_file"})

    def tools(self, trial: TrialEvidence) -> tuple[Tool, ...]:
        workspace = trial.workspace_dir
        reader: tuple[Tool, ...] = (
            (ReadFileTool(workspace),) if workspace is not None and workspace.exists() else ()
        )
        return (*trial.extras_owned_by(self.name), *reader)

    def section(self, trial: TrialEvidence) -> str:
        return ""

    def closing(self, trial: TrialEvidence, offered: frozenset[str]) -> str | None:
        return _INSPECT_THE_FINAL_STATE


@dataclass(frozen=True)
class ToolResultsPart:
    """Every tool result numbered and cut at ``max_chars``, with ``show_call`` for the rest."""

    enabled: bool
    max_chars: int | None
    name: ClassVar[EvidencePartName] = EvidencePartName.TOOL_RESULTS
    numbers_transcript: ClassVar[bool] = True
    recorded_as: ClassVar[ToolRecord | None] = None
    replayed_tools: ClassVar[frozenset[str]] = frozenset()

    def tools(self, trial: TrialEvidence) -> tuple[Tool, ...]:
        if trial.numbered is None or self.max_chars is None:
            return ()
        return (ShowCallTool(trial.numbered, tool_result_chars=self.max_chars),)

    def section(self, trial: TrialEvidence) -> str:
        return ""

    def closing(self, trial: TrialEvidence, offered: frozenset[str]) -> str | None:
        return (
            "You cannot inspect the final state. Grade each criterion from this record, "
            "reading a cut result in full with show_call where a criterion depends on it, "
            "then call submit_report."
        )


@dataclass(frozen=True)
class StateChangingCallsPart:
    """The agent's calls that can change state, each with the first line of its result."""

    enabled: bool
    name: ClassVar[EvidencePartName] = EvidencePartName.STATE_CHANGING_CALLS
    numbers_transcript: ClassVar[bool] = True
    recorded_as: ClassVar[ToolRecord | None] = None
    replayed_tools: ClassVar[frozenset[str]] = frozenset()

    def tools(self, trial: TrialEvidence) -> tuple[Tool, ...]:
        return ()

    def section(self, trial: TrialEvidence) -> str:
        if trial.numbered is None:
            raise RuntimeError("the state-changing calls read the numbered transcript")
        calls = state_changing_calls(trial.numbered, trial.tool_effects)
        call_lines = (
            "\n".join(_state_changing_call_line(call) for call in calls)
            if calls
            else "(none: the agent made no call that can change state)"
        )
        return (
            "Below are the agent's calls that can change state, in the order it made "
            "them, each with the first line of the result the agent received. Long "
            "arguments are cut here; the transcript shows each call in full.\n"
            "===== STATE-CHANGING CALLS =====\n"
            f"{call_lines}\n"
            "===== END STATE-CHANGING CALLS =====\n\n"
        )

    def closing(self, trial: TrialEvidence, offered: frozenset[str]) -> str | None:
        return None


def _state_changing_call_line(call: RecordedCall) -> str:
    reference = f"[call {call.number}]" if call.number is not None else "[no result]"
    return neutralise_judge_delimiters(
        f"{reference} {call.name}({call_arguments_brief(call)}) -> {call_result_head(call)}"
    )


def evidence_parts(evidence: JudgeEvidence) -> tuple[EvidencePart, ...]:
    """The seven parts ``evidence`` configures, on or off."""
    knowledge_search = evidence.knowledge_search_options()
    tool_results = evidence.tool_results_options()
    return (
        AgentPolicyPart(enabled=evidence.agent_policy),
        KnowledgeSearchPart(
            enabled=knowledge_search is not None,
            snippet_chars=knowledge_search.snippet_chars if knowledge_search else None,
        ),
        StateDiffPart(enabled=evidence.state_diff),
        DbPart(enabled=evidence.db),
        WorkspacePart(enabled=evidence.workspace),
        ToolResultsPart(
            enabled=tool_results is not None,
            max_chars=tool_results.max_chars if tool_results else None,
        ),
        StateChangingCallsPart(enabled=evidence.state_changing_calls),
    )


#: The parts an extra read tool may name as its owner.
EXTRA_TOOL_OWNERS: frozenset[EvidencePartName] = frozenset(
    {EvidencePartName.KNOWLEDGE_SEARCH, EvidencePartName.DB, EvidencePartName.WORKSPACE}
)

#: The recorded tool names an offline replay shims, by the part that owns them.
REPLAYED_TOOL_OWNERS: Mapping[str, EvidencePartName] = {
    name: part.name for part in evidence_parts(JudgeEvidence()) for name in part.replayed_tools
}


# ---------------------------------------------------------------------------
# The composition
# ---------------------------------------------------------------------------

#: The order the parts' tools are offered in, after ``submit_report``.
_TOOL_ORDER: tuple[EvidencePartName, ...] = (
    EvidencePartName.TOOL_RESULTS,
    EvidencePartName.DB,
    EvidencePartName.KNOWLEDGE_SEARCH,
    EvidencePartName.WORKSPACE,
)

#: The order the parts' sections follow the transcript in.
_SECTION_ORDER: tuple[EvidencePartName, ...] = (
    EvidencePartName.STATE_CHANGING_CALLS,
    EvidencePartName.STATE_DIFF,
)

#: The order the parts are asked for the closing instruction; the first that gives one wins.
_CLOSING_ORDER: tuple[EvidencePartName, ...] = (
    EvidencePartName.STATE_DIFF,
    EvidencePartName.DB,
    EvidencePartName.WORKSPACE,
    EvidencePartName.TOOL_RESULTS,
)

_CANNOT_INSPECT = (
    "You cannot inspect the final state. Grade each criterion from this record, "
    "then call submit_report."
)


@dataclass(frozen=True)
class ComposedEvidence:
    """The judge's opening message and the tools it is offered besides ``submit_report``.

    ``read_tools_offered`` are the offered final-state read tools an offline replay
    shims; ``kb_tools_offered`` / ``kb_tools_withheld`` the knowledge-search tools
    offered, and withheld because the part is off.
    """

    opening_message: str
    tools: tuple[Tool, ...]
    read_tools_offered: tuple[str, ...]
    kb_tools_offered: tuple[str, ...]
    kb_tools_withheld: tuple[str, ...]


def compose_evidence(
    evidence: JudgeEvidence,
    *,
    agent_system_prompt: str,
    transcript: list[dict[str, Any]],
    state_diff: str | None,
    db_reader: DBReader | None,
    kb_search: KnowledgeSearch | None,
    extra_read_tools: Sequence[Tool],
    workspace_dir: Path | None,
    tool_effects: ToolEffects,
) -> ComposedEvidence:
    """Compose the judge's view of one trial from the parts ``evidence`` turns on.

    Raises:
        ValueError: an extra read tool names no part in :data:`EXTRA_TOOL_OWNERS`.
    """
    _refuse_unowned_extras(extra_read_tools)
    parts = {part.name: part for part in evidence_parts(evidence)}
    cut_chars = options.max_chars if (options := evidence.tool_results_options()) else None
    numbered = (
        number_tool_results(transcript, tool_result_chars=cut_chars)
        if any(part.enabled and part.numbers_transcript for part in parts.values())
        else None
    )
    trial = TrialEvidence(
        agent_system_prompt=agent_system_prompt,
        transcript=transcript,
        numbered=numbered,
        state_diff=state_diff,
        db_reader=db_reader,
        kb_search=kb_search,
        extra_read_tools=tuple(extra_read_tools),
        workspace_dir=workspace_dir,
        tool_effects=tool_effects,
    )
    tools, surface = _offered_tools([parts[name] for name in _TOOL_ORDER], trial)
    return ComposedEvidence(
        opening_message=_opening_message(
            parts, trial, cut_chars, frozenset(tool.name for tool in tools)
        ),
        tools=tools,
        read_tools_offered=tuple(surface["read_tools"]),
        kb_tools_offered=tuple(surface["kb_tools"]),
        kb_tools_withheld=tuple(surface["kb_withheld"]),
    )


def _refuse_unowned_extras(extra_read_tools: Sequence[Tool]) -> None:
    for tool in extra_read_tools:
        owner = getattr(tool, "evidence_part", None)
        if owner not in EXTRA_TOOL_OWNERS:
            raise ValueError(
                f"extra read tool {tool.name!r} names no evidence part that owns it "
                f"(evidence_part={owner!r}); tag it with one of "
                f"{sorted(part.value for part in EXTRA_TOOL_OWNERS)}"
            )


def _offered_tools(
    parts: Sequence[EvidencePart], trial: TrialEvidence
) -> tuple[tuple[Tool, ...], dict[str, list[str]]]:
    tools: list[Tool] = []
    surface: dict[str, list[str]] = {"read_tools": [], "kb_tools": [], "kb_withheld": []}
    for part in parts:
        candidates = part.tools(trial)
        names = [tool.name for tool in candidates]
        if not part.enabled:
            if part.recorded_as == "kb_tools":
                surface["kb_withheld"] += names
            continue
        tools += candidates
        if part.recorded_as is not None:
            surface[part.recorded_as] += names
    return tuple(tools), surface


def _opening_message(
    parts: Mapping[EvidencePartName, EvidencePart],
    trial: TrialEvidence,
    cut_chars: int | None,
    offered: frozenset[str],
) -> str:
    policy = parts[EvidencePartName.AGENT_POLICY]
    sections = "".join(parts[name].section(trial) for name in _SECTION_ORDER if parts[name].enabled)
    closing = next(
        (
            clause
            for name in _CLOSING_ORDER
            if parts[name].enabled and (clause := parts[name].closing(trial, offered)) is not None
        ),
        _CANNOT_INSPECT,
    )
    shown = trial.numbered.messages if trial.numbered is not None else trial.transcript
    return (
        f"{policy.section(trial) if policy.enabled else ''}"
        f"{_transcript_intro(trial.numbered, cut_chars)}\n"
        "===== TRANSCRIPT =====\n"
        f"{format_transcript(shown)}\n"
        "===== END TRANSCRIPT =====\n\n"
        f"{sections}"
        f"{closing}"
    )


def _transcript_intro(numbered: NumberedTranscript | None, cut_chars: int | None) -> str:
    if numbered is None:
        return "Here is the full transcript of the agent's interaction:"
    if cut_chars is None:
        return (
            "Here is the full transcript of the agent's interaction. Each tool result "
            "starts with its number, [call N]."
        )
    return (
        "Here is the full transcript of the agent's interaction. Each tool result "
        f"starts with its number, [call N], and a result longer than {cut_chars} "
        "characters is cut; show_call returns it in full."
    )
