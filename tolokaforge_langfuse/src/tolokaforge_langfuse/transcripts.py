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
    The transcript as ingestion bodies: the trace, its root span, one generation per assistant
    turn, one span per tool result. The same bodies the trial projection builds, so
    :mod:`tolokaforge_langfuse.otlp_spans` turns them into v4 spans unchanged.

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
from tolokaforge_langfuse.model_names import (
    ModelIdentity,
    ModelNameResolver,
    ModelNameResolverError,
    RawModelNameResolver,
)
from tolokaforge_langfuse.vocabulary import (
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

TOOL_IO_DROP = "drop"
TOOL_IO_SCRUB = "scrub"
TOOL_IO_POLICIES = (TOOL_IO_DROP, TOOL_IO_SCRUB)
SCRUB_MAX_CHARS = 2000

_ZONE_SUFFIX = re.compile(r"[+-]\d{2}:?\d{2}$")


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
    """One turn the agent took: what it said, what it was thinking, what it called."""

    index: int
    model: str | None
    text: str
    reasoning: str
    tool_calls: tuple[ToolCall, ...]
    usage: Mapping[str, Any]
    timestamp: str | None = None
    is_error: bool = False


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
        trace=trace,
        observation=module.observation_id,
        tool_key=module.tool_key,
        version=int(module.CONTRACT_VERSION),
    )


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
    outcomes: list[ToolOutcome] = []
    result: RunResult | None = None
    cli_version: str | None = None
    session_id: str | None = None
    stamps: list[str] = []
    for position, event in enumerate(events):
        if not isinstance(event, Mapping):
            raise TranscriptRefused(f"{origin}: event {position} is not a mapping")
        kind = event.get("type")
        if kind not in EVENT_TYPES:
            raise TranscriptRefused(f"{origin}: event {position} has unknown type {kind!r}")
        session_id = session_id or _str(event.get("session_id"))
        stamp = _normalize_ts(event.get("timestamp"))
        if stamp:
            stamps.append(stamp)
        if kind == "system":
            # everything else a system event says describes the machine the agent ran on
            if event.get("subtype") == SYSTEM_INIT:
                cli_version = cli_version or _str(event.get("claude_code_version"))
            continue
        if kind == "assistant":
            turns.append(_assistant_turn(event, len(turns), stamp, origin, position))
            continue
        if kind == "user":
            outcomes.extend(_tool_outcomes(event, position, stamp, origin))
            continue
        result = _result(event)
    started = stamps[0] if stamps else None
    ended = _ended(stamps, result, started)
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
    except json.JSONDecodeError:
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
        except json.JSONDecodeError as exc:
            raise TranscriptError(f"{origin}: line {number} is not JSON: {exc}") from exc
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
        if kind not in ASSISTANT_BLOCKS:
            raise TranscriptRefused(
                f"{origin}: event {position} has unknown assistant content block {kind!r}"
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
    )


def _tool_outcomes(
    event: Mapping[str, Any], position: int, stamp: str | None, origin: str
) -> list[ToolOutcome]:
    message = _message(event, origin, position)
    outcomes: list[ToolOutcome] = []
    for block in _blocks(message, origin, position):
        kind = block.get("type")
        if kind not in USER_BLOCKS:
            raise TranscriptRefused(
                f"{origin}: event {position} has unknown user content block {kind!r}"
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


def _ended(stamps: Sequence[str], result: RunResult | None, started: str | None) -> str | None:
    if len(stamps) > 1:
        return stamps[-1]
    if started and result is not None and result.duration_ms:
        try:
            base = datetime.fromisoformat(started.replace("Z", "+00:00"))
        except ValueError:
            return stamps[-1] if stamps else None
        end = base.timestamp() + result.duration_ms / 1000.0
        return datetime.fromtimestamp(end, tz=timezone.utc).isoformat().replace("+00:00", "Z")
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
    identity = _identity(transcript, options.resolver)
    start = transcript.started_at
    end = transcript.ended_at or start
    result = transcript.result
    tool_names = transcript.tool_names

    metadata = _trace_metadata(transcript, options, caller, identity, ids)
    clashes = sorted(set(options.metadata) & set(metadata))
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
        "name": f"{options.label}/{transcript.transcript_id}",
        "timestamp": start,
        "sessionId": options.session,
        "input": None,
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
            "span-create",
            {
                "id": root_id,
                "traceId": trace_id,
                "name": f"transcript {transcript.transcript_id}",
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
    for turn in transcript.turns:
        body: dict[str, Any] = {
            "id": ids.observation(trace_id, "gen", turn.index),
            "traceId": trace_id,
            "parentObservationId": root_id,
            "name": f"assistant turn {turn.index}",
            "startTime": turn.timestamp or start,
            "endTime": turn.timestamp or end,
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
            },
        }
        if identity is not None:
            body["model"] = identity.canonical
        usage = _usage_details(turn.usage)
        if usage:
            body["usageDetails"] = usage
        events.append(_envelope("generation-create", body))
        context.append({"role": "assistant", "content": turn.text[:CONTEXT_CHARS]})

    for outcome in transcript.outcomes:
        name = tool_names.get(outcome.call_id)
        call = _call_of(transcript, outcome.call_id)
        events.append(
            _envelope(
                "span-create",
                {
                    "id": ids.observation(
                        trace_id, "tool", ids.tool_key(outcome.call_id, outcome.position)
                    ),
                    "traceId": trace_id,
                    "parentObservationId": root_id,
                    "name": f"tool: {name or 'unknown'}",
                    "startTime": outcome.timestamp or start,
                    "endTime": outcome.timestamp or end,
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


def _trace_metadata(
    transcript: Transcript,
    options: TranscriptOptions,
    caller: Mapping[str, str],
    identity: ModelIdentity | None,
    ids: IdContract,
) -> dict[str, Any]:
    result = transcript.result
    return {
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
        "model_names": _text(" ".join(_models(transcript))),
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


def _identity(transcript: Transcript, resolver: ModelNameResolver) -> ModelIdentity | None:
    """The agent's own model, from the first turn that names one."""
    names = _models(transcript)
    if not names:
        return None
    try:
        return resolver.resolve(None, names[0])
    except ModelNameResolverError as exc:
        raise TranscriptError(f"{transcript.origin}: {exc}") from exc


def _models(transcript: Transcript) -> list[str]:
    seen: list[str] = []
    for turn in transcript.turns:
        if turn.model and turn.model not in seen:
            seen.append(turn.model)
    return seen


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
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
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
    "CONTEXT_CHARS",
    "CONTEXT_MESSAGES",
    "EVENT_TYPES",
    "HARNESS",
    "NO_ATTEMPT",
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
    "transcript_files",
]
