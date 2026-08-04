"""Subset-native CLI shim — ``tolokaforge --version`` and ``tolokaforge run-trial``.

The runner subset wheel binds its ``[project.scripts]`` entry
``tolokaforge = tolokaforge.runner._cli:main`` at this module. Two subcommands,
matching the runner image's committed exec surface verbatim:

- ``tolokaforge --version`` — prints the installed subset wheel's version,
  resolved via :func:`importlib.metadata.version`. When the metadata lookup
  fails (subset wheel not installed) the command exits non-zero rather than
  silently misreporting a version.
- ``tolokaforge run-trial`` (hidden) — reads one JSON-Lines ``start``
  envelope on stdin (ADR-0022 § Surface 3), drives one trial against the
  **local runner service on ``localhost:50051``** using the runner subset's
  gRPC client, and writes exactly one ``result`` or ``error`` envelope on
  stdout.

The shim is thin by design. Unlike the base wheel's ``run-trial`` (which
composes an adapter + runtime backend + conductor + trial grader), this
subset-native shim orchestrates in-process against the local runner service:
it cannot spin up compose stacks, cannot switch backends, and cannot exercise
adapter-specific setup. The narrower semantics are documented in
``docs/STANDALONE_RUNNER.md``.

stdout carries the wire (one JSON object per line, flushed); diagnostics and
tracebacks go to stderr so the wire stays pure.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import signal
import sys
import traceback
from dataclasses import dataclass
from types import FrameType
from typing import Any, TextIO

import click

WIRE_VERSION = 1
"""ADR-0022 § Surface 3 wire-protocol version. Independent of the wheel version."""

SUBSET_DISTRIBUTION_NAME = "tolokaforge-runner-subset"
"""Distribution name of the subset wheel. Matches the name the custom hatch
builder writes into ``METADATA``."""

DEFAULT_RUNNER_ADDRESS = "localhost:50051"
"""Local gRPC endpoint of the runner service the shim drives trials against.

