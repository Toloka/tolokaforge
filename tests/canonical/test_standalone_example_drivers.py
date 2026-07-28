"""Shape locks for the standalone stack example drivers.

The drivers under ``deploy/standalone/examples/`` are documented, cold-user-run
entry points (STANDALONE_RUNNER.md's quickstart). This keyless, always-on guard
catches gross rot without Docker or a provider key: the Python driver must
compile, and the bundled task pack it drives must exist on disk. It is a *shape*
lock, not a behaviour lock — a rename of ``load_task`` / ``TaskConfig.source_dir``
still compiles here and surfaces only in the paid integration lane
(``tests/integration/deploy/test_standalone_example.py``), whose
``TrialResult.model_validate`` is the typed behaviour lock.
"""

from __future__ import annotations

import json
import os
import py_compile
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical


_STUB_TASK_JSON = '{"id": "stub", "nested": {"messages": [1, 2]}}'
_STUB_PROVIDER = "openrouter"
_STUB_MODEL = "stub/model"

_ENVELOPE_CMD_RE = re.compile(r"START=\$\((?P<cmd>jq\b.*?)\)\n", re.DOTALL)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES_DIR = _REPO_ROOT / "deploy" / "standalone" / "examples"
_PYTHON_DRIVER = _EXAMPLES_DIR / "drive_one_trial.py"
_SHELL_DRIVER = _EXAMPLES_DIR / "drive_one_trial.sh"

_BUNDLED_TASK_REL = (
    "examples/native/tool_use/dataset/tasks/tool_use/tool_use_public_example_01/task.yaml"
)


def test_python_driver_compiles() -> None:
    """The Python driver parses — a syntax rot guard for the cold-user script."""
    assert _PYTHON_DRIVER.exists(), f"{_PYTHON_DRIVER} is missing — the driver must ship"
    py_compile.compile(str(_PYTHON_DRIVER), doraise=True)


def test_python_driver_drives_the_bundled_task_pack() -> None:
    """The driver references the bundled pack, and that ``task.yaml`` exists.

    Locks the reference both ways: the driver source must point at the bundled
    ``tool_use`` pack, and that pack must be present — so deleting the pack or
    repointing the driver at a missing one trips CI keylessly.
    """
    source = _PYTHON_DRIVER.read_text()
    assert (
        _BUNDLED_TASK_REL in source
    ), f"{_PYTHON_DRIVER.name} must drive the bundled pack {_BUNDLED_TASK_REL!r}"
    task_yaml = _REPO_ROOT / _BUNDLED_TASK_REL
    assert task_yaml.is_file(), f"bundled task pack the driver drives is missing: {task_yaml}"


def test_shell_driver_parses() -> None:
    """The shell driver is valid POSIX ``sh`` — a syntax rot guard."""
    assert _SHELL_DRIVER.exists(), f"{_SHELL_DRIVER} is missing — the driver must ship"
    subprocess.run(["sh", "-n", str(_SHELL_DRIVER)], check=True, capture_output=True, text=True)


def test_shell_driver_uses_no_grpcurl() -> None:
    """The shell driver drives the ``run-trial`` exec wire, never ``grpcurl``.

    The runner gRPC exposes no whole-trial RPC (per-trial primitives + HealthCheck,
    reflection off), so a ``grpcurl``-driven trial is infeasible. Guard against
    reintroducing that path.
    """
    assert "grpcurl" not in _SHELL_DRIVER.read_text(), (
        "the shell driver must drive the run-trial exec wire, not grpcurl — the "
        "runner gRPC has no whole-trial RPC"
    )


def _extract_start_envelope_jq(source: str) -> str:
    """Return the ``jq`` invocation the shell driver uses to build the start envelope."""
    match = _ENVELOPE_CMD_RE.search(source)
    assert match is not None, (
        "could not locate the START=$(jq ...) envelope construction in the shell driver — "
        "the envelope-shape guard cannot find what it protects"
    )
    return match.group("cmd")


def test_shell_driver_start_envelope_uses_compact_jq() -> None:
    """The start-envelope ``jq`` emits compact output — keyless, no jq binary needed.

    The ``run-trial`` wire is JSON-Lines: one envelope per line. A pretty-printing
    ``jq -n`` splits the start envelope across many lines, and the runner reads only
    the first (``{``) and dies. This source guard requires the compact ``-c`` flag and
    runs on every PR even where jq is absent, so the single-line contract cannot rot
    silently the way ``sh -n`` alone let it.
    """
    cmd = _extract_start_envelope_jq(_SHELL_DRIVER.read_text())
    flags = cmd.split("'", 1)[0]
    assert "-c" in flags, (
        f"the start envelope must be built with a compact jq (jq -c), got: {flags.strip()!r} — "
        "a pretty-printed envelope violates the JSON-Lines wire"
    )


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq not on PATH")
def test_shell_driver_start_envelope_is_single_line() -> None:
    """Executing the driver's own ``jq`` invocation yields exactly one wire line.

    Behaviour lock: run the envelope-construction command lifted verbatim from the
    shell driver against a stub task and assert the output is a single line of valid
    JSON. This catches any multi-line regression regardless of how it is spelled.
    """
    cmd = _extract_start_envelope_jq(_SHELL_DRIVER.read_text())
    completed = subprocess.run(
        ["sh", "-c", cmd],
        env={
            "PATH": os.environ["PATH"],
            "TASK_JSON": _STUB_TASK_JSON,
            "PROVIDER": _STUB_PROVIDER,
            "MODEL": _STUB_MODEL,
        },
        capture_output=True,
        text=True,
        check=True,
    )
    envelope = completed.stdout.strip()
    assert (
        "\n" not in envelope
    ), f"start envelope must be a single JSON-Lines wire line, got:\n{envelope}"
    assert json.loads(envelope)["type"] == "start"
