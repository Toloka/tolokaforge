"""Guard the "one agent-visible walk, split entry points" invariant.

Two orchestration entry points share one walker:

* :func:`~tolokaforge.core.grading.filesystem_view.read_agent_visible_filesystem`
  materialises ``{'/env/fs/agent-visible/<rel>': content}`` for the
  runner-side non-harness state factory. One caller outside the pure
  module: ``runner/service.py``.
* :func:`~tolokaforge.core.grading.filesystem_view.iter_agent_visible_rel_paths`
  yields the same walk one rel-path at a time (peak memory: one file's
  bytes) for ``SubstrateService.ListFilesystemDir``. One caller outside
  the pure module: ``runner/substrate_service.py``.

``ReadFilesystemPath`` reuses :func:`is_excluded_rel_path` for its
per-path refusal.

A third caller of either entry point, or a caller that re-inlines the
raw ``rglob("*") + is_symlink + read_text(encoding="utf-8")`` chain,
would fork the exclusion policy and re-split the seam.

Text-level assertions rather than an AST pass — the goal is to catch a
copy-paste of the recipe into a foreign caller, not to prove semantic
equivalence, and a text sweep is what a reviewer would run.

Symmetric with ``tests/canonical/test_fold_defined_once.py`` (composite
fold) and ``tests/canonical/test_timeline_from_wire_defined_once.py``
(timeline wire).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE_ROOT = _REPO_ROOT / "tolokaforge"
_FILESYSTEM_VIEW_MODULE = _PACKAGE_ROOT / "core" / "grading" / "filesystem_view.py"
_RUNNER_SITE = _PACKAGE_ROOT / "runner" / "service.py"
_SUBSTRATE_SITE = _PACKAGE_ROOT / "core" / "grading" / "substrate.py"
_SUBSTRATE_SERVICE_SITE = _PACKAGE_ROOT / "runner" / "substrate_service.py"
_DOCS_ROOT = _REPO_ROOT / "docs"
_TESTS_CANONICAL_ROOT = _REPO_ROOT / "tests" / "canonical"


def _iter_package_python_files() -> list[Path]:
    return sorted(p for p in _PACKAGE_ROOT.rglob("*.py") if p.is_file())


@pytest.mark.parametrize(
    "symbol", ["read_agent_visible_filesystem", "iter_agent_visible_rel_paths"]
)
def test_walker_entry_point_is_defined_exactly_once_in_the_pure_module(
    symbol: str,
) -> None:
    """Each orchestration entry point has one ``def`` header in the tree."""
    def_pattern = re.compile(rf"^def\s+{symbol}\b", re.MULTILINE)
    definitions = [
        path
        for path in _iter_package_python_files()
        if def_pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert definitions == [_FILESYSTEM_VIEW_MODULE], (
        f"Expected exactly one ``def {symbol}`` in "
        f"{_FILESYSTEM_VIEW_MODULE.relative_to(_REPO_ROOT)}, "
        f"found: {[p.relative_to(_REPO_ROOT) for p in definitions]}"
    )


@pytest.mark.parametrize(
    ("symbol", "expected_callers"),
    [
        ("read_agent_visible_filesystem", (_SUBSTRATE_SITE, _RUNNER_SITE)),
        ("iter_agent_visible_rel_paths", (_SUBSTRATE_SERVICE_SITE,)),
    ],
)
def test_walker_entry_point_has_its_named_production_callers_only(
    symbol: str, expected_callers: tuple[Path, ...]
) -> None:
    """Each entry point has its named production callers outside the module,
    and no others. An unnamed callsite in ``tolokaforge/`` would signal recipe
    leakage into a codepath the pure module was not designed for.

    ``read_agent_visible_filesystem`` has two callers: ``runner/service.py``
    (runner-side non-harness state factory) and ``core/grading/substrate.py``
    (``SnapshotGradingSubstrate.filesystem_state`` walks the extracted bundle
    tmpdir through the shared helper). Each is one entry per module, so the
    "one walker, no re-inline" invariant holds per site.
    """
    call_pattern = re.compile(rf"\b{symbol}\(")
    call_sites = [
        path
        for path in _iter_package_python_files()
        if path != _FILESYSTEM_VIEW_MODULE and call_pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert sorted(call_sites) == sorted(expected_callers), (
        f"Expected production callers of {symbol}( at "
        f"{sorted(p.relative_to(_REPO_ROOT) for p in expected_callers)}; "
        f"found: {sorted(p.relative_to(_REPO_ROOT) for p in call_sites)}"
    )


def test_agent_visible_excludes_is_defined_exactly_once() -> None:
    """The exclusion set is authored in exactly one place."""
    def_pattern = re.compile(r"^AGENT_VISIBLE_EXCLUDES\s*[:=]", re.MULTILINE)
    definitions = [
        path
        for path in _iter_package_python_files()
        if def_pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert definitions == [_FILESYSTEM_VIEW_MODULE], (
        f"Expected exactly one AGENT_VISIBLE_EXCLUDES definition in "
        f"{_FILESYSTEM_VIEW_MODULE.relative_to(_REPO_ROOT)}, "
        f"found: {[p.relative_to(_REPO_ROOT) for p in definitions]}"
    )


def test_runner_and_substrate_service_do_not_re_inline_the_raw_walk() -> None:
    """Neither production caller re-inlines the raw walker recipe.

    ``rglob("*") + is_symlink + read_text(encoding="utf-8")`` in the same
    file would be a copy of the walker's shape and fork the exclusion
    policy. Scoped narrowly enough that unrelated ``rglob`` uses (log
    rotation, cache probes) do not trip the guard.
    """
    forbidden = re.compile(
        r"\.rglob\(\"\*\"\)[\s\S]{0,240}?\.is_symlink\([\s\S]{0,240}?"
        r"\.read_text\(encoding=\"utf-8\"\)"
    )
    for path in (_RUNNER_SITE, _SUBSTRATE_SERVICE_SITE):
        text = path.read_text(encoding="utf-8")
        assert not forbidden.search(text), (
            f"{path.relative_to(_REPO_ROOT)} re-inlines the raw "
            f"``rglob + is_symlink + read_text(encoding='utf-8')`` chain — "
            f"route it through filesystem_view.read_agent_visible_filesystem "
            f"or filesystem_view.iter_agent_visible_rel_paths instead."
        )


def test_no_reference_to_the_deleted_private_helper_remains() -> None:
    """No ``_read_agent_visible_filesystem`` reference survives under
    ``tolokaforge/`` or ``docs/``. Exactly ONE occurrence lives under
    ``tests/canonical/`` — the literal below in this guard. A count that
    drifts up signals a stale docstring or comment survived the rename.
    """
    needle = "_read_agent_visible_filesystem"
    for root in (_PACKAGE_ROOT, _DOCS_ROOT):
        hits = [
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix in {".py", ".proto", ".md"}
            and needle in path.read_text(encoding="utf-8")
        ]
        assert hits == [], (
            f"{root.relative_to(_REPO_ROOT)}/ still references the deleted "
            f"``_read_agent_visible_filesystem``: "
            f"{[p.relative_to(_REPO_ROOT) for p in hits]}"
        )
    canonical_hits = [
        path
        for path in _TESTS_CANONICAL_ROOT.rglob("*.py")
        if path.is_file() and needle in path.read_text(encoding="utf-8")
    ]
    assert canonical_hits == [Path(__file__)], (
        f"tests/canonical/ should hold exactly one reference to "
        f"``_read_agent_visible_filesystem`` — this guard's own literal — "
        f"found: {[p.relative_to(_REPO_ROOT) for p in canonical_hits]}"
    )
