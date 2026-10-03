"""Coding-agent transcripts as traces: the reader, the outbound gate, the projection.

An agent transcript is the record of an agent working *on* a benchmark rather than *in* it: an
agent that integrates a new model into the engine, or one that checks an evaluation's results. It
is not benchmark data, so it carries ``source:agent-transcript`` and belongs in a destination that
accepts transcripts.

Three steps, in order, each usable on its own:

``read_claude_output``
    Claude Code's ``-p`` output in any of its three shapes (line-delimited JSON from
    ``--output-format stream-json``, one JSON array from ``--output-format json --verbose``, the
    result object alone from ``--output-format json``) becomes a :class:`Transcript`. **The
    allowlist lives here.** An event type, a content block or a message shape this reader does not
    know is not guessed about: it refuses the whole file. Agent output is arbitrary text produced
    on a machine that holds credentials, so an unrecognised shape is a reason to send nothing.

``redact``
    The tool input/output policy. Under the default ``drop`` every tool argument and every tool
    result is replaced by its size; under ``scrub`` the text is truncated and every credential
    shape the sentinel knows is removed. ``drop`` is the default because a tool result is exactly
    where ``env``, ``cat .env`` and shell history land, and a scrub that misses one shape is a
    leak on a shared instance. A transcript that has not been through this step cannot be
    projected: :func:`build_events` refuses it.

``build_events``
    The transcript as ingestion bodies: the trace, its root (an ``agent`` observation), one
    generation per assistant turn, one ``tool`` observation per tool result. The same bodies the
    trial projection builds, so :mod:`tolokaforge_langfuse.otlp_spans` turns them into v4 spans
    unchanged. The prompt the agent was given is not in its output, so it is the caller's to pass
    (``TranscriptOptions.input``); it becomes the trace's input.

    **A turn is one model response.** The CLI writes a response with several content blocks
    (thinking, text, tool calls) as several ``assistant`` events, one per block, and every one of
    them repeats the response's usage. The reader joins the events of one message id into one turn
    and counts its usage once. **A turn's clock** runs from the event before it to its last event
    (the model call), and a tool's from the turn that called it to its result. **A turn's cost** is
    its share of what the CLI reported the run cost (``total_cost_usd``), shared out by the turns'
    tokens at Claude's relative list prices, so the trace's cost is the CLI's figure
    (``cost_basis: cli``); a run the CLI reported no cost for states zero (``cost_basis: none``)
    rather than leave the receiver to price it from its own table.

**The id contract is injected, not imported.** The engine's ``tolokaforge.observability.ids`` and
an offline uploader's own module implement the same contract v2 and differ in one keyword;
:func:`id_contract` adapts either, so this module holds the projection once and neither producer
has to import the other's. Ids put the transcript id in the task position with trial index 0:
``trace|<run_tag>|<run_id>|<transcript_id>|0|na``, generations keyed by assistant-turn ordinal and
tool spans by the call id the agent's own output gives them.

What this module never does: keep a ``system`` event's description of the machine the agent ran on
(``cwd``, ``memory_paths``, ``mcp_servers``, ``apiKeySource``), or send anything. The sentinel scan
that runs immediately before a send is :mod:`tolokaforge_langfuse.safety`, the uploader's job.

Engine-free by construction: it reads agent output and returns bodies, so an offline uploader
imports it next to any engine pin, or with none.
"""

from __future__ import annotations

import inspect
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from tolokaforge_langfuse import safety
from tolokaforge_langfuse.costs import COST_BASIS_CLI
from tolokaforge_langfuse.model_names import (
    ModelIdentity,
    ModelNameResolver,
    ModelNameResolverError,
    RawModelNameResolver,
)
from tolokaforge_langfuse.vocabulary import (
    EVENT_AGENT,
    EVENT_TOOL,
    NAME_AGENT,
    NAME_TRANSCRIPT,
    SOURCE_TRANSCRIPT,
    TRANSCRIPT_CALLER_PREFIXES,
    VocabularyError,
    check_value,
    order_tags,
)

NONE = "none"
HARNESS = "claude-code"
SOURCE = SOURCE_TRANSCRIPT

# contract v2: a transcript occupies the task position of a trial id, with no trial and no attempt
TRIAL_INDEX = 0
NO_ATTEMPT = "na"
ROOT_KEY = "-"

# how much of the conversation rides on a generation's input, and how much of one message
# (the trial projection's numbers, so a transcript reads like a trial in the same UI)
CONTEXT_MESSAGES = 6
CONTEXT_CHARS = 2000

# the three shapes of ``claude -p`` output
SHAPE_STREAM = "stream-json"
SHAPE_ARRAY = "json-array"
SHAPE_RESULT = "json-object"

# the allowlist: everything this reader knows how to project, and nothing else
EVENT_TYPES = frozenset({"system", "assistant", "user", "result"})
SYSTEM_INIT = "init"
ASSISTANT_BLOCKS = frozenset({"text", "tool_use", "thinking", "redacted_thinking"})
USER_BLOCKS = frozenset({"tool_result", "text"})

# how much of an unknown type a refusal quotes
SHOWN_CHARS = 40

TOOL_IO_DROP = "drop"
TOOL_IO_SCRUB = "scrub"
TOOL_IO_POLICIES = (TOOL_IO_DROP, TOOL_IO_SCRUB)
SCRUB_MAX_CHARS = 2000

# the trace metadata key that keeps the transcript's own model name when the caller names the
# model that served the run (``TranscriptOptions.model``)
CLI_MODEL_KEY = "cli_model"

