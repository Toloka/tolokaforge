"""Session-lifetime bash tool matching Anthropic's ``bash_20250124`` contract.

Three roles live here:

- :class:`PersistentShellTool` is the *schema provider*. The builtin registry
  advertises its ``get_schema()`` to the LLM; it is never executed (the runner
  drives execution through ``PersistentShellToolWrapper``).
- :class:`LocalBashSession` is the local *engine*: a single ``bash`` subprocess
  held for the trial.
- :class:`DockerComposeBashSession` is the compose *engine*: a held
  ``docker exec`` into an already-running service container.

Both engines share the sentinel protocol, per-command stdio staging,
per-command timeout, and 16 KB middle-truncation via :class:`_PtyBashSession`;
they differ only in how the bash pipe is opened and how a runaway command is
terminated on timeout. Working directory, exported environment, and shell
functions persist across :meth:`run` calls; :meth:`close` + re-:meth:`open`
yields a clean shell.

Each command runs inside the session shell with stdin from ``/dev/null`` and
stdout/stderr staged to per-call files, which the shell relays back over the
pty and removes once the command returns. Only the shell itself ever holds the
pty: a service the command leaves running (``uvicorn … &``) keeps writing to
its files after the call returns and survives a later timeout or shell exit,
and a program that reads stdin sees EOF instead of swallowing the sentinel.

The local session opens ``bash`` under a PTY with job control (``set -m``)
enabled: the runaway command runs in its own foreground process group, so
terminating that group leaves the session leader (``bash``) alive with its
accumulated state intact.
"""

from __future__ import annotations

import math
import os
import pty
import select
import shlex
import signal
import subprocess
import termios
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from tolokaforge.tools.registry import Tool, ToolCategory, ToolPolicy, ToolResult

_DEFAULT_TIMEOUT_S = 120.0
_OPEN_TIMEOUT_S = 15.0

# How many commands one ``bash_session_batch`` call may carry. Mirrors
# ``tolokaforge_coding_harnesses.adapter_support.MAX_BATCH_COMMANDS``: the two
# tools are compared against each other, so a call of one must be able to hold
# what a call of the other holds.
MAX_SESSION_BATCH_COMMANDS = 10

# Middle-truncation budget for a single command's output.
_MAX_OUTPUT_CHARS = 16384
_HEAD_CHARS = 8192
_TAIL_CHARS = 8192

_SENTINEL_PREFIX = "__DONE_"

# Directory the session shell stages each command's stdout/stderr under; a
# host path for the local engine, an in-container path for the compose engine.
_STAGE_DIR = "/tmp"
_STAGE_PREFIX = ".tolokaforge-session-"

# How long a surviving shell gets to relay the staged files and print the
# sentinel once its runaway foreground command has been killed.
_RELAY_GRACE_S = 2.0
# How long a surviving shell gets to drain an in-flight stage relay and answer a
# fresh sentinel before the session is reset. A command that ignored the kill's
# SIGINT keeps relaying a large stage after SIGKILL; draining it here keeps that
# output from bleeding into the next command.
_RESYNC_GRACE_S = 10.0
_COLLECT_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one command in a persistent shell session.

    ``timed_out`` and ``shell_exited`` are exclusive: a timed-out command was
    still running at the deadline; ``shell_exited`` means the shell process
    itself ended during the command (``exit``, ``set -e``, ``exec``), its
    ``exit_code`` is the shell's, and the session is closed. ``session_reset``
    means the engine replaced the shell to recover from the timeout, so the
    working directory, environment and functions of the previous shell are
    gone and the command may still be running.
    """

    output: str
    exit_code: int | None
    timed_out: bool
    shell_exited: bool = False
    session_reset: bool = False


@dataclass(frozen=True)
class _Stage:
    """In-shell paths one command's stdout and stderr are staged to."""

    out: str
    err: str

    @classmethod
    def new(cls) -> _Stage:
        token = uuid.uuid4().hex
        base = f"{_STAGE_DIR}/{_STAGE_PREFIX}{token}"
        return cls(out=f"{base}.out", err=f"{base}.err")


