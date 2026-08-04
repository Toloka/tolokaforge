"""Runner subset partition lock (ADR-0025 § "The module partition").

:mod:`tolokaforge.core._runner_subset` names the exact set of first-party
files the runner subset build target ships. This test freezes that
partition against the actual runner container's runtime import closure so
drift in either direction fails CI:

1. **Positive lock (closure ⊆ subset).** Every first-party module the runner
   container's boot path reaches (walking imports from
   :mod:`tolokaforge.runner.__main__` + :mod:`tolokaforge.runner.service`)
   must be classified in-subset. Adding a runner import that would need a
   module the subset omits fails here — otherwise it fails at container
   start, silently in the artifact and loudly in production.

2. **Negative lock (subset ⊆ closure ∪ lazy-loadable).** Every file the
   subset ships must either be reached by that same closure or be a lazy /
   entry-point-loadable module the runner reconstructs from a task schema
   at ``RegisterTrial`` (the ``tolokaforge.tools.builtin.*`` drivers and the
   two persistent-shell / str-replace-editor wrappers dispatched through
   :mod:`tolokaforge.tools.builtin.registry`). A subset entry that fits
   neither is dead weight in the image.

3. **Cross-import invariant.** No file classified in the subset may import
   a file classified out of the subset. A shared-spine module that reaches
   an orchestrator-only sibling would either fail at import inside the
   runner container or force the sibling into the subset — either way the
   partition is wrong. The static AST walk catches this at any nesting
   depth (module-level or deferred function-body).

The subset closure runs in a clean subprocess for the same reason as
:mod:`tests.canonical.test_runner_import_boundary`: the pytest process has
already imported much of the tree, so an in-process footprint proves
nothing.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from tolokaforge.core._runner_subset import (
    RUNNER_SUBSET_DATA_FILES,
    RUNNER_SUBSET_EXCLUDED_FILES,
    RUNNER_SUBSET_LOOSE_FILES,
    RUNNER_SUBSET_PACKAGES,
    is_in_runner_subset,
)

pytestmark = pytest.mark.canonical

REPO_ROOT = Path(__file__).resolve().parents[2]
TOLOKAFORGE_ROOT = REPO_ROOT / "tolokaforge"

# Lazy / dispatch-loaded modules the runner reaches from a live
# ``RegisterTrial``, not from boot. Present in the subset because the runner
# reconstructs them from a task schema at runtime; absent from the
# boot-time closure because no boot-side import statement names them.
LAZY_LOADABLE_SUBSET_MODULES: frozenset[str] = frozenset(
    {
        "tolokaforge/tools/__init__.py",
        "tolokaforge/tools/builtin/__init__.py",
        "tolokaforge/tools/builtin/bash.py",
        "tolokaforge/tools/builtin/browser.py",
        "tolokaforge/tools/builtin/calculator.py",
        "tolokaforge/tools/builtin/db_json.py",
        "tolokaforge/tools/builtin/files.py",
        "tolokaforge/tools/builtin/http_request.py",
        "tolokaforge/tools/builtin/mobile.py",
        "tolokaforge/tools/builtin/rag_search.py",
        "tolokaforge/tools/builtin/registry.py",
        # ``runner.proto`` is a source-level protobuf definition, not a
        # Python module — the compiled ``runner_pb2*.py`` files carry its
        # runtime surface. Kept in the subset for repro / regeneration.
        "tolokaforge/runner/runner.proto",
        # Audit / documentation artifact — imported by this test but not by
        # any runtime path. Shipping it inside the subset lets the follow-up
        # build script and image-side introspection consume it verbatim.
        "tolokaforge/core/_runner_subset.py",
        # Subset-native CLI shim (ADR-0026). Not reached by the runner boot
        # closure — the shim is invoked via ``docker exec tolokaforge …`` on
        # a running container after boot, so its imports (``click``,
        # ``importlib.metadata``) are cold at ``python -m tolokaforge.runner``.
        # Shipped in the subset because pip's ``[project.scripts]`` binds
        # ``tolokaforge = tolokaforge.runner._cli:main`` at install time.
        "tolokaforge/runner/_cli.py",
    }
)


_CLOSURE_PROBE = r"""
import builtins
import json
import sys

edges = {}
real_import = builtins.__import__


