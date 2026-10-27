"""The resolver's wheel builds run the build backend ``uv.lock`` pins, through uv and through pip.

Builds a one-module hatchling project with the real builders and reads the backend
version the built wheel records."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tests.utils.image_constraints import IMAGE_CONSTRAINTS_PATH, parse_constraint_lines
from tolokaforge.docker.wheel_resolver import _build_wheel_locked

pytestmark = pytest.mark.canonical


def _locked_hatchling_version() -> str:
    (version,) = {
        pin.version
        for pin in parse_constraint_lines(IMAGE_CONSTRAINTS_PATH.read_text())
        if pin.name == "hatchling"
    }
    return version


@pytest.fixture()
def hatchling_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "tiny_pkg").mkdir(parents=True)
    (project / "tiny_pkg" / "__init__.py").write_text("")
    (project / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["hatchling"]\nbuild-backend = "hatchling.build"\n\n'
        '[project]\nname = "tiny-pkg"\nversion = "0.1.0"\n'
    )
    return project


def _wheel_generator(out_dir: Path) -> str:
    (wheel,) = out_dir.glob("tiny_pkg-0.1.0-*.whl")
    with zipfile.ZipFile(wheel) as zf:
        metadata = zf.read("tiny_pkg-0.1.0.dist-info/WHEEL").decode()
    (generator,) = [line for line in metadata.splitlines() if line.startswith("Generator: ")]
    return generator


def _path_without_uv() -> str:
    return os.pathsep.join(
        entry for entry in os.environ["PATH"].split(os.pathsep) if not (Path(entry) / "uv").exists()
    )


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv CLI not available")
def test_uv_build_runs_the_locked_backend(
    hatchling_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "executable", str(tmp_path / "no-python-so-pip-cannot-run"))
    out_dir = tmp_path / "dist"

    ok, failure = _build_wheel_locked(hatchling_project, out_dir)

    assert ok, failure
    assert _wheel_generator(out_dir) == f"Generator: hatchling {_locked_hatchling_version()}"


def test_pip_fallback_runs_the_locked_backend(
    hatchling_project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv_dir = tmp_path / "pip-venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True, capture_output=True)
    monkeypatch.setenv("PATH", _path_without_uv())
    assert shutil.which("uv") is None
    monkeypatch.setattr(sys, "executable", str(venv_dir / "bin" / "python"))
    out_dir = tmp_path / "dist"

    ok, failure = _build_wheel_locked(hatchling_project, out_dir)

    assert ok, failure
    assert _wheel_generator(out_dir) == f"Generator: hatchling {_locked_hatchling_version()}"
