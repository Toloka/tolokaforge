"""Import-boundary lock for the ``tolokaforge.runner`` surface.

Three locks enforce that the runner sits *below* the orchestration / adapter /
CLI / docker-build surfaces and never drags them (or their deps) into the
runner image, inverting the dependency direction.

Locks 1 and 2 run in a *clean subprocess* — the pytest process has already
imported much of ``tolokaforge`` (conftest, sibling tests), so an in-process
footprint would be polluted and prove nothing. The subprocess imports only
``tolokaforge.runner.__main__`` + ``tolokaforge.runner.service`` (the runner's
module-load surface) while tracking parent→child import edges, so a violation
is reported with the first-party import chain that pulled it.

1. **Dependency-footprint subset.** Every third-party distribution the runner
   pulls at module load must lie inside the transitive-requires closure of
   ``[project.dependencies] ∪ [project.optional-dependencies].runner`` — the
   deps the runner image installs. A new ``import pandas`` in a runner module
   would fail here loud, naming the chain. The closure follows every requires
   edge (including extra-guarded ones): a deliberate over-approximation of the
   *allowed* side, so a legitimate transitive dep of a declared package never
   trips the test, while a genuinely foreign package still does.

2. **First-party module-load forbidden-surface.** The runner's transitive
   first-party closure *at module load* must not reach the orchestration /
   adapter / CLI / docker-build surfaces. Allowed core surfaces are
   ``core.grading.*``, ``core.llm.*``, ``core.models``, ``core.trial``,
   ``core.deprecations``, and ``secrets``.

3. **Static-AST forbidden-import-statement guard.** A source-level walk of every
   ``*.py`` under ``tolokaforge/runner/`` flags any ``import`` / ``from … import``
   statement that names a module under a forbidden prefix, *at any nesting depth*
   — including deferred function-body imports that never execute at module load
   and so slip past locks 1 and 2. A module is named either by the ``from``
   target (``from tolokaforge.adapters import …``, absolute or relative
   ``from ..adapters import …``) or by an imported name resolved against a
   non-forbidden parent (``from tolokaforge import adapters`` reaches the
   forbidden ``tolokaforge.adapters`` leaf). It matches statements only;
   ``importlib.import_module("<target>")`` string indirection is out of this
   guard's scope.

All three consume one ``FORBIDDEN_PREFIXES`` definition: locks 1/2 receive it as
a literal in the subprocess source, lock 3 imports it directly.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_ROOT = REPO_ROOT / "tolokaforge" / "runner"

FORBIDDEN_PREFIXES = (
    "tolokaforge.core.orchestrator",
    "tolokaforge.core.per_trial_runtime",
    "tolokaforge.core.shared_stack_runtime",
    "tolokaforge.core.compose_materialisation",
    "tolokaforge.adapters",
    "tolokaforge.cli",
    "tolokaforge.docker",
    "tolokaforge.runtime.reset_recipes",
)

_ANALYSIS = r"""
import builtins
import json
import re
import sys
from pathlib import Path

import importlib.metadata as im

import toml

REPO_ROOT = Path(sys.argv[1])

# grpc/__init__.py imports grpc_tools under a try/except back-compat shim, so
# `import grpc` drags grpc_tools into sys.modules; grpcio-tools is dev-only and
# absent from the slim runner image, so its presence here is not a runner-
# surface violation. grpc_reflection is the same shape.
GUARDED_OPTIONAL = {"grpc_tools", "grpc_reflection"}


def norm(name):
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def dep_name(spec):
    match = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
    return norm(match.group(1)) if match else None


pyproject = toml.loads((REPO_ROOT / "pyproject.toml").read_text())
roots = set()
for spec in pyproject["project"]["dependencies"]:
    name = dep_name(spec)
    if name and not name.startswith("tolokaforge"):
        roots.add(name)
for spec in pyproject["project"]["optional-dependencies"]["runner"]:
    name = dep_name(spec)
    if name and not name.startswith("tolokaforge"):
        roots.add(name)

allowed = set()
stack = list(roots)
while stack:
    dist = stack.pop()
    if dist in allowed:
        continue
    allowed.add(dist)
    try:
        requirements = im.requires(dist) or []
    except im.PackageNotFoundError:
        requirements = []
    for req in requirements:
        name = dep_name(req)
        if name and name not in allowed:
            stack.append(name)

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


def chain(module):
    path = [module]
    seen = {module}
    while module in edges and edges[module] and edges[module] not in seen:
        module = edges[module]
        path.append(module)
        seen.add(module)
    return " <- ".join(path)


distributions = im.packages_distributions()
stdlib = set(sys.stdlib_module_names) | set(sys.builtin_module_names)

footprint_violations = []
for name in list(sys.modules):
    top = name.split(".")[0]
    if top in stdlib or top == "tolokaforge" or top.startswith("_") or top in GUARDED_OPTIONAL:
        continue
    dists = distributions.get(top, [])
    if not dists or any(norm(dist) in allowed for dist in dists):
        continue
    footprint_violations.append(
        {"module": top, "dists": sorted(norm(dist) for dist in dists), "chain": chain(top)}
    )

forbidden_violations = []
for name in sorted(sys.modules):
    if name.split(".")[0] != "tolokaforge":
        continue
    if any(name == f or name.startswith(f + ".") for f in FORBIDDEN_PREFIXES):
        forbidden_violations.append({"module": name, "chain": chain(name)})

