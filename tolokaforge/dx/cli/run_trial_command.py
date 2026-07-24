"""``tolokaforge run-trial`` — run one trial as a subprocess over a JSON-Lines pipe.

A wire adapter over :func:`tolokaforge.core.run_trial.run_trial`: read one
``start`` envelope on stdin, run the trial, write exactly one ``result`` /
``error`` envelope on stdout. All trial composition, backend selection, and
provisioning stay inside ``run_trial``; this module owns framing, the
error → wire mapping, and signal / EOF termination only.

stdout carries the wire (one JSON object per line, flushed); diagnostics and
tracebacks go to stderr so the wire stays pure.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import traceback
from dataclasses import dataclass
from types import FrameType
from typing import Any, TextIO

import click
from pydantic import ValidationError

from tolokaforge.core.models import TaskConfig
from tolokaforge.core.plugin_registry import UnknownImplementationError
from tolokaforge.core.run_trial import run_trial as run_trial_library
from tolokaforge.core.runtime import ProvisionError
from tolokaforge.core.trial import TrialResult

WIRE_VERSION = 1


class ProtocolError(Exception):
    """A stdin envelope violates the wire framing (bad ``v``, unknown ``type``,
    unknown top-level key, or malformed JSON)."""


class _Cancelled(Exception):
    """External cancellation — SIGTERM / SIGINT, premature stdin EOF, or a
    synchronous ``cancel`` envelope — surfaced as ``error_type:"cancelled"``."""


_ALLOWED_START_KEYS = frozenset({"v", "type", "task", "models", "runtime", "grader", "conductor"})
_ALLOWED_CANCEL_KEYS = frozenset({"v", "type"})


@dataclass(frozen=True)
class StartMessage:
    """A parsed ``start`` envelope: the arguments one ``run_trial`` call needs."""

    task: dict[str, Any] | None
    models: dict[str, Any] | None
    runtime: str
    grader: str
    conductor: str


@dataclass(frozen=True)
class CancelMessage:
    """Sentinel for a well-formed synchronous ``cancel`` envelope."""


def parse_envelope(line: str | bytes) -> StartMessage | CancelMessage:
    """Parse one stdin line into a :class:`StartMessage` or :class:`CancelMessage`.

    Raises :class:`ProtocolError` on malformed JSON, a ``v`` other than
    :data:`WIRE_VERSION`, or a ``type`` other than ``start`` / ``cancel``.
    """
    if isinstance(line, bytes):
        line = line.decode("utf-8", errors="strict")
    try:
        envelope = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProtocolError(f"malformed JSON envelope: {exc}") from exc

    if not isinstance(envelope, dict):
        raise ProtocolError(f"envelope must be a JSON object, got {type(envelope).__name__}")

    version = envelope.get("v")
    if version != WIRE_VERSION:
        raise ProtocolError(
            f"unsupported protocol version {version!r}; this build speaks v={WIRE_VERSION}"
        )

    message_type = envelope.get("type")
    if message_type == "cancel":
        unknown = set(envelope) - _ALLOWED_CANCEL_KEYS
        if unknown:
            raise ProtocolError(f"unknown envelope key(s): {sorted(unknown)}")
        return CancelMessage()
    if message_type != "start":
        raise ProtocolError(f"unknown message type {message_type!r}; expected 'start' or 'cancel'")

    unknown = set(envelope) - _ALLOWED_START_KEYS
    if unknown:
        raise ProtocolError(f"unknown envelope key(s): {sorted(unknown)}")

    return StartMessage(
        task=envelope.get("task"),
        models=envelope.get("models"),
        runtime=envelope.get("runtime", "auto"),
        grader=envelope.get("grader", "runner_rpc"),
        conductor=envelope.get("conductor", "in_process"),
    )


def marshal_result(result: TrialResult) -> dict[str, Any]:
    """Serialise a successful trial into the ``result`` wire envelope."""
    return {"v": WIRE_VERSION, "type": "result", "result": result.model_dump(mode="json")}


def marshal_error(exc: Exception) -> dict[str, Any]:
    """Map any exception to a typed ``error`` wire envelope (``fatal`` always
    true — a single-trial invocation has no non-terminal errors)."""
    return {
        "v": WIRE_VERSION,
        "type": "error",
        "error_type": _classify_error(exc),
        "message": str(exc),
        "fatal": True,
    }


def _classify_error(exc: Exception) -> str:
    """Name the wire ``error_type`` for ``exc`` per the ADR mapping table."""
    if isinstance(exc, _Cancelled):
        return "cancelled"
    if isinstance(exc, ProtocolError):
        return "ProtocolError"
    if isinstance(exc, ValidationError):
        return "ValidationError"
    if isinstance(exc, UnknownImplementationError):
        return "UnknownImplementationError"
    if isinstance(exc, ProvisionError):
        return "ProvisionError"
    return "InternalError"


def _emit(out: TextIO, message: dict[str, Any]) -> None:
    """Write one wire envelope to ``out`` and flush."""
    out.write(json.dumps(message) + "\n")
    out.flush()


def _read_first_envelope(stream: TextIO) -> StartMessage | CancelMessage:
    """Return the first non-empty stdin line parsed; EOF first → ``_Cancelled``."""
    for raw in stream:
        line = raw.strip()
        if line:
            return parse_envelope(line)
    raise _Cancelled("stdin closed before a start message")


def _run_from_start(message: StartMessage) -> TrialResult:
    """Reconstruct the task and dispatch to the library ``run_trial`` with the wire seams."""
    task = TaskConfig.model_validate(message.task)
    return run_trial_library(
        task=task,
        models=message.models,
        runtime=message.runtime,
        grader=message.grader,
        conductor=message.conductor,
    )


def _raise_cancelled(signum: int, _frame: FrameType | None) -> None:
    raise _Cancelled(f"received signal {signum}")


def _install_signal_handlers() -> None:
    """Install SIGTERM / SIGINT → ``_Cancelled`` where the platform supports it."""
    for name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, name, None)
        if signum is not None:
            signal.signal(signum, _raise_cancelled)


def _run_trial(in_stream: TextIO, out_stream: TextIO) -> int:
    """Drive one trial from ``in_stream``, writing the wire to ``out_stream``.

    The whole ``run_trial`` call sits inside the ``try`` so a signal-raised
    ``_Cancelled`` unwinds through ``run_trial``'s ``finally`` teardown before
    the ``cancelled`` error is emitted. The broad ``except`` is the ADR wire
    contract — every failure emits a typed ``error`` on the wire, writes its
    traceback to stderr, and exits non-zero; nothing is swallowed.
    """
    try:
        message = _read_first_envelope(in_stream)
        if isinstance(message, CancelMessage):
            raise _Cancelled("cancel requested before a start message")
        result = _run_from_start(message)
    except Exception as exc:
        _emit(out_stream, marshal_error(exc))
        traceback.print_exc(file=sys.stderr)
        return 1
    _emit(out_stream, marshal_result(result))
    return 0


def _reroute_stdout_to_stderr() -> TextIO:
    """Point the process stdout descriptor at stderr; return the pure wire stream.

    ``StructuredLogger`` binds a ``StreamHandler(sys.stdout)`` (and C-level code
    may write to fd 1 directly), so a trial's diagnostics would otherwise
    pollute the JSON-Lines wire. Duplicate the real stdout to a private
    descriptor for wire writes, then ``dup2`` stderr over fd 1 so every stray
    stdout write — Python or C-level, already-bound handler or not — lands on
    stderr instead. ``sys.stdout`` is repointed at stderr for Python-level
    writes made after this call.
    """
    sys.stdout.flush()
    wire_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(wire_fd, "w", encoding="utf-8")


@click.command(name="run-trial")
def run_trial() -> None:
    """Run one trial as a subprocess over a JSON-Lines pipe.

    Reads exactly one request envelope on stdin, runs a single trial, and writes
    exactly one terminal envelope on stdout. Every message is UTF-8 JSON, one
    object per line (JSON Lines), and carries "v":1 — the wire-protocol version,
    independent of the tolokaforge package version.

    \b
    stdin (one line, then EOF):
      {"v":1,"type":"start","task":{...},"models":{"agent":{...}},
       "runtime":"auto","grader":"runner_rpc","conductor":"in_process"}
      {"v":1,"type":"cancel"}

    "task" and "models" mirror the tolokaforge.runner.run_trial arguments; "runtime",
    "grader", and "conductor" are registered implementation names and default to
    "auto" / "runner_rpc" / "in_process" when omitted.

    \b
    stdout (exactly one line):
      {"v":1,"type":"result","result":{...TrialResult...}}
      {"v":1,"type":"error","error_type":"...","message":"...","fatal":true}

    \b
    error_type / exit code:
      (success, result emitted) ................................. exit 0
      ProtocolError  (malformed JSON / bad v / unknown type) .... exit 1
      cancelled      (cancel envelope / EOF / SIGTERM / SIGINT) .. exit 1
      ValidationError (invalid task or models) ................. exit 1
      UnknownImplementationError (unknown runtime/grader/conductor) exit 1
      ProvisionError (substrate failed to provision) ........... exit 1
      InternalError  (any other failure) ....................... exit 1

    The full traceback for any error is written to stderr; stdout carries only
    the wire. On POSIX, SIGTERM and SIGINT trigger clean teardown then a
    "cancelled" error.

    File assets on the task (grading.yaml, initial_state.json, tools) resolve
    against the subprocess working directory: spawn with cwd at the task-pack
    root, or send a fully-inline task that references no on-disk files.

    See docs/adr/0022-runtime-independence.md § Surface 3 for the full contract.
    """
    _install_signal_handlers()
    wire_out = _reroute_stdout_to_stderr()
    raise SystemExit(_run_trial(sys.stdin, wire_out))
