"""``tolokaforge run-trial`` — run one trial, over a JSON-Lines pipe or one-shot from a file.

Two modes, one command:

* **Pipe** (no ``--inputs``): a wire adapter over
  :func:`tolokaforge.core.run_trial.run_trial`. Read one ``start`` envelope on
  stdin, run the trial, write exactly one ``result`` / ``error`` envelope on
  stdout. A harness in any language drives it over a subprocess pipe. All trial
  composition, backend selection, and provisioning stay inside ``run_trial``;
  this module owns framing, the error → wire mapping, and signal / EOF
  termination only. stdout carries the wire (one JSON object per line, flushed);
  diagnostics and tracebacks go to stderr so the wire stays pure.

* **One-shot** (``--inputs <file>``): read a serialized
  :class:`~tolokaforge.core.trial.TrialNodeInputs`, run one trial through the
  standalone entry :func:`~tolokaforge.core.trial_node.run_trial_from_inputs`,
  write the bundle and a ``trial_result.json`` under ``--output-dir``, and exit
  with a code a sandbox-per-trial backend can act on: ``0`` when a result was
  produced (a graded failure included), :data:`ONESHOT_EXIT_DETERMINISTIC` for a
  failure a retry cannot change, and :data:`ONESHOT_EXIT_TRANSIENT` for one a
  retry may clear.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any, TextIO

import click
from pydantic import ValidationError

from tolokaforge.core.models import TaskConfig
from tolokaforge.core.plugin_registry import UnknownImplementationError
from tolokaforge.core.run_trial import run_trial as run_trial_library
from tolokaforge.core.runtime import ProvisionError
from tolokaforge.core.trial import TrialResult
from tolokaforge.trial_node.protocol import NodeErrorType

if TYPE_CHECKING:
    from tolokaforge.core.trial import TrialNodeInputs

WIRE_VERSION = 1

ONESHOT_EXIT_DETERMINISTIC = 2
"""One-shot (``--inputs``) exit code for a failure a retry cannot change: a
provisioning fault, a drifted task pack, a non-node-runnable adapter, or invalid
config. A backend reads it as "do not retry"."""

ONESHOT_EXIT_TRANSIENT = 3
"""One-shot (``--inputs``) exit code for a failure a retry may clear: an
unexpected internal fault inside the trial. A backend reads it as "retry"."""

_ONESHOT_DETERMINISTIC_ERROR_TYPES = frozenset(
    {
        NodeErrorType.PROVISION_ERROR,
        NodeErrorType.TASK_PACK_DRIFT,
        NodeErrorType.NOT_IMPLEMENTED,
        NodeErrorType.INVALID_CONFIG,
    }
)

TRIAL_RESULT_FILENAME = "trial_result.json"
"""The one-shot mode's serialized :class:`TrialResult`, written at ``--output-dir``."""


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


def _load_oneshot_inputs(
    inputs_path: Path, task_pack_root: Path | None, output_dir: Path | None
) -> TrialNodeInputs:
    """Deserialize ``TrialNodeInputs`` from *inputs_path* with the local placement applied.

    Mirrors the trial node's deserialize-then-place step: validate under the
    ``task_pack_root`` context so each manifest ``compose_file`` is rebased onto
    this host's pack mount, then point the inputs' filesystem placement at the
    flags. ``--output-dir`` overrides ``output_dir``; ``--task-pack-root`` points
    ``task_pack_dir`` at ``<root>/<task_id>`` when the inputs carry a pack to
    mount (and so a hash to verify it against).
    """
    from tolokaforge.core.trial import TrialNodeInputs, pack_dir_under_root

    context = {"task_pack_root": str(task_pack_root)} if task_pack_root is not None else None
    inputs = TrialNodeInputs.model_validate_json(
        inputs_path.read_text(encoding="utf-8"), context=context
    )
    placement: dict[str, Any] = {}
    if output_dir is not None:
        placement["output_dir"] = output_dir
    if task_pack_root is not None and inputs.task_pack_dir is not None:
        placement["task_pack_dir"] = pack_dir_under_root(task_pack_root, inputs.task.task_id)
    if placement:
        inputs = inputs.model_copy(update=placement)
    return inputs


def _write_trial_result(result: TrialResult, output_dir: Path | None) -> None:
    """Write the serialized ``TrialResult`` at *output_dir*; a no-op without one."""
    if output_dir is None:
        return
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / TRIAL_RESULT_FILENAME).write_text(
        result.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )


