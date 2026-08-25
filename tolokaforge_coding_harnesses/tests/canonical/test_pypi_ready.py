"""The package builds a PyPI-installable sdist + wheel from any checkout.

The registry is not published to PyPI today; keeping the invariants the future
publish will need pinned as a canonical test is the alternative to discovering
a broken sdist on the day someone actually runs the publish workflow. Two
axes:

- **Static hygiene.** The ``pyproject.toml`` declares no git-URL or path-only
  dependency in ``[project.dependencies]``, so ``pip install
  tolokaforge-coding-harnesses`` would resolve every runtime dep from PyPI.
- **Buildable artifact.** ``hatchling build`` produces both an sdist and a
  wheel from the package's own directory, and both artifacts carry the data
  files consumers actually read at import time: ``data/harnesses.yaml``,
  ``data/registry_meta.yaml``, ``install-harness.sh``, ``middleware_proxy.py``,
  ``container_injection.py``. A hatchling misconfiguration that dropped any of
  these would ship a wheel that boots but fails on first use.

Both checks run against the checked-out ``pyproject.toml``: publish workflows
do not modify it, so what is in the tree is what would ship.
"""

from __future__ import annotations

import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest
import tomllib

pytestmark = pytest.mark.canonical

PACKAGE_DIR = Path(__file__).resolve().parents[2]
"""``tolokaforge_coding_harnesses/`` — the directory carrying pyproject.toml."""


REQUIRED_SHIPPED_PATHS = (
    "src/tolokaforge_coding_harnesses/__init__.py",
    "src/tolokaforge_coding_harnesses/_registry.py",
    "src/tolokaforge_coding_harnesses/adapter_support.py",
    "src/tolokaforge_coding_harnesses/container_injection.py",
    "src/tolokaforge_coding_harnesses/fingerprint.py",
    "src/tolokaforge_coding_harnesses/install-harness.sh",
    "src/tolokaforge_coding_harnesses/middleware_proxy.py",
    "src/tolokaforge_coding_harnesses/path_resolvers.py",
    "src/tolokaforge_coding_harnesses/protocols.py",
    "src/tolokaforge_coding_harnesses/testing.py",
    "src/tolokaforge_coding_harnesses/data/harnesses.yaml",
    "src/tolokaforge_coding_harnesses/data/registry_meta.yaml",
)
"""Every path a runtime reads through the public surface. A wheel missing any
one boots but breaks on first call — a class of bug the boundary test in
``tests/unit/test_package_boundary.py`` catches at import time from a source
checkout, but not from a hatchling-built artifact where the checkout layout
is not what the wheel exposes."""


def _pyproject() -> dict:
    with (PACKAGE_DIR / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


class TestStaticHygiene:
    """Text-level invariants on ``pyproject.toml`` that block a PyPI publish
    from resolving every runtime dep against Docker Hub / GitHub instead of
    PyPI."""

    def test_project_dependencies_carry_no_git_url(self) -> None:
        cfg = _pyproject()
        deps = cfg["project"].get("dependencies", [])
        offenders = [d for d in deps if "@ git+" in d or d.startswith("git+") or " @ file:" in d]
        assert not offenders, (
            f"[project.dependencies] carries non-PyPI-resolvable entries {offenders!r}. "
            "PyPI wheels must depend only on published names; move any git-URL or "
            "path dep to a workspace source (dev-only) or drop it."
        )

    def test_project_dependencies_all_declare_bounds(self) -> None:
        # A bare ``foo`` in dependencies resolves to whatever the newest release
        # is at install time — fine for arbitrary consumers, catastrophic for a
        # reproducibility-focused harness registry. Every dep declares at least
        # a lower bound.
        cfg = _pyproject()
        deps = cfg["project"].get("dependencies", [])
        unbounded = [
            d for d in deps if not any(op in d for op in ("==", ">=", "<=", "~=", ">", "<", "!="))
        ]
        assert not unbounded, (
            f"unbounded dependencies would resolve to the newest release at install "
            f"time — pin at least a lower bound on: {unbounded!r}"
        )

    def test_wheel_target_names_the_src_package(self) -> None:
        cfg = _pyproject()
        wheel_cfg = (
            cfg.get("tool", {})
            .get("hatch", {})
            .get("build", {})
            .get("targets", {})
            .get("wheel", {})
        )
        packages = wheel_cfg.get("packages", [])
        assert "src/tolokaforge_coding_harnesses" in packages, (
            f"hatchling wheel target must include 'src/tolokaforge_coding_harnesses' — "
            f"currently declares packages={packages!r}"
        )

    def test_project_metadata_carries_a_pypi_publishable_shape(self) -> None:
        cfg = _pyproject()["project"]
        # Every field a PyPI upload requires. Absent fields would fail
        # ``twine check`` at publish time; catching them here is cheaper.
        assert cfg["name"] == "tolokaforge-coding-harnesses"
        assert "description" in cfg and cfg["description"]
        assert "license" in cfg
        assert "requires-python" in cfg
        # PyPI requires either an author or maintainer field.
        assert cfg.get("authors") or cfg.get("maintainers")


class TestBuildableArtifact:
    """``hatchling`` builds sdist + wheel from the package directory alone, and
    every path a runtime reads at import lands in both artifacts. Runs
    ``python -m hatchling`` in a subprocess so a hatchling misconfiguration
    surfaces as a build failure here, not as a broken publish."""

    @pytest.fixture(scope="class")
    @staticmethod
    def built_artifacts(tmp_path_factory: pytest.TempPathFactory) -> Path:
        dist_dir = tmp_path_factory.mktemp("dist")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "hatchling",
                "build",
                "--target",
                "sdist",
                "-d",
                str(dist_dir),
            ],
            cwd=str(PACKAGE_DIR),
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "hatchling",
                "build",
                "--target",
                "wheel",
                "-d",
                str(dist_dir),
            ],
            cwd=str(PACKAGE_DIR),
            check=True,
            capture_output=True,
        )
        return dist_dir

    def test_sdist_ships_every_required_path(self, built_artifacts: Path) -> None:
        sdists = list(built_artifacts.glob("*.tar.gz"))
        assert len(sdists) == 1, f"expected one sdist, got {sdists}"
        with tarfile.open(sdists[0], "r:gz") as tar:
            # sdists prefix names with ``<name>-<version>/``. Strip it.
            names = {n.split("/", 1)[1] for n in tar.getnames() if "/" in n}
        missing = [p for p in REQUIRED_SHIPPED_PATHS if p not in names]
        assert not missing, (
            f"sdist {sdists[0].name} is missing paths a runtime reads at import: "
            f"{missing!r}. Every published sdist must be self-contained."
        )

    def test_wheel_ships_every_required_data_and_script(self, built_artifacts: Path) -> None:
        wheels = list(built_artifacts.glob("*.whl"))
        assert len(wheels) == 1, f"expected one wheel, got {wheels}"
        with zipfile.ZipFile(wheels[0], "r") as zf:
            names = set(zf.namelist())
        # Wheels flatten the ``src/`` prefix — the package installs as
        # ``tolokaforge_coding_harnesses/…`` on the target's site-packages.
        wheel_required = tuple(p.removeprefix("src/") for p in REQUIRED_SHIPPED_PATHS)
        missing = [p for p in wheel_required if p not in names]
        assert not missing, (
            f"wheel {wheels[0].name} is missing paths a runtime reads at import: "
            f"{missing!r}. A pip install of this wheel would break on first use."
        )
