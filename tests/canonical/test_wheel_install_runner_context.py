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
def built_wheels_dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the base tolokaforge wheel + the tolokaforge-models sibling wheel
    into one directory (returned).

    Uses ``python -m hatchling`` rather than ``hatch build`` to avoid the
    ``hatch`` CLI dep — matches ``test_runner_subset_install_smoke.py``'s
    invocation for the same reason (compat with the ``uv`` version pin).
    ``uv pip install --find-links <dist_dir>`` resolves the engine's dep on
    ``tolokaforge-models`` against the sibling wheel in this dir.
    """
    dist_dir = tmp_path_factory.mktemp("dist")
    for project_dir, label in ((REPO_ROOT, "engine"), (REPO_ROOT / "tolokaforge_models", "models")):
        result = subprocess.run(
            [sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-d", str(dist_dir)],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.fail(
                f"{label} wheel build failed (exit {result.returncode}):\n"
                f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
            )
    return dist_dir


@pytest.fixture(scope="module")
def built_base_wheel(built_wheels_dist: Path) -> Path:
    """Path to the engine ``tolokaforge-*.whl`` produced by :func:`built_wheels_dist`."""
    wheels = list(built_wheels_dist.glob("*.whl"))
    # Positive match on the base distribution name — ``tolokaforge-<version>``.
    # A negative filter against one known sibling (e.g. ``tolokaforge_runner_
    # subset``) would silently include a hypothetical third target with a
    # different name; matching ``tolokaforge-`` (dash after the package name,
    # not underscore) unambiguously picks the base wheel.
    base_wheels = [w for w in wheels if w.name.startswith("tolokaforge-")]
    assert len(base_wheels) == 1, (
        f"expected exactly one base tolokaforge wheel in {built_wheels_dist}, "
        f"got: {[w.name for w in wheels]}"
    )
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
    built_base_wheel: Path, built_wheels_dist: Path, tmp_path_factory: pytest.TempPathFactory
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
            "--find-links",
            str(built_wheels_dist),
            str(built_base_wheel),
        ]
    else:
        install_cmd = [
            str(venv_python),
            "-m",
            "pip",
            "install",
            "--quiet",
            "--find-links",
            str(built_wheels_dist),
            str(built_base_wheel),
        ]

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
    # The probe reads its expected-entries list from the module-level
    # ``_EXPECTED_CONTEXT_ENTRIES`` constant, interpolated below as a Python
    # literal. This avoids duplicating the list between the outer test and
    # the probe body — the exact class of duplication that caused v0.14.1
    # to ship broken. A single source of truth for what the runner Dockerfile
    # ``COPY`` lines expect.
    probe_prelude = (
        f"_EXPECTED_ENTRIES = {list(_EXPECTED_CONTEXT_ENTRIES)!r}\n"
        f"_PYPROJECT_MARKER = {'[tool.hatch.build.targets.custom]'!r}\n"
    )
    probe_body = textwrap.dedent("""
        import json
        import shutil
        from pathlib import Path

        import tolokaforge
        from tolokaforge.docker.builder import (
            assemble_build_context,
            get_image_definition,
            repo_root,
        )
        from tolokaforge.docker.image_source_policy import resolve_image_source
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
        # #1068 — SSOT check across the wheel boundary. On a wheel
        # install the runner service must declare its published repo,
        # and the pull-policy resolver (given the engine version
        # ``importlib.metadata`` reports for THIS venv) must resolve
        # ``auto`` to ``pull``. A regression that drops
        # ``published_image_repo`` from the ServiceDefinition, or that
        # ships a broken ``__version__`` sentinel, would fail here in a
        # real wheel install — not just in unit-test mocks.
        engine_version = tolokaforge.__version__
        resolved_auto = resolve_image_source(
            request="auto",
            is_wheel_install=wheel_install,
            engine_version=engine_version,
        )

        # Assemble the build context via the PRODUCTION path — the exact
        # call ``EngineStack.build_images`` makes on a live run.
        build_dir = assemble_build_context(root, svc.dockerfile, svc.context_files)
        try:
            files_present = {
                name: (build_dir / name).exists() for name in _EXPECTED_ENTRIES
            }
            # Content-shape assertion: a broken force-include copy could
            # land an empty pyproject.toml — the existence check above
            # would pass, but ``hatchling build --target custom`` inside
            # the runner-image builder stage would fail with a much
            # deeper error. Grab the pyproject text for the outer test
            # to shape-check.
            pyproject_path = build_dir / "pyproject.toml"
            pyproject_text = pyproject_path.read_text() if pyproject_path.exists() else ""
        finally:
            shutil.rmtree(build_dir, ignore_errors=True)

        print(json.dumps({
            "repo_root": str(root),
            "wheel_install": wheel_install,
            "lists_equal": lists_equal,
            "files_present": files_present,
            "pyproject_has_custom_target": _PYPROJECT_MARKER in pyproject_text,
            "pyproject_size": len(pyproject_text),
            "runner_published_repo": svc.published_image_repo,
            "engine_version": engine_version,
            "resolved_auto": resolved_auto,
        }))
        """).strip()
    probe = probe_prelude + probe_body
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

    # Content-shape check on the shipped pyproject.toml. Presence alone is
    # not enough — a force-include glitch that lands an empty or truncated
    # file would satisfy the existence assertion above but ``hatchling
    # build --target custom`` inside the runner-image builder stage would
    # fail on the missing ``[tool.hatch.build.targets.custom]`` section.
    # This catches that class of regression at the context-assembly layer
    # instead of one Docker-layer deeper.
    assert probe_data["pyproject_has_custom_target"], (
        "shipped ``pyproject.toml`` in the assembled build dir is missing "
        "``[tool.hatch.build.targets.custom]`` — the section the runner-image "
        "builder stage reads to produce the subset wheel. The file exists "
        f"but is {probe_data['pyproject_size']} bytes; either a stale copy "
        "was force-included or the pyproject's custom target section was "
        "removed. Check ``[tool.hatch.build.targets.wheel.force-include]`` "
        "and ``[tool.hatch.build.targets.custom]`` in the repo-root ``pyproject.toml``."
    )

    # #1068 — SSOT: the runner ServiceDefinition on a wheel install must
    # declare its published repo, and the pull-policy resolver must map
    # ``auto`` to ``pull`` for a real wheel install with a real reported
    # version. A regression that drops ``published_image_repo`` or that
    # ships a broken ``__version__`` fallback would silently keep every
    # wheel-installed user on the slow local-build path.
    assert probe_data["runner_published_repo"] == "tolokasoft1/tolokaforge-runner", (
        "runner ServiceDefinition on a wheel install did not declare "
        f"``published_image_repo=tolokasoft1/tolokaforge-runner`` "
        f"(got: {probe_data['runner_published_repo']!r}). The pull-vs-build "
        "policy in ``EngineStack._maybe_pull_service_image`` needs this field "
        "to resolve to ``pull``; a missing / renamed value silently forces "
        "every wheel-installed user onto the local-build path."
    )
    assert probe_data["resolved_auto"] == "pull", (
        "``resolve_image_source(request='auto', is_wheel_install=True, "
        f"engine_version={probe_data['engine_version']!r})`` returned "
        f"{probe_data['resolved_auto']!r} on a real wheel install. The wheel "
        "should report a real version from ``importlib.metadata`` and route "
        "to ``pull``; if it does not, either ``__version__`` fell back to the "
        "unknown sentinel (metadata missing from the built wheel) or the "
        "policy resolver's default behaviour changed."
    )
