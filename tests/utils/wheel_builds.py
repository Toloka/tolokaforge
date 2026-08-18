"""Wheel builds for the artifact-tier tests.

The subset partition audit and the subset-install smoke both make claims about
what pip installs into the runner image, so both must read a wheel built from
the tree under test. Builds land in a caller-supplied directory — a
per-session ``tmp_path_factory`` path at every call site — so an artifact left
in ``dist/`` by an earlier commit can never satisfy either module.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

SUBSET_WHEEL_GLOB = "tolokaforge_runner_subset-*.whl"
MODELS_WHEEL_GLOB = "tolokaforge_models-*.whl"

_BUILD_TIMEOUT_S = 180


def build_subset_wheel(dist_dir: Path) -> Path:
    """Build ``tolokaforge-runner-subset`` from the current tree into *dist_dir*."""
    return _hatchling_build(
        project_dir=REPO_ROOT,
        target="custom",
        dist_dir=dist_dir,
        wheel_glob=SUBSET_WHEEL_GLOB,
        label="subset",
    )


def build_models_wheel(dist_dir: Path) -> Path:
    """Build ``tolokaforge-models`` from the current tree into *dist_dir*.

    The subset wheel's ``Requires-Dist: tolokaforge-models`` cannot resolve from
    PyPI in this repo's dev / CI environment, so a scratch-venv install passes
    this wheel alongside the subset one."""
    return _hatchling_build(
        project_dir=REPO_ROOT / "tolokaforge_models",
        target="wheel",
        dist_dir=dist_dir,
        wheel_glob=MODELS_WHEEL_GLOB,
        label="tolokaforge-models",
    )


def _hatchling_build(
    *,
    project_dir: Path,
    target: str,
    dist_dir: Path,
    wheel_glob: str,
    label: str,
) -> Path:
    """Drive the PEP 517 backend from the active interpreter and return the wheel.

    ``python -m hatchling`` rather than the ``hatch`` CLI: only the backend is a
    dev dependency, and hatch's ``default`` environment does not resolve inside
    a uv-managed venv."""
    build_result = subprocess.run(
        [sys.executable, "-m", "hatchling", "build", "-t", target, "-d", str(dist_dir)],
        cwd=project_dir,
        capture_output=True,
        text=True,
        timeout=_BUILD_TIMEOUT_S,
    )
    if build_result.returncode != 0:
        pytest.fail(
            f"{label} wheel build failed (exit {build_result.returncode}):\n"
            f"stdout:\n{build_result.stdout}\nstderr:\n{build_result.stderr}"
        )
    wheels = sorted(dist_dir.glob(wheel_glob))
    if len(wheels) != 1:
        pytest.fail(
            f"{label} wheel build left {len(wheels)} artifacts matching {wheel_glob} "
            f"in {dist_dir} — expected exactly the one just built: "
            f"{[w.name for w in wheels]}"
        )
    return wheels[0]