# how much of the prompt the agent was given (``TranscriptOptions.input``) rides on the trace as
# its input: an evaluation analysis's prompt is 25-35 KB (a shared context, one brief, the run's
# facts), so only a runaway one is cut
INPUT_MAX_CHARS = 65_536
# the trace metadata keys that say what became of that prompt: its length as given, whether the
# cap cut it, and the rules the scrub fired on it
INPUT_CHARS_KEY = "input_chars"
INPUT_TRUNCATED_KEY = "input_truncated"
INPUT_REDACTED_RULES_KEY = "input_redacted_rules"
# schema keys the projection writes on some traces only: reserved on every trace, so a caller's
# metadata cannot take one where the projection happens not to write it
RESERVED_KEYS = frozenset(
    {CLI_MODEL_KEY, INPUT_CHARS_KEY, INPUT_TRUNCATED_KEY, INPUT_REDACTED_RULES_KEY}
)

_ZONE_SUFFIX = re.compile(r"[+-]\d{2}:?\d{2}$")
# a later run of the same work: ``analysis/four_bucket/2``, ``resolve/3``
_ORDINAL_SEGMENT = re.compile(r"(?:/\d+)+$")
# a transcript trace's name: a template over the run's ``{label}``, the ``{transcript}`` id and
# the ``{step}`` (the transcript id without the ordinal of a later run)
DEFAULT_TRACE_NAME = "{label}/{transcript}"
_NAME_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
NAME_PLACEHOLDERS = frozenset({"label", "transcript", "step"})

# Claude's list prices relative to a model's input price, the same for every Claude model: output
# five times the input, a cache read a tenth, a five-minute cache write 1.25 and a one-hour write
# twice the input price. A turn's share of the run's cost is its tokens at these weights.
WEIGHT_OUTPUT = 5.0
WEIGHT_CACHE_READ = 0.1
WEIGHT_CACHE_WRITE_5M = 1.25
WEIGHT_CACHE_WRITE_1H = 2.0


def _scrub_shapes() -> tuple[tuple[str, re.Pattern[str]], ...]:
    """The sentinel's byte shapes as text patterns.

    One source of truth on purpose: a shape :mod:`tolokaforge_langfuse.safety` detects is a shape
    the scrub removes, so the two can never drift into a scrub that leaves behind what the gate
    then refuses (or worse, the reverse).
    """
    keep = re.IGNORECASE | re.MULTILINE | re.DOTALL
    return tuple(
        (rule, re.compile(pattern.pattern.decode("utf-8"), pattern.flags & keep))
        for rule, pattern in safety.SHAPES
    )


SCRUB_SHAPES = _scrub_shapes()


class TranscriptError(ValueError):
    """The transcript cannot be used; the message names the source and what is wrong."""


class TranscriptRefused(TranscriptError):
    """The transcript holds a shape this reader does not know, so none of it is sent."""


# -- what a transcript is ----------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    """One tool the agent called. ``call_id`` joins it to its result."""

    call_id: str
    name: str
    arguments: Any
    redaction: str = NONE
    removed: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssistantTurn:
    """One turn the agent took (one model response): what it said, what it was thinking, what it
    called. ``timestamp`` is the clock of its last event, ``started_at`` the clock of the event
    before it: the model call ran between the two. ``events`` counts the stream events the CLI
    split the response into."""

    index: int
    model: str | None
    text: str
    reasoning: str
    tool_calls: tuple[ToolCall, ...]
    usage: Mapping[str, Any]
    timestamp: str | None = None
    is_error: bool = False
    started_at: str | None = None
    message_id: str | None = None
    events: int = 1


@dataclass(frozen=True)
class ToolOutcome:
    """What one tool returned to the agent."""

    position: int
    call_id: str
    output: Any
    is_error: bool = False
    timestamp: str | None = None
    redaction: str = NONE
    removed: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunResult:
    """How the agent run ended, from the CLI's own result event."""

    subtype: str | None = None
    text: str | None = None
    total_cost_usd: float | None = None
    num_turns: int | None = None
    duration_ms: int | None = None
    duration_api_ms: int | None = None
    is_error: bool = False
    stop_reason: str | None = None
    terminal_reason: str | None = None
    permission_denials: int = 0
    usage: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Transcript:
    """One agent run, read and (once ``tool_io`` is set) gated."""

    transcript_id: str
    shape: str
    turns: tuple[AssistantTurn, ...] = ()
    outcomes: tuple[ToolOutcome, ...] = ()
    result: RunResult | None = None
    cli_version: str | None = None
    session_id: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    # the policy that produced this transcript's tool input and output; None until ``redact`` ran,
    # and ``build_events`` refuses a transcript that still says None
    tool_io: str | None = None
    origin: str = "<text>"

    @property
    def tool_names(self) -> Mapping[str, str]:
        return {call.call_id: call.name for turn in self.turns for call in turn.tool_calls}


@dataclass(frozen=True)
class BuiltTranscript:
    trace_id: str
    events: list[dict[str, Any]]


# -- the id contract, injected -----------------------------------------------------------------

RUN_TAG_KEYWORDS = ("run_tag", "version")


@dataclass(frozen=True)
class IdContract:
    """The contract-v2 id functions this projection needs, adapted from a producer's own module."""

    trace: Callable[[str, str, str], str]
    observation: Callable[..., str]
    tool_key: Callable[[object, object], str]
    version: int

    def root(self, trace: str) -> str:
        return self.observation(trace, "root", ROOT_KEY)


