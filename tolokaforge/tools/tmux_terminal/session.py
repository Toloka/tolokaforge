"""A persistent tmux terminal the agent types into, for one trial.

Three roles live here, mirroring :mod:`tolokaforge.tools.persistent_shell`:

- :class:`TmuxBackend` is the *seam*: where tmux runs. :class:`LocalTmuxBackend`
  runs it on the host; :class:`DockerExecTmuxBackend` execs into an
  already-running container.
- :class:`TmuxTerminalSession` is the *engine*: one long-lived
  ``tmux new-session … 'bash --login'`` held for the trial, keystrokes in and
  new terminal output back.
- :mod:`tolokaforge.tools.tmux_terminal.pure` holds every decision that does
  not need a terminal.

Why a terminal rather than another one-shot ``bash(command)`` tool: a held
pane lets a model drive ``vim``, sit in a REPL, send ``C-c``, and answer an
interactive prompt. Keys are sent verbatim with tmux key-name semantics, so
``C-c`` and ``C-d`` mean the control characters and a trailing ``\\n`` (or a
literal ``Enter`` key) is what actually executes a command — typing without
one leaves the line pending, which is the point.

=========================================================================
THIRD-PARTY ATTRIBUTION — THIS FILE HAS BEEN MODIFIED
=========================================================================
Derived from ``harbor/agents/terminus_2/tmux_session.py`` in harbor 0.23.0,
Copyright the harbor authors, licensed under the Apache License, Version 2.0.
Upstream ships no NOTICE file. A copy of the Apache License, Version 2.0 is at
the repository root (``LICENSE``); tolokaforge is distributed under the same
licence.

This file is a MODIFIED derivative. Changes from the original:

- **Incremental output is a line watermark, not a substring scan.** Upstream
  ``_find_new_content`` re-captured the entire scrollback on every call and
  located the previous buffer inside it with ``str.index`` — an O(n·m) scan of
  a multi-megabyte string — then *overwrote* that index with
  ``previous_buffer.rfind("\\n")``, an offset into a different string. It
  produced the right slice only because the previous buffer happens to be a
  prefix in practice. Replaced with :func:`~.pure.plan_capture` over
  ``#{history_size}`` / ``#{cursor_y}``, capturing only the new line range.
- **Scrollback eviction is reported, not hidden.** Upstream fell back to the
  visible screen whenever it could not locate the previous buffer, so dropped
  output looked like no output. Saturated scrollback now emits
  :data:`~.pure.EVICTED_MARKER`. ``history-limit`` is a bounded 20,000 lines
  rather than upstream's 10,000,000.
- **tmux is verified, never installed.** Upstream shelled out to apt/dnf/apk,
  fell back to building tmux from source, bounded the whole thing at 240s and
  *continued silently on failure*. Task images here run with networking
  disabled, so :meth:`TmuxTerminalSession.open` verifies ``tmux -V`` once and
  raises :class:`TmuxUnavailableError` naming the image. All install,
  package-manager-detection and build-from-source code is removed.
- **A status line.** Foreground command, idle, last exit code and elapsed,
  probed out-of-band. Upstream had none, so an agent polling a long build got
  a byte-identical observation every turn.
- **Synchronous, subprocess-based, private tmux server.** The async
  ``BaseEnvironment`` dependency is replaced by the :class:`TmuxBackend`
  Protocol; every tmux invocation is argv on a per-session ``-L`` socket, so
  the session cannot see or disturb any other tmux server on the host.
- **Output is capped** through
  :func:`~tolokaforge.core.tool_output_truncation.keep_head_and_tail`.
- Asciinema recording, marker merging, and the blocking ``tmux wait -S done``
  mode are dropped; they have no consumer here.
"""

from __future__ import annotations

import shlex
import subprocess
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from tolokaforge.core.tool_output_truncation import keep_head_and_tail
from tolokaforge.tools.tmux_terminal.pure import (
    PROBE_FORMAT,
    CaptureKind,
    CapturePlan,
    PaneStatus,
    PaneWatermark,
    PasteKey,
    SendKeysBatch,
    format_status_line,
    parse_pane_probe,
    plan_capture,
    plan_key_delivery,
    plan_paste,
    render_observation,
    trim_trailing_blank_lines,
)

__all__ = [
    "DockerExecTmuxBackend",
    "ExecResult",
    "LocalTmuxBackend",
    "TmuxBackend",
    "TmuxSessionError",
    "TmuxTerminalSession",
    "TmuxUnavailableError",
]

