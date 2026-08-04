"""End-to-end regression guard for the wheel-install runner build path.

**Background — why this test exists.** The v0.14.0 release shipped an engine
that died in ``build_images()`` before a single trial ran (installed as a
wheel; ``repo_root()`` was ``site-packages``; repo-root files absent). PR
#858 patched the resolver at ``tolokaforge.docker.builder`` — and v0.14.1
shipped **still broken** because the production path takes
``core_stack().services["runner"].context_files``, which held a **duplicate
hardcoded copy of the repo-relative list**. PR #864 wired ``core_stack()``
to consume from ``get_image_definition("runner")`` instead so the two lists
cannot drift.

The earlier version of this file called ``get_image_definition("runner")``
directly inside the wheel-install venv. That is the RESOLVER surface, not
production. It would have (and did) pass on v0.14.1's broken code. This
rewrite drives the *production* path — the same one
``Orchestrator._start_all_services`` → ``EngineStack.build_images()`` →
``assemble_build_context()`` takes at runtime.

**What this locks:**

- **The base wheel's ``[tool.hatch.build.targets.wheel.force-include]``
  table** must ship every input the runner Dockerfile needs
  (``test_base_wheel_ships_runner_context_via_force_include``). A future
  edit that drops any force-include entry fails here.
- **The wheel-install branch, wired end-to-end through ``core_stack()``**,
  must produce a runner build context that assembles into a directory
  containing every file the Dockerfile ``COPY`` lines expect
  (``test_core_stack_runner_context_assembles_from_wheel_install``). A
  future edit that duplicates the context list back into ``core_stack()``,
  or a resolver regression, fails here.

Both tests use the actual base wheel produced by
``python -m hatchling build -t wheel`` and install it into a scratch venv;
the second additionally runs the entry point outside the repo tree so a
CWD-prepend into ``sys.path`` can't hide a partial wheel-install failure.
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

# Every file the runner Dockerfile ``COPY`` lines reference by name — the
# assembled build context under a wheel install must land each one so
# ``hatchling build --target custom`` inside the runner-image builder
# stage finds its inputs.
_EXPECTED_CONTEXT_ENTRIES: tuple[str, ...] = (
    "pyproject.toml",
    "README.md",
    "LICENSE",
    ".python-version",
    "scripts/hatch",
    "tolokaforge",
)


@pytest.fixture(scope="module")
def built_base_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the base tolokaforge wheel once per module via ``hatchling build -t wheel``.

    Uses ``python -m hatchling`` rather than ``hatch build`` to avoid the
    ``hatch`` CLI dep — matches ``test_runner_subset_install_smoke.py``'s
    invocation for the same reason (compat with the ``uv`` version pin).
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
    entries would re-introduce the v0.14.0 failure mode from a direction
    the end-to-end test below cannot cover (because the assembly step
    would legitimately fail on the missing input, leaving the reader to
    diagnose whether the pyproject table drifted or the resolver did).
    """
    with zipfile.ZipFile(built_base_wheel) as zf:
        names = set(zf.namelist())

    missing = [path for path in _EXPECTED_FORCE_INCLUDES if path not in names]
    assert not missing, (
        f"base wheel {built_base_wheel.name} is missing force-include entries "
        f"the runner build context needs on a wheel install: {missing}. "
        "Check ``[tool.hatch.build.targets.wheel.force-include]`` in ``pyproject.toml``."
    )


