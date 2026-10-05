"""Inner turn/token counts recovered from a harness's own native logs.

A harness trial is one tool call: the CLI owns its planning loop inside the
container, so the engine issues no LLM request and measures no turns or tokens
of its own. Two taps already recover what the CLI did — the totals it prints on
stdout (:mod:`.stdout_telemetry`) and the per-response usage a request
middleware writes (:mod:`.usage_log`). This module is the third and
lowest-precedence tap: the agent-session logs the harness leaves under its own
artifact tree, which a run preserves only when its output format keeps native
artifacts.

Those logs are the harness's own, so there is no single cross-harness schema to
parse. The convention read here is the one the shipped harnesses already write
and the package already documents for a transcript stream (see
:mod:`.stdout_telemetry` on ``kimi-code``): newline-delimited JSON under an
``agent/`` subtree, one object per record, where an assistant turn carries
``{"role": "assistant", ...}`` (or a ``type`` naming a turn) and may carry a
``usage`` block. Turns are counted from those records; tokens are summed from
the ``usage`` blocks when present. Anything a log does not carry stays ``None``,
and an unreadable or absent log recovers nothing — a telemetry read may never
fail a trial, so this module raises on nothing and returns ``None`` instead.

Lives beside the registry, like the sibling telemetry modules, because what a
harness writes to its own log tree is a property of the harness, not of the
engine that folds the numbers.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "NATIVE_LOG_USAGE_SOURCE",
    "HarnessNativeLogCounts",
    "parse_native_logs",
]

NATIVE_LOG_USAGE_SOURCE = "native_log"
"""Value stamped on ``Metrics.harness_usage_source`` when a trial's tokens were
recovered from the harness's native logs rather than its stdout or the wire.

A name rather than a boolean, matching ``middleware_proxy`` (the wire tap), so a
reader of the three-state attribution can tell which tap measured the tokens and
a fourth tap is a new value here rather than a new flag."""

_AGENT_DIR_SEGMENT = "agent"
"""The subtree a harness writes its agent-session logs into (``logs/agent/``).

Only records under a path with this segment are read, so a verifier's
``reward.txt`` and other non-session artifacts contribute no turn or token
count. A harness that writes its sessions elsewhere overrides the adapter hook
rather than widening this."""


@dataclass(frozen=True)
class HarnessNativeLogCounts:
    """Inner counts recovered from a harness's native agent-session logs.

    Plain-``int`` fields, never the engine's :class:`Usage`: this package ships
    to runtimes that do not install the engine (the boundary invariant in
    ``tests/unit/test_package_boundary.py``), so the engine folds these into its
    own types rather than this module producing them.

    ``None`` means "the logs did not carry it", never "carried as zero" — the
    same load-bearing distinction :class:`~.stdout_telemetry.HarnessStdoutTelemetry`
    draws, so a log that records turns but no tokens leaves every token field
    ``None`` and the caller does not price a trial at a spurious ``$0``.

    ``prompt_tokens`` is the inclusive prompt total — cache reads and writes
    included — because that is the basis the engine's pricing expects. A record
    carrying the Anthropic-shaped ``input_tokens`` (the non-cached remainder) is
    folded with its cache counters to reach that total; a record carrying an
    OpenAI-shaped ``prompt_tokens`` is already inclusive and passes through.
    """

    turns: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None

    @property
    def has_token_counts(self) -> bool:
        """Whether the logs carried any token accounting at all."""
        return self.prompt_tokens is not None or self.completion_tokens is not None

    @property
    def has_any(self) -> bool:
        """Whether anything at all was recovered — a turn count or any token."""
        return self.turns is not None or self.has_token_counts


def parse_native_logs(native_files: Mapping[str, bytes]) -> HarnessNativeLogCounts | None:
    """Inner counts recovered from *native_files*, or ``None`` when none are.

    *native_files* is the ``relative path -> bytes`` mapping the engine staged
    out of the trial container — the harness's own artifact subtree. Only files
    under an ``agent/`` directory are read, as newline-delimited JSON; every
    other path (a verifier's ``reward.txt``, a binary artifact, a log in another
    shape) contributes nothing and is skipped rather than failing the parse.

    ``None`` whenever nothing could be recovered: no agent logs, none readable as
    UTF-8 JSONL, or none carrying a turn record. The function raises on nothing —
    a malformed line, a corrupt file, a non-mapping ``usage`` block are each
    skipped — because folding native logs may never cost a trial its result.
    """
    turns = 0
    found_turn = False
    found_usage = False
    prompt = completion = reasoning = cache_read = cache_creation = 0

    for key, data in native_files.items():
        if _AGENT_DIR_SEGMENT not in _path_segments(key):
            continue
        for record in _json_object_lines(data):
            if not _is_agent_turn(record):
                continue
            found_turn = True
            turns += 1
            usage = record.get("usage")
            if not isinstance(usage, Mapping):
                continue
            found_usage = True
            prompt += _prompt_total(usage)
            completion += _as_int(usage.get("output_tokens")) + _as_int(
                usage.get("completion_tokens")
            )
            reasoning += _as_int(usage.get("reasoning_tokens")) + _as_int(
                usage.get("reasoning_output_tokens")
            )
            cache_read += _cache_read(usage)
            cache_creation += _cache_creation(usage)

    if not found_turn:
        return None
    return HarnessNativeLogCounts(
        turns=turns,
        prompt_tokens=prompt if found_usage else None,
        completion_tokens=completion if found_usage else None,
        reasoning_tokens=reasoning if found_usage else None,
        cache_read_input_tokens=cache_read if found_usage else None,
        cache_creation_input_tokens=cache_creation if found_usage else None,
    )


def _path_segments(key: str) -> list[str]:
    """The path's components, leading slashes stripped (``logs/agent/x`` → …)."""
    return key.strip("/").split("/")