DEFAULT_PANE_WIDTH = 160
DEFAULT_PANE_HEIGHT = 40

# Bounded on purpose. Scrollback is agent-visible context, not an archive: at
# 160 columns, 20,000 lines is a few megabytes the capture arithmetic can
# address cheaply, and output beyond it belongs in a file the agent reads back.
DEFAULT_HISTORY_LIMIT = 20_000

DEFAULT_MAX_OUTPUT_CHARS = 16_384

_EXEC_TIMEOUT_S = 30.0
_READY_TIMEOUT_S = 15.0


class TmuxSessionError(RuntimeError):
    """A tmux command the session depends on failed."""


class TmuxUnavailableError(TmuxSessionError):
    """tmux is not present where the session must run."""


@dataclass(frozen=True)
class ExecResult:
    """Outcome of one command run by a :class:`TmuxBackend`."""

    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@runtime_checkable
class TmuxBackend(Protocol):
    """Where tmux runs.

    Implementations differ only in *how* a command reaches the machine holding
    the pane; every tmux invocation the session makes goes through
    :meth:`exec`.
    """

    @property
    def location(self) -> str:
        """Human-readable target, for error messages that must be actionable."""
        ...

    def exec(self, argv: Sequence[str], timeout_s: float = _EXEC_TIMEOUT_S) -> ExecResult:
        """Run *argv* with no shell and return its outcome."""
        ...

    def exec_shell(self, script: str, timeout_s: float = _EXEC_TIMEOUT_S) -> ExecResult:
        """Run *script* under ``sh -c``, for the few steps that need a shell."""
        ...


