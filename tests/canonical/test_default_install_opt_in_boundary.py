"""The default install must stay the engine loop — other harnesses are opt-in.

Opt-in boundary: other harnesses (terminal_bench, harbor, inspect_ai, ...) ship as
out-of-tree adapter packages installed only through extras. A plain
``pip install tolokaforge`` must never depend on an adapter package or a third-party
harness distribution, so installing the engine never pulls another harness's
dependencies. This locks that boundary against an accidental future edit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]

pytestmark = pytest.mark.canonical

_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

# Third-party harness distributions that must never be a base dependency.
_FORBIDDEN_HARNESS_DISTS = {"inspect-ai", "harbor"}

# Adapter packages the repo ships; each must be reachable only via an extra.
_SHIPPED_ADAPTER_DISTS = {
    "tolokaforge-adapter-terminal-bench",
    "tolokaforge-adapter-inspect-ai",
    "tolokaforge-adapter-harbor",
}


def _dist_name(requirement: str) -> str:
    """Normalise a requirement string to its distribution name."""
    name = requirement.strip()
    for sep in ("[", "(", ">", "<", "=", "!", "~", ";", " ", "@"):
        name = name.split(sep, 1)[0]
    return name.strip().lower().replace("_", "-")


def _project() -> dict:
    return tomllib.loads(_PYPROJECT.read_text())["project"]


def test_base_dependencies_contain_no_adapter_or_harness() -> None:
    offenders = [
        dep
        for dep in _project()["dependencies"]
        if _dist_name(dep).startswith("tolokaforge-adapter-")
        or _dist_name(dep) in _FORBIDDEN_HARNESS_DISTS
    ]
    assert not offenders, (
        "`pip install tolokaforge` (default) must not depend on an adapter / other-harness "
        f"package — other harnesses are opt-in via extras. Offending base deps: {offenders}"
    )


def test_adapter_packages_are_reachable_only_through_extras() -> None:
    proj = _project()
    base_dists = {_dist_name(d) for d in proj["dependencies"]}
    extra_dists = {
        _dist_name(req) for reqs in proj.get("optional-dependencies", {}).values() for req in reqs
    }
    for adapter in _SHIPPED_ADAPTER_DISTS:
        assert adapter not in base_dists, f"{adapter} leaked into base dependencies"
        assert adapter in extra_dists, f"{adapter} must be installable via an extra (opt-in)"