def tracking_import(name, globals=None, locals=None, fromlist=(), level=0):
    parent = (globals or {}).get("__name__")
    before = set(sys.modules)
    module = real_import(name, globals, locals, fromlist, level)
    for new_module in set(sys.modules) - before:
        edges.setdefault(new_module, parent)
    return module


builtins.__import__ = tracking_import
import tolokaforge.runner.__main__  # noqa: F401
import tolokaforge.runner.service  # noqa: F401
builtins.__import__ = real_import

first_party = sorted(m for m in sys.modules if m.split(".")[0] == "tolokaforge")
json.dump(first_party, sys.stdout)
"""


@pytest.fixture(scope="module")
def runner_boot_closure() -> frozenset[str]:
    """Set of first-party modules imported by ``python -m tolokaforge.runner``."""
    result = subprocess.run(
        [sys.executable, "-c", _CLOSURE_PROBE],
        capture_output=True,
        text=True,
    )
    assert (
        result.returncode == 0
    ), f"runner boot closure probe failed (exit {result.returncode}):\n{result.stderr}"
    return frozenset(json.loads(result.stdout))


def _module_to_path(module_name: str) -> str:
    """Repo-relative POSIX path for *module_name* — package ``__init__`` for a
    package, ``<leaf>.py`` for a module."""
    rel = module_name.replace(".", "/")
    pkg_init = TOLOKAFORGE_ROOT.parent / rel / "__init__.py"
    if pkg_init.is_file():
        return f"{rel}/__init__.py"
    return f"{rel}.py"


def _enumerate_subset_files() -> frozenset[str]:
    """All ``.py`` (and ``.proto``) files the subset covers, per the current
    classification in :mod:`tolokaforge.core._runner_subset`."""
    files: set[str] = set()
    for pkg in RUNNER_SUBSET_PACKAGES:
        pkg_dir = REPO_ROOT / pkg
        for path in pkg_dir.rglob("*"):
            if "__pycache__" in path.parts:
                continue
            if not path.is_file():
                continue
            if path.suffix not in {".py", ".proto"}:
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel in RUNNER_SUBSET_EXCLUDED_FILES:
                continue
            files.add(rel)
    files.update(RUNNER_SUBSET_LOOSE_FILES)
    return frozenset(files)


def test_declared_loose_files_exist() -> None:
    """Every path in the loose-files tuple must exist on disk."""
    missing = [p for p in RUNNER_SUBSET_LOOSE_FILES if not (REPO_ROOT / p).is_file()]
    assert not missing, "RUNNER_SUBSET_LOOSE_FILES names paths that no longer exist:\n" + "\n".join(
        f"  - {m}" for m in missing
    )


def test_runner_boot_closure_is_covered_by_subset(runner_boot_closure: frozenset[str]) -> None:
    """Every module reached by ``python -m tolokaforge.runner`` must map to a
    file the subset ships."""
    missing: list[str] = []
    for module in sorted(runner_boot_closure):
        rel = _module_to_path(module)
        if not is_in_runner_subset(rel):
            missing.append(f"{module} -> {rel}")
    assert not missing, (
        "runner boot closure reaches files the subset would omit — the "
        "runner container would fail at import inside the slim image:\n"
        + "\n".join(f"  - {m}" for m in missing)
    )


def test_subset_files_are_reachable(runner_boot_closure: frozenset[str]) -> None:
    """Every file shipped in the subset is either reached at boot or a
    dispatch-loaded driver the runner reconstructs from task schemas."""
    boot_files = {_module_to_path(m) for m in runner_boot_closure}
    subset_files = _enumerate_subset_files()
    dead: list[str] = []
    for rel in sorted(subset_files):
        if rel in boot_files:
            continue
        if rel in LAZY_LOADABLE_SUBSET_MODULES:
            continue
        dead.append(rel)
    assert not dead, (
        "subset ships files that neither the runner boot closure nor the "
        "lazy-loadable dispatch surface reaches — dead weight in the "
        "runner image:\n" + "\n".join(f"  - {d}" for d in dead)
    )


def _resolve_import_from(node: ast.ImportFrom, package: str) -> str | None:
    if node.level == 0:
        return node.module
    bits = package.rsplit(".", node.level - 1)
    if len(bits) < node.level:
        return None
    base = bits[0]
    return f"{base}.{node.module}" if node.module else base


def _module_qualname(path: Path) -> tuple[str, bool]:
    parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return ".".join(parts), is_package


def _target_to_path(dotted: str) -> str | None:
    """Repo-relative path for a dotted first-party import target, or None if
    the target names a file we cannot classify (external package, missing
    module, plain namespace)."""
    if not (dotted == "tolokaforge" or dotted.startswith("tolokaforge.")):
        return None
    rel = dotted.replace(".", "/")
    pkg_init = REPO_ROOT / rel / "__init__.py"
    if pkg_init.is_file():
        return f"{rel}/__init__.py"
    module_file = REPO_ROOT / f"{rel}.py"
    if module_file.is_file():
        return f"{rel}.py"
    return None


def test_subset_files_do_not_import_orchestrator_only_siblings() -> None:
    """A shared-spine file that reached an orchestrator-only sibling would
    force that sibling into the subset (or crash inside the slim image).
    Static AST walk covers every ``import`` / ``from … import`` at any
    nesting depth, including deferred function-body imports."""
    subset_files = _enumerate_subset_files()
    violations: list[str] = []
    for rel in sorted(subset_files):
        path = REPO_ROOT / rel
        if path.suffix != ".py":
            continue
        module_name, is_package = _module_qualname(path)
        package = module_name if is_package else module_name.rpartition(".")[0]
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover - would fail the ruff gate first
            violations.append(f"{rel}: parse error {exc}")
            continue
        for node in ast.walk(tree):
            targets: list[tuple[int, str]] = []
            if isinstance(node, ast.Import):
                for alias in node.names:
                    targets.append((node.lineno, alias.name))
            elif isinstance(node, ast.ImportFrom):
                resolved = _resolve_import_from(node, package)
                if resolved is None:
                    continue
                targets.append((node.lineno, resolved))
                for alias in node.names:
                    targets.append((node.lineno, f"{resolved}.{alias.name}"))
            for lineno, dotted in targets:
                target_path = _target_to_path(dotted)
                if target_path is None:
                    continue
                if target_path == rel:
                    continue
                if is_in_runner_subset(target_path):
                    continue
                violations.append(f"{rel}:{lineno} imports {dotted} -> {target_path}")
    assert not violations, (
        "subset files reach orchestrator-only siblings — the partition is "
        "inconsistent:\n" + "\n".join(f"  - {v}" for v in violations)
    )


def test_declared_excluded_files_exist() -> None:
    """Every path in the excluded-files tuple must exist on disk — otherwise
    the exclusion is stale and hides that a real file drifted."""
    missing = [p for p in RUNNER_SUBSET_EXCLUDED_FILES if not (REPO_ROOT / p).is_file()]
    assert (
        not missing
    ), "RUNNER_SUBSET_EXCLUDED_FILES names paths that no longer exist:\n" + "\n".join(
        f"  - {m}" for m in missing
    )


def _load_pyproject_custom_target() -> dict[str, list[str]]:
    """Read ``[tool.hatch.build.targets.custom]`` from ``pyproject.toml``.

    Uses ``tomllib`` on 3.11+, ``tomli`` on 3.10 (already a transitive dev
    dependency)."""
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:  # Python 3.10 branch
        import tomli as tomllib  # type: ignore[import-not-found,no-redef]

    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    return data["tool"]["hatch"]["build"]["targets"]["custom"]


def test_pyproject_custom_target_mirrors_runner_subset_module() -> None:
    """The ``only-include`` / ``exclude`` lists under
    ``[tool.hatch.build.targets.custom]`` must mirror the four tuples in
    :mod:`tolokaforge.core._runner_subset` exactly. The subset build's
    package graph is derived from those tuples; drift between the two would
    silently ship a wheel whose contents no longer match the audited
    partition.

    ``RUNNER_SUBSET_DATA_FILES`` is expected in ``only-include`` alongside
    the packages and loose Python files: pricing / preset payloads that
    runtime code reads at import time via ``importlib.resources`` (GitHub
    #830). Force-includes for path-remapped files (the repo-root
    ``.python-version`` dotfile → ``tolokaforge/_python_version.txt``) live
    in the sibling ``force-include`` table and are checked separately."""
    custom = _load_pyproject_custom_target()

    expected_only_include = {
        *RUNNER_SUBSET_PACKAGES,
        *RUNNER_SUBSET_LOOSE_FILES,
        # Data files ship at their destination path; the force-include
        # exception (``.python-version`` → ``tolokaforge/_python_version.txt``)
        # is not listed in ``only-include`` because hatchling reads it from
        # the ``force-include`` table.
        *(p for p in RUNNER_SUBSET_DATA_FILES if p != "tolokaforge/_python_version.txt"),
    }
    actual_only_include = set(custom.get("only-include", []))
    only_missing = sorted(expected_only_include - actual_only_include)
    only_extra = sorted(actual_only_include - expected_only_include)
    assert (
        not only_missing and not only_extra
    ), "pyproject `[tool.hatch.build.targets.custom].only-include` drifted " "from RUNNER_SUBSET_PACKAGES ∪ LOOSE_FILES ∪ DATA_FILES:\n" + "".join(
        f"  - missing in pyproject: {m}\n" for m in only_missing
    ) + "".join(
        f"  - extra in pyproject:   {e}\n" for e in only_extra
    )

    expected_exclude = set(RUNNER_SUBSET_EXCLUDED_FILES)
    actual_exclude = set(custom.get("exclude", []))
    exclude_missing = sorted(expected_exclude - actual_exclude)
    exclude_extra = sorted(actual_exclude - expected_exclude)
    assert (
        not exclude_missing and not exclude_extra
    ), "pyproject `[tool.hatch.build.targets.custom].exclude` drifted from " "RUNNER_SUBSET_EXCLUDED_FILES:\n" + "".join(
        f"  - missing in pyproject: {m}\n" for m in exclude_missing
    ) + "".join(
        f"  - extra in pyproject:   {e}\n" for e in exclude_extra
    )


def test_pyproject_custom_target_points_at_builder_script() -> None:
    """The custom build target must reference the runner-subset builder
    script; that file is what teaches hatchling to rename the distribution,
    swap the dependency list, and register the subset-native CLI shim as
    the subset wheel's ``[console_scripts]`` entry (ADR-0026)."""
    custom = _load_pyproject_custom_target()
    script_rel = custom.get("path")
    assert script_rel == "scripts/hatch/hatch_runner_subset_builder.py", (
        "[tool.hatch.build.targets.custom].path must point at "
        "scripts/hatch/hatch_runner_subset_builder.py; got: " + repr(script_rel)
    )
    assert (
        REPO_ROOT / script_rel
    ).is_file(), f"custom builder script does not exist on disk: {script_rel}"


