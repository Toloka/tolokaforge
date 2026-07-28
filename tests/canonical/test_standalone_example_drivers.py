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

import py_compile
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical


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
