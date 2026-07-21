"""Import-boundary lock for the ``tolokaforge.runner`` surface.

Two locks, both run in a *clean subprocess* — the pytest process has already
imported much of ``tolokaforge`` (conftest, sibling tests), so an in-process
footprint would be polluted and prove nothing. The subprocess imports only
``tolokaforge.runner.__main__`` + ``tolokaforge.runner.service`` (the runner's
module-load surface) while tracking parent→child import edges, so a violation
is reported with the first-party import chain that pulled it.

1. **Dependency-footprint subset.** Every third-party distribution the runner
   pulls at module load must lie inside the transitive-requires closure of
   ``[project.dependencies] ∪ [project.optional-dependencies].runner`` — the
   deps the runner image installs. A new ``import pandas`` in a runner module
   would fail here loud, naming the chain.

2. **First-party forbidden-surface.** The runner's transitive first-party
   closure must not reach the orchestration / adapter / CLI / docker-build
   surfaces (a runner that imported the orchestrator would drag those and their
   deps into the image and invert the dependency direction). Allowed core
   surfaces are ``core.grading.*``, ``core.llm.*``, ``core.models``,
   ``core.trial``, ``core.deprecations``, and ``secrets``.

The closure follows every requires edge (including extra-guarded ones): it is a
deliberate over-approximation of the *allowed* side, so a legitimate transitive
dep of a declared package never trips the test, while a genuinely foreign
package — related to no declared dep — still does.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical

REPO_ROOT = Path(__file__).resolve().parents[2]

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


def _run_analysis() -> dict:
    result = subprocess.run(
        [sys.executable, "-c", _ANALYSIS, str(REPO_ROOT)],
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