class _SubprocessTmuxBackend:
    """Shared subprocess plumbing; subclasses only supply the argv prefix."""

    def _prefix(self) -> list[str]:
        raise NotImplementedError

    @property
    def location(self) -> str:
        raise NotImplementedError

    def exec(self, argv: Sequence[str], timeout_s: float = _EXEC_TIMEOUT_S) -> ExecResult:
        full = [*self._prefix(), *argv]
        try:
            completed = subprocess.run(
                full,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ExecResult(
                returncode=124,
                stdout="",
                stderr=f"command timed out after {timeout_s}s: {shlex.join(full)}",
            )
        except OSError as exc:
            return ExecResult(returncode=127, stdout="", stderr=str(exc))
        return ExecResult(
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )

    def exec_shell(self, script: str, timeout_s: float = _EXEC_TIMEOUT_S) -> ExecResult:
        return self.exec(["sh", "-c", script], timeout_s=timeout_s)


class LocalTmuxBackend(_SubprocessTmuxBackend):
    """Runs tmux on the host process's own machine."""

    def _prefix(self) -> list[str]:
        return []

    @property
    def location(self) -> str:
        return "this host"


class DockerExecTmuxBackend(_SubprocessTmuxBackend):
    """Runs tmux inside an already-running container via ``docker exec``.

    Targets a container someone else brought up; this backend only execs into
    it, it never starts or stops the stack. *image* is carried purely so
    :class:`TmuxUnavailableError` can name the image that needs ``tmux``
    installed at build time.
    """

    def __init__(
        self,
        container_name: str,
        *,
        image: str | None = None,
        user: str | None = None,
    ) -> None:
        self._container_name = container_name
        self._image = image
        self._user = user

    @property
    def container_name(self) -> str:
        return self._container_name

    @property
    def image(self) -> str | None:
        return self._image

    @property
    def user(self) -> str | None:
        return self._user

    def _prefix(self) -> list[str]:
        # ``--user`` before ``-i``: matches ``docker exec --help`` order and the
        # argv shape in persistent_shell.py / str_replace_editor.py.
        argv = ["docker", "exec"]
        if self._user is not None:
            argv.extend(["--user", self._user])
        argv.extend(["-i", self._container_name])
        return argv

    @property
    def location(self) -> str:
        if self._image:
            return f"container {self._container_name!r} (image {self._image!r})"
        return f"container {self._container_name!r}"


class TmuxTerminalSession:
    """One long-lived tmux pane held for the duration of a trial.

    Lifecycle mirrors :class:`~tolokaforge.tools.persistent_shell.BashSession`:
    :meth:`open` once per trial, :meth:`send_keys` / :meth:`observe` per turn,
    :meth:`close` at teardown. Working directory, environment, shell history,
    and any full-screen application the agent started all persist across calls,
    because it is literally the same shell the whole time.
    """

    def __init__(
        self,
        backend: TmuxBackend,
        *,
        session_name: str | None = None,
        pane_width: int = DEFAULT_PANE_WIDTH,
        pane_height: int = DEFAULT_PANE_HEIGHT,
        history_limit: int = DEFAULT_HISTORY_LIMIT,
        env: Mapping[str, str] | None = None,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    ) -> None:
        if pane_width <= 0 or pane_height <= 0:
            raise ValueError(
                f"pane_width and pane_height must be positive, got {pane_width!r}x{pane_height!r}"
            )
        if history_limit <= 0:
            raise ValueError(f"history_limit must be positive, got {history_limit!r}")

        self._backend = backend
        self._session_name = session_name or f"tolokaforge-{uuid.uuid4().hex[:12]}"
        self._socket_name = f"tf-{self._session_name}"
        self._pane_width = pane_width
        self._pane_height = pane_height
        self._history_limit = history_limit
        self._env = dict(env or {})
        self._max_output_chars = max_output_chars

        self._log_path = f"/tmp/{self._session_name}.log"
        self._exit_code_path = f"/tmp/.{self._session_name}.rc"
        self._config_path = f"/tmp/.{self._session_name}.tmux.conf"
        self._watermark: PaneWatermark | None = None
        self._last_keystroke_at: float | None = None
        self._open = False

    # -- properties -----------------------------------------------------------

    @property
    def session_name(self) -> str:
        return self._session_name

    @property
    def log_path(self) -> str:
        """In-environment path ``pipe-pane`` writes the raw pane stream to."""
        return self._log_path

    @property
    def pane_height(self) -> int:
        return self._pane_height

    @property
    def is_open(self) -> bool:
        return self._open

    # -- lifecycle ------------------------------------------------------------

    def verify_tmux(self) -> str:
        """Return the tmux version, or raise with an actionable message.

        Deliberately not an install path. Task images run with networking
        disabled, so a missing tmux cannot be fixed at run time and pretending
        otherwise turns a build-time packaging bug into a silent capability
        loss halfway through a trial.
        """
        result = self._backend.exec(["tmux", "-V"])
        if result.ok:
            return result.stdout.strip()
        raise TmuxUnavailableError(
            f"tmux is not available on {self._backend.location}: `tmux -V` exited "
            f"{result.returncode} ({(result.stderr or result.stdout).strip()!r}). "
            "The persistent terminal needs tmux present at image build time — task "
            "containers run with networking disabled, so it cannot be installed on "
            "demand. Add tmux to that image's package install step."
        )

    def open(self, cwd: str | None = None) -> None:
        """Start the pane and make it ready to receive keystrokes."""
        if self._open:
            raise TmuxSessionError(f"tmux session {self._session_name!r} is already open")

        self.verify_tmux()

        # ``history-limit`` is applied when a pane is created, so it has to be
        # in place before ``new-session`` rather than set afterwards. A config
        # file read at server start is the only ordering tmux offers. The
        # private ``-L`` socket is what makes a global option safe here: this
        # server holds only our session.
        self._shell_or_raise(
            f"printf '%s\\n' {shlex.quote(f'set -g history-limit {self._history_limit}')} "
            f"> {shlex.quote(self._config_path)}",
            "write the tmux config",
        )

        new_session = [
            "-f",
            self._config_path,
            "new-session",
            "-d",
            "-s",
            self._session_name,
            "-x",
            str(self._pane_width),
            "-y",
            str(self._pane_height),
            "-e",
            "TERM=xterm-256color",
            "-e",
            "SHELL=/bin/bash",
        ]
        for key, value in self._env.items():
            new_session.extend(["-e", f"{key}={value}"])
        new_session.append("bash --login")
        self._tmux_or_raise(new_session, "start the tmux session")
        self._open = True

        try:
            self._tmux_or_raise(
                ["pipe-pane", "-t", self._session_name, f"cat > {shlex.quote(self._log_path)}"],
                "start pane logging",
            )
            self._await_pane()
            self._install_prompt_command()
            if cwd:
                self.type_keys([f"cd {shlex.quote(cwd)}", "Enter"])
                time.sleep(0.2)
            self.type_keys(["clear", "Enter"])
            time.sleep(0.3)
        except Exception:
            self.close()
            raise

        # Drop the setup keystrokes: the trial's first observation should be the
        # cleared screen, not our own bookkeeping.
        self._watermark = None

    def close(self) -> None:
        """Kill the private tmux server and release the pane.

        The ``pipe-pane`` log at :attr:`log_path` is left in place: it is the
        raw transcript of everything the pane ever showed, and a caller
        collecting trial artefacts needs it to outlive the session.
        """
        if not self._open:
            return
        self._tmux(["kill-server"])
        self._backend.exec_shell(
            f"rm -f {shlex.quote(self._exit_code_path)} {shlex.quote(self._config_path)}"
        )
        self._open = False
        self._watermark = None

    # -- interaction ----------------------------------------------------------

    def type_keys(self, keys: str | Sequence[str]) -> None:
        """Deliver *keys* to the pane without consuming an observation.

        Keys carry tmux ``send-keys`` semantics: ``C-c`` and ``C-d`` are the
        control characters, ``Enter`` submits, and any other string is typed
        literally. A command without a trailing ``Enter`` (or ``\\n``) is left
        pending on the command line rather than executed — that is what lets a
        model fill in an interactive prompt or type into a full-screen editor.

        A key too large for one ``send-keys`` command is staged through a paste
        buffer instead of being dropped.
        """
        self._require_open()
        key_list = [keys] if isinstance(keys, str) else list(keys)
        for step in plan_key_delivery(self._send_keys_prefix(), key_list):
            if isinstance(step, SendKeysBatch):
                self._exec_or_raise(list(step.argv), "send keys")
            elif isinstance(step, PasteKey):
                self._paste(step.key)
        self._last_keystroke_at = time.monotonic()

    def send_keys(self, keys: str | Sequence[str], *, min_wait_s: float = 0.0) -> str:
        """Type *keys*, wait *min_wait_s*, and return the resulting observation.

        The wait is a floor, not a completion guarantee: a command that is
        still running when it expires reports itself through the status line
        rather than blocking the turn. Callers that need the command to have
        finished pair :meth:`type_keys` with :meth:`wait_until_idle`.
        """
        self.type_keys(keys)
        if min_wait_s > 0:
            time.sleep(min_wait_s)
        return self.observe()

    def observe(self) -> str:
        """Return new terminal output since the last observation, plus status."""
        self._require_open()
        status = self._probe()
        plan = self._plan(status)

        new_output = ""
        if plan.captures_new_output:
            new_output = trim_trailing_blank_lines(self._capture(plan.start_line))
        show_new = bool(new_output.strip())
        screen = "" if show_new else trim_trailing_blank_lines(self._capture(None))

        if status is not None:
            self._watermark = status.watermark

        body, _ = keep_head_and_tail(new_output if show_new else screen, self._max_output_chars)
        return render_observation(
            new_output=body if show_new else "",
            screen="" if show_new else body,
            status_line=format_status_line(status, self._elapsed_s()),
            evicted=plan.evicted,
        )

    def status(self) -> PaneStatus | None:
        """One out-of-band pane probe, or ``None`` when it could not be read."""
        self._require_open()
        return self._probe()

    def wait_until_idle(self, timeout_s: float, *, poll_s: float = 0.2) -> bool:
        """Poll until the pane is back at a shell prompt, or *timeout_s* passes.

        Returns immediately if the pane is already idle, so a caller that has
        just typed a command should let the shell pick it up first.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            status = self._probe()
            if status is not None and status.idle:
                return True
            time.sleep(poll_s)
        return False

    # -- internals ------------------------------------------------------------

    def _require_open(self) -> None:
        if not self._open:
            raise TmuxSessionError(f"tmux session {self._session_name!r} is not open")

    def _tmux_prefix(self) -> list[str]:
        return ["tmux", "-L", self._socket_name]

    def _send_keys_prefix(self) -> list[str]:
        # ``--`` marks the end of options so a key such as ``-n`` is typed, not parsed.
        return [*self._tmux_prefix(), "send-keys", "-t", self._session_name, "--"]

    def _tmux(self, argv: Sequence[str], timeout_s: float = _EXEC_TIMEOUT_S) -> ExecResult:
        return self._backend.exec([*self._tmux_prefix(), *argv], timeout_s=timeout_s)

    def _tmux_or_raise(self, argv: Sequence[str], action: str) -> ExecResult:
        return self._exec_or_raise([*self._tmux_prefix(), *argv], action)

    def _exec_or_raise(self, argv: Sequence[str], action: str) -> ExecResult:
        result = self._backend.exec(argv)
        if result.ok:
            return result
        raise TmuxSessionError(
            f"failed to {action} for tmux session {self._session_name!r} on "
            f"{self._backend.location}: exit {result.returncode}, "
            f"stderr={result.stderr.strip()!r}, stdout={result.stdout.strip()!r}"
        )

    def _await_pane(self) -> None:
        """Block until the login shell has drawn something on the pane.

        A pane reports ``pane_current_command`` as its shell from the instant
        it exists, well before that shell has finished sourcing its login
        files and is reading input — keys typed into the gap are echoed by the
        tty and then discarded when the shell initialises its terminal. Waiting
        for the pane to be non-blank waits for evidence the shell actually
        produced output, which is the earliest point typing is safe.
        """
        deadline = time.monotonic() + _READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._capture(None).strip():
                return
            time.sleep(0.1)
        raise TmuxSessionError(
            f"tmux session {self._session_name!r} on {self._backend.location} drew no "
            f"shell prompt within {_READY_TIMEOUT_S}s"
        )

    def _install_prompt_command(self) -> None:
        """Record every command's exit status where the probe can read it.

        ``PROMPT_COMMAND`` runs just before each prompt is drawn, so the file
        holds the status of the last command that finished. Reading it
        out-of-band keeps the exit code off the pane, where it would pollute
        the output the agent sees.

        The file appearing is also the session's real readiness signal: it can
        only be written by the shell, after the shell has read and run a line
        we typed. Until it shows up the assignment is retyped, because a first
        line typed into a shell still initialising its terminal is silently
        dropped.
        """
        quoted = shlex.quote(self._exit_code_path)
        self._backend.exec_shell(f"rm -f {quoted}")
        assignment = f'PROMPT_COMMAND=\'__tf_rc=$?; printf "%s\\n" "$__tf_rc" > {quoted}\''
        deadline = time.monotonic() + _READY_TIMEOUT_S
        while time.monotonic() < deadline:
            self.type_keys([assignment, "Enter"])
            time.sleep(0.3)
            if self._backend.exec_shell(f"test -f {quoted}").ok:
                return
        raise TmuxSessionError(
            f"the shell in tmux session {self._session_name!r} on "
            f"{self._backend.location} did not run a typed command within "
            f"{_READY_TIMEOUT_S}s (no exit-code file at {self._exit_code_path})"
        )

    def _probe(self) -> PaneStatus | None:
        tmux = " ".join(shlex.quote(part) for part in self._tmux_prefix())
        script = (
            f"{tmux} display-message -p -t {shlex.quote(self._session_name)} "
            f"-F {shlex.quote(PROBE_FORMAT)}; "
            f"cat {shlex.quote(self._exit_code_path)} 2>/dev/null"
        )
        result = self._backend.exec_shell(script)
        if not result.ok and not result.stdout:
            return None
        return parse_pane_probe(result.stdout)

    def _plan(self, status: PaneStatus | None) -> CapturePlan:
        if status is None:
            # No usable watermark — show the visible screen and keep the old
            # watermark so the next successful probe measures from a real point.
            return CapturePlan(kind=CaptureKind.FIRST, start_line=0, new_lines=0)
        return plan_capture(
            self._watermark,
            status.watermark,
            self._pane_height,
            self._history_limit,
        )

    def _capture(self, start_line: int | None) -> str:
        argv = ["capture-pane", "-p", "-J", "-t", self._session_name]
        if start_line is not None:
            argv.extend(["-S", str(start_line), "-E", "-"])
        result = self._tmux(argv)
        if not result.ok:
            raise TmuxSessionError(
                f"failed to capture the pane for tmux session {self._session_name!r} on "
                f"{self._backend.location}: exit {result.returncode}, "
                f"stderr={result.stderr.strip()!r}"
            )
        return result.stdout

    def _paste(self, key: str) -> None:
        plan = plan_paste(
            self._tmux_prefix(),
            self._session_name,
            key,
            uuid.uuid4().hex,
        )
        try:
            for command in plan.stage_commands:
                self._shell_or_raise(command, "stage an oversized keystroke")
            self._shell_or_raise(plan.paste_command, "paste an oversized keystroke")
        finally:
            self._backend.exec_shell(plan.cleanup_command)

    def _shell_or_raise(self, script: str, action: str) -> None:
        result = self._backend.exec_shell(script)
        if result.ok:
            return
        raise TmuxSessionError(
            f"failed to {action} for tmux session {self._session_name!r} on "
            f"{self._backend.location}: exit {result.returncode}, "
            f"stderr={result.stderr.strip()!r}"
        )

    def _elapsed_s(self) -> float:
        if self._last_keystroke_at is None:
            return 0.0
        return time.monotonic() - self._last_keystroke_at
