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
from pathlib import Path

import pytest

from tolokaforge.core._runner_subset import (
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
    ``[tool.hatch.build.targets.custom]`` must mirror the three tuples in
    :mod:`tolokaforge.core._runner_subset` exactly. The subset build's
    package graph is derived from those tuples; drift between the two would
    silently ship a wheel whose contents no longer match the audited
    partition."""
    custom = _load_pyproject_custom_target()

    expected_only_include = {*RUNNER_SUBSET_PACKAGES, *RUNNER_SUBSET_LOOSE_FILES}
    actual_only_include = set(custom.get("only-include", []))
    only_missing = sorted(expected_only_include - actual_only_include)
    only_extra = sorted(actual_only_include - expected_only_include)
    assert (
        not only_missing and not only_extra
    ), "pyproject `[tool.hatch.build.targets.custom].only-include` drifted " "from RUNNER_SUBSET_PACKAGES ∪ RUNNER_SUBSET_LOOSE_FILES:\n" + "".join(
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
    swap the dependency list, and strip entry points for the subset wheel."""
    custom = _load_pyproject_custom_target()
    script_rel = custom.get("path")
    assert script_rel == "scripts/hatch/hatch_runner_subset_builder.py", (
        "[tool.hatch.build.targets.custom].path must point at "
        "scripts/hatch/hatch_runner_subset_builder.py; got: " + repr(script_rel)
    )
    assert (
        REPO_ROOT / script_rel
    ).is_file(), f"custom builder script does not exist on disk: {script_rel}"