json.dump(
    {"footprint_violations": footprint_violations, "forbidden_violations": forbidden_violations},
    sys.stdout,
)
"""

# Feed the single FORBIDDEN_PREFIXES definition into the subprocess as a literal
# so locks 1/2 and lock 3 share one source of truth (no duplicated string list).
_SUBPROCESS_SOURCE = f"FORBIDDEN_PREFIXES = {FORBIDDEN_PREFIXES!r}\n{_ANALYSIS}"


def _run_analysis() -> dict:
    result = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_SOURCE, str(REPO_ROOT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"runner import-boundary analysis subprocess failed (exit {result.returncode}):\n"
        f"{result.stderr}"
    )
    return json.loads(result.stdout)


def _format(violations: list[dict]) -> str:
    return "\n".join(f"  - {v}" for v in violations)


@pytest.fixture(scope="module")
def analysis() -> dict:
    return _run_analysis()


def test_runner_dependency_footprint_within_declared_surface(analysis: dict) -> None:
    violations = analysis["footprint_violations"]
    assert not violations, (
        "runner imports third-party distributions outside "
        "[project.dependencies] ∪ [project.optional-dependencies].runner:\n" + _format(violations)
    )


def test_runner_does_not_import_forbidden_first_party_surface(analysis: dict) -> None:
    violations = analysis["forbidden_violations"]
    assert not violations, "runner's first-party closure reaches a forbidden surface:\n" + _format(
        violations
    )


def _matches_forbidden(target: str) -> bool:
    return any(target == f or target.startswith(f + ".") for f in FORBIDDEN_PREFIXES)


def _resolve_import_from(node: ast.ImportFrom, package: str) -> str | None:
    """Resolve an ``ast.ImportFrom`` to its absolute dotted target, or None.

    Absolute imports (``level == 0``) return ``node.module`` verbatim. Relative
    imports resolve against *package* the way CPython does: strip ``level - 1``
    trailing components, then append ``node.module``. None when the import
    reaches beyond the top-level package (unresolvable).
    """
    if node.level == 0:
        return node.module
    bits = package.rsplit(".", node.level - 1)
    if len(bits) < node.level:
        return None
    base = bits[0]
    return f"{base}.{node.module}" if node.module else base


def _forbidden_imports_in_source(
    source: str, module_qualname: str, is_package: bool
) -> list[tuple[int, str]]:
    """Return ``(lineno, target)`` for each forbidden import statement in *source*.

    Every ``ast.Import`` / ``ast.ImportFrom`` at any nesting depth is collected;
    a target under a ``FORBIDDEN_PREFIXES`` entry is a violation. For a
    ``from … import`` whose module target is not itself forbidden, each imported
    name is also checked as a submodule of that target, so
    ``from tolokaforge import adapters`` is flagged as ``tolokaforge.adapters``.
    *module_qualname* is the file's dotted module name (e.g.
    ``tolokaforge.runner.service``); *is_package* is True for an ``__init__``
    module. Relative imports resolve against the module's package: a non-package
    module's own name is dropped first, so ``from ..adapters`` in
    ``tolokaforge.runner.foo`` targets ``tolokaforge.adapters`` — not
    ``tolokaforge.runner.adapters``.
    """
    package = module_qualname if is_package else module_qualname.rpartition(".")[0]
    violations: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _matches_forbidden(alias.name):
                    violations.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            target = _resolve_import_from(node, package)
            if not target:
                continue
            if _matches_forbidden(target):
                violations.append((node.lineno, target))
            else:
                for alias in node.names:
                    full = f"{target}.{alias.name}"
                    if _matches_forbidden(full):
                        violations.append((node.lineno, full))
    return violations


def _module_qualname(path: Path) -> tuple[str, bool]:
    """Return ``(dotted module name, is_package)`` for a repo source file."""
    parts = list(path.relative_to(REPO_ROOT).with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return ".".join(parts), is_package


def test_runner_has_no_forbidden_import_statements() -> None:
    violations: list[str] = []
    for path in sorted(RUNNER_ROOT.rglob("*.py")):
        module_qualname, is_package = _module_qualname(path)
        for lineno, target in _forbidden_imports_in_source(
            path.read_text(encoding="utf-8"), module_qualname, is_package
        ):
            violations.append(f"{path.relative_to(REPO_ROOT)}:{lineno} imports {target!r}")
    assert not violations, (
        "runner source has import statements to a forbidden surface "
        "(deferred/function-body imports included):\n" + "\n".join(f"  - {v}" for v in violations)
    )


def test_forbidden_import_detector_flags_deferred_imports() -> None:
    absolute_deferred = "def _init():\n    from tolokaforge.adapters import available_adapters\n"
    assert _forbidden_imports_in_source(
        absolute_deferred, "tolokaforge.runner.service", is_package=False
    ) == [(2, "tolokaforge.adapters")]

    relative_deferred = "def _init():\n    from ..adapters import available_adapters\n"
    assert _forbidden_imports_in_source(
        relative_deferred, "tolokaforge.runner.submodule", is_package=False
    ) == [(2, "tolokaforge.adapters")]

    parent_import_leaf = "def _init():\n    from tolokaforge import adapters\n"
    assert _forbidden_imports_in_source(
        parent_import_leaf, "tolokaforge.runner.service", is_package=False
    ) == [(2, "tolokaforge.adapters")]

    parent_import_forbidden_leaf = "def _init():\n    from tolokaforge.core import orchestrator\n"
    assert _forbidden_imports_in_source(
        parent_import_forbidden_leaf, "tolokaforge.runner.service", is_package=False
    ) == [(2, "tolokaforge.core.orchestrator")]