def id_contract(module: Any) -> IdContract:
    """Adapt an id module to this projection.

    The engine's module and an offline uploader's implement the same contract and differ in one
    keyword: the run tag is ``run_tag`` in one and ``version`` in the other. The spelling is
    resolved once, from the signature, and one this module does not know is an error rather than a
    guess. Folding the two modules into one is a later change; until then this is the seam.
    """
    trace_id = getattr(module, "trace_id", None)
    if trace_id is None:
        raise TranscriptError(f"id module {_module_name(module)}: no trace_id")
    try:
        parameters = inspect.signature(trace_id).parameters
    except (TypeError, ValueError) as exc:
        raise TranscriptError(f"id module {_module_name(module)}: trace_id unreadable: {exc}")
    keyword = next((name for name in RUN_TAG_KEYWORDS if name in parameters), None)
    if keyword is None:
        raise TranscriptError(
            f"id module {_module_name(module)}: trace_id takes none of "
            f"{', '.join(RUN_TAG_KEYWORDS)} as its run-tag keyword"
        )
    for required in ("observation_id", "tool_key", "CONTRACT_VERSION"):
        if not hasattr(module, required):
            raise TranscriptError(f"id module {_module_name(module)}: no {required}")

    def trace(run_tag: str, run_id: str, task_id: str) -> str:
        return str(
            trace_id(
                **{keyword: run_tag},
                run_id=run_id,
                task_id=task_id,
                trial_index=TRIAL_INDEX,
                attempt=NO_ATTEMPT,
            )
        )

    return IdContract(
        trace=_refusing(trace, "trace id"),
        observation=_refusing(module.observation_id, "observation id"),
        tool_key=_refusing(module.tool_key, "tool key"),
        version=int(module.CONTRACT_VERSION),
    )


def _refusing(function: Callable[..., str], what: str) -> Callable[..., str]:
    """An id function whose refusal of a component (a ``|`` in the run id, a call id with
    surrounding whitespace) refuses the transcript, like every other shape it cannot take."""

    def call(*args: Any, **kwargs: Any) -> str:
        try:
            return function(*args, **kwargs)
        except ValueError as exc:
            raise TranscriptError(f"the id contract refuses this {what}: {exc}") from exc

    return call


def _module_name(module: Any) -> str:
    return str(getattr(module, "__name__", module))


# -- reading -----------------------------------------------------------------------------------


def read_claude_output(path: Path, *, transcript_id: str | None = None) -> Transcript:
    """Read one file of ``claude -p`` output. The transcript id defaults to the file's stem."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise TranscriptError(f"{path}: cannot be read: {exc}") from exc
    return read_claude_text(text, transcript_id=transcript_id or Path(path).stem, origin=str(path))


def read_claude_text(text: str, *, transcript_id: str, origin: str = "<text>") -> Transcript:
    """Read ``claude -p`` output already in memory. Refuses an unknown shape (see the module
    docstring): nothing of a refused transcript is projected."""
    if not str(transcript_id).strip():
        raise TranscriptError(f"{origin}: a transcript id is required")
    shape, events = _events(text, origin)
    turns: list[AssistantTurn] = []
    by_message: dict[str, int] = {}
    outcomes: list[ToolOutcome] = []
    result: RunResult | None = None
    cli_version: str | None = None
    session_id: str | None = None
    stamps: list[str] = []
    # the clock of the event right before this one (None when that event has none): where a model
    # call this event ends began; never an earlier clock, which would stretch the call
    before: str | None = None
    for position, event in enumerate(events):
        if not isinstance(event, Mapping):
            raise TranscriptRefused(f"{origin}: event {position} is not a mapping")
        kind = event.get("type")
        if not isinstance(kind, str) or kind not in EVENT_TYPES:
            raise TranscriptRefused(f"{origin}: event {position} has unknown type {_shown(kind)}")
        session_id = session_id or _str(event.get("session_id"))
        stamp = _normalize_ts(event.get("timestamp"))
        if stamp:
            stamps.append(stamp)
        prior, before = before, stamp
        if kind == "system":
            # everything else a system event says describes the machine the agent ran on
            if event.get("subtype") == SYSTEM_INIT:
                cli_version = cli_version or _str(event.get("claude_code_version"))
            continue
        if kind == "assistant":
            part = _assistant_turn(event, len(turns), stamp, origin, position)
            joined = by_message.get(part.message_id) if part.message_id else None
            if joined is None:
                if part.message_id:
                    by_message[part.message_id] = len(turns)
                turns.append(replace(part, started_at=prior))
            else:
                turns[joined] = _joined(turns[joined], part)
            continue
        if kind == "user":
            outcomes.extend(_tool_outcomes(event, position, stamp, origin))
            continue
        result = _result(event)
    started = stamps[0] if stamps else None
    ended = _ended(stamps, result, started, origin)
    return Transcript(
        transcript_id=str(transcript_id),
        shape=shape,
        turns=tuple(turns),
        outcomes=tuple(outcomes),
        result=result,
        cli_version=cli_version,
        session_id=session_id,
        started_at=started,
        ended_at=ended,
        origin=origin,
    )


def _events(text: str, origin: str) -> tuple[str, list[Any]]:
    """The output's shape and its events. One array, one object, or one object per line."""
    stripped = text.strip()
    if not stripped:
        raise TranscriptError(f"{origin}: empty")
    try:
        whole = json.loads(stripped)
    except (ValueError, RecursionError):
        # not one JSON document (a stream, or a value the decoder cannot hold): read it by line
        return SHAPE_STREAM, _lines(stripped, origin)
    if isinstance(whole, list):
        return SHAPE_ARRAY, whole
    if isinstance(whole, Mapping):
        # ``--output-format json`` gives the result event alone; a one-line stream file parses the
        # same way and says so through its own type
        return (SHAPE_RESULT if whole.get("type") == "result" else SHAPE_STREAM), [whole]
    raise TranscriptRefused(f"{origin}: not a JSON array, object or line-delimited stream")