def _json_object_lines(data: bytes) -> list[Mapping[str, Any]]:
    """Every line of *data* that decodes and parses as a JSON object, in order.

    A file that is not UTF-8, or whose lines are not JSON objects, yields none —
    the record below treats "nothing readable" and "no agent log" alike.
    """
    try:
        text = data.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        return []
    objects: list[Mapping[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, Mapping):
            objects.append(parsed)
    return objects


def _is_agent_turn(record: Mapping[str, Any]) -> bool:
    """Whether *record* is one assistant/agent turn in the transcript.

    Matches the two shapes the shipped harnesses write: an OpenAI-style message
    (``role == "assistant"``) and a typed turn event (``type`` naming a turn).
    """
    if record.get("role") == "assistant":
        return True
    return record.get("type") in {"assistant", "turn", "turn.completed"}


def _prompt_total(usage: Mapping[str, Any]) -> int:
    """The inclusive prompt total for one ``usage`` block.

    Anthropic-shaped records report ``input_tokens`` as the non-cached remainder,
    so the cache counters are folded in to reach the inclusive total the engine's
    pricing expects. An OpenAI-shaped record carries an already-inclusive
    ``prompt_tokens`` and passes through untouched.
    """
    if "input_tokens" in usage:
        return _as_int(usage.get("input_tokens")) + _cache_read(usage) + _cache_creation(usage)
    return _as_int(usage.get("prompt_tokens"))


def _cache_read(usage: Mapping[str, Any]) -> int:
    """Cache-read tokens under any of the names the shipped shapes use."""
    for name in ("cache_read_input_tokens", "cached_input_tokens", "cached_tokens"):
        if name in usage:
            return _as_int(usage.get(name))
    return 0


def _cache_creation(usage: Mapping[str, Any]) -> int:
    """Cache-creation tokens under any of the names the shipped shapes use."""
    for name in ("cache_creation_input_tokens", "cache_write_input_tokens"):
        if name in usage:
            return _as_int(usage.get(name))
    return 0


def _as_int(value: object) -> int:
    """*value* as a non-negative ``int``, or ``0`` for anything non-numeric.

    A missing, null, or malformed count reads as nothing counted rather than
    failing the parse — a log is read for what it unambiguously carries.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, float):
        return int(value) if value > 0 else 0
    return 0
