"""Pure helpers for the persistent tmux terminal — no I/O, no tmux, no Docker.

Everything here is a total function over plain values so the parts of the
terminal that are easy to get wrong (capture-range arithmetic, keystroke
batching, probe parsing, observation rendering) can be tested without a
terminal.

=========================================================================
THIRD-PARTY ATTRIBUTION — THIS FILE HAS BEEN MODIFIED
=========================================================================
Portions of this file are derived from ``harbor/agents/terminus_2/
tmux_session.py`` in harbor 0.23.0, Copyright the harbor authors, licensed
under the Apache License, Version 2.0. Upstream ships no NOTICE file. A copy
of the Apache License, Version 2.0 is at the repository root (``LICENSE``);
tolokaforge is distributed under the same licence.

This file is a MODIFIED derivative. Changes from the original:

- Extracted the tmux-message-size model, the key-batching loop, the
  base64 paste-buffer staging, and the observation headers out of the
  ``TmuxSession`` class into free functions with no ``self`` and no
  ``await``, so they are unit-testable without a container.
- Key delivery returns an explicit plan of ``SendKeysBatch`` / ``PasteKey``
  steps over **argv lists** rather than building shell command strings;
  callers exec argv directly, so no shell parses the payload.
- Replaced upstream ``_find_new_content`` — which substring-scanned the whole
  scrollback and then sliced ``current_buffer`` at an index computed inside
  the *previous* buffer — with :func:`plan_capture`, content-free arithmetic
  over tmux line watermarks.
- Added scrollback-eviction detection and an explicit marker; upstream
  silently degraded to the visible screen.
- Added :class:`PaneStatus` / :func:`parse_pane_probe` /
  :func:`format_status_line`; upstream had no status surface at all.
"""

from __future__ import annotations

import base64
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "CURRENT_SCREEN_HEADER",
    "EVICTED_MARKER",
    "NEW_OUTPUT_HEADER",
    "PASTE_BASE64_CHUNK_LEN",
    "PROBE_FORMAT",
    "SEND_KEYS_MAX_COMMAND_BYTES",
    "CaptureKind",
    "CapturePlan",
    "PaneStatus",
    "PaneWatermark",
    "PasteKey",
    "PastePlan",
    "SendKeysBatch",
    "format_status_line",
    "plan_capture",
    "plan_key_delivery",
    "plan_paste",
    "parse_pane_probe",
    "render_observation",
    "trim_trailing_blank_lines",
]


# tmux rejects a client command above its internal message buffer (~16 KB
# since tmux 1.9, https://github.com/tmux/tmux/issues/254) with "command too
# long". Stay under that ceiling; keys that cannot fit go through a paste
# buffer instead (see :func:`plan_paste`).
SEND_KEYS_MAX_COMMAND_BYTES = 16_000

# Base64 characters staged per exec when writing a paste-buffer file. The exec
# does not go through tmux, so the bound is the shell's ARG_MAX; stay far below
# it while keeping round-trips few.
PASTE_BASE64_CHUNK_LEN = 65_536

# tmux format string for the out-of-band pane probe. Ordering is load-bearing:
# :func:`parse_pane_probe` splits the last three fields off the right so a
# command name containing "|" cannot shift the numeric fields.
PROBE_FORMAT = "#{pane_current_command}|#{alternate_on}|#{history_size}|#{cursor_y}"

NEW_OUTPUT_HEADER = "New Terminal Output:"
CURRENT_SCREEN_HEADER = "Current Terminal Screen:"

EVICTED_MARKER = (
    "[terminal] scrollback is full (history-limit reached) — output older than the "
    "retained history has been dropped and cannot be recovered. Redirect long-running "
    "output to a file and read it back instead of scrolling it through the pane."
)

UNKNOWN = "?"

# Process names that mean "the pane is sitting at a shell prompt", not running
# a foreground command. A login shell reports as "bash"; some builds prefix a
# dash for a login shell.
SHELL_COMMANDS = frozenset(
    {"bash", "-bash", "sh", "-sh", "dash", "zsh", "-zsh", "ksh", "fish", "ash"}
)


# --------------------------------------------------------------------------
# Capture-range arithmetic
# --------------------------------------------------------------------------


