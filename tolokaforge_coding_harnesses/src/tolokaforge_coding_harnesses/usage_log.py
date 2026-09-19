"""Token usage the middleware proxy measured on the wire.

:mod:`~tolokaforge_coding_harnesses.stdout_telemetry` recovers what a coding
harness CLI *prints* about itself. ``kimi-code`` prints nothing — no token
counts and no cost appear anywhere in its stream — so for that CLI the only
place the numbers exist is the provider traffic itself. It is also the only
shipped harness declaring :attr:`HarnessSpec.request_middleware`, so every one
of its requests passes through
:mod:`~tolokaforge_coding_harnesses.middleware_proxy`, which appends one NDJSON
record per provider response to
:data:`~tolokaforge_coding_harnesses.MIDDLEWARE_USAGE_LOG_CONTAINER_PATH`.

This module sums those records into one per-trial total. It takes the records
as text rather than as a path because they are only ever reachable inside the
container: the shipped runtime bind-mounts the container's log directory from
the per-trial compose context, which is a temporary copy teardown deletes, so
the file has no host path a consumer could open. A consumer reads it out of
the live container and hands the bytes here.

The records are OpenAI Chat Completions ``usage`` blocks, so ``prompt_tokens``
already includes the cached prompt the record carries separately as
``cache_read_input_tokens``, and ``completion_tokens`` already includes
``reasoning_tokens`` — the same inclusive basis
:class:`~tolokaforge_coding_harnesses.HarnessStdoutTelemetry` declares, so a
consumer prices either source the same way.

**Every record counts, whatever its status.** A provider that answered 429 or
500 and still returned a usage block still billed for the attempt, and the
proxy writes a record only when a usage block was actually present. That also
makes this tap the *broader* measurement of the two: it sees retries a CLI's
own summary may quietly exclude.

Lives beside the registry because the record format is the shipped proxy's own
output, in the same way the stdout dialects are the shipped CLIs' output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

__all__ = [
    "HarnessRequestOutcomes",
    "summarise_harness_requests",
    "MIDDLEWARE_PROXY_USAGE_SOURCE",
    "HarnessWireUsage",
    "sum_harness_usage_records",
]

MIDDLEWARE_PROXY_USAGE_SOURCE = "middleware_proxy"
"""Name of this tap, for a consumer stamping where a trial's tokens came from.

A single value today because one shipped harness routes through one proxy. It
is a name rather than a boolean so a second tap — a different middleware, or a
gateway-side accounting feed — is a new value here instead of a second flag
nobody's reader knows to check.
"""

_COUNT_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_read_input_tokens",
    "reasoning_tokens",
)


@dataclass(frozen=True)
class HarnessWireUsage:
    """Per-trial totals summed from the proxy's per-request records.

    Counts are inclusive on the same basis the proxy's source blocks use:
    :attr:`prompt_tokens` includes :attr:`cache_read_input_tokens`, and
    :attr:`completion_tokens` includes :attr:`reasoning_tokens`. Both are
    subsets, not addends.

    There is no cache-*write* counter: an OpenAI-shaped ``usage`` block has no
    field for one, so the proxy records none and a consumer must not infer one.

    Zero is a real answer here, unlike in :class:`HarnessStdoutTelemetry` —
    :func:`sum_harness_usage_records` returns ``None`` rather than a zeroed
    record when there was nothing to read, so every instance of this class
    stands for at least one request whose usage the provider reported.
    """

    requests: int
    """Records summed. At least ``1``."""

    prompt_tokens: int
    completion_tokens: int
    cache_read_input_tokens: int
    reasoning_tokens: int

    skipped_lines: int
    """Lines that were not a JSON object carrying at least one count.

    Non-zero means this total is a lower bound. Worth reporting — a proxy
    killed mid-append writes a partial last line — but never worth failing a
    trial over: the records that did parse are still what the provider billed.
    """


@dataclass(frozen=True)
class HarnessRequestOutcomes:
    """How the provider answered a trial's requests, regardless of usage.

    Separate from :class:`HarnessWireUsage` because the questions are
    different: that one asks what the trial spent, this one asks whether the
    trial was served at all. A CLI whose every request was refused still
    writes a transcript, still exits, and still leaves a repository the
    grader will happily score — so "nothing succeeded" has to be legible
    before the score is.
    """

    requests: int
    """Records carrying a status. At least ``1`` when this is not ``None``."""

    failed: int
    """Records whose status was outside the 2xx range."""

    statuses: tuple[int, ...]
    """The distinct non-2xx statuses seen, ascending — what to put in a message."""

    @property
    def none_succeeded(self) -> bool:
        """Whether every request the proxy saw was refused."""
        return self.requests > 0 and self.failed == self.requests


def summarise_harness_requests(records: str) -> HarnessRequestOutcomes | None:
    """Outcomes of the requests *records* describes, or ``None`` when none do.

    ``None`` means the proxy recorded no request at all, which is the ordinary
    absence — no middleware, or a CLI that never called a provider. It is not
    evidence of failure, and a caller must not read it as one.
    """
    requests = 0
    failed = 0
    statuses: set[int] = set()
    for line in records.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        status = record.get("status")
        if not isinstance(status, int):
            continue
        if not _is_completion_path(record.get("path")):
            # The proxy taps every path, and several harnesses allowlist a
            # model-list GET on their credential gateway. Counting one of
            # those as a served request would mask a trial whose every
            # completion was refused — the case this exists to catch.
            continue
        requests += 1
        if not 200 <= status < 300:
            failed += 1
            statuses.add(status)
    if requests == 0:
        return None
    return HarnessRequestOutcomes(
        requests=requests, failed=failed, statuses=tuple(sorted(statuses))
    )


_COMPLETION_PATH_MARKERS = (
    "/chat/completions",
    "/completions",
    "/messages",
    "/responses",
    ":generatecontent",
    ":streamgeneratecontent",
)
"""Path fragments identifying a request that asks a model to do work.