# ---------------------------------------------------------------------------
# Subset-native CLI shim (ADR-0026) — the subset wheel's ``[project.scripts]``
# binds ``tolokaforge = tolokaforge.runner._cli:main``. The shim itself lives
# in the subset partition and must obey the same import-boundary rules the
# rest of the shared spine does; the tests below lock those two invariants.
# ---------------------------------------------------------------------------


CLI_SHIM_PATH = "tolokaforge/runner/_cli.py"
"""Repo-relative path of the subset-native CLI shim."""

_CLI_FORBIDDEN_IMPORT_ROOTS: frozenset[str] = frozenset(
    {
        # dx tree — Click command tree + terminal front-end, base-wheel only.
        "tolokaforge.dx",
        # Adapters — native / terminal-bench / bundle writer, base-wheel only.
        "tolokaforge.adapters",
        # Base-wheel console-script entry (would collide with the shim).
        "tolokaforge._entry",
        # Docker + orchestrator machinery — the whole "compose stack + drive a
        # multi-trial run" surface the shim explicitly does not reach into.
        "tolokaforge.docker",
        "tolokaforge.core.orchestrator",
        "tolokaforge.core.run_trial",
        "tolokaforge.core.compose_materialisation",
        "tolokaforge.core.shared_stack_runtime",
        "tolokaforge.core.per_trial_runtime",
        "tolokaforge.core.docker_adapter",
        "tolokaforge.core.plugin_registry",
        "tolokaforge.core.runtime",
        "tolokaforge.core.conductor",
        "tolokaforge.core.trial_grader",
    }
)