class CaptureKind(str, Enum):
    """Why a given capture range was chosen."""

    FIRST = "first"
    """No previous watermark — show the visible screen."""

    IDLE = "idle"
    """The pane produced no new lines since the last capture."""

    RESET = "reset"
    """Scrollback shrank (``clear-history``, or a new pane) — restart tracking."""

    GROWTH = "growth"
    """New lines are retained and addressable; capture exactly those."""

    EVICTED = "evicted"
    """Scrollback saturated; some output is unrecoverable."""


@dataclass(frozen=True)
class PaneWatermark:
    """Where the pane's write cursor stood at one point in time.

    ``history_size`` is tmux's ``#{history_size}`` — lines currently held in
    scrollback above the visible pane. ``cursor_line`` is ``#{cursor_y}``, the
    cursor's row within the visible pane. Their sum is a line counter that
    advances both when output scrolls *and* when it merely fills an unfilled
    pane, which ``history_size`` alone does not.
    """

    history_size: int
    cursor_line: int

    @property
    def absolute_line(self) -> int:
        return self.history_size + self.cursor_line


@dataclass(frozen=True)
class CapturePlan:
    """The ``capture-pane -S`` argument to use, and why."""

    kind: CaptureKind
    start_line: int
    new_lines: int

    @property
    def captures_new_output(self) -> bool:
        """Whether this plan addresses new output rather than the whole screen."""
        return self.kind in (CaptureKind.GROWTH, CaptureKind.EVICTED)

    @property
    def evicted(self) -> bool:
        return self.kind is CaptureKind.EVICTED


def plan_capture(
    previous: PaneWatermark | None,
    current: PaneWatermark,
    pane_height: int,
    history_limit: int,
) -> CapturePlan:
    """Decide which pane lines are new since *previous*.

    tmux addresses pane lines relative to the top of the visible pane: line 0
    is the first visible row, negative values reach into scrollback, and
    ``-history_size`` is the oldest retained line. The returned
    :attr:`CapturePlan.start_line` is in that coordinate system and pairs with
    ``capture-pane -E -`` (through the bottom of the visible pane).

    The first captured line is deliberately the row the cursor occupied at
    *previous*: that row may have been written only partially last time, so
    re-emitting it is what makes a half-written prompt line come out whole.

    Saturated scrollback (``history_size >= history_limit``) means tmux is
    dropping the oldest lines as new ones arrive, so a watermark can no longer
    be trusted to address them. That is reported as
    :attr:`CaptureKind.EVICTED` — everything retained is captured and the
    caller prepends :data:`EVICTED_MARKER` — rather than silently degrading.
    """
    if pane_height <= 0:
        raise ValueError(f"pane_height must be positive, got {pane_height!r}")
    if history_limit <= 0:
        raise ValueError(f"history_limit must be positive, got {history_limit!r}")

    if previous is None:
        return CapturePlan(kind=CaptureKind.FIRST, start_line=0, new_lines=0)

    if current.history_size >= history_limit:
        retained = current.history_size + pane_height
        return CapturePlan(
            kind=CaptureKind.EVICTED,
            start_line=-current.history_size,
            new_lines=retained,
        )

    if current.history_size < previous.history_size:
        return CapturePlan(kind=CaptureKind.RESET, start_line=0, new_lines=0)

    new_lines = current.absolute_line - previous.absolute_line
    if new_lines <= 0:
        return CapturePlan(kind=CaptureKind.IDLE, start_line=0, new_lines=0)

    start_line = current.cursor_line - new_lines
    start_line = max(start_line, -current.history_size)
    start_line = min(start_line, pane_height - 1)
    return CapturePlan(kind=CaptureKind.GROWTH, start_line=start_line, new_lines=new_lines)


# --------------------------------------------------------------------------
# Pane status
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PaneStatus:
    """One out-of-band read of what the pane is doing."""

    foreground: str
    alternate_screen: bool
    watermark: PaneWatermark
    last_exit: int | None

    @property
    def idle(self) -> bool:
        """Whether the pane is sitting at a shell prompt.

        A full-screen application (``vim``, ``less``) holds the alternate
        screen even though ``pane_current_command`` may still name a shell, so
        alternate-screen state vetoes idleness.
        """
        return not self.alternate_screen and self.foreground in SHELL_COMMANDS