A trial is served when its *completions* are served. Everything else a CLI
sends through the proxy — a model list, a health probe, a token count — can
succeed against a provider that refuses every actual request.
"""


def _is_completion_path(path: object) -> bool:
    """Whether *path* is a request asking a model to do work."""
    if not isinstance(path, str):
        # A record without a path predates the field or came from a shape this
        # does not recognise; counting it keeps the old behaviour rather than
        # silently shrinking the evidence.
        return True
    lowered = path.lower()
    return any(marker in lowered for marker in _COMPLETION_PATH_MARKERS)


def _is_record(line: str) -> bool:
    """Whether *line* is a JSON object the proxy could have written."""
    stripped = line.strip()
    if not stripped.startswith("{"):
        return False
    try:
        return isinstance(json.loads(stripped), dict)
    except json.JSONDecodeError:
        return False


def sum_harness_usage_records(records: str) -> HarnessWireUsage | None:
    """Sum the NDJSON usage *records*, or ``None`` when they carry none.

    ``None`` covers the ordinary absences, which are not errors: the harness
    booted no proxy, the CLI made no provider call, the read came back empty,
    or every line was unreadable. All of them mean "the wire measured
    nothing", and a zeroed total would instead claim the trial spent nothing.

    A malformed line is skipped and counted in
    :attr:`HarnessWireUsage.skipped_lines`; the rest of the records still
    count.
    """
    totals = dict.fromkeys(_COUNT_FIELDS, 0)
    requests = 0
    skipped = 0
    for line in records.splitlines():
        counts = _record_counts(line)
        if counts is None:
            # A record the proxy wrote for a response that reported no usage —
            # a refusal, or a provider that simply omitted the block. It is not
            # a damaged line, and counting it as one would raise a warning
            # about corruption on every failed request.
            if line.strip() and not _is_record(line):
                skipped += 1
            continue
        requests += 1
        for field, value in counts.items():
            totals[field] += value

    if requests == 0:
        return None
    return HarnessWireUsage(requests=requests, skipped_lines=skipped, **totals)


def _record_counts(line: str) -> dict[str, int] | None:
    """The counts one NDJSON *line* carries, or ``None`` when it carries none.

    ``None`` for a line that is not a JSON object, and for one that is but
    reports no count in any field — the proxy writes a record only when the
    response reported usage, so such a line is a shape this reader does not
    recognise rather than a request that spent nothing.

    A field the proxy wrote as ``null`` (the provider reported that counter but
    not this one) contributes ``0`` to the sum, which is the arithmetic
    identity and not a claim about what was reported.
    """
    stripped = line.strip()
    if not stripped.startswith("{"):
        return None
    try:
        record = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    counts = {field: _as_count(record.get(field)) for field in _COUNT_FIELDS}
    if all(count is None for count in counts.values()):
        return None
    return {field: count or 0 for field, count in counts.items()}


def _as_count(value: object) -> int | None:
    """*value* when it is a plain integer token count, else ``None``.

    ``bool`` is an ``int`` subclass and is never a token count, so it is
    rejected rather than counted as 0 / 1 — the same rule the proxy applies
    when it writes the record.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
