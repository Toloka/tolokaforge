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

from tests.utils.wheel_builds import build_subset_wheel
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
        "tolokaforge/tools/builtin/build_check.py",
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
        # Subset-native CLI shim (ADR-0027). Not reached by the runner boot
        # closure — the shim is invoked via ``docker exec tolokaforge …`` on
        # a running container after boot, so its imports (``click``,
        # ``importlib.metadata``) are cold at ``python -m tolokaforge.runner``.
        # Shipped in the subset because pip's ``[project.scripts]`` binds
        # ``tolokaforge = tolokaforge.runner._cli:main`` at install time.
        "tolokaforge/runner/_cli.py",
        # Sub-component seam reference impls resolved through
        # ``importlib.metadata`` entry-points at ``RunnerServiceImpl.__init__``
        # (``load_judge_model_provider('litellm')`` /
        # ``load_rubric_evaluator('llm_judge')`` /
        # ``load_transcript_rule_matcher('default')`` /
        # ``load_state_check_backend('jsonpath'|'db_probes')``), not by a
        # module-level import. ``judge.py`` is reached at grade time from
        # ``default_rubric_evaluator``; ``rubric.py`` is reached at grade
        # time from ``judge.py``. Shipped in the subset because the runner
        # container calls each one on the grading path.
        "tolokaforge/core/grading/default_judge_model_provider.py",
        "tolokaforge/core/grading/default_rubric_evaluator.py",
        "tolokaforge/core/grading/default_state_check_backends.py",
        "tolokaforge/core/grading/default_transcript_rule_matcher.py",
        "tolokaforge/core/grading/judge.py",
        "tolokaforge/core/grading/rubric.py",
        # Seam-neutral judge-prompt composition helpers (body + marker
        # contract, ``effective_judge_system_prompt``). Reached at grade
        # time from ``core.grading.judge`` (runner-side, via the lazy
        # dispatch above) and at bundle-write time from
        # ``core.conductor`` (orchestrator-side, host-only). Boot-time
        # closure never observes it.
        "tolokaforge/core/judge_prompt.py",
        # Shared tool-artifact extraction helper. ``RunnerServiceImpl``
        # reaches it via a function-local import at ``RegisterTrial`` time
        # (``runner/service.py::_extract_tool_artifacts``) so a task packing
        # ``tool_artifacts`` can decode ``checks.py`` + siblings before the
        # trial starts — never at ``python -m tolokaforge.runner`` boot,
        # which is why the boot-time closure walk does not observe it.
        "tolokaforge/core/grading/tool_artifacts.py",
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


def _is_type_checking_guard(node: ast.If) -> bool:
    """Return True when *node* is ``if TYPE_CHECKING:`` (or ``typing.TYPE_CHECKING``).

    A guarded import runs only under a static type checker — it never
    executes inside the runner container and cannot drag a target module
    into the runtime closure.
    """
    test = node.test
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
        return isinstance(test.value, ast.Name) and test.value.id == "typing"
    return False


def _collect_runtime_imports(tree: ast.AST) -> list[ast.stmt]:
    """Return every ``Import``/``ImportFrom`` reachable at runtime.

    ``ast.walk`` traverses ``if TYPE_CHECKING:`` blocks too, so the raw
    walk over-reports for the partition check. This helper skips those
    guard branches while keeping every other block (function bodies,
    class bodies, plain conditionals) intact.
    """
    imports: list[ast.stmt] = []
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.If) and _is_type_checking_guard(node):
            stack.extend(node.orelse)
            continue
        if isinstance(node, ast.Import | ast.ImportFrom):
            imports.append(node)
        for child in ast.iter_child_nodes(node):
            stack.append(child)
    return imports


def test_subset_files_do_not_import_orchestrator_only_siblings() -> None:
    """A shared-spine file that reached an orchestrator-only sibling would
    force that sibling into the subset (or crash inside the slim image).
    Static AST walk covers every ``import`` / ``from … import`` at any
    nesting depth, including deferred function-body imports. ``if
    TYPE_CHECKING:`` blocks are skipped — those imports run only under a
    static type checker and never inside the runner container."""
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
        for node in _collect_runtime_imports(tree):
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
    the subset wheel's ``[console_scripts]`` entry (ADR-0027)."""
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
# Subset-native CLI shim (ADR-0027) — the subset wheel's ``[project.scripts]``
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
    ).is_file(), f"ADR-0027 subset-native CLI shim missing on disk: {CLI_SHIM_PATH}"
    assert is_in_runner_subset(CLI_SHIM_PATH), (
        f"{CLI_SHIM_PATH} classifies out-of-subset — the subset wheel would "
        "install without the ADR-0027 shim, and `docker exec … tolokaforge …` "
        "would fail with 'executable file not found in $PATH' inside the "
        "runner image."
    )