def parse_pane_probe(stdout: str) -> PaneStatus | None:
    """Parse the combined probe stdout, or ``None`` when it is unusable.

    Line 1 is :data:`PROBE_FORMAT` expanded by ``tmux display-message -p -F``.
    Line 2, when present, is the last exit code written by the
    ``PROMPT_COMMAND`` installed at session open; it is absent while a
    foreground command is running and before the first prompt is drawn.

    Returning ``None`` rather than raising is deliberate: a dead session, a
    tmux that printed an error, and a pane that has not drawn a prompt yet all
    degrade to a ``?`` status line instead of failing the observation.
    """
    lines = stdout.splitlines()
    if not lines:
        return None

    parts = lines[0].strip().rsplit("|", 3)
    if len(parts) != 4:
        return None

    foreground, alternate, history, cursor = (part.strip() for part in parts)
    if not foreground:
        return None
    try:
        history_size = int(history)
        cursor_line = int(cursor)
    except ValueError:
        return None
    if history_size < 0 or cursor_line < 0:
        return None

    return PaneStatus(
        foreground=foreground,
        alternate_screen=alternate == "1",
        watermark=PaneWatermark(history_size=history_size, cursor_line=cursor_line),
        last_exit=_parse_exit_code(lines[1] if len(lines) > 1 else ""),
    )


def _parse_exit_code(line: str) -> int | None:
    token = line.strip()
    if not token or not token.lstrip("-").isdigit():
        return None
    return int(token)


def format_status_line(status: PaneStatus | None, elapsed_s: float) -> str:
    """Render the one-line terminal status an observation ends with.

    Without this line, an agent polling a long build sees a byte-identical
    screen every turn; the stuck detector hashes those into one signature and
    forces the trial's score to zero. ``elapsed`` alone makes every poll
    distinct, and ``foreground`` / ``idle`` tell the model whether waiting is
    the right move.
    """
    elapsed = f"{max(elapsed_s, 0.0):.1f}s"
    if status is None:
        return (
            f"[terminal] foreground={UNKNOWN} idle={UNKNOWN} last_exit={UNKNOWN} elapsed={elapsed}"
        )
    last_exit = UNKNOWN if status.last_exit is None else str(status.last_exit)
    line = (
        f"[terminal] foreground={status.foreground} "
        f"idle={'yes' if status.idle else 'no'} "
        f"last_exit={last_exit} elapsed={elapsed}"
    )
    if status.alternate_screen:
        line += " alternate_screen=yes"
    return line


# --------------------------------------------------------------------------
# Observation rendering
# --------------------------------------------------------------------------


def trim_trailing_blank_lines(text: str) -> str:
    """Drop the blank rows ``capture-pane`` pads an under-filled pane with."""
    lines = text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def render_observation(
    *,
    new_output: str,
    screen: str,
    status_line: str,
    evicted: bool = False,
) -> str:
    """Assemble the observation a tool call returns.

    Header selection follows the upstream contract: new output since the last
    call is shown under :data:`NEW_OUTPUT_HEADER`, and when there is none the
    current visible screen is shown under :data:`CURRENT_SCREEN_HEADER` — the
    model always gets *something* addressable. The status line is appended
    last and is never truncated; callers cap the body before calling.
    """
    sections: list[str] = []
    if evicted:
        sections.append(EVICTED_MARKER)
    if new_output.strip():
        sections.append(f"{NEW_OUTPUT_HEADER}\n{new_output}")
    else:
        sections.append(f"{CURRENT_SCREEN_HEADER}\n{screen}")
    sections.append(status_line)
    return "\n".join(sections)


# --------------------------------------------------------------------------
# Keystroke delivery
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SendKeysBatch:
    """One ``tmux send-keys`` invocation, as argv."""

    argv: tuple[str, ...]


@dataclass(frozen=True)
class PasteKey:
    """A key too large for a send-keys command; stage it via a paste buffer."""

    key: str