def test_core_stack_runner_context_assembles_from_wheel_install(
    built_base_wheel: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Install the base wheel into a scratch venv and drive the PRODUCTION
    path — ``core_stack().services['runner'].context_files`` +
    ``assemble_build_context()`` — end-to-end. Fail if the assembled
    directory is missing any file the runner Dockerfile ``COPY`` expects.

    **Why this exact call shape.** ``Orchestrator._start_all_services`` →
    ``EngineStack.build_images()`` → ``_build_one_image()`` calls
    ``assemble_build_context(repo_root(), svc.dockerfile, svc.context_files)``
    where ``svc`` comes from ``core_stack().services["runner"]``. The
    earlier version of this file exercised ``get_image_definition("runner")``
    inside the wheel-install venv — the resolver's surface, not
    production's — and (silently) passed on v0.14.1's broken code because
    ``core_stack()`` held its own hardcoded duplicate of the list. PR #864
    wired ``core_stack()`` to read from ``get_image_definition()``; this
    test locks that wiring end-to-end.

    The probe additionally asserts the resolver and the service stack
    return the same list — a necessary-but-not-sufficient invariant that
    would already be violated if the duplicate returned.
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
        import shutil
        from pathlib import Path

        from tolokaforge.docker.builder import (
            assemble_build_context,
            get_image_definition,
            repo_root,
        )
        from tolokaforge.docker.stacks.core import core_stack

        # Confirm the venv actually looks like a wheel install — this is
        # the trigger condition for the wheel-install branch of
        # ``_runner_definition``. If a false-positive source checkout
        # were detected, the resolver would return repo-relative paths
        # and the assembly below would fail with a misleading error.
        root = repo_root()
        wheel_install = not (root / "pyproject.toml").is_file()

        svc = core_stack().services["runner"]
        gi = get_image_definition("runner")
        lists_equal = svc.context_files == gi["context_files"]

        # Assemble the build context via the PRODUCTION path — the exact
        # call ``EngineStack.build_images`` makes on a live run.
        build_dir = assemble_build_context(root, svc.dockerfile, svc.context_files)
        try:
            files_present = {
                name: (build_dir / name).exists()
                for name in [
                    "pyproject.toml",
                    "README.md",
                    "LICENSE",
                    ".python-version",
                    "scripts/hatch",
                    "tolokaforge",
                ]
            }
        finally:
            shutil.rmtree(build_dir, ignore_errors=True)

        print(json.dumps({
            "repo_root": str(root),
            "wheel_install": wheel_install,
            "lists_equal": lists_equal,
            "files_present": files_present,
        }))
        """).strip()
    probe_result = subprocess.run(
        [str(venv_python), "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(probe_cwd),
    )
    if probe_result.returncode != 0:
        pytest.fail(
            "probe inside scratch venv failed — this is the v0.14.0/v0.14.1 "
            f"failure mode (exit {probe_result.returncode}):\n"
            f"stdout:\n{probe_result.stdout}\nstderr:\n{probe_result.stderr}"
        )
    probe_data = json.loads(probe_result.stdout)

    # ``repo_root()`` inside the wheel-install venv must NOT be the actual
    # repo — the whole point of the trigger. If test isolation broke, we
    # would silently take the source-checkout branch and the assertion
    # below would misleadingly pass.
    resolved_repo_root = Path(probe_data["repo_root"])
    assert not resolved_repo_root.samefile(REPO_ROOT), (
        f"scratch venv unexpectedly resolved repo_root to the actual repo "
        f"({resolved_repo_root}). Test isolation broken — the venv should not "
        "reach the source-checkout branch."
    )
    assert probe_data["wheel_install"], (
        f"probe found a pyproject.toml under {resolved_repo_root} — the "
        "wheel-install branch of ``_runner_definition`` was NOT triggered, "
        "so this test would only exercise the source-checkout path. "
        "Check that the base wheel does not ship a top-level pyproject.toml."
    )

    # The service-stack and resolver must return the same list. PR #864's
    # unit test asserts this in isolation with a monkeypatched wheel-install
    # layout; here we assert it end-to-end against an actual installed wheel.
    # A regression that re-duplicates the list into ``core_stack()`` would
    # break this even if the assembly below still happened to work.
    assert probe_data["lists_equal"], (
        "``core_stack().services['runner'].context_files`` diverged from "
        "``get_image_definition('runner')['context_files']`` on a wheel install. "
        "One of them was hand-edited without updating the other — this is the "
        "duplication that caused v0.14.1 to ship broken (see PR #864)."
    )

    # The assembled build directory must contain every file the runner
    # Dockerfile ``COPY`` lines expect. This is the assertion that would
    # have failed on v0.14.1 before PR #864 — the exact production error.
    files_present = probe_data["files_present"]
    missing = [name for name in _EXPECTED_CONTEXT_ENTRIES if not files_present.get(name)]
    assert not missing, (
        "``assemble_build_context()`` via the production path "
        "(``core_stack().services['runner'].context_files``) did not land the "
        f"following entries the runner Dockerfile ``COPY`` lines expect: {missing}. "
        "This is the exact v0.14.0/v0.14.1 failure mode; check that "
        "``core_stack()`` reads its context list from ``get_image_definition()`` "
        "and that the base wheel's ``force-include`` table ships every input."
    )