def test_cli_shim_does_not_reach_orchestrator_or_dx() -> None:
    """The ADR-0027 shim binds to the runner subset's public surface only —
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
        "the ADR-0027 partition contract would be violated:\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# Subset wheel structural checks — build once, inspect the artifact.
#
# The subset wheel is the actual thing pip installs into the runner image,
# so its METADATA / RECORD / entry_points.txt are the ground truth. Tests
# below verify what pip sees, not what pyproject implies.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def subset_wheel_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The subset wheel built from the current tree.

    The tests below read the archive to state what pip installs into the
    runner image, so the archive has to come from the code under test — an
    artifact left in ``dist/`` by an earlier commit would let them attest to
    a partition that no longer exists."""
    return build_subset_wheel(tmp_path_factory.mktemp("subset_wheel"))


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
    """The subset wheel's ``entry_points.txt`` must bind the ADR-0027 shim
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
        "subset wheel entry_points.txt does not bind the ADR-0027 shim: "
        f"expected 'tolokaforge = tolokaforge.runner._cli:main', got:\n"
        f"{entry_points_txt}"
    )


def test_subset_wheel_carries_runner_reachable_entry_point_groups(
    subset_wheel_path: Path,
) -> None:
    """The subset wheel's ``entry_points.txt`` must carry every group the
    runner reaches through
    :func:`~tolokaforge.core.plugin_registry.discover_entry_points` at
    boot or during a Grade RPC, AND every row's ``module:attr`` target
    must ship in the subset wheel. A missing group crashes the runner at
    first seam load with "Unknown implementation" (the exact regression
    that broke v0.20.0-rc.1's runtime start); a shipped group whose row
    targets a module the subset excludes crashes with ``ModuleNotFoundError``
    from ``.load()`` — same class of failure, one indirection deeper."""
    import configparser

    from scripts.hatch.hatch_runner_subset_builder import (
        RUNNER_REACHABLE_ENTRY_POINT_GROUPS,
    )

    entry_points_txt = _wheel_read(subset_wheel_path, "*.dist-info/entry_points.txt")
    parser = configparser.ConfigParser()
    parser.read_string(entry_points_txt)
    members = _wheel_members(subset_wheel_path)
    for group in RUNNER_REACHABLE_ENTRY_POINT_GROUPS:
        assert parser.has_section(group), (
            f"subset wheel entry_points.txt is missing runner-reachable "
            f"group [{group}] — the runner container will crash at first "
            "seam load. Full entry_points.txt:\n" + entry_points_txt
        )
        for name, target in parser.items(group):
            module_dotted = target.partition(":")[0].strip()
            module_rel = module_dotted.replace(".", "/") + ".py"
            package_init = module_dotted.replace(".", "/") + "/__init__.py"
            assert module_rel in members or package_init in members, (
                f"subset wheel entry-point [{group}]/{name}={target} points at "
                f"{module_dotted!r} but neither {module_rel} nor "
                f"{package_init} ships in the subset wheel — .load() will "
                "raise ModuleNotFoundError at first use."
            )


_LOADER_TO_GROUP: dict[str, str] = {
    "load_runtime_backend": "tolokaforge.runtime_backends",
    "load_trial_grader": "tolokaforge.trial_graders",
    "load_conductor": "tolokaforge.conductors",
    "load_readiness_probe": "tolokaforge.service_readiness_probes",
    "load_turn_policy": "tolokaforge.turn_policies",
    "load_custom_check_executor": "tolokaforge.custom_check_executors",
    "load_judge_model_provider": "tolokaforge.judge_model_providers",
    "load_rubric_evaluator": "tolokaforge.rubric_evaluators",
    "load_transcript_rule_matcher": "tolokaforge.transcript_rule_matchers",
    "load_state_check_backend": "tolokaforge.state_check_backends",
    "load_trace_check_operator": "tolokaforge.trace_check_operators",
    "load_grading_substrate": "tolokaforge.grading_substrates",
}


def test_subset_partition_load_calls_are_in_the_allowlist() -> None:
    """Every ``load_*`` seam call reachable from the subset partition must
    resolve to a group in ``RUNNER_REACHABLE_ENTRY_POINT_GROUPS``. This
    closes the drift direction the header-only lock cannot: someone adds
    a NEW runner-side ``load_&lt;seam&gt;(…)`` call, forgets the allowlist,
    and the runner container boots then crashes on the first call.
    A pyproject-side registration alone is not enough — the subset wheel's
    ``entry_points.txt`` is what ``importlib.metadata`` reads inside the
    slim image."""
    from scripts.hatch.hatch_runner_subset_builder import (
        RUNNER_REACHABLE_ENTRY_POINT_GROUPS,
    )

    allowlist = frozenset(RUNNER_REACHABLE_ENTRY_POINT_GROUPS)
    subset_files = _enumerate_subset_files()
    used_loaders: dict[str, set[str]] = {}
    for rel in subset_files:
        if not rel.endswith(".py"):
            continue
        source_path = REPO_ROOT / rel
        try:
            tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name in _LOADER_TO_GROUP:
                used_loaders.setdefault(name, set()).add(rel)

    missing_from_allowlist = {
        loader: (_LOADER_TO_GROUP[loader], sorted(sites))
        for loader, sites in used_loaders.items()
        if _LOADER_TO_GROUP[loader] not in allowlist
    }
    assert not missing_from_allowlist, (
        "subset partition reaches load_* seams whose groups are not in "
        "RUNNER_REACHABLE_ENTRY_POINT_GROUPS — add each group to the allowlist "
        "in scripts/hatch/hatch_runner_subset_builder.py:\n"
        + "\n".join(
            f"  {loader} → {group} (call sites: {', '.join(sites)})"
            for loader, (group, sites) in sorted(missing_from_allowlist.items())
        )
    )


def test_subset_wheel_ships_cli_shim_module(subset_wheel_path: Path) -> None:
    """The subset wheel must include ``tolokaforge/runner/_cli.py`` as a
    Python source file; the console script binding is inert without it."""
    members = _wheel_members(subset_wheel_path)
    assert "tolokaforge/runner/_cli.py" in members, (
        "subset wheel does not ship tolokaforge/runner/_cli.py — the "
        f"ADR-0027 shim's binding target is missing from the wheel. "
        f"Wheel members starting with tolokaforge/runner/: "
        f"{sorted(m for m in members if m.startswith('tolokaforge/runner/'))}"
    )


def test_subset_wheel_requires_tolokaforge_models(subset_wheel_path: Path) -> None:
    """The runner container's pricing / preset / provider tables now ship
    inside the :mod:`tolokaforge_models` wheel — the subset wheel resolves
    them through ``Requires-Dist: tolokaforge-models`` at pip-install time.
    Without this dep, the runner image would boot with an empty pricing
    table (silent cost-tracking regression) and the preset registry would
    raise at first grading-model resolution."""
    metadata_txt = _wheel_read(subset_wheel_path, "*.dist-info/METADATA")
    matches = [
        line
        for line in metadata_txt.splitlines()
        if line.startswith("Requires-Dist: tolokaforge-models")
    ]
    assert matches, (
        "subset wheel METADATA is missing a Requires-Dist entry for "
        "tolokaforge-models — the runner image would install without the "
        "data-file provider and boot with empty pricing / preset / provider "
        "tables. Add the pin to scripts/hatch/hatch_runner_subset_builder.py "
        "SUBSET_DEPENDENCIES."
    )


def test_subset_wheel_does_not_ship_engine_data_dir(subset_wheel_path: Path) -> None:
    """Data files live in :mod:`tolokaforge_models`; the subset wheel must
    not re-ship them under ``tolokaforge/core/data/``. A stale entry in
    ``[tool.hatch.build.targets.custom].only-include`` would silently
    double-ship the tables and diverge from the models wheel on every
    subsequent overlay tweak."""
    members = _wheel_members(subset_wheel_path)
    stale = sorted(m for m in members if m.startswith("tolokaforge/core/data/"))
    assert not stale, (
        "subset wheel ships engine-side data files under "
        "tolokaforge/core/data/ — the cutover left them behind:\n"
        + "\n".join(f"  - {m}" for m in stale)
    )


def test_subset_wheel_metadata_is_the_subset_distribution(subset_wheel_path: Path) -> None:
    """The subset wheel's METADATA must name the distribution
    ``tolokaforge-runner-subset`` (not ``tolokaforge``) so pip's install
    metadata inside the runner image makes clear which build variant is
    installed. ``importlib.metadata.version('tolokaforge-runner-subset')``
    — the version resolution the ADR-0027 shim's ``--version`` uses — is
    what this METADATA line services."""
    metadata_txt = _wheel_read(subset_wheel_path, "*.dist-info/METADATA")
    assert "Name: tolokaforge-runner-subset" in metadata_txt, (
        "subset wheel METADATA is missing the tolokaforge-runner-subset "
        "distribution name — importlib.metadata.version() lookups in the "
        f"ADR-0027 shim would fail inside the runner image:\n{metadata_txt[:300]}"
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
