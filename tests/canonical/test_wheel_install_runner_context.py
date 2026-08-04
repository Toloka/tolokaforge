"""Locks PR #858: base wheel install → runner build context resolves correctly.

The bug that reached v0.14.0 lived exactly here. CI only ever built from a
source checkout, where all six runner context paths exist. When tolokaforge
is installed as a wheel into ``site-packages``, the repo-root files
(``pyproject.toml``, ``README.md``, ``LICENSE``, ``.python-version``,
``scripts/hatch/``) are absent — the arena-eval production path — and
``build_images()`` died with ``FileNotFoundError: pyproject.toml`` before
a single trial ran.

The unit tests in ``tests/unit/test_docker_build_context.py`` mock the
wheel-install layout via ``monkeypatch.setattr("...builder.repo_root", ...)``.
That is enough to lock the resolution logic but not the pyproject-side
``[tool.hatch.build.targets.wheel.force-include]`` table: a future refactor
that drops a force-include entry would leave the unit tests green while
re-introducing the v0.14.0 failure mode.

This file closes that blind spot end-to-end. It builds the actual base
wheel with ``python -m hatchling build -t wheel``, inspects the wheel to
confirm every force-include entry landed, installs the wheel into a
scratch venv, and invokes ``_runner_definition()`` from within that venv.
Every entry the wheel-install branch returns must resolve to a real file.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical

REPO_ROOT = Path(__file__).resolve().parents[2]

# The paths the runner Dockerfile's ``hatchling build --target custom``
# stage needs from an installed base wheel — everything the source-checkout
# branch reads from the repo root has to survive as a force-included copy.
# Kept in one place so a future audit can grep for the constant.
_EXPECTED_FORCE_INCLUDES: tuple[str, ...] = (
    "tolokaforge/_subset_build/pyproject.toml",
    "tolokaforge/_subset_build/README.md",
    "tolokaforge/_subset_build/LICENSE",
    "tolokaforge/_subset_build/scripts/hatch/hatch_runner_subset_builder.py",
    "tolokaforge/_python_version.txt",
)


@pytest.fixture(scope="module")
def built_base_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the base tolokaforge wheel once per module via ``hatchling build -t wheel``.

    Uses ``python -m hatchling`` rather than ``hatch build`` to avoid the
    ``hatch`` CLI dep — PR #840's fix drove the subset-wheel tests via the
    same invocation for the same reason (compat with the ``uv`` version pin).
    """
    dist_dir = tmp_path_factory.mktemp("dist")
    result = subprocess.run(
        [sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-d", str(dist_dir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(
            f"base wheel build failed (exit {result.returncode}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    wheels = list(dist_dir.glob("*.whl"))
    # The custom target may co-produce the subset wheel; the base wheel is
    # the one whose distribution name is ``tolokaforge`` (not
    # ``tolokaforge_runner_subset``).
    base_wheels = [w for w in wheels if not w.name.startswith("tolokaforge_runner_subset")]
    assert (
        len(base_wheels) == 1
    ), f"expected exactly one base tolokaforge wheel in {dist_dir}, got: {[w.name for w in wheels]}"
    return base_wheels[0]


def test_base_wheel_ships_runner_context_via_force_include(built_base_wheel: Path) -> None:
    """Every force-include entry the runner build context needs must be
    present in the built base wheel.

    Locks the ``[tool.hatch.build.targets.wheel.force-include]`` table in
    ``pyproject.toml`` — a future refactor that drops any of these
    entries would re-introduce the v0.14.0 failure mode.
    """
    with zipfile.ZipFile(built_base_wheel) as zf:
        names = set(zf.namelist())

    missing = [path for path in _EXPECTED_FORCE_INCLUDES if path not in names]
    assert not missing, (
        f"base wheel {built_base_wheel.name} is missing force-include entries "
        f"the runner build context needs on a wheel install: {missing}. "
        "Check ``[tool.hatch.build.targets.wheel.force-include]`` in ``pyproject.toml``."
    )


def test_runner_definition_resolves_from_wheel_install(
    built_base_wheel: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Install the base wheel into a scratch venv, invoke ``_runner_definition()``
    inside that venv, and verify every wheel-install branch entry resolves.

    The unit-test coverage in ``test_docker_build_context.py`` monkeypatches
    ``repo_root`` and ``installed_package_dir`` to fake a wheel-install
    layout. That locks the resolution logic. It does NOT lock the
    pyproject-side ``force-include`` table nor the ``dup2``-free interaction
    between site-packages layout and the resolver — both are what v0.14.0
    tripped over. This test exercises the real path from a fresh install.
    """
    venv_dir = tmp_path_factory.mktemp("scratch_venv")
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True,
        capture_output=True,
    )
    # POSIX layout first; fall back to Windows if the POSIX path is absent
    # (CI runs on Linux, but the fallback keeps the fixture cross-platform).
    venv_python = venv_dir / "bin" / "python"
    if not venv_python.exists():
        venv_python = venv_dir / "Scripts" / "python.exe"

    # Prefer ``uv pip install`` for resolver speed (~4s vs ~120s with plain
    # ``pip`` when the base wheel's ~140 transitive deps have to resolve).
    # Falls back to the venv's own ``pip`` if ``uv`` is absent from PATH —
    # matches the pattern in ``test_runner_subset_install_smoke.py`` which
    # documents the same 30x speedup.
    uv_on_path = subprocess.run(["uv", "--version"], capture_output=True, text=True).returncode == 0
    if uv_on_path:
        install_cmd = [
            "uv",
            "pip",
            "install",
            "--python",
            str(venv_python),
            "--quiet",
            str(built_base_wheel),
        ]
    else:
        install_cmd = [str(venv_python), "-m", "pip", "install", "--quiet", str(built_base_wheel)]

    install_result = subprocess.run(install_cmd, capture_output=True, text=True)
    if install_result.returncode != 0:
        pytest.fail(
            f"wheel install into scratch venv failed (exit {install_result.returncode}):\n"
            f"cmd: {install_cmd}\n"
            f"stdout:\n{install_result.stdout}\nstderr:\n{install_result.stderr}"
        )

    # Run the probe from a CWD outside the repo — otherwise the scratch
    # venv's ``sys.path[0]`` prepends the CWD (repo root), and
    # ``tolokaforge`` imports would silently pick the source-tree package
    # instead of the wheel-installed one. Same trap ``test_runner_subset_
    # install_smoke.py`` documents.
    probe_cwd = tmp_path_factory.mktemp("probe_cwd_outside_repo")
    probe = textwrap.dedent("""
        import json
        from tolokaforge.docker.builder import (
            get_image_definition,
            installed_package_dir,
            repo_root,
        )

        definition = get_image_definition("runner")
        serialized = []
        for entry in definition["context_files"]:
            if isinstance(entry, tuple):
                serialized.append(
                    {"kind": "tuple", "source": str(entry[0]), "destination": entry[1]}
                )
            else:
                serialized.append({"kind": "plain", "path": str(entry)})
        print(
            json.dumps(
                {
                    "repo_root": str(repo_root()),
                    "installed_package_dir": str(installed_package_dir()),
                    "context_files": serialized,
                }
            )
        )
        """).strip()
    probe_result = subprocess.run(
        [str(venv_python), "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(probe_cwd),
    )
    if probe_result.returncode != 0:
        pytest.fail(
            f"probe inside scratch venv failed (exit {probe_result.returncode}):\n"
            f"stdout:\n{probe_result.stdout}\nstderr:\n{probe_result.stderr}"
        )
    probe_data = json.loads(probe_result.stdout)

    # ``repo_root()`` inside a wheel-install venv resolves to the venv's
    # site-packages, not the actual repo root — that is the whole trigger
    # for the wheel-install branch of ``_runner_definition``.
    resolved_repo_root = Path(probe_data["repo_root"])
    assert not resolved_repo_root.samefile(REPO_ROOT), (
        f"scratch venv unexpectedly resolved repo_root to the actual repo "
        f"({resolved_repo_root}). Test isolation broken — the venv should not "
        "reach the source-checkout branch."
    )

    # Every context entry must be absolute and point at a real file that
    # exists on disk. This is the assertion that would have failed on
    # v0.14.0 before #858's fix.
    context_files = probe_data["context_files"]
    assert context_files, "wheel-install runner definition returned no context files"
    for entry in context_files:
        src_str = entry["source"] if entry["kind"] == "tuple" else entry["path"]
        src = Path(src_str)
        assert src.is_absolute(), (
            f"wheel-install entry must be an absolute path (source-checkout "
            f"paths do not exist in site-packages): {entry}"
        )
        assert src.exists(), (
            f"wheel-install context entry does not exist on disk: {entry}. "
            "This is exactly the v0.14.0 failure mode — the base wheel's "
            "force-include table is missing an entry ``_runner_definition`` "
            "reaches for. Check ``pyproject.toml``'s "
            "``[tool.hatch.build.targets.wheel.force-include]`` block."
        )

    # The ``.python-version`` rename must be a tuple-form entry. The base
    # wheel force-includes ``.python-version`` as ``_python_version.txt``
    # (per the ``.python-version -> tolokaforge/_python_version.txt`` entry
    # in pyproject); the wheel-install branch must re-land it under its
    # dotfile name in the build context so the runner Dockerfile's
    # ``COPY .python-version`` finds it. A regression that dropped the
    # tuple form would land the file under ``_python_version.txt`` in the
    # build context and the Docker build would fail.
    tuple_entries = [e for e in context_files if e["kind"] == "tuple"]
    python_version_tuple = [e for e in tuple_entries if e["destination"] == ".python-version"]
    assert python_version_tuple, (
        "wheel-install context is missing the ``(source, .python-version)`` tuple entry. "
        "The base wheel's force-include ships ``.python-version`` under a renamed "
        "packaged path; ``_runner_definition`` must re-land it under the dotfile "
        "name for the Dockerfile's ``COPY .python-version`` line."
    )
    tuple_source = Path(python_version_tuple[0]["source"])
    assert tuple_source.name == "_python_version.txt", (
        f"``.python-version`` tuple sources the wrong file: {tuple_source}. "
        "The packaged copy lives at ``tolokaforge/_python_version.txt`` per "
        "the pyproject's ``force-include`` table."
    )