@dataclass(frozen=True)
class PastePlan:
    """Shell steps that stage one oversized key and paste it into the pane."""

    path: str
    stage_commands: tuple[str, ...]
    paste_command: str
    cleanup_command: str


def _utf8_len(text: str) -> int:
    """tmux measures message sizes in bytes, not code points."""
    return len(text.encode("utf-8"))


def _quoted_len(parts: Sequence[str]) -> int:
    return _utf8_len(" ".join(shlex.quote(part) for part in parts))


def plan_key_delivery(
    prefix: Sequence[str],
    keys: Sequence[str],
    max_command_bytes: int = SEND_KEYS_MAX_COMMAND_BYTES,
) -> list[SendKeysBatch | PasteKey]:
    """Split *keys* into send-keys batches, pasting any key that cannot fit.

    *prefix* is the argv up to and including ``--`` (for example
    ``["tmux", "-L", "sock", "send-keys", "-t", "trial", "--"]``); the ``--``
    is what keeps a key such as ``-n`` from being read as an option.

    Sizing uses shell-quoted lengths even though callers exec argv directly
    and never hand the payload to a shell: quoted length is a strict upper
    bound on what tmux's own message buffer has to hold, so the bound stays
    conservative on every tmux build.
    """
    prefix = list(prefix)
    prefix_len = _quoted_len(prefix)
    max_key_len = max_command_bytes - prefix_len - 1
    if max_key_len <= 0:
        raise ValueError(
            f"max_command_bytes={max_command_bytes} leaves no room for keys after the "
            f"{prefix_len}-byte send-keys prefix"
        )

    plan: list[SendKeysBatch | PasteKey] = []
    batch: list[str] = []
    batch_len = prefix_len

    def flush() -> None:
        nonlocal batch, batch_len
        if batch:
            plan.append(SendKeysBatch(argv=tuple(prefix + batch)))
            batch = []
            batch_len = prefix_len

    for key in keys:
        key_len = _utf8_len(shlex.quote(key))
        if key_len > max_key_len:
            flush()
            plan.append(PasteKey(key=key))
            continue
        addition = 1 + key_len  # separating space + the quoted key
        if batch and batch_len + addition > max_command_bytes:
            flush()
        batch.append(key)
        batch_len += addition

    flush()
    return plan


def plan_paste(
    tmux_prefix: Sequence[str],
    session_name: str,
    key: str,
    token: str,
    *,
    tmp_dir: str = "/tmp",
    chunk_len: int = PASTE_BASE64_CHUNK_LEN,
) -> PastePlan:
    """Build the shell steps that deliver an oversized *key* via a paste buffer.

    ``send-keys`` rejects a command above tmux's message size limit, which
    loses long literal payloads such as a heredoc answer. The payload is
    instead base64-staged into a file (that exec does not go through tmux, so
    the limit does not apply) and pasted with ``load-buffer`` /
    ``paste-buffer``. ``paste-buffer -d`` deletes the buffer afterwards, and
    its default newline-to-carriage-return conversion makes a multi-line
    payload behave as if typed line by line.
    """
    if chunk_len <= 0:
        raise ValueError(f"chunk_len must be positive, got {chunk_len!r}")

    path = f"{tmp_dir.rstrip('/')}/.tolokaforge-tmux-paste-{token}"
    quoted_path = shlex.quote(path)
    buffer_name = f"tolokaforge-paste-{token}"
    payload = base64.b64encode(key.encode("utf-8")).decode("ascii")

    stage_commands = tuple(
        f"printf %s {shlex.quote(payload[offset : offset + chunk_len])} "
        f"| base64 -d {'>>' if offset else '>'} {quoted_path}"
        for offset in range(0, max(len(payload), 1), chunk_len)
    )
    tmux = " ".join(shlex.quote(part) for part in tmux_prefix)
    paste_command = (
        f"{tmux} load-buffer -b {shlex.quote(buffer_name)} {quoted_path} && "
        f"{tmux} paste-buffer -d -b {shlex.quote(buffer_name)} -t {shlex.quote(session_name)}"
    )
    return PastePlan(
        path=path,
        stage_commands=stage_commands,
        paste_command=paste_command,
        cleanup_command=f"rm -f {quoted_path}",
    )