def _lines(text: str, origin: str) -> list[Any]:
    events: list[Any] = []
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except (ValueError, RecursionError) as exc:
            # a JSONDecodeError, an integer past the decoder's digit limit, or nesting past the
            # recursion limit: none of them is a line this reader can take
            raise TranscriptError(
                f"{origin}: line {number} is not JSON it can read: {exc}"
            ) from exc
    if not events:
        raise TranscriptError(f"{origin}: no events")
    return events


def _assistant_turn(
    event: Mapping[str, Any], index: int, stamp: str | None, origin: str, position: int
) -> AssistantTurn:
    message = _message(event, origin, position)
    texts: list[str] = []
    thoughts: list[str] = []
    calls: list[ToolCall] = []
    for block in _blocks(message, origin, position):
        kind = block.get("type")
        if not isinstance(kind, str) or kind not in ASSISTANT_BLOCKS:
            raise TranscriptRefused(
                f"{origin}: event {position} has unknown assistant content block {_shown(kind)}"
            )
        if kind == "text":
            texts.append(_str(block.get("text")) or "")
        elif kind in ("thinking", "redacted_thinking"):
            thoughts.append(_str(block.get("thinking")) or "")
        else:
            call_id = _str(block.get("id"))
            if not call_id:
                raise TranscriptRefused(f"{origin}: event {position} has a tool_use without an id")
            calls.append(
                ToolCall(
                    call_id=call_id,
                    name=_str(block.get("name")) or "unknown",
                    arguments=block.get("input"),
                )
            )
    usage = message.get("usage")
    return AssistantTurn(
        index=index,
        model=_str(message.get("model")),
        text="\n".join(t for t in texts if t),
        reasoning="\n".join(t for t in thoughts if t),
        tool_calls=tuple(calls),
        usage=dict(usage) if isinstance(usage, Mapping) else {},
        timestamp=stamp,
        is_error=bool(event.get("is_api_error_message") or message.get("is_api_error_message")),
        message_id=_str(message.get("id")),
    )


def _joined(turn: AssistantTurn, part: AssistantTurn) -> AssistantTurn:
    """A turn with one more stream event of its message: the blocks in order, the usage once
    (the event's own, which repeats the message's, unless it carries none) and the event's clock."""
    return replace(
        turn,
        model=turn.model or part.model,
        text="\n".join(t for t in (turn.text, part.text) if t),
        reasoning="\n".join(t for t in (turn.reasoning, part.reasoning) if t),
        tool_calls=turn.tool_calls + part.tool_calls,
        usage=part.usage or turn.usage,
        timestamp=part.timestamp or turn.timestamp,
        is_error=turn.is_error or part.is_error,
        events=turn.events + 1,
    )


def _tool_outcomes(
    event: Mapping[str, Any], position: int, stamp: str | None, origin: str
) -> list[ToolOutcome]:
    message = _message(event, origin, position)
    outcomes: list[ToolOutcome] = []
    for block in _blocks(message, origin, position):
        kind = block.get("type")
        if not isinstance(kind, str) or kind not in USER_BLOCKS:
            raise TranscriptRefused(
                f"{origin}: event {position} has unknown user content block {_shown(kind)}"
            )
        if kind != "tool_result":
            continue
        call_id = _str(block.get("tool_use_id"))
        if not call_id:
            raise TranscriptRefused(
                f"{origin}: event {position} has a tool_result without a tool_use_id"
            )
        outcomes.append(
            ToolOutcome(
                position=position,
                call_id=call_id,
                output=block.get("content"),
                is_error=bool(block.get("is_error")),
                timestamp=stamp,
            )
        )
    return outcomes


def _shown(kind: object) -> str:
    """An unknown type as a refusal names it. The refusal reaches a receipt and a job summary
    unscanned, so a string is capped and anything else is named by its kind, never its content."""
    if isinstance(kind, str):
        return repr(kind if len(kind) <= SHOWN_CHARS else kind[:SHOWN_CHARS] + "...")
    return f"<{type(kind).__name__}>"


def _message(event: Mapping[str, Any], origin: str, position: int) -> Mapping[str, Any]:
    message = event.get("message")
    if not isinstance(message, Mapping):
        raise TranscriptRefused(f"{origin}: event {position} has no message mapping")
    return message


def _blocks(message: Mapping[str, Any], origin: str, position: int) -> list[Mapping[str, Any]]:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, Sequence):
        raise TranscriptRefused(f"{origin}: event {position} has no content list")
    blocks: list[Mapping[str, Any]] = []
    for block in content:
        if not isinstance(block, Mapping):
            raise TranscriptRefused(f"{origin}: event {position} has a non-mapping content block")
        blocks.append(block)
    return blocks


def _result(event: Mapping[str, Any]) -> RunResult:
    denials = event.get("permission_denials")
    usage = event.get("usage")
    return RunResult(
        subtype=_str(event.get("subtype")),
        text=_str(event.get("result")),
        total_cost_usd=_float(event.get("total_cost_usd")),
        num_turns=_int(event.get("num_turns")),
        duration_ms=_int(event.get("duration_ms")),
        duration_api_ms=_int(event.get("duration_api_ms")),
        is_error=bool(event.get("is_error")),
        stop_reason=_str(event.get("stop_reason")),
        terminal_reason=_str(event.get("terminal_reason")),
        permission_denials=len(denials) if isinstance(denials, Sequence) else 0,
        usage=dict(usage) if isinstance(usage, Mapping) else {},
    )


