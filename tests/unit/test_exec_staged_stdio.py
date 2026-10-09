"""A command exec'd into the trial container leaves no pipe behind.

A service the agent starts with ``&`` inherits the command's stdio. Were that
the exec's own pipes, the engine would stop reading them once the exec
returned and the service's next writes would fail with EPIPE — fatal to a Go
or Node process. :func:`~tolokaforge.runner.tool_factory._docker_exec_plan`
therefore runs the command with its stdio staged in files inside the
container, relays the files on the exec's streams when the command finishes,
and reads them back if the exec is killed on its deadline.

The wrapper scripts are plain bash, so they are exercised here under a real
bash with the files on the host — the contract holds without Docker.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from tolokaforge.runner.tool_factory import (
    _STAGED_STDIO_READ_SCRIPT,
    _STAGED_STDIO_SCRIPT,
    _docker_exec_plan,
    _run_argv_preserving_partial_output,
)

pytestmark = pytest.mark.unit

_FD_KINDS = (
    "import os, stat\n"
    "kind = lambda fd: 'file' if stat.S_ISREG(os.fstat(fd).st_mode) else "
    "'chr' if stat.S_ISCHR(os.fstat(fd).st_mode) else 'pipe'\n"
)


def _staged_argv(tmp_path: Path, command: str) -> list[str]:
    out, err = tmp_path / "out", tmp_path / "err"
    return ["bash", "-c", _STAGED_STDIO_SCRIPT, "tolokaforge-exec", str(out), str(err), command]


def _read_back_argv(tmp_path: Path) -> list[str]:
    out, err = tmp_path / "out", tmp_path / "err"
    return ["bash", "-c", _STAGED_STDIO_READ_SCRIPT, "tolokaforge-exec", str(out), str(err)]


def test_command_runs_on_files_and_dev_null_not_the_execs_pipes(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(_FD_KINDS + "print(kind(0), kind(1), kind(2))")
    proc = subprocess.run(
        _staged_argv(tmp_path, f"{sys.executable} {probe}"),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.stdout == "chr file file\n", proc


def test_output_exit_code_and_stream_split_are_relayed_and_files_removed(tmp_path: Path) -> None:
    proc = subprocess.run(
        _staged_argv(tmp_path, "printf 'out\\n'; printf 'err\\n' >&2; exit 7"),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (proc.returncode, proc.stdout, proc.stderr) == (7, "out\n", "err\n")
    assert not (tmp_path / "out").exists() and not (tmp_path / "err").exists()


def test_heredoc_with_quotes_and_newlines_reaches_the_command_verbatim(tmp_path: Path) -> None:
    body = "line 'one' \"two\"\n$(not expanded) `nor this`\n" * 400
    target = tmp_path / "written.txt"
    command = f"cat > {target} <<'EOF'\n{body}EOF\nwc -c < {target}"
    proc = subprocess.run(
        _staged_argv(tmp_path, command), capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc
    assert target.read_text() == body
    assert proc.stdout.strip() == str(len(body.encode()))


def test_a_child_that_outlives_the_command_keeps_writable_stdio(tmp_path: Path) -> None:
    """The service case: a backgrounded child is still writing after the
    exec has returned, and those writes succeed."""
    report = tmp_path / "report"
    child = tmp_path / "child.py"
    child.write_text(
        _FD_KINDS + "import time\n"
        "time.sleep(0.5)\n"  # outlive the exec
        "k = kind(1), kind(2)\n"
        "ok = True\n"
        "try:\n"
        "    os.write(1, b'late stdout\\n'); os.write(2, b'late stderr\\n')\n"
        "except OSError as exc:\n"
        "    ok = exc\n"
        f"open({str(report)!r}, 'w').write(f'{{k}} {{ok}}')\n"
    )
    proc = subprocess.run(
        _staged_argv(tmp_path, f"{sys.executable} {child} & echo started"),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.stdout == "started\n"
    deadline = time.monotonic() + 10
    while not report.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert report.read_text() == "('file', 'file') True"


def test_timeout_reads_the_staged_output_back(tmp_path: Path) -> None:
    """Nothing reaches the exec's pipes before the command finishes, so the
    partial output a timed-out command has produced comes from the files."""
    output = _run_argv_preserving_partial_output(
        _staged_argv(tmp_path, "echo progress; echo trouble >&2; sleep 5"),
        timeout_s=0.5,
        partial_output_argv=_read_back_argv(tmp_path),
    )
    assert output.startswith("progress\n")
    assert "[timed out after 0.5s; partial output preserved]" in output
    assert output.endswith("trouble\n")


def test_docker_exec_plan_shape() -> None:
    plan = _docker_exec_plan("tbench_task-1_0_main", "echo hi", user="agent")
    prefix = ["docker", "exec", "--user", "agent", "-i", "tbench_task-1_0_main", "bash", "-c"]
    assert plan.argv[: len(prefix)] == prefix
    script, name, out, err, command = plan.argv[len(prefix) :]
    assert script == _STAGED_STDIO_SCRIPT
    assert name == "tolokaforge-exec"
    assert out.startswith("/tmp/.tolokaforge-exec-") and out.endswith(".out")
    assert err == out[: -len(".out")] + ".err"
    assert command == "echo hi"
    assert plan.partial_output_argv == [*prefix, _STAGED_STDIO_READ_SCRIPT, name, out, err]
    # Every exec stages into its own files: two commands running at once in
    # the same container must not share them.
    assert _docker_exec_plan("c", "echo hi").argv != plan.argv
    assert "--user" not in _docker_exec_plan("c", "echo hi").argv