@runtime_checkable
class BashSession(Protocol):
    """A held bash pipe whose state persists across commands.

    Implementations differ only in *how* the pipe is opened (a local
    subprocess vs. an exec into a running container); the sentinel protocol,
    per-command timeout, and output truncation are shared behaviour.
    """

    @property
    def pid(self) -> int:
        """PID of the session-leader shell process (for kill-safety checks)."""
        ...

    @property
    def is_open(self) -> bool:
        """Whether a shell is held; ``False`` once it exited or was closed."""
        ...

    def open(self, cwd: str | None) -> None:
        """Start the shell, optionally seeding its working directory."""
        ...

    def run(self, command: str, timeout_s: float) -> CommandResult:
        """Run *command*, returning its output, exit code, and timeout flag."""
        ...

    def close(self) -> None:
        """Terminate the shell and release its resources."""
        ...


def _truncate_middle(text: str) -> str:
    """Return *text* unchanged, or head+marker+tail when over budget."""
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    elided = len(text) - _HEAD_CHARS - _TAIL_CHARS
    marker = (
        f"\n...[truncated: {elided} chars elided; re-run a narrower "
        f"command or grep for the pattern you need]...\n"
    )
    return text[:_HEAD_CHARS] + marker + text[-_TAIL_CHARS:]


