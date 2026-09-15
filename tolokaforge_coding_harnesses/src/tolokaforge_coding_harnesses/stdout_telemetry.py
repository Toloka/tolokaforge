"""Telemetry a coding-harness CLI reports about itself on stdout.

A harness trial is one tool call: the CLI owns its planning loop inside the
container, so the engine issues no LLM request and has no turn, token or cost
accounting of its own to record. Some CLIs print their own totals, and the
trial bundle already captures that stream verbatim — this module turns it into
a neutral record the caller can fold into whatever metrics it keeps.

Availability is a property of what each CLI actually prints, not of the
harness surface. ``claude-code`` terminates its ``stream-json`` output with a
``result`` event carrying turns, cost and token usage. The other shipped
harnesses print prose (``codex``, ``kimi-code``) or an event stream whose
terminal event has no usage block (``grok-build``, ``opencode``), so they
declare no dialect here — :func:`parse_harness_stdout` returns ``None`` for
them and the caller keeps whatever accounting it already had. "No dialect"
therefore reads as "not measured", which a zero-filled record could not say.

Lives beside the registry because a stdout dialect is a property of the CLI,
in the same way its argv and version pin are.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "STDOUT_TELEMETRY_DIALECTS",
    "HarnessStdoutTelemetry",
    "parse_harness_stdout",
]

CLAUDE_CODE_STREAM_JSON = "claude-code/stream-json"
"""Dialect of ``claude-code`` under ``--output-format=stream-json``: one JSON
object per line, terminating in ``{"type": "result", …}``."""

STDOUT_TELEMETRY_DIALECTS: dict[str, str] = {"claude-code": CLAUDE_CODE_STREAM_JSON}
"""Harness name → the stdout dialect it prints. A harness absent from this
mapping prints nothing a parser can read; see the module docstring."""


@dataclass(frozen=True)
class HarnessStdoutTelemetry:
    """What a harness CLI reported about its own run.

    Counts are the CLI's own accounting, not the engine's: ``turns`` is the
    CLI's internal turn count, and ``cost_usd`` is what the CLI billed itself,
    which is why :attr:`dialect` travels with the numbers — a consumer can
    then distinguish a CLI-reported figure from an engine-measured one.

    ``cost_usd`` and ``duration_s`` are ``None`` when the result event omitted
    them. Token counts default to zero, because a result event that carries a
    usage block always carries the token fields.
    """

    dialect: str
    turns: int
    cost_usd: float | None
    duration_s: float | None
    prompt_tokens: int
    completion_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int


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
    # The mapping and the branches above are edited together; a dialect with
    # no branch is a programming error, not a runtime condition.
    raise AssertionError(f"no parser for stdout dialect {dialect!r}")


def _parse_claude_code_stream_json(stdout: str) -> HarnessStdoutTelemetry | None:
    result = _last_result_event(stdout)
    if result is None:
        return None
    usage = result.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    duration_ms = _as_float(result.get("duration_ms"))
    return HarnessStdoutTelemetry(
        dialect=CLAUDE_CODE_STREAM_JSON,
        turns=_as_int(result.get("num_turns")),
        cost_usd=_as_float(result.get("total_cost_usd")),
        duration_s=duration_ms / 1000.0 if duration_ms is not None else None,
        prompt_tokens=_as_int(usage.get("input_tokens")),
        completion_tokens=_as_int(usage.get("output_tokens")),
        cache_read_input_tokens=_as_int(usage.get("cache_read_input_tokens")),
        cache_creation_input_tokens=_as_int(usage.get("cache_creation_input_tokens")),
    )


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
