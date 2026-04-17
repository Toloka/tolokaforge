"""Subprocess execution helpers for dev-mcp server."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

# Default max output chars returned to the agent.
# Full output is always saved to a temp log file.
DEFAULT_MAX_OUTPUT = 20_000


def _verbose() -> bool:
    """Return True if DEV_MCP_VERBOSE is set to a truthy value (1, true, yes).

    When enabled, subprocess output is also echoed to stderr for live monitoring.
    """
    return os.environ.get("DEV_MCP_VERBOSE", "").strip().lower() in ("1", "true", "yes")


def _max_output_chars() -> int:
    """Read max output limit from env, falling back to default."""
    raw = os.environ.get("DEV_MCP_MAX_OUTPUT", "")
    if raw.strip().isdigit():
        return int(raw.strip())
    return DEFAULT_MAX_OUTPUT


def _make_log_path(tool_name: str) -> Path:
    """Create a predictable temp log file path."""
    ts = time.strftime("%Y%m%d_%H%M%S")
    # Use /tmp directly — these are ephemeral dev logs
    return Path(tempfile.gettempdir()) / f"dev_mcp_{tool_name}_{ts}_{os.getpid()}.log"


def _truncate(text: str, max_chars: int, log_path: Path) -> str:
    """Truncate text if it exceeds max_chars, appending a pointer to the full log."""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return (
        text[:half]
        + f"\n\n...<TRUNCATED>Full output is in {log_path}</TRUNCATED>...\n\n"
        + text[-half:]
    )


def format_result(
    *,
    exit_code: int | None,
    output: str,
    log_path: Path,
    timed_out: bool = False,
    timeout: int = 0,
) -> str:
    """Format the execution result into a structured text block."""
    max_chars = _max_output_chars()
    truncated_output = _truncate(output, max_chars, log_path)

    lines: list[str] = []
    if timed_out:
        lines.append(f"TIMED OUT after {timeout}s")
    else:
        lines.append(f"Exit code: {exit_code}")
    lines.append(f"Log: {log_path}")
    lines.append("")
    label = "output (partial)" if timed_out else "output"
    lines.append(f"--- {label} ---")
    lines.append(truncated_output)

    return "\n".join(lines)


async def _read_stream(
    stream: asyncio.StreamReader,
    log_file,
    chunks: list[bytes],
    *,
    echo_stderr: bool = False,
) -> None:
    """Read from *stream* line-by-line, tee-ing each line to *log_file* and *chunks*.

    When *echo_stderr* is True, also mirrors each line to stderr for live monitoring.
    This runs as a coroutine and returns when the stream reaches EOF.
    """
    while True:
        line = await stream.readline()
        if not line:
            break
        # Tee to log file (real-time)
        log_file.write(line)
        log_file.flush()
        # Optionally echo to stderr for live monitoring (stdout is MCP protocol channel)
        if echo_stderr:
            sys.stderr.buffer.write(line)
            sys.stderr.buffer.flush()
        # Accumulate for in-memory result
        chunks.append(line)


async def run_command(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: int = 120,
    env: dict[str, str] | None = None,
    tool_name: str = "command",
) -> str:
    """Execute a command and return a formatted result string.

    - Combines stdout and stderr into a single stream.
    - Tees output in real-time to a temp log file (and stderr when DEV_MCP_VERBOSE=1).
    - Truncates returned output at DEV_MCP_MAX_OUTPUT chars.
    """
    log_path = _make_log_path(tool_name)
    echo_stderr = _verbose()

    # Build environment: inherit current env + overlay
    run_env = dict(os.environ)
    if env:
        run_env.update(env)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,  # combine stderr into stdout
            cwd=str(cwd),
            env=run_env,
        )
    except FileNotFoundError as exc:
        # Command not found — return immediately
        error_msg = f"Command not found: {cmd[0]}\n{exc}"
        log_path.write_text(error_msg)
        return format_result(
            exit_code=127,
            output=error_msg,
            log_path=log_path,
        )

    timed_out = False
    chunks: list[bytes] = []

    try:
        with open(log_path, "wb") as log_file:
            try:
                await asyncio.wait_for(
                    _read_stream(proc.stdout, log_file, chunks, echo_stderr=echo_stderr),
                    timeout=timeout,
                )
                await proc.wait()
            except asyncio.TimeoutError:
                timed_out = True
                proc.kill()
                # Drain any remaining output after kill
                try:
                    remaining = await asyncio.wait_for(proc.stdout.read(), timeout=5)
                    if remaining:
                        log_file.write(remaining)
                        log_file.flush()
                        chunks.append(remaining)
                except (asyncio.TimeoutError, Exception):
                    pass
                await proc.wait()
    except Exception:
        # If we can't open the log file or something else goes wrong,
        # still try to return what we have
        pass

    output = b"".join(chunks).decode("utf-8", errors="replace")

    return format_result(
        exit_code=proc.returncode,
        output=output,
        log_path=log_path,
        timed_out=timed_out,
        timeout=timeout,
    )
