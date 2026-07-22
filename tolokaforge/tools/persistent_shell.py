"""Session-lifetime bash tool matching Anthropic's ``bash_20250124`` contract.

Three roles live here:

- :class:`PersistentShellTool` is the *schema provider*. The builtin registry
  advertises its ``get_schema()`` to the LLM; it is never executed (the runner
  drives execution through ``PersistentShellToolWrapper``).
- :class:`LocalBashSession` is the local *engine*: a single ``bash`` subprocess
  held for the trial.
- :class:`DockerComposeBashSession` is the compose *engine*: a held
  ``docker exec`` into an already-running service container.

Both engines share the sentinel protocol, per-command timeout, and 16 KB
middle-truncation via :class:`_PtyBashSession`; they differ only in how the
bash pipe is opened and how a runaway command is terminated on timeout.
Working directory, exported environment, and shell functions persist across
:meth:`run` calls; :meth:`close` + re-:meth:`open` yields a clean shell.

The local session opens ``bash`` under a PTY with job control (``set -m``)
enabled: the runaway command runs in its own foreground process group, so
terminating that group leaves the session leader (``bash``) alive with its
accumulated state intact.
"""

from __future__ import annotations

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

# Middle-truncation budget for a single command's output.
_MAX_OUTPUT_CHARS = 16384
_HEAD_CHARS = 8192
_TAIL_CHARS = 8192

_SENTINEL_PREFIX = "__DONE_"


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one command in a persistent shell session."""

    output: str
    exit_code: int | None
    timed_out: bool


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

    Subclasses provide the process spawn (:meth:`_popen`) and the per-command
    timeout kill strategy (:meth:`_terminate_runaway`); the sentinel protocol,
    timeout loop, and output truncation are shared here.
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
        self._master_fd: int | None = None
        self._cwd: str | None = None

    @property
    def pid(self) -> int:
        if self._proc is None:
            raise RuntimeError("bash session is not open")
        return self._proc.pid

    def open(self, cwd: str | None) -> None:
        if self._proc is not None:
            raise RuntimeError("bash session is already open")
        self._cwd = cwd
        self._spawn(cwd)
        self._write("set -m; PS1=''; PS2=''\n")
        # Sentinel round-trip flushes shell-init output and confirms readiness.
        if self.run(":", timeout_s=_OPEN_TIMEOUT_S).timed_out:
            raise RuntimeError("bash session failed to become ready")
        self._seed_cwd(cwd)

    def run(self, command: str, timeout_s: float) -> CommandResult:
        if self._master_fd is None:
            raise RuntimeError("bash session is not open")
        sentinel = f"{_SENTINEL_PREFIX}{uuid.uuid4().hex}__"
        marker = sentinel.encode()
        self._write(f"{command}\n")
        self._write(f"printf '%s %d\\n' {sentinel} $?\n")

        buf = b""
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._on_timeout(buf)
            ready, _, _ = select.select([self._master_fd], [], [], min(remaining, 0.2))
            if not ready:
                continue
            chunk = self._read()
            if not chunk:
                return self._on_timeout(buf)
            buf += chunk
            parsed = self._split_on_sentinel(buf, marker)
            if parsed is not None:
                output, exit_code = parsed
                return CommandResult(
                    output=_truncate_middle(output),
                    exit_code=exit_code,
                    timed_out=False,
                )

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

    # -- spawn / cwd hooks ----------------------------------------------------

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

    def _terminate_runaway(self) -> None:
        """Terminate a timed-out command, leaving the session usable."""
        raise NotImplementedError

    # -- internals ------------------------------------------------------------

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
        output = buf[:idx].decode("utf-8", errors="replace").replace("\r\n", "\n")
        return output, exit_code

    def _on_timeout(self, buf: bytes) -> CommandResult:
        self._terminate_runaway()
        buf += self._drain(0.3)
        output = buf.decode("utf-8", errors="replace").replace("\r\n", "\n")
        return CommandResult(output=_truncate_middle(output), exit_code=None, timed_out=True)

    def _kill_process_group(self, sig: int) -> None:
        assert self._proc is not None
        try:
            os.killpg(os.getpgid(self._proc.pid), sig)
        except (ProcessLookupError, OSError):
            pass

    def _write(self, data: str) -> None:
        assert self._master_fd is not None
        os.write(self._master_fd, data.encode())

    def _read(self) -> bytes:
        assert self._master_fd is not None
        try:
            return os.read(self._master_fd, 65536)
        except OSError:
            return b""

    def _drain(self, timeout_s: float) -> bytes:
        if self._master_fd is None:
            return b""
        out = b""
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            ready, _, _ = select.select([self._master_fd], [], [], 0.1)
            if not ready:
                continue
            chunk = self._read()
            if not chunk:
                break
            out += chunk
        return out


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

    def _terminate_runaway(self) -> None:
        """Kill the foreground command's process group, never the shell itself.

        Job control (``set -m``) puts the external command in its own
        foreground process group that owns the tty; signalling that group
        leaves ``bash`` (the session leader) alive with its state intact.
        """
        assert self._master_fd is not None and self._proc is not None
        try:
            foreground = os.tcgetpgrp(self._master_fd)
        except OSError:
            return
        shell_pgid = os.getpgid(self._proc.pid)
        if foreground <= 0 or foreground == shell_pgid:
            return  # only the shell is in the foreground — nothing to terminate
        for sig in (signal.SIGINT, signal.SIGKILL):
            try:
                os.killpg(foreground, sig)
            except ProcessLookupError:
                return
            time.sleep(0.2)


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
    client and restart the in-container session to regain a clean prompt; the
    container may briefly hold the orphaned command. This is a documented
    limitation — subsequent commands still run, but session state accumulated
    before a timed-out command is lost (unlike the local engine, which
    preserves it).
    """

    def __init__(self, container_name: str) -> None:
        super().__init__()
        self._container_name = container_name

    @property
    def container_name(self) -> str:
        return self._container_name

    def _popen(self, cwd: str | None, slave_fd: int) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            ["docker", "exec", "-i", self._container_name, "bash", "--norc", "--noprofile"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
            close_fds=True,
        )

    def _seed_cwd(self, cwd: str | None) -> None:
        if cwd:
            self.run(f"cd {shlex.quote(cwd)} 2>/dev/null || true", timeout_s=_OPEN_TIMEOUT_S)

    def _terminate_runaway(self) -> None:
        """Kill the host ``docker exec`` client and restart the in-container session.

        The runaway command may briefly survive as an in-container orphan (see
        the class docstring); killing the client and reopening restores a usable
        prompt for the next command.
        """
        self._kill_process_group(signal.SIGKILL)
        if self._proc is not None:
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
        self._spawn(self._cwd)
        self._write("set -m; PS1=''; PS2=''\n")
        self.run(":", timeout_s=_OPEN_TIMEOUT_S)
        self._seed_cwd(self._cwd)


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
