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
BASE_WHEEL_GLOB = "tolokaforge-*.whl"
CODING_HARNESSES_WHEEL_GLOB = "tolokaforge_coding_harnesses-*.whl"

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


def build_base_wheel(dist_dir: Path) -> Path:
    """Build the base ``tolokaforge`` wheel from the current tree into *dist_dir*.

    This is the wheel a ``pip install tolokaforge`` ships — it carries the
    ``_subset_build/`` force-included sources every first-party image's build
    context needs on a wheel install."""
    return _hatchling_build(
        project_dir=REPO_ROOT,
        target="wheel",
        dist_dir=dist_dir,
        wheel_glob=BASE_WHEEL_GLOB,
        label="tolokaforge (base)",
    )


def build_coding_harnesses_wheel(dist_dir: Path) -> Path:
    """Build ``tolokaforge-coding-harnesses`` from the current tree into *dist_dir*."""
    return _hatchling_build(
        project_dir=REPO_ROOT / "tolokaforge_coding_harnesses",
        target="wheel",
        dist_dir=dist_dir,
        wheel_glob=CODING_HARNESSES_WHEEL_GLOB,
        label="tolokaforge-coding-harnesses",
    )


def build_workspace_wheels(dist_dir: Path) -> Path:
    """Build the base wheel plus every workspace sibling it depends on
    (``tolokaforge-models``, ``tolokaforge-coding-harnesses``) into *dist_dir*.

    Returned so ``uv pip install --find-links <dist_dir> <base_wheel>`` resolves
    the engine's sibling deps against this directory. Use :func:`base_wheel_in`
    to pick the base wheel out of the result."""
    build_base_wheel(dist_dir)
    build_models_wheel(dist_dir)
    build_coding_harnesses_wheel(dist_dir)
    return dist_dir


def base_wheel_in(dist_dir: Path) -> Path:
    """The single base ``tolokaforge-<version>`` wheel in *dist_dir*.

    Matches the dash after the package name, so a sibling wheel
    (``tolokaforge_models-*`` / ``tolokaforge_coding_harnesses-*``, underscore)
    is never mistaken for the base distribution."""
    base_wheels = [w for w in dist_dir.glob(BASE_WHEEL_GLOB) if w.name.startswith("tolokaforge-")]
    if len(base_wheels) != 1:
        pytest.fail(
            f"expected exactly one base tolokaforge wheel in {dist_dir}, got: "
            f"{[w.name for w in base_wheels]}"
        )
    return base_wheels[0]


def make_wheel_install_venv(venv_dir: Path, dist_dir: Path, base_wheel: Path) -> Path:
    """Create a scratch venv, install *base_wheel* (resolving siblings against
    *dist_dir*), and return the venv's python executable.

    Prefers ``uv pip install`` for resolver speed (~4s vs ~120s with plain pip
    over the engine's transitive deps), falling back to the venv's own pip."""
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True, capture_output=True)
    venv_python = venv_dir / "bin" / "python"
    if not venv_python.exists():
        venv_python = venv_dir / "Scripts" / "python.exe"

    uv_on_path = subprocess.run(["uv", "--version"], capture_output=True, text=True).returncode == 0
    if uv_on_path:
        install_cmd = [
            "uv",
            "pip",
            "install",
            "--python",
            str(venv_python),
            "--quiet",
            "--find-links",
            str(dist_dir),
            str(base_wheel),
        ]
    else:
        install_cmd = [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--quiet",
            "--find-links",
            str(dist_dir),
            str(base_wheel),
        ]
    result = subprocess.run(install_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        pytest.fail(
            f"wheel install into scratch venv failed (exit {result.returncode}):\n"
            f"cmd: {install_cmd}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return venv_python


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