class _PtyBashSession:
    """Shared engine for a bash pipe held under a host PTY.

    Subclasses provide the process spawn (:meth:`_popen`), the per-command
    timeout kill strategy (:meth:`_terminate_runaway`) and the read-back of a
    command's staged files (:meth:`_collect_stage`); the sentinel protocol,
    stdio staging, timeout loop, and output truncation are shared here.
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
        self._master_fd: int | None = None
        self._cwd: str | None = None
        self._ready = False

    @property
    def pid(self) -> int:
        if self._proc is None:
            raise RuntimeError("bash session is not open")
        return self._proc.pid

    @property
    def is_open(self) -> bool:
        return self._master_fd is not None

    def open(self, cwd: str | None) -> None:
        if self._proc is not None:
            raise RuntimeError("bash session is already open")
        self._cwd = cwd
        if not self._open_shell(cwd):
            raise RuntimeError("bash session failed to become ready")

    def run(self, command: str, timeout_s: float) -> CommandResult:
        if self._master_fd is None:
            raise RuntimeError("bash session is not open")
        sentinel = f"{_SENTINEL_PREFIX}{uuid.uuid4().hex}__"
        marker = sentinel.encode()
        stage = _Stage.new()
        self._write(self._staged_command(command, stage, sentinel))

        buf, parsed, exited = self._read_until_sentinel(b"", marker, time.monotonic() + timeout_s)
        if parsed is not None:
            output, exit_code = parsed
            return CommandResult(
                output=_truncate_middle(output), exit_code=exit_code, timed_out=False
            )
        if exited:
            return self._on_process_exit(buf, stage)
        return self._on_timeout(buf, marker, stage)

    def close(self) -> None:
        if self._proc is not None:
            self._kill_process_group(signal.SIGKILL)
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            self._proc = None
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        self._ready = False

    # -- spawn / cwd hooks ----------------------------------------------------

    def _open_shell(self, cwd: str | None) -> bool:
        """Spawn the shell, confirm it answers a sentinel, seed its cwd.

        Returns whether the shell became ready; a pipe that closes before the
        first sentinel round-trip leaves the session closed.
        """
        self._spawn(cwd)
        # Bracketed paste is readline's and only exists in an interactive
        # shell; off, the pty carries the relayed command output alone.
        self._write("set -m; PS1=''; PS2=''; bind 'set enable-bracketed-paste off' 2>/dev/null\n")
        ready = self.run(":", timeout_s=_OPEN_TIMEOUT_S)
        if ready.timed_out or ready.shell_exited:
            return False
        self._ready = True
        self._seed_cwd(cwd)
        return True

    def _spawn(self, cwd: str | None) -> None:
        master, slave = pty.openpty()
        attrs = termios.tcgetattr(slave)
        attrs[3] &= ~termios.ECHO  # drop terminal echo so output is command stdout only
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        self._proc = self._popen(cwd, slave)
        os.close(slave)
        self._master_fd = master

    def _popen(self, cwd: str | None, slave_fd: int) -> subprocess.Popen[bytes]:
        raise NotImplementedError

    def _seed_cwd(self, cwd: str | None) -> None:
        """Seed the working directory when the pipe can't at spawn time.

        The local engine passes ``cwd`` straight to ``Popen`` and needs no
        seeding; the compose engine overrides this to ``cd`` inside the
        container after the shell is ready.
        """

    def _terminate_runaway(self) -> bool:
        """Terminate a timed-out command, leaving the session usable.

        Returns whether the shell that ran the command is still the session's
        shell: ``True`` when only the command was killed and the shell will
        relay its staged files itself, ``False`` when the shell was replaced.
        """
        raise NotImplementedError

    def _collect_stage(self, stage: _Stage, remove: bool) -> str:
        """Read back a command's staged stdout and stderr, in that order.

        Used when the shell could not relay them itself: it exited during the
        command, or was replaced after a timeout. ``remove`` deletes the files;
        a timed-out command the engine left running keeps them.
        """
        raise NotImplementedError

    # -- internals ------------------------------------------------------------

    @staticmethod
    def _staged_command(command: str, stage: _Stage, sentinel: str) -> str:
        """The text written to the shell for one command.

        The command runs as a brace group in the session shell itself, so
        ``cd``, ``export`` and function definitions persist, with stdin from
        ``/dev/null`` and stdout/stderr on the stage files. The shell then
        relays the files on the pty, removes them, and prints the sentinel
        with the command's exit status. The leading ``:`` keeps a command that
        is empty or only a comment a valid group.
        """
        out, err = shlex.quote(stage.out), shlex.quote(stage.err)
        return (
            f"{{ :; {command}\n"
            f"}} >{out} 2>{err} </dev/null; __tolokaforge_rc=$?; "
            f"cat {out}; cat {err} >&2; rm -f {out} {err}; "
            f"printf '%s %d\\n' {sentinel} \"$__tolokaforge_rc\"\n"
        )

    @staticmethod
    def _split_on_sentinel(buf: bytes, marker: bytes) -> tuple[str, int | None] | None:
        """Return (output, exit_code) once the full sentinel line has arrived."""
        idx = buf.find(marker)
        if idx == -1:
            return None
        after = buf[idx + len(marker) :]
        newline = after.find(b"\n")
        if newline == -1:
            return None  # exit-code line not fully received yet
        token = after[:newline].strip()
        exit_code = int(token) if token.isdigit() else None
        output = _decode(buf[:idx])
        return output, exit_code

    def _read_until_sentinel(
        self, buf: bytes, marker: bytes, deadline: float
    ) -> tuple[bytes, tuple[str, int | None] | None, bool]:
        """Read the pty until the sentinel line, the deadline, or the pipe closing.

        Returns the accumulated bytes, the parsed ``(output, exit_code)`` when
        the sentinel arrived, and whether the pipe closed (the shell exited).
        """
        while True:
            parsed = self._split_on_sentinel(buf, marker)
            if parsed is not None:
                return buf, parsed, False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return buf, None, False
            if not self._wait_readable(min(remaining, 0.2)):
                continue
            chunk = self._read()
            if not chunk:
                return buf, None, True
            buf += chunk

    def _on_timeout(self, buf: bytes, marker: bytes, stage: _Stage) -> CommandResult:
        same_shell = self._terminate_runaway()
        if same_shell:
            # The shell relays the stage files and prints the sentinel once its
            # foreground command is gone; the exit status it carries is the
            # kill's, so the result stays a timeout.
            buf, parsed, _ = self._read_until_sentinel(
                buf, marker, time.monotonic() + _RELAY_GRACE_S
            )
            if parsed is not None:
                return CommandResult(
                    output=_truncate_middle(parsed[0]), exit_code=None, timed_out=True
                )
            # No sentinel in the grace window: the shell either aborted the
            # command line on SIGINT and is idle, or ignored it and is still
            # relaying a large stage after SIGKILL. Read the stage files
            # directly, then resync the pty to a clean prompt so an in-flight
            # relay cannot bleed into the next command; a shell that will not
            # resync is reset.
            output = _decode(buf) + self._collect_stage(stage, remove=True)
            return CommandResult(
                output=_truncate_middle(output),
                exit_code=None,
                timed_out=True,
                session_reset=not self._resync(),
            )
        output = _decode(buf) + self._collect_stage(stage, remove=same_shell)
        return CommandResult(
            output=_truncate_middle(output),
            exit_code=None,
            timed_out=True,
            session_reset=not same_shell,
        )

    def _resync(self) -> bool:
        """Drain any in-flight stage relay and confirm a clean prompt.

        Writes a fresh sentinel and reads the pty until it arrives, discarding
        everything before it — including a stage relay the shell is still
        writing after a SIGKILL. Returns whether the shell answered in time; a
        shell that closed the pipe or stayed silent is closed here so the next
        command reopens a clean session.
        """
        sentinel = f"{_SENTINEL_PREFIX}{uuid.uuid4().hex}__"
        self._write(f"printf '%s\\n' {shlex.quote(sentinel)}\n")
        _, parsed, closed = self._read_until_sentinel(
            b"", sentinel.encode(), time.monotonic() + _RESYNC_GRACE_S
        )
        if parsed is not None and not closed:
            return True
        self.close()
        return False

    def _on_process_exit(self, buf: bytes, stage: _Stage) -> CommandResult:
        """The pipe closed mid-command: the shell exited, and the session with it.

        Not a timeout: there is no runaway command to terminate and no live
        shell to preserve, so this never enters :meth:`_terminate_runaway`
        (whose compose variant would reopen and read EOF again, without
        bound, while the target container is not running). The stage files
        were never relayed, so they are read back directly when a shell had
        been ready to write them.
        """
        exit_code: int | None = None
        if self._proc is not None:
            try:
                exit_code = self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        was_ready = self._ready
        self.close()
        output = _decode(buf)
        if was_ready:
            output += self._collect_stage(stage, remove=True)
        return CommandResult(
            output=_truncate_middle(output),
            exit_code=exit_code,
            timed_out=False,
            shell_exited=True,
        )

    def _kill_process_group(self, sig: int) -> None:
        assert self._proc is not None
        try:
            os.killpg(os.getpgid(self._proc.pid), sig)
        except (ProcessLookupError, OSError):
            pass

    def _write(self, data: str) -> None:
        assert self._master_fd is not None
        buf = data.encode()
        while buf:
            buf = buf[os.write(self._master_fd, buf) :]

    def _read(self) -> bytes:
        assert self._master_fd is not None
        try:
            return os.read(self._master_fd, 65536)
        except OSError:
            return b""

    def _wait_readable(self, timeout_s: float) -> bool:
        """Whether the pty master has something to read within ``timeout_s``.

        ``poll`` rather than ``select``: ``select`` cannot name a descriptor at
        or above ``FD_SETSIZE`` (1024) and raises ``ValueError`` when handed
        one, however few are actually open. A runner holding a pty and a gRPC
        socket per concurrent trial reaches those numbers while nowhere near
        ``RLIMIT_NOFILE``, and every command on the session that drew a high
        descriptor would fail. ``poll`` has no such ceiling.

        ``POLLHUP``, ``POLLERR`` and ``POLLNVAL`` need no registering — they are
        reported whatever the mask — so a shell that exited, and a descriptor
        closed under a reader the backstop abandoned, both wake this the same
        way a readable one does. :meth:`_read` turns either into the empty read
        the callers treat as a closed pipe, where ``select`` would have raised
        in the abandoned thread instead.
        """
        assert self._master_fd is not None
        poller = select.poll()
        poller.register(self._master_fd, select.POLLIN)
        # Round up: poll() takes whole milliseconds, and truncating a sub-
        # millisecond wait to 0 would spin the caller's loop instead of waiting.
        return bool(poller.poll(math.ceil(timeout_s * 1000)))


def _decode(buf: bytes) -> str:
    return buf.decode("utf-8", errors="replace").replace("\r\n", "\n")


class LocalBashSession(_PtyBashSession):
    """A local ``bash`` subprocess held for the trial (PTY + job control)."""

    def _popen(self, cwd: str | None, slave_fd: int) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            ["bash", "--norc", "--noprofile"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
            cwd=cwd,
            close_fds=True,
        )

    def _terminate_runaway(self) -> bool:
        """Kill the foreground command's process group, never the shell itself.

        Job control (``set -m``) puts the external command in its own
        foreground process group that owns the tty; signalling that group
        leaves ``bash`` (the session leader) alive with its state intact.
        """
        assert self._master_fd is not None and self._proc is not None
        try:
            foreground = os.tcgetpgrp(self._master_fd)
        except OSError:
            return True
        shell_pgid = os.getpgid(self._proc.pid)
        if foreground <= 0 or foreground == shell_pgid:
            return True  # only the shell is in the foreground — nothing to terminate
        for sig in (signal.SIGINT, signal.SIGKILL):
            try:
                os.killpg(foreground, sig)
            except ProcessLookupError:
                return True
            time.sleep(0.2)
        return True

    def _collect_stage(self, stage: _Stage, remove: bool) -> str:
        """The stage files are host files: read them directly."""
        parts = []
        for path in (stage.out, stage.err):
            try:
                with open(path, "rb") as fh:
                    parts.append(_decode(fh.read()))
            except OSError:
                continue
            if remove:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        return "".join(parts)


# Read-back of a command's stage files from outside its shell: relays them as
# they stand, exit 3 once the command has removed them. ``$3`` set removes
# them afterwards.
_STAGE_READ_SCRIPT = '[ -e "$1" ] || exit 3; cat "$1"; cat "$2" >&2; [ -z "$3" ] || rm -f "$1" "$2"'


class DockerComposeBashSession(_PtyBashSession):
    """A held ``docker exec -i <container> bash`` into a running service.

    Targets an *already-running* container (brought up by the environment /
    another lifecycle consumer); this engine only holds an exec session for the
    trial, it never brings the stack up or down. State (cwd, environment,
    functions) persists across :meth:`run` calls exactly as for the local
    engine.

    Kill-safety is weaker than the local engine: a signal to the host-side
    ``docker exec`` process group does not reach the command running inside the
    container. When a per-command timeout fires we kill the host ``docker exec``
    client and open a fresh in-container session to regain a clean prompt; the
    timed-out command keeps running inside the container, on its stage files,
    and the result says so (``session_reset``). Subsequent commands run in the
    new shell, whose cwd, environment and functions start from scratch (unlike
    the local engine, which preserves them).
    """

    def __init__(self, container_name: str, user: str | None = None) -> None:
        """Hold a ``docker exec -i`` bash session into *container_name*.

        ``user`` optionally sets ``docker exec --user <user>`` so the
        session runs as a specific user (name or ``uid:gid``) rather
        than the container's default. Task-side callers use this to
        drop privileges: an image whose ENTRYPOINT runs as root can
        still hand the agent an unprivileged shell, so root-owned files
        (e.g. a hidden test oracle staged by the container's own
        grader) stay unreachable to the agent. Default ``None`` inherits
        the container's default user and preserves prior behaviour.
        """
        super().__init__()
        self._container_name = container_name
        self._user = user

    @property
    def container_name(self) -> str:
        return self._container_name

    @property
    def user(self) -> str | None:
        return self._user

    def _exec_prefix(self) -> list[str]:
        # ``--user`` before ``-i``: matches ``docker exec --help`` order and
        # the editor's ``_exec`` (str_replace_editor.py) argv shape so a
        # future grep over the codebase sees one neighbour pattern.
        argv = ["docker", "exec"]
        if self._user is not None:
            argv.extend(["--user", self._user])
        argv.extend(["-i", self._container_name])
        return argv

    def _popen(self, cwd: str | None, slave_fd: int) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [*self._exec_prefix(), "bash", "--norc", "--noprofile"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
            close_fds=True,
        )

    def _seed_cwd(self, cwd: str | None) -> None:
        # Host work_dir need not exist inside the container; swallow the cd
        # failure so the session still opens (the local engine's isdir guard
        # has no in-container equivalent here).
        if cwd:
            self.run(f"cd {shlex.quote(cwd)} 2>/dev/null || true", timeout_s=_OPEN_TIMEOUT_S)

    def _terminate_runaway(self) -> bool:
        """Kill the host ``docker exec`` client and open a fresh in-container session.

        The runaway command survives as an in-container process on its stage
        files (see the class docstring); killing the client and reopening
        restores a usable prompt for the next command.
        """
        self.close()
        self._open_shell(self._cwd)
        return False

    def _collect_stage(self, stage: _Stage, remove: bool) -> str:
        """Read the stage files back through a one-shot exec into the container."""
        argv = [
            *self._exec_prefix(),
            "bash",
            "-c",
            _STAGE_READ_SCRIPT,
            "tolokaforge-session",
            stage.out,
            stage.err,
            "rm" if remove else "",
        ]
        try:
            done = subprocess.run(argv, capture_output=True, timeout=_COLLECT_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError):
            return ""
        if done.returncode == 3:
            return ""
        return _decode(done.stdout) + _decode(done.stderr)


class PersistentShellTool(Tool):
    """Schema provider for the ``bash_session`` tool.

    Publishes the Anthropic ``bash_20250124`` input schema (``command`` +
    ``restart``) so the native adapter can advertise it. Execution is handled
    by the runner-side wrapper, not here.

    ``__init__`` accepts and ignores the ``tool_config`` keys the wrapper
    consumes (``service``, ``timeout_s``, ``compose_project_prefix``): the
    adapter builds the schema provider as ``cls(**tool_config)``, so an
    unexpected keyword would raise inside schema extraction and drop the tool
    from the LLM's view.
    """

    def __init__(
        self,
        service: str | None = None,
        timeout_s: float | None = None,
        compose_project_prefix: str | None = None,
        **_: object,
    ) -> None:
        super().__init__(
            name="bash_session",
            description=(
                "Run commands in a persistent bash shell. The working directory, "
                "exported environment variables, and shell functions persist "
                "across calls. Set restart=true to reset the shell to a clean "
                "state (omit command when restarting)."
            ),
            policy=ToolPolicy(timeout_s=_DEFAULT_TIMEOUT_S, category=ToolCategory.COMPUTE),
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The bash command to run in the persistent session.",
                        },
                        "restart": {
                            "type": "boolean",
                            "description": "Restart the shell, discarding all session state.",
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        }

    def execute(self, **kwargs: Any) -> ToolResult:
        raise NotImplementedError(
            "PersistentShellTool is a schema provider; execution is handled by "
            "PersistentShellToolWrapper on the runner."
        )


class PersistentShellBatchTool(Tool):
    """Schema provider for the ``bash_session_batch`` tool.

    The batching sibling of :class:`PersistentShellTool`, and the stateful
    sibling of ``bash_batch``. One call carries an array of up to
    :data:`MAX_SESSION_BATCH_COMMANDS` commands which run in order against the
    trial's single held shell, so cwd, environment, functions and anything
    sourced carry from one command to the next and on into the following call.

    The parameter shape — a required ``note`` ahead of a required ``commands``
    array — is ``bash_batch``'s, so the two tools differ in statefulness and
    nothing else. Execution is handled by the runner-side wrapper, not here.

    ``__init__`` accepts and ignores the wrapper's ``tool_config`` keys for the
    same reason :class:`PersistentShellTool` does: the adapter builds the
    schema provider as ``cls(**tool_config)``.
    """

    def __init__(
        self,
        service: str | None = None,
        timeout_s: float | None = None,
        compose_project_prefix: str | None = None,
        **_: object,
    ) -> None:
        super().__init__(
            name="bash_session_batch",
            description=(
                "Run bash commands inside the task container, in order. All "
                "commands share one persistent shell: the working directory, "
                "exported environment variables, shell functions and anything "
                "you `source` persist from one command to the next and across "
                "calls, as do files, installed packages and running services. "
                "A command that fails does not stop the ones after it, and "
                "every command's output comes back labelled in one result. "
                "Default to several commands per call: running one at a time "
                "when more were already decided wastes a turn — editing a file "
                "and then running its tests is one call, and so is a batch of "
                "independent reads. Start a new call only when the next "
                "command truly depends on reading this one's output."
            ),
            policy=ToolPolicy(timeout_s=_DEFAULT_TIMEOUT_S, category=ToolCategory.COMPUTE),
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "note": {
                            "type": "string",
                            "description": (
                                "A sentence or two: what the last output actually "
                                "showed, what is still left, and what you expect "
                                "these commands to produce."
                            ),
                        },
                        "commands": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": MAX_SESSION_BATCH_COMMANDS,
                            "description": (
                                "Shell commands to run in order, at most "
                                f"{MAX_SESSION_BATCH_COMMANDS} per call. Prefer to send "
                                "several here at once; start a new call only when "
                                "the next command depends on reading this call's "
                                "output."
                            ),
                        },
                        "restart": {
                            "type": "boolean",
                            "description": (
                                "Discard all session state — working directory, "
                                "environment variables and shell functions — before "
                                "these commands run."
                            ),
                        },
                    },
                    "required": ["note", "commands"],
                    "additionalProperties": False,
                },
            },
        }

    def execute(self, **kwargs: Any) -> ToolResult:
        raise NotImplementedError(
            "PersistentShellBatchTool is a schema provider; execution is handled by "
            "PersistentShellToolWrapper on the runner."
        )