def _ended(
    stamps: Sequence[str], result: RunResult | None, started: str | None, origin: str
) -> str | None:
    if len(stamps) > 1:
        return stamps[-1]
    if started and result is not None and result.duration_ms:
        try:
            base = datetime.fromisoformat(started.replace("Z", "+00:00"))
        except ValueError:
            return stamps[-1] if stamps else None
        try:
            end = base.timestamp() + result.duration_ms / 1000.0
            ended = datetime.fromtimestamp(end, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            raise TranscriptRefused(f"{origin}: the result's duration_ms is out of range") from exc
        return ended.isoformat().replace("+00:00", "Z")
    return stamps[-1] if stamps else None


# -- the gate ----------------------------------------------------------------------------------


def redact(
    transcript: Transcript, *, policy: str = TOOL_IO_DROP, max_chars: int = SCRUB_MAX_CHARS
) -> Transcript:
    """Apply the tool input/output policy. A transcript must go through this before it can be
    projected; see the module docstring for why ``drop`` is the default."""
    if policy not in TOOL_IO_POLICIES:
        raise TranscriptError(
            f"tool i/o policy {policy!r} is not one of {', '.join(TOOL_IO_POLICIES)}"
        )
    turns = tuple(
        replace(
            turn,
            tool_calls=tuple(
                replace(call, **_apply(call.arguments, policy, max_chars))
                for call in turn.tool_calls
            ),
        )
        for turn in transcript.turns
    )
    outcomes = tuple(
        replace(outcome, **_apply(outcome.output, policy, max_chars, key="output"))
        for outcome in transcript.outcomes
    )
    return replace(transcript, turns=turns, outcomes=outcomes, tool_io=policy)


def _apply(value: Any, policy: str, max_chars: int, *, key: str = "arguments") -> dict[str, Any]:
    if policy == TOOL_IO_DROP:
        return {key: {"redacted": True, "chars": len(_serialise(value))}, "redaction": policy}
    text, removed = scrub(_serialise(value), max_chars=max_chars)
    return {key: text, "redaction": policy, "removed": removed}


def scrub(text: str, *, max_chars: int = SCRUB_MAX_CHARS) -> tuple[str, tuple[str, ...]]:
    """Truncate, then remove every credential shape the sentinel knows. Returns the text and the
    rules that fired."""
    truncated = text[:max_chars]
    if len(text) > max_chars:
        truncated += f"... [{len(text) - max_chars} more characters]"
    removed: list[str] = []
    for rule, pattern in SCRUB_SHAPES:
        truncated, hits = pattern.subn(f"[redacted:{rule}]", truncated)
        if hits:
            removed.append(rule)
    return truncated, tuple(removed)


def _serialise(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


# -- the projection ----------------------------------------------------------------------------


@dataclass(frozen=True)
class TranscriptOptions:
    """Everything the projection needs that the transcript itself does not say."""

    run_tag: str
    run_id: str
    label: str
    session: str | None = None
    environment: str | None = None
    release: str | None = None
    producer_version: str = NONE
    project: str = NONE
    project_verified: bool = False
    # the caller's share of the tags: team, run_kind, ci_run, ci_chain (already resolved)
    caller_tags: Mapping[str, str] = field(default_factory=dict)
    # free trace metadata the producer knows and this module does not (the stage, the iteration,
    # the subject of the work, the pull request); it may not overwrite a schema key
    metadata: Mapping[str, Any] = field(default_factory=dict)
    resolver: ModelNameResolver = field(default_factory=RawModelNameResolver)
    # the model that served the run, when the CLI was pointed at an alias a gateway routes to
    # another model: the CLI reports the alias, so only the caller can name it. It becomes the
    # generations' model and the model facets; the transcript's own names stay in the trace
    # metadata (``cli_model``: the first it reports; ``model_names``: all of them, then this one
    # when they do not include it). None (or empty): the first name the transcript reports, and
    # nothing is inferred from a second one
    model: str | None = None
    # the prompt the agent was given (``claude -p <prompt>``), which its output does not repeat.
    # It becomes the trace's input, and so the root observation's: cut at INPUT_MAX_CHARS with a
    # visible marker and scrubbed of every shape the sentinel knows, whatever the tool i/o
    # policy, while the trace metadata keeps its full length and what the cap and the scrub did.
    # None: the trace has no input
    input: str | None = None
    # the trace's name, a template over ``{label}``, ``{transcript}`` and ``{step}``; None:
    # DEFAULT_TRACE_NAME
    name: str | None = None
    # the trace's user, as given (an expert's id, say); None: no user
    user: str | None = None
    # or a model the trace's user is: a reference the resolver reads (under a deployment's rules an
    # arena config stem reads too), its identity the user, so the receiver's views by user are
    # views by model; a reference the resolver cannot read is the user as given. Never with ``user``
    user_model: str | None = None


def build_events(
    transcript: Transcript, options: TranscriptOptions, *, ids: IdContract
) -> BuiltTranscript:
    """Project one gated transcript into its ingestion events."""
    if transcript.tool_io is None:
        raise TranscriptError(
            f"{transcript.origin}: the tool i/o policy has not been applied; call redact() first"
        )
    caller = _caller_tags(options.caller_tags)
    trace_id = ids.trace(options.run_tag, options.run_id, transcript.transcript_id)
    root_id = ids.root(trace_id)
    served = _tag_value("model", options.model) if options.model else None
    identity, unresolved = _identity(transcript, options.resolver, served)
    start = transcript.started_at
    end = transcript.ended_at or start
    result = transcript.result
    tool_names = transcript.tool_names

    prompt, prompt_facts = _input(options.input)
    metadata = _trace_metadata(transcript, options, caller, identity, ids, served)
    metadata["model_unresolved"] = _text(unresolved)
    metadata.update(prompt_facts)
    # a reserved key is the projection's even on a trace that does not carry it
    clashes = sorted(set(options.metadata) & (set(metadata) | RESERVED_KEYS))
    if clashes:
        raise TranscriptError(
            f"{transcript.origin}: metadata may not override schema keys: " + ", ".join(clashes)
        )
    metadata.update(options.metadata)

    tags = order_tags(
        [
            f"project:{_tag_value('project', options.project)}",
            f"harness:{HARNESS}",
            f"source:{SOURCE}",
            *(identity.tags if identity else ()),
            *(f"{prefix}:{value}" for prefix, value in caller.items()),
        ]
    )

    trace_body: dict[str, Any] = {
        "id": trace_id,
        "name": trace_name(options.name, transcript.transcript_id, label=options.label),
        "timestamp": start,
        "sessionId": options.session,
        "userId": _user(options),
        "input": prompt,
        "output": result.text if result else None,
        "tags": tags,
        "metadata": metadata,
        "environment": options.environment,
        "release": options.release,
        "version": options.producer_version,
    }
    events = [_envelope("trace-create", trace_body)]
    events.append(
        _envelope(
            EVENT_AGENT,
            {
                "id": root_id,
                "traceId": trace_id,
                "name": NAME_TRANSCRIPT,
                "startTime": start,
                "endTime": end,
                "level": "ERROR" if result is not None and result.is_error else "DEFAULT",
                "statusMessage": _status_message(result),
                "metadata": {
                    "kind": "root",
                    "transcript_id": transcript.transcript_id,
                    "harness": HARNESS,
                    "turn_count": len(transcript.turns),
                    "tool_call_count": len(transcript.outcomes),
                    "stop_reason": _text(result.stop_reason if result else None),
                },
            },
        )
    )

    context: list[dict[str, Any]] = []
    costs = turn_costs(transcript)
    for turn in transcript.turns:
        started = turn.started_at or turn.timestamp or start
        body: dict[str, Any] = {
            "id": ids.observation(trace_id, "gen", turn.index),
            "traceId": trace_id,
            "parentObservationId": root_id,
            "name": NAME_AGENT,
            "startTime": started,
            "endTime": turn.timestamp or started,
            "input": context[-CONTEXT_MESSAGES:],
            "output": {
                "content": turn.text,
                "tool_calls": [
                    {"id": call.call_id, "name": call.name, "arguments": call.arguments}
                    for call in turn.tool_calls
                ],
            },
            "level": "ERROR" if turn.is_error else "DEFAULT",
            "metadata": {
                "role": "agent",
                "message_index": turn.index,
                "reasoning": turn.reasoning or NONE,
                "model_raw": _text(turn.model),
                "tool_io": transcript.tool_io,
                "message_id": _text(turn.message_id),
                "stream_events": turn.events,
                "cost_basis": COST_BASIS_CLI if costs is not None else NONE,
            },
            # the cost is stated either way: a generation without one is priced by the receiver
            # from its own model table
            "costDetails": {"total": costs[turn.index] if costs is not None else 0},
        }
        if identity is not None:
            body["model"] = identity.canonical
        usage = _usage_details(turn.usage)
        if usage:
            body["usageDetails"] = usage
        events.append(_envelope("generation-create", body))
        context.append({"role": "assistant", "content": turn.text[:CONTEXT_CHARS]})

    called_at = {
        call.call_id: turn.timestamp for turn in transcript.turns for call in turn.tool_calls
    }
    for outcome in transcript.outcomes:
        name = tool_names.get(outcome.call_id)
        call = _call_of(transcript, outcome.call_id)
        # from the turn that called the tool to its result
        started = called_at.get(outcome.call_id) or outcome.timestamp or start
        events.append(
            _envelope(
                EVENT_TOOL,
                {
                    "id": ids.observation(
                        trace_id, "tool", ids.tool_key(outcome.call_id, outcome.position)
                    ),
                    "traceId": trace_id,
                    "parentObservationId": root_id,
                    "name": f"tool: {name or 'unknown'}",
                    "startTime": started,
                    "endTime": outcome.timestamp or started,
                    "input": call.arguments if call is not None else None,
                    "output": outcome.output,
                    "level": "ERROR" if outcome.is_error else "DEFAULT",
                    "metadata": {
                        "role": "agent_tool",
                        "kind": "tool",
                        "call_id": outcome.call_id,
                        "tool_name": _text(name),
                        "tool_io": outcome.redaction,
                        "redacted_rules": ",".join(outcome.removed) or NONE,
                        "key_source": "call_id",
                    },
                },
            )
        )

    # every observation body carries the trace's environment (a body without it is filed under
    # the receiver's default whatever the trace says)
    for event in events:
        if event["type"] != "trace-create" and options.environment:
            event["body"].setdefault("environment", options.environment)
    return BuiltTranscript(trace_id=trace_id, events=events)


def step_of(transcript_id: str) -> str:
    """What the agent worked at: the transcript id without the ordinal of a later run
    (``analysis/four_bucket/2`` -> ``analysis/four_bucket``, ``resolve/3`` -> ``resolve``)."""
    return _ORDINAL_SEGMENT.sub("", transcript_id) or transcript_id


def check_trace_name(template: str) -> str:
    """A transcript trace-name template: text with the placeholders ``NAME_PLACEHOLDERS`` and no
    other brace; anything else refuses the transcript rather than name it with a literal ``{``."""
    unknown = sorted(set(_NAME_PLACEHOLDER.findall(template)) - NAME_PLACEHOLDERS)
    rest = _NAME_PLACEHOLDER.sub("", template)
    if not template.strip() or unknown or "{" in rest or "}" in rest:
        raise TranscriptError(
            f"trace-name template {template!r}: the placeholders are "
            f"{', '.join('{' + p + '}' for p in sorted(NAME_PLACEHOLDERS))}, and no other brace"
        )
    return template


def trace_name(template: str | None, transcript_id: str, *, label: str) -> str:
    """A transcript trace's name: ``template`` (default ``{label}/{transcript}``) over the run's
    label, the transcript id and its step (``{step}`` groups the runs of one kind of work)."""
    values = {"label": label, "transcript": transcript_id, "step": step_of(transcript_id)}
    return _NAME_PLACEHOLDER.sub(
        lambda match: values[match.group(1)],
        check_trace_name(DEFAULT_TRACE_NAME if template is None else template),
    )


def _user(options: TranscriptOptions) -> str | None:
    """The trace's user: ``user`` as given, or the identity of ``user_model`` (the reference as
    given when the resolver cannot read it)."""
    given = (options.user or "").strip()
    reference = (options.user_model or "").strip()
    if given and reference:
        raise TranscriptError("a transcript's user is either given or a model, not both")
    if not reference:
        return given or None
    try:
        return options.resolver.resolve(None, reference).canonical
    except ModelNameResolverError:
        return reference


def turn_costs(transcript: Transcript) -> list[float] | None:
    """Each turn's share of what the CLI reported the run cost, by turn index; ``None`` when it
    reported no cost. The CLI states the run's total, never a turn's, so the total is shared out
    by the turns' tokens at Claude's relative list prices and the turns add up to it exactly;
    turns without any token share it evenly."""
    result = transcript.result
    if result is None or result.total_cost_usd is None:
        return None
    turns = transcript.turns
    weights = [_weight(turn.usage) for turn in turns]
    whole = sum(weights)
    if whole <= 0:
        return [result.total_cost_usd / len(turns) for _ in turns]
    return [result.total_cost_usd * weight / whole for weight in weights]


def _weight(usage: Mapping[str, Any]) -> float:
    """A turn's tokens at Claude's list prices relative to the model's input price."""
    creation = usage.get("cache_creation")
    split = isinstance(creation, Mapping) and any(
        creation.get(key) is not None
        for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
    )
    if split:
        assert isinstance(creation, Mapping)
        writes = WEIGHT_CACHE_WRITE_5M * (
            _int(creation.get("ephemeral_5m_input_tokens")) or 0
        ) + WEIGHT_CACHE_WRITE_1H * (_int(creation.get("ephemeral_1h_input_tokens")) or 0)
    else:
        writes = WEIGHT_CACHE_WRITE_5M * (_int(usage.get("cache_creation_input_tokens")) or 0)
    return (
        (_int(usage.get("input_tokens")) or 0)
        + WEIGHT_OUTPUT * (_int(usage.get("output_tokens")) or 0)
        + WEIGHT_CACHE_READ * (_int(usage.get("cache_read_input_tokens")) or 0)
        + writes
    )


def _trace_metadata(
    transcript: Transcript,
    options: TranscriptOptions,
    caller: Mapping[str, str],
    identity: ModelIdentity | None,
    ids: IdContract,
    served: str | None,
) -> dict[str, Any]:
    result = transcript.result
    metadata: dict[str, Any] = {
        "transcript_id": transcript.transcript_id,
        "task_id": transcript.transcript_id,  # the id-contract position a transcript occupies
        "trial_index": TRIAL_INDEX,
        "attempt": NO_ATTEMPT,
        "run_id": options.run_id,
        "run_tag": options.run_tag,
        "label": options.label,
        "source": SOURCE,
        "harness": HARNESS,
        "project": options.project,
        "project_verified": options.project_verified,
        "team": _text(caller.get("team")),
        "run_kind": _text(caller.get("run_kind")),
        "ci_run": _text(caller.get("ci_run")),
        "ci_chain": _text(caller.get("ci_chain")),
        "producer_version": options.producer_version,
        "id_contract": ids.version,
        "model_name": _text(identity.canonical if identity else None),
        "model_names": _text(" ".join(_model_names(transcript, served))),
        "model_rules": _text(getattr(options.resolver, "rules_version", None)),
        "cli_version": _text(transcript.cli_version),
        "cli_session_id": _text(transcript.session_id),
        "transcript_shape": transcript.shape,
        "tool_io": transcript.tool_io,
        "turn_count": len(transcript.turns),
        "tool_call_count": len(transcript.outcomes),
        "trace_time_source": "event_timestamp" if transcript.started_at else "upload_time",
        "result_subtype": _text(result.subtype if result else None),
        "total_cost_usd": result.total_cost_usd if result else None,
        "num_turns": result.num_turns if result else None,
        "duration_ms": result.duration_ms if result else None,
        "duration_api_ms": result.duration_api_ms if result else None,
        "stop_reason": _text(result.stop_reason if result else None),
        "terminal_reason": _text(result.terminal_reason if result else None),
        "permission_denials": result.permission_denials if result else 0,
        "is_error": bool(result.is_error) if result else False,
    }
    if served:
        metadata[CLI_MODEL_KEY] = _text(_cli_model(transcript))
    return metadata


def _input(prompt: str | None) -> tuple[str | None, dict[str, Any]]:
    """The caller's prompt as the trace's input, and the trace metadata that says what became of
    it. Cut with the scrub's own marker, then scrubbed: the prompt is built from templates and
    run facts rather than tool output, but it is text leaving the runner all the same, and a
    shape the scrub leaves in is one the sentinel refuses the whole transcript over."""
    if prompt is None:
        return None, {}
    kept, removed = scrub(prompt, max_chars=INPUT_MAX_CHARS)
    return kept, {
        INPUT_CHARS_KEY: len(prompt),
        INPUT_TRUNCATED_KEY: len(prompt) > INPUT_MAX_CHARS,
        INPUT_REDACTED_RULES_KEY: ",".join(removed) or NONE,
    }


def _caller_tags(tags: Mapping[str, str]) -> dict[str, str]:
    """The caller's tags, checked against the transcript subset of the vocabulary: a transcript
    carries no dataset, scope, domain, config or task, because those describe a trial."""
    allowed = set(TRANSCRIPT_CALLER_PREFIXES)
    unknown = sorted(set(tags) - allowed)
    if unknown:
        raise TranscriptError(
            f"tags {', '.join(unknown)} do not describe a transcript; allowed: "
            + ", ".join(TRANSCRIPT_CALLER_PREFIXES)
        )
    missing = [prefix for prefix in ("team", "run_kind") if not tags.get(prefix)]
    if missing:
        raise TranscriptError(f"a transcript needs {', '.join(missing)}")
    return {prefix: _tag_value(prefix, tags[prefix]) for prefix in tags if tags[prefix]}


def _tag_value(prefix: str, value: str) -> str:
    try:
        return check_value(prefix, str(value))
    except VocabularyError as exc:
        raise TranscriptError(str(exc)) from exc


def _identity(
    transcript: Transcript, resolver: ModelNameResolver, served: str | None
) -> tuple[ModelIdentity | None, str | None]:
    """The agent's model: the one the caller says served the run, else the first the transcript
    names; and the name, when the resolver's rules cannot read it (a bare CLI alias such as
    ``claude-opus-4-8`` names no vendor). Such a name stands as it is spelled rather than refuse
    the transcript."""
    name = served or next(iter(_models(transcript)), None)
    if name is None:
        return None, None
    try:
        return resolver.resolve(None, name), None
    except ModelNameResolverError:
        return RawModelNameResolver().resolve(None, name), name


def _models(transcript: Transcript) -> list[str]:
    seen: list[str] = []
    for turn in transcript.turns:
        if turn.model and turn.model not in seen:
            seen.append(turn.model)
    return seen


def _model_names(transcript: Transcript, served: str | None) -> list[str]:
    """Every name the run goes by: the transcript's own, then the served model it does not name."""
    names = _models(transcript)
    if served and served not in names:
        names.append(served)
    return names


def _cli_model(transcript: Transcript) -> str | None:
    """The first name the transcript reports: the one its trace would carry without the served
    model, whatever the spelling the caller gives that model."""
    return next(iter(_models(transcript)), None)


def _call_of(transcript: Transcript, call_id: str) -> ToolCall | None:
    for turn in transcript.turns:
        for call in turn.tool_calls:
            if call.call_id == call_id:
                return call
    return None


def _usage_details(usage: Mapping[str, Any]) -> dict[str, int]:
    """The CLI's Anthropic-shaped usage as Langfuse usage details: a non-overlapping breakdown
    with an explicit total, under the key names the trial projection already writes."""
    if not usage:
        return {}
    input_tokens = _int(usage.get("input_tokens")) or 0
    output_tokens = _int(usage.get("output_tokens")) or 0
    cache_read = _int(usage.get("cache_read_input_tokens"))
    cache_creation = _int(usage.get("cache_creation_input_tokens"))
    details = {
        "input": input_tokens,
        "output": output_tokens,
        "total": input_tokens + output_tokens + (cache_read or 0) + (cache_creation or 0),
    }
    if cache_read is not None:
        details["cache_read_input_tokens"] = cache_read
    if cache_creation is not None:
        details["cache_creation_input_tokens"] = cache_creation
    return details


def _status_message(result: RunResult | None) -> str:
    if result is None:
        return ""
    if result.is_error:
        return _text(result.subtype or result.stop_reason)
    return ""


def _envelope(event_type: str, body: dict[str, Any]) -> dict[str, Any]:
    """One ingestion event; the envelope id is per send (an update needs a new one)."""
    return {
        "id": uuid.uuid4().hex,
        "type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "body": body,
    }


# -- small shared helpers (the trial projection's, kept engine-free here) -----------------------


def _text(value: Any) -> str:
    if value is None or value == "":
        return NONE
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _normalize_ts(value: object) -> str | None:
    """A clock as the RFC 3339 text the ingestion API accepts, ``Z`` appended when it carries no
    zone (the trial projection's rule, so both producers stamp alike)."""
    if not value:
        return None
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return (
            datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    text = str(value)
    return text if text.endswith("Z") or _ZONE_SUFFIX.search(text) else text + "Z"


def transcript_files(root: Path) -> list[Path]:
    """Agent output files under ``<root>``: ``*.jsonl`` and ``*.json``, in name order."""
    directory = Path(root)
    if directory.is_file():
        return [directory]
    return sorted(
        path
        for pattern in ("*.jsonl", "*.json")
        for path in directory.glob(pattern)
        if path.is_file()
    )


__all__ = [
    "ASSISTANT_BLOCKS",
    "CLI_MODEL_KEY",
    "CONTEXT_CHARS",
    "CONTEXT_MESSAGES",
    "EVENT_TYPES",
    "HARNESS",
    "INPUT_CHARS_KEY",
    "INPUT_MAX_CHARS",
    "INPUT_REDACTED_RULES_KEY",
    "INPUT_TRUNCATED_KEY",
    "NO_ATTEMPT",
    "RESERVED_KEYS",
    "SCRUB_MAX_CHARS",
    "SCRUB_SHAPES",
    "SHAPE_ARRAY",
    "SHAPE_RESULT",
    "SHAPE_STREAM",
    "SOURCE",
    "TOOL_IO_DROP",
    "TOOL_IO_POLICIES",
    "TOOL_IO_SCRUB",
    "TRIAL_INDEX",
    "USER_BLOCKS",
    "AssistantTurn",
    "BuiltTranscript",
    "IdContract",
    "RunResult",
    "ToolCall",
    "ToolOutcome",
    "Transcript",
    "TranscriptError",
    "TranscriptOptions",
    "TranscriptRefused",
    "build_events",
    "id_contract",
    "read_claude_output",
    "read_claude_text",
    "redact",
    "scrub",
    "step_of",
    "trace_name",
    "transcript_files",
    "turn_costs",
]