def _oneshot_failure_exit_code(exc: Exception) -> int:
    """Split a one-shot trial failure into the deterministic / transient exit code.

    Classifies through the same :func:`classify_trial_exception` the node and the
    local thread dispatch read, so the exit code a backend acts on agrees with
    how a trial failure is treated everywhere else.
    """
    from tolokaforge.trial_node.classify import classify_trial_exception

    error_type, _stage, _reason = classify_trial_exception(exc)
    if error_type in _ONESHOT_DETERMINISTIC_ERROR_TYPES:
        return ONESHOT_EXIT_DETERMINISTIC
    return ONESHOT_EXIT_TRANSIENT


def _run_oneshot(inputs_path: Path, task_pack_root: Path | None, output_dir: Path | None) -> int:
    """Run one trial from a serialized ``TrialNodeInputs`` file and return its exit code.

    ``0`` when a ``TrialResult`` was produced and written — a graded failure
    included, because the trial ran. A raised exception is classified into the
    deterministic / transient split so the caller can tell "do not retry" from
    "retry"; its full traceback goes to stderr.
    """
    from tolokaforge.core.trial_node import run_trial_from_inputs

    try:
        inputs = _load_oneshot_inputs(inputs_path, task_pack_root, output_dir)
        result = run_trial_from_inputs(inputs)
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        return _oneshot_failure_exit_code(exc)
    _write_trial_result(result, inputs.output_dir)
    return 0


@click.command(name="run-trial")
@click.option(
    "--inputs",
    "inputs_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="One-shot mode: a serialized TrialNodeInputs JSON file to run once.",
)
@click.option(
    "--task-pack-root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="One-shot mode: read the task pack from <root>/<task_id> and rebase its "
    "compose files onto it.",
)
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="One-shot mode: where to write the bundle and trial_result.json.",
)
def run_trial(
    inputs_path: Path | None, task_pack_root: Path | None, output_dir: Path | None
) -> None:
    """Run one trial — over a JSON-Lines pipe, or one-shot from a serialized inputs file.

    \b
    One-shot mode (--inputs <file>):
      tolokaforge run-trial --inputs trial.json \\
        --task-pack-root <dir> --output-dir <dir>
    Reads a serialized TrialNodeInputs, runs one trial through the standalone
    entry, writes the bundle and trial_result.json under --output-dir, and exits
    with a code a sandbox-per-trial backend can act on. --task-pack-root and
    --output-dir override the inputs' task_pack_dir / output_dir with this host's
    paths; both apply only to --inputs.

    \b
    One-shot exit codes:
      (TrialResult produced and written; a graded failure too) .. exit 0
      deterministic failure — do not retry ..................... exit 2
        (ProvisionError / TaskPackDrift / NotImplemented / InvalidConfig)
      transient failure — a retry may clear it ................. exit 3
        (any other, unexpected, internal fault)

    \b
    Pipe mode (no --inputs): reads exactly one request envelope on stdin, runs a
    single trial, and writes exactly one terminal envelope on stdout. Every
    message is UTF-8 JSON, one object per line (JSON Lines), and carries "v":1 —
    the wire-protocol version, independent of the tolokaforge package version.

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
    pipe error_type / exit code:
      (success, result emitted) ................................. exit 0
      ProtocolError  (malformed JSON / bad v / unknown type) .... exit 1
      cancelled      (cancel envelope / EOF / SIGTERM / SIGINT) .. exit 1
      ValidationError (invalid task or models) ................. exit 1
      UnknownImplementationError (unknown runtime/grader/conductor) exit 1
      ProvisionError (substrate failed to provision) ........... exit 1
      InternalError  (any other failure) ....................... exit 1

    The full traceback for any error is written to stderr; in pipe mode stdout
    carries only the wire. On POSIX, SIGTERM and SIGINT trigger clean teardown
    then a "cancelled" error.

    File assets on the task (grading.yaml, initial_state.json, tools) resolve in
    one-shot mode against --task-pack-root; in pipe mode against the subprocess
    working directory (spawn with cwd at the task-pack root, or send a
    fully-inline task that references no on-disk files).

    See docs/adr/0022-runtime-independence.md § Surface 3 and docs/API.md for the
    full contract.
    """
    if inputs_path is not None:
        raise SystemExit(_run_oneshot(inputs_path, task_pack_root, output_dir))
    if task_pack_root is not None or output_dir is not None:
        raise click.UsageError(
            "--task-pack-root and --output-dir apply only to the one-shot --inputs mode."
        )
    _install_signal_handlers()
    wire_out = _reroute_stdout_to_stderr()
    raise SystemExit(_run_trial(sys.stdin, wire_out))