Inside the runner container the shim is invoked via ``docker exec`` after the
runner service is listening on this port; outside the container the caller is
responsible for starting a runner subprocess on the same host+port."""


# ---------------------------------------------------------------------------
# Wire framing — ADR-0022 § Surface 3
# ---------------------------------------------------------------------------


class ProtocolError(Exception):
    """A stdin envelope violates the wire framing (bad ``v``, unknown ``type``,
    unknown top-level key, or malformed JSON)."""


class _Cancelled(Exception):
    """External cancellation — SIGTERM / SIGINT, premature stdin EOF, or a
    synchronous ``cancel`` envelope — surfaced as ``error_type:"cancelled"``."""


class ProvisionError(Exception):
    """The runner service could not provision the trial (register / grade RPC
    failure, or the requested task shape needs adapter machinery the subset
    does not carry). Maps to the ADR-0022 ``ProvisionError`` wire error type
    so external harnesses branch on the same error class as the base wheel."""


_ALLOWED_START_KEYS = frozenset({"v", "type", "task", "models", "runtime", "grader", "conductor"})
_ALLOWED_CANCEL_KEYS = frozenset({"v", "type"})


@dataclass(frozen=True)
class StartMessage:
    """A parsed ``start`` envelope: the arguments a ``run_trial`` call needs."""

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


def marshal_result(result_payload: dict[str, Any]) -> dict[str, Any]:
    """Serialise a successful trial into the ``result`` wire envelope."""
    return {"v": WIRE_VERSION, "type": "result", "result": result_payload}


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
    """Name the wire ``error_type`` for ``exc`` per the ADR-0022 mapping table."""
    if isinstance(exc, _Cancelled):
        return "cancelled"
    if isinstance(exc, ProtocolError):
        return "ProtocolError"
    if isinstance(exc, ProvisionError):
        return "ProvisionError"
    # Pydantic ValidationError — imported lazily so the wire framing has no
    # hard runtime dep on Pydantic for the garbage-in error path (importing
    # the model_config module chain is unnecessary for a protocol error).
    try:
        from pydantic import ValidationError as _PydanticValidationError

        if isinstance(exc, _PydanticValidationError):
            return "ValidationError"
    except ImportError:  # pragma: no cover - Pydantic is a subset runtime dep
        pass
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


# ---------------------------------------------------------------------------
# Trial driver — the subset-native path from `start` envelope to `result`.
# ---------------------------------------------------------------------------


def _check_runner_service_reachable(address: str) -> None:
    """Confirm the local runner gRPC service is listening.

    Any subset-native trial the shim would drive routes through this
    endpoint; failing here surfaces "the runner service is not up on this
    host" as a distinct :class:`ProvisionError` before the caller sees the
    downstream adapter-gap error and blames the wrong layer.
    """
    import grpc  # imported lazily so ``--version`` pays no gRPC import cost

    channel = grpc.insecure_channel(address)
    try:
        grpc.channel_ready_future(channel).result(timeout=5.0)
    except grpc.FutureTimeoutError as exc:
        raise ProvisionError(
            f"local runner gRPC service at {address} is not reachable within 5s — "
            "the runner container must be running with the service listening "
            "before the subset-native run-trial shim is invoked"
        ) from exc
    finally:
        channel.close()


def _run_from_start(message: StartMessage) -> dict[str, Any]:
    """Drive one trial against the local runner service, returning the
    trajectory-shaped result payload for the ``result`` envelope.

    The subset shim relies on the local runner service to hold every piece
    the trial needs (tool reconstruction from the task schema, per-trial
    state, grading). It does not carry adapter machinery, so a task whose
    representation requires adapter-side transformation (native tasks with
    on-disk assets, tool_artifacts to bundle, environment_manifest to
    materialise) surfaces here as a :class:`ProvisionError` — the honest
    signal that the base wheel's ``run_trial`` (called from outside the
    container) is the right entry for that task, and the subset-native shim
    is meant for in-process wire-driven trials where the caller has
    materialised the task-description shape ahead of time.

    The full driver is implemented lazily so ``--version`` and the wire
    error paths don't pay the import cost — imports of
    :mod:`tolokaforge.runner.runner_pb2_grpc`, :mod:`tolokaforge.core.loop`,
    and :mod:`tolokaforge.core.llm.client` only happen here.
    """
    # Fail fast when the shim is invoked without enough to build a trial —
    # the ADR-0027 § Consequences trade-off names this explicitly: the
    # subset-native ``run-trial`` cannot exercise adapter-specific setup.
    if message.task is None:
        raise ProvisionError("start envelope is missing 'task'")
    if message.models is None:
        raise ProvisionError("start envelope is missing 'models'")

    # Confirm the local runner service is reachable before failing on the
    # adapter gap, so an operator whose container never came up sees the
    # right diagnostic instead of the downstream missing-adapter message.
    address = os.environ.get("EXECUTOR_ADDRESS", DEFAULT_RUNNER_ADDRESS)
    _check_runner_service_reachable(address)

    # The register RPC needs a serialised ``TrialSpec``. Building one from a
    # bare ``TaskConfig`` requires adapter machinery (native adapter reads
    # on-disk assets, transforms YAML to schema, bundles tool artifacts) —
    # base-wheel only per ADR-0025. The subset shim delegates that
    # composition to the caller: an external harness that has already
    # produced a TaskDescription-shaped payload should call the runner
    # service's ``RegisterTrial`` RPC directly; anything else surfaces as a
    # ProvisionError so the operator sees the honest reason and routes
    # through the base wheel's ``run_trial`` API instead.
    raise ProvisionError(
        "the subset-native run-trial shim cannot compose a TrialSpec from a "
        "TaskConfig — adapter machinery lives in the base wheel. Drive trials "
        "via the base wheel's tolokaforge.core.run_trial.run_trial() from "
        "outside the container, or call the local runner service's "
        f"RegisterTrial RPC directly at {address}. "
        "See docs/STANDALONE_RUNNER.md for the subset-shim contract."
    )


def _raise_cancelled(signum: int, _frame: FrameType | None) -> None:
    raise _Cancelled(f"received signal {signum}")


def _install_signal_handlers() -> None:
    """Install SIGTERM / SIGINT → ``_Cancelled`` where the platform supports it."""
    for name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal, name, None)
        if signum is not None:
            signal.signal(signum, _raise_cancelled)


def _drive_run_trial(in_stream: TextIO, out_stream: TextIO) -> int:
    """Drive one trial from ``in_stream``, writing the wire to ``out_stream``.

    The whole envelope-to-result path sits inside the ``try`` so a
    signal-raised ``_Cancelled`` unwinds cleanly before the ``cancelled``
    error is emitted. The broad ``except`` is the ADR-0022 wire contract —
    every failure emits a typed ``error`` on the wire, writes its
    traceback to stderr, and exits non-zero; nothing is swallowed.
    """
    try:
        message = _read_first_envelope(in_stream)
        if isinstance(message, CancelMessage):
            raise _Cancelled("cancel requested before a start message")
        result_payload = _run_from_start(message)
    except Exception as exc:
        _emit(out_stream, marshal_error(exc))
        traceback.print_exc(file=sys.stderr)
        return 1
    _emit(out_stream, marshal_result(result_payload))
    return 0


def _reroute_stdout_to_stderr() -> TextIO:
    """Point the process stdout descriptor at stderr; return the pure wire stream.

    Any logger bound to ``sys.stdout`` — or C-level code writing to fd 1
    directly — would otherwise pollute the JSON-Lines wire. Duplicate the
    real stdout to a private descriptor for wire writes, then ``dup2``
    stderr over fd 1 so every stray stdout write lands on stderr instead.
    ``sys.stdout`` is repointed at stderr for Python-level writes made
    after this call.
    """
    sys.stdout.flush()
    wire_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return os.fdopen(wire_fd, "w", encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def _resolve_subset_version() -> str:
    """Return the installed subset wheel's version, or raise loudly.

    ``importlib.metadata.version`` raises :class:`PackageNotFoundError` when
    the distribution is not installed; we surface that as a non-zero exit
    rather than fabricating a version string. Inside the runner image the
    metadata lookup always succeeds — the subset wheel is what pip installed
    to produce the image."""
    return importlib.metadata.version(SUBSET_DISTRIBUTION_NAME)


@click.group(invoke_without_command=True)
@click.version_option(
    version=None,  # populated dynamically in main()
    package_name=SUBSET_DISTRIBUTION_NAME,
    prog_name="tolokaforge",
    message="%(prog)s %(version)s",
)
@click.pass_context
def _cli(ctx: click.Context) -> None:
    """Tolokaforge runner-subset CLI shim.

    The runner Docker image installs the subset wheel and binds this shim as
    its ``tolokaforge`` console script (ADR-0027). Two subcommands preserve
    the runner image's committed ``docker exec`` surface:

    \b
    - tolokaforge --version : print the installed subset wheel's version.
    - tolokaforge run-trial : run one trial as a JSON-Lines subprocess.
    """
    # ``invoke_without_command=True`` lets ``--version`` fire click's own
    # eager option handler above and exit before dispatch; when neither
    # ``--version`` nor a subcommand was given, show help and exit 0 (the
    # runner image's operator surface has no default action).
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@_cli.command(name="run-trial", hidden=True)
def _run_trial() -> None:
    """Run one trial as a subprocess over a JSON-Lines pipe.

    Reads exactly one request envelope on stdin, drives a single trial
    against the local runner service on ``localhost:50051``, and writes
    exactly one terminal envelope on stdout. Every message is UTF-8 JSON,
    one object per line (JSON Lines), and carries ``"v":1`` — the
    wire-protocol version, independent of the tolokaforge package version.

    \b
    stdin (one line, then EOF):
      {"v":1,"type":"start","task":{...},"models":{"agent":{...}},
       "runtime":"auto","grader":"runner_rpc","conductor":"in_process"}
      {"v":1,"type":"cancel"}

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
      ProvisionError (subset shim cannot compose a TrialSpec —
                      route through the base wheel's run_trial) .. exit 1
      InternalError  (any other failure) ....................... exit 1

    The full traceback for any error is written to stderr; stdout carries only
    the wire. On POSIX, SIGTERM and SIGINT trigger clean teardown then a
    "cancelled" error.

    Trial-execution semantics inside the subset image are narrower than the
    base wheel's ``tolokaforge run-trial``: the shim orchestrates in-process
    against the local gRPC runner and cannot spin up compose stacks, switch
    backends, or exercise adapter-specific setup. See ADR-0027 for the full
    contract.
    """
    _install_signal_handlers()
    wire_out = _reroute_stdout_to_stderr()
    raise SystemExit(_drive_run_trial(sys.stdin, wire_out))


def main() -> None:
    """Console-script entry point bound by the subset wheel's ``[project.scripts]``.

    Resolves the subset wheel's version at call time (so click's
    ``--version`` option prints the metadata-resolved value rather than a
    baked-in literal) and dispatches into the click group.
    """
    # Click's ``version_option`` accepts ``version=None`` and resolves the
    # version from ``importlib.metadata`` at option-parse time when
    # ``package_name`` is set — matching the "fail loud if the metadata
    # lookup fails" requirement of ADR-0027.
    _cli(prog_name="tolokaforge", standalone_mode=True)


if __name__ == "__main__":  # pragma: no cover
    main()
