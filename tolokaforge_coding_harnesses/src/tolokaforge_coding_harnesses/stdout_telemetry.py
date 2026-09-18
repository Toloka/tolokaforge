"""Telemetry a coding-harness CLI reports about itself on stdout.

A harness trial is one tool call: the CLI owns its planning loop inside the
container, so the engine issues no LLM request and has no turn, token or cost
accounting of its own to record. Some CLIs print their own totals, and the
trial bundle already captures that stream verbatim — this module turns it into
a neutral record the caller can fold into whatever metrics it keeps.

Availability is a property of what each CLI actually prints, not of the harness
surface, and the three shipped dialects agree on nothing but being JSON lines:

* ``claude-code`` terminates its ``stream-json`` output with a single
  ``result`` event carrying turns, cost and a usage block.
* ``codex`` under ``--json`` emits one ``turn.completed`` **per turn**, each
  with that turn's usage, so totals are a sum across them. It reports no cost.
* ``kimi-code`` prints an OpenAI-shaped message transcript and no usage at
  all — turns are countable from it, tokens are not.

So what a CLI reports is a *subset*, and the record below distinguishes "not
reported" (``None``) from "reported as zero". A caller that needs the tokens
``kimi-code`` withholds has to measure them at the wire instead.

``grok-build`` declares no dialect: its stream is ``text`` events closing on
an ``end`` event that carries only a stop reason, so there is nothing to read.
:func:`parse_harness_stdout` returns ``None`` for it and the caller keeps
whatever accounting it already had.

Lives beside the registry because a stdout dialect is a property of the CLI,
in the same way its argv and version pin are.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "OPENCODE_JSON",
    "STDOUT_TELEMETRY_DIALECTS",
    "HarnessStdoutTelemetry",
    "parse_harness_stdout",
]

CLAUDE_CODE_STREAM_JSON = "claude-code/stream-json"
"""Dialect of ``claude-code`` under ``--output-format=stream-json``: one JSON
object per line, terminating in ``{"type": "result", …}`` which carries turns,
cost and a usage block."""

CODEX_JSON = "codex/json"
"""Dialect of ``codex exec --json``: one JSON object per line, with one
``{"type": "turn.completed", "usage": {…}}`` per turn. Usage is per-turn, so a
reader sums across them. Reports tokens but no cost."""

OPENCODE_JSON = "opencode/json"
"""Dialect of ``opencode run --format=json``: one JSON object per line, with a
``step_finish`` per step carrying that step's ``tokens`` and ``cost``. Usage is
per-step, so a reader sums across them. Reports both tokens and cost."""

KIMI_CODE_STREAM_JSON = "kimi-code/stream-json"
"""Dialect of ``kimi-code --output-format stream-json``: an OpenAI-shaped
message transcript (``{"role": "assistant", …}`` / ``{"role": "tool", …}``)
closing with a ``session.resume_hint`` meta line. Carries turns only — no
token counts and no cost anywhere in the stream."""

STDOUT_TELEMETRY_DIALECTS: dict[str, str] = {
    "claude-code": CLAUDE_CODE_STREAM_JSON,
    "codex": CODEX_JSON,
    "kimi-code": KIMI_CODE_STREAM_JSON,
    "opencode": OPENCODE_JSON,
}
"""Harness name → the stdout dialect it prints. A harness absent from this
mapping prints nothing a parser can read; see the module docstring."""


@dataclass(frozen=True)
class HarnessStdoutTelemetry:
    """What a harness CLI reported about its own run.

    Counts are the CLI's own accounting, not the engine's: ``turns`` is the
    CLI's internal turn count and means different things per CLI (``codex``
    reports one turn where ``claude-code`` reports twenty for comparable work),
    and ``cost_usd`` is what the CLI billed itself. :attr:`dialect` travels
    with the numbers so a consumer can tell a CLI-reported figure from an
    engine-measured one, and tell the CLIs apart.

    **``None`` means "not reported", and zero means "reported as zero".** The
    distinction is load-bearing: ``kimi-code`` prints turns and no usage at
    all, so its token fields are ``None`` — a zero there would read as a trial
    that somehow spent nothing. ``codex`` reports tokens but no cost, so its
    ``cost_usd`` is ``None`` and the caller prices the tokens instead.

    **``prompt_tokens`` is the prompt total: cache reads and cache writes
    included.** The CLIs do not agree on that basis — ``codex`` prints an
    ``input_tokens`` its ``cached_input_tokens`` is a subset of, while
    ``claude-code`` prints the *non-cached* remainder beside its two cache
    counters — so each parser converts to the inclusive total and this field
    means one thing across dialects. Anything else pushes a per-CLI
    subtract-or-not rule onto every consumer, and a consumer that prices
    tokens by deriving fresh input as ``prompt - cache_read - cache_write``
    gets a negative number on one of the two.

    ``completion_tokens`` is likewise the output total, ``reasoning_tokens``
    included where a CLI breaks that out — it is a subset, not an addend.
    """

    dialect: str
    turns: int
    cost_usd: float | None
    duration_s: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None
    reasoning_tokens: int | None = None

    @property
    def has_token_counts(self) -> bool:
        """Whether the CLI reported any token accounting at all."""
        return self.prompt_tokens is not None or self.completion_tokens is not None


def parse_harness_stdout(harness: str, stdout: str) -> HarnessStdoutTelemetry | None:
    """Telemetry *harness* reported in *stdout*, or ``None`` when there is none.

    ``None`` covers three distinct cases the caller treats alike — the harness
    declares no dialect, the stream is empty, or the stream never reached its
    terminal totals event (a CLI killed at its deadline, or output truncated).
    All three mean "the CLI did not report its own accounting", which is why
    none of them is an error.
    """
    dialect = STDOUT_TELEMETRY_DIALECTS.get(harness)
    if dialect is None or not stdout.strip():
        return None
    if dialect == CLAUDE_CODE_STREAM_JSON:
        return _parse_claude_code_stream_json(stdout)
    if dialect == CODEX_JSON:
        return _parse_codex_json(stdout)
    if dialect == KIMI_CODE_STREAM_JSON:
        return _parse_kimi_code_stream_json(stdout)
    if dialect == OPENCODE_JSON:
        return _parse_opencode_json(stdout)
    # The mapping and the branches above are edited together; a dialect with
    # no branch is a programming error, not a runtime condition.
    raise AssertionError(f"no parser for stdout dialect {dialect!r}")


def _parse_claude_code_stream_json(stdout: str) -> HarnessStdoutTelemetry | None:
    """Read the terminal ``result`` event of ``claude-code``'s stream.

    Its usage block is Anthropic-shaped, so ``input_tokens`` counts only the
    prompt tokens that missed the cache: a 19-turn session reports 129 input
    tokens beside 705k cache reads. :attr:`HarnessStdoutTelemetry.prompt_tokens`
    is the inclusive total, so the three are summed here — and the sum is what
    reproduces the CLI's own ``total_cost_usd`` when priced.
    """
    result = _last_result_event(stdout)
    if result is None:
        return None
    usage = result.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    duration_ms = _as_float(result.get("duration_ms"))
    cache_read = _as_int(usage.get("cache_read_input_tokens"))
    cache_creation = _as_int(usage.get("cache_creation_input_tokens"))
    return HarnessStdoutTelemetry(
        dialect=CLAUDE_CODE_STREAM_JSON,
        turns=_as_int(result.get("num_turns")),
        cost_usd=_as_float(result.get("total_cost_usd")),
        duration_s=duration_ms / 1000.0 if duration_ms is not None else None,
        prompt_tokens=_as_int(usage.get("input_tokens")) + cache_read + cache_creation,
        completion_tokens=_as_int(usage.get("output_tokens")),
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
    )


def _parse_codex_json(stdout: str) -> HarnessStdoutTelemetry | None:
    """Sum the per-turn usage ``codex exec --json`` reports.

    The stream runs ``thread.started`` → ``turn.started`` → ``item.*`` →
    ``turn.completed``, and each ``turn.completed`` carries **that turn's**
    usage — so totals are a sum across them, not the last one. Codex reports
    no cost, so :attr:`HarnessStdoutTelemetry.cost_usd` stays ``None`` and the
    caller prices the tokens.

    Its usage block is OpenAI-shaped: ``input_tokens`` already includes
    ``cached_input_tokens``, and ``output_tokens`` already includes
    ``reasoning_output_tokens``. Both match the inclusive basis the record
    declares, so the counts are summed across turns and otherwise passed
    through — unlike ``claude-code``, which reports its non-cached remainder.
    """
    turns = 0
    prompt = completion = cache_read = cache_write = reasoning = 0
    for event in _json_lines(stdout):
        if event.get("type") != "turn.completed":
            continue
        turns += 1
        usage = event.get("usage")
        if not isinstance(usage, Mapping):
            continue
        prompt += _as_int(usage.get("input_tokens"))
        completion += _as_int(usage.get("output_tokens"))
        cache_read += _as_int(usage.get("cached_input_tokens"))
        cache_write += _as_int(usage.get("cache_write_input_tokens"))
        reasoning += _as_int(usage.get("reasoning_output_tokens"))
    if turns == 0:
        return None
    return HarnessStdoutTelemetry(
        dialect=CODEX_JSON,
        turns=turns,
        cost_usd=None,
        duration_s=None,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
        reasoning_tokens=reasoning,
    )


def _parse_opencode_json(stdout: str) -> HarnessStdoutTelemetry | None:
    """Sum the per-step usage ``opencode run --format=json`` reports.

    The stream runs ``step_start`` → ``tool_use`` → ``step_finish`` per step,
    and each ``step_finish`` carries **that step's** tokens and cost under
    ``part`` — so totals are a sum across them, not the last one.

    Whether ``tokens.input`` already includes the cached prompt depends on the
    **provider** opencode routed to, not on opencode: the shipped Anthropic
    block reports the non-cached remainder (a recorded step: ``input=1``,
    ``cache.write=387``, ``cache.read=15623``, ``output=304``, ``total=16315``
    — their sum), while an OpenAI-shaped provider reports an inclusive
    ``input`` with the cached part as a subset of it. Reading either as the
    other doubles or halves a cache-heavy trial's prompt.

    So the basis is not assumed, it is read off the step: ``total`` says which
    arithmetic the provider used, and the cache counters are folded in only
    when the exclusive reading is the one that reconciles. A step whose
    ``total`` reconciles with neither, or reports none, is folded — the shipped
    default is Anthropic-shaped and that is the safer error, since it
    understates a prompt rather than billing a cached one twice.

    ``reasoning`` is already inside ``output``, matching what the record
    declares and what the caller's pricing expects.

    Cost is summed only across the steps that reported one, and stays ``None``
    when no step did — a ``0.0`` here would claim a trial that ran spent
    nothing, which is the reading this dialect exists to remove.
    """
    turns = 0
    prompt = completion = cache_read = cache_write = reasoning = 0
    cost: float | None = None
    for event in _json_lines(stdout):
        if event.get("type") != "step_finish":
            continue
        part = event.get("part")
        if not isinstance(part, Mapping):
            continue
        turns += 1
        step_cost = _as_float(part.get("cost"))
        if step_cost is not None:
            cost = step_cost if cost is None else cost + step_cost
        tokens = part.get("tokens")
        if not isinstance(tokens, Mapping):
            continue
        cache = tokens.get("cache")
        read = _as_int(cache.get("read")) if isinstance(cache, Mapping) else 0
        write = _as_int(cache.get("write")) if isinstance(cache, Mapping) else 0
        step_input = _as_int(tokens.get("input"))
        step_output = _as_int(tokens.get("output"))
        total = _as_int(tokens.get("total"))
        inclusive = total > 0 and step_input + step_output == total and (read or write)
        prompt += step_input if inclusive else step_input + read + write
        completion += step_output
        reasoning += _as_int(tokens.get("reasoning"))
        cache_read += read
        cache_write += write
    if turns == 0:
        return None
    return HarnessStdoutTelemetry(
        dialect=OPENCODE_JSON,
        turns=turns,
        cost_usd=cost,
        duration_s=None,
        prompt_tokens=prompt,
        completion_tokens=completion,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
        reasoning_tokens=reasoning,
    )


def _parse_kimi_code_stream_json(stdout: str) -> HarnessStdoutTelemetry | None:
    """Count the turns in ``kimi-code``'s transcript. It reports no usage.

    The stream is an OpenAI-shaped message transcript — assistant turns with
    ``tool_calls``, tool results keyed by ``tool_call_id`` — closing with a
    ``{"role": "meta", "type": "session.resume_hint", …}`` line. No token
    counts and no cost appear anywhere in it, so every token field stays
    ``None``: this CLI's usage has to come from the wire instead.
    """
    turns = sum(1 for event in _json_lines(stdout) if event.get("role") == "assistant")
    if turns == 0:
        return None
    return HarnessStdoutTelemetry(
        dialect=KIMI_CODE_STREAM_JSON,
        turns=turns,
        cost_usd=None,
        duration_s=None,
        prompt_tokens=None,
        completion_tokens=None,
        cache_read_input_tokens=None,
        cache_creation_input_tokens=None,
    )


def _json_lines(stdout: str) -> list[Mapping[str, Any]]:
    """Every line of *stdout* that parses as a JSON object, in order.

    Non-JSON lines are skipped rather than failing the parse: the runner
    appends ``[exit code: N]`` / ``[timed out after Ns; …]`` sentinels and
    interleaved stderr on a non-success exit, and the totals a CLI printed
    before that are still valid.
    """
    events: list[Mapping[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, Mapping):
            events.append(event)
    return events


def _last_result_event(stdout: str) -> Mapping[str, Any] | None:
    """The last ``{"type": "result"}`` object in *stdout*, whatever shape it came in.

    ``claude-code`` prints one object under ``--output-format json``, an array
    of every message when ``--verbose`` is added, and one object per line under
    ``stream-json``. All three end in the same result event; the shipped argv
    uses ``stream-json``, and the other two cost nothing to accept.
    """
    try:
        parsed = json.loads(stdout)
    except ValueError:
        parsed = None
    if isinstance(parsed, Mapping):
        return parsed if parsed.get("type") == "result" else None
    if isinstance(parsed, list):
        events = [item for item in parsed if _is_result_event(item)]
        return events[-1] if events else None

    last: Mapping[str, Any] | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            # A truncated or interleaved line is not a parse failure of the
            # stream: the totals event is the last one, so keep scanning.
            continue
        if _is_result_event(event):
            last = event
    return last


def _is_result_event(event: object) -> bool:
    return isinstance(event, Mapping) and event.get("type") == "result"


def _as_int(value: object) -> int:
    """*value* as a count, or ``0`` when the CLI omitted or malformed it."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


def _as_float(value: object) -> float | None:
    """*value* as a measurement, or ``None`` when the CLI omitted or malformed it."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except ValueError:
        return None