def test_cli_shim_is_in_the_subset() -> None:
    """The CLI shim lives inside the ``tolokaforge/runner`` subpackage
    (which the ``RUNNER_SUBSET_PACKAGES`` tuple ships wholesale), so
    ``is_in_runner_subset`` must return ``True`` for it. If the file moves
    or the partition drifts, the subset wheel would silently omit the
    console-script binding target."""
    assert (
        REPO_ROOT / CLI_SHIM_PATH
    ).is_file(), f"ADR-0026 subset-native CLI shim missing on disk: {CLI_SHIM_PATH}"
    assert is_in_runner_subset(CLI_SHIM_PATH), (
        f"{CLI_SHIM_PATH} classifies out-of-subset — the subset wheel would "
        "install without the ADR-0026 shim, and `docker exec … tolokaforge …` "
        "would fail with 'executable file not found in $PATH' inside the "
        "runner image."
    )


def test_cli_shim_does_not_reach_orchestrator_or_dx() -> None:
    """The ADR-0026 shim binds to the runner subset's public surface only —
    LLM client, tool-calling loop, gRPC client, trial models — and must not
    reach into ``tolokaforge/dx/*``, ``tolokaforge/adapters/*``, or the
    orchestrator-only sibling modules inside ``tolokaforge/core/*``. A
    static AST walk over every ``import`` / ``from … import`` (including
    deferred function-body imports) enforces the boundary; the wider
    ``test_subset_files_do_not_import_orchestrator_only_siblings`` covers
    the same rule for the whole subset, but the shim gets its own
    assertion so a regression here surfaces at the point of the offence."""
    path = REPO_ROOT / CLI_SHIM_PATH
    tree = ast.parse(path.read_text(encoding="utf-8"))
    violations: list[str] = []
    for node in ast.walk(tree):
        targets: list[tuple[int, str]] = []
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            resolved = node.module if node.level == 0 else f"tolokaforge.runner.{node.module}"
            targets.append((node.lineno, resolved))
        for lineno, dotted in targets:
            for forbidden in _CLI_FORBIDDEN_IMPORT_ROOTS:
                if dotted == forbidden or dotted.startswith(forbidden + "."):
                    violations.append(
                        f"{CLI_SHIM_PATH}:{lineno} imports {dotted} "
                        f"(under forbidden root {forbidden})"
                    )
                    break
    assert not violations, (
        "subset-native CLI shim reaches base-wheel-only surfaces — the "
        "runner image would import a module the subset does not ship, or "
        "the ADR-0026 partition contract would be violated:\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# Subset wheel structural checks — build once, inspect the artifact.
#
# The subset wheel is the actual thing pip installs into the runner image,
# so its METADATA / RECORD / entry_points.txt are the ground truth. Tests
# below verify what pip sees, not what pyproject implies.
# ---------------------------------------------------------------------------

_SUBSET_WHEEL_GLOB = "tolokaforge_runner_subset-*.whl"


def _find_subset_wheel() -> Path | None:
    """Return the latest subset wheel under ``dist/``, or ``None`` if the
    build has not run yet in this checkout."""
    dist_dir = REPO_ROOT / "dist"
    if not dist_dir.is_dir():
        return None
    wheels = sorted(dist_dir.glob(_SUBSET_WHEEL_GLOB))
    return wheels[-1] if wheels else None


@pytest.fixture(scope="module")
def subset_wheel_path() -> Path:
    """Path to the subset wheel — build one on demand if the ``dist/`` copy
    is stale or absent.

    Invokes the hatchling PEP 517 build backend directly via
    ``python -m hatchling build -t custom``. Equivalent to
    ``hatch build --target custom`` but drives hatchling straight from the
    active interpreter, so it has no dependency on the ``hatch`` CLI being
    installed or on hatch's ``default`` environment being resolvable — both
    of which fail cleanly in a uv-managed venv where only ``hatchling``
    (the build backend, listed in ``[dependency-groups] dev``) is present."""
    existing = _find_subset_wheel()
    if existing is not None:
        return existing
    build_result = subprocess.run(
        [sys.executable, "-m", "hatchling", "build", "-t", "custom"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if build_result.returncode != 0:
        pytest.fail(
            f"subset wheel build failed (exit {build_result.returncode}):\n"
            f"stdout:\n{build_result.stdout}\nstderr:\n{build_result.stderr}"
        )
    wheel = _find_subset_wheel()
    if wheel is None:
        pytest.fail("subset wheel build reported success but produced no artifact under dist/")
    return wheel


def _wheel_read(wheel_path: Path, member_glob: str) -> str:
    """Read one wheel member (first name matching ``member_glob``) as UTF-8."""
    with zipfile.ZipFile(wheel_path) as zf:
        for name in zf.namelist():
            if Path(name).match(member_glob):
                return zf.read(name).decode("utf-8")
    raise AssertionError(f"member matching {member_glob!r} not found in {wheel_path.name}")


def _wheel_members(wheel_path: Path) -> frozenset[str]:
    """Return every path the wheel archive lists (RECORD-equivalent)."""
    with zipfile.ZipFile(wheel_path) as zf:
        return frozenset(zf.namelist())


def test_subset_wheel_binds_cli_shim_console_script(subset_wheel_path: Path) -> None:
    """The subset wheel's ``entry_points.txt`` must bind the ADR-0026 shim
    as the ``tolokaforge`` console script. Without this, ``docker exec
    tolokaforge-runner tolokaforge --version`` fails with "executable
    file not found in $PATH" — the ADR-0024 committed exec surface would
    vanish on the slim image."""
    entry_points_txt = _wheel_read(subset_wheel_path, "*.dist-info/entry_points.txt")
    assert "[console_scripts]" in entry_points_txt, (
        "subset wheel entry_points.txt is missing the [console_scripts] "
        f"section:\n{entry_points_txt}"
    )
    assert "tolokaforge = tolokaforge.runner._cli:main" in entry_points_txt, (
        "subset wheel entry_points.txt does not bind the ADR-0026 shim: "
        f"expected 'tolokaforge = tolokaforge.runner._cli:main', got:\n"
        f"{entry_points_txt}"
    )


def test_subset_wheel_ships_cli_shim_module(subset_wheel_path: Path) -> None:
    """The subset wheel must include ``tolokaforge/runner/_cli.py`` as a
    Python source file; the console script binding is inert without it."""
    members = _wheel_members(subset_wheel_path)
    assert "tolokaforge/runner/_cli.py" in members, (
        "subset wheel does not ship tolokaforge/runner/_cli.py — the "
        f"ADR-0026 shim's binding target is missing from the wheel. "
        f"Wheel members starting with tolokaforge/runner/: "
        f"{sorted(m for m in members if m.startswith('tolokaforge/runner/'))}"
    )


def test_subset_wheel_ships_data_files_for_pricing_and_presets(
    subset_wheel_path: Path,
) -> None:
    """GitHub #830: the subset wheel must include the pricing table and
    model-preset registry as bundled data files. Without them, the runner
    image boots with an empty pricing table (silent cost-tracking regression)
    and the preset registry raises at first grading-model resolution."""
    members = _wheel_members(subset_wheel_path)
    expected = {
        "tolokaforge/core/data/pricing.json",
        "tolokaforge/core/data/model_presets.yaml",
    }
    missing = sorted(expected - members)
    assert not missing, (
        "subset wheel is missing data files (GitHub #830 regression):\n"
        + "\n".join(f"  - {m}" for m in missing)
        + "\nAll RUNNER_SUBSET_DATA_FILES entries must ship in the wheel."
    )


def test_subset_wheel_metadata_is_the_subset_distribution(subset_wheel_path: Path) -> None:
    """The subset wheel's METADATA must name the distribution
    ``tolokaforge-runner-subset`` (not ``tolokaforge``) so pip's install
    metadata inside the runner image makes clear which build variant is
    installed. ``importlib.metadata.version('tolokaforge-runner-subset')``
    — the version resolution the ADR-0026 shim's ``--version`` uses — is
    what this METADATA line services."""
    metadata_txt = _wheel_read(subset_wheel_path, "*.dist-info/METADATA")
    assert "Name: tolokaforge-runner-subset" in metadata_txt, (
        "subset wheel METADATA is missing the tolokaforge-runner-subset "
        "distribution name — importlib.metadata.version() lookups in the "
        f"ADR-0026 shim would fail inside the runner image:\n{metadata_txt[:300]}"
    )


def test_subset_wheel_does_not_ship_orchestrator_only_files(
    subset_wheel_path: Path,
) -> None:
    """The subset partition's ``EXCLUDED_FILES`` tuple is meaningful only
    if the wheel builder actually drops those files. This test asserts
    against the ground-truth wheel archive, not the enumeration."""
    members = _wheel_members(subset_wheel_path)
    leaked = sorted(f for f in RUNNER_SUBSET_EXCLUDED_FILES if f in members)
    assert not leaked, (
        "subset wheel ships files the partition marks as orchestrator-only "
        "— the runner image installs orchestrator scaffolding it does not "
        "need and cannot use:\n" + "\n".join(f"  - {leak}" for leak in leaked)
    )
