"""Structural lock: no ``pb2`` reach anywhere under ``composite/``.

The composite package is topology-neutral dispatch; it returns pure core
types (``CheckResult``, ``StateChecksReadResult``, ``TraceChecksResult``,
``JudgeResult``, ``TranscriptEvaluationResult``). Wire encoding lives in
:mod:`tolokaforge.runner.grading`; a direct ``runner_pb2`` import here
would re-collapse the substrate seam ADR-0040 makes the boundary of.

Complements the ``.importlinter`` ``no-pb2-reach-from-core-grading``
contract — the linter enforces module-level forbid rules across
``core.grading`` at package granularity (with ``allow_indirect_imports =
true``); this test locks the composite package specifically, at
unit-tier cost, so a regression trips at pytest collection even when
``lint-imports`` has not run. ``TYPE_CHECKING`` guards are walked
separately: the body of ``if TYPE_CHECKING:`` is type-only (never
resolved at runtime), so ``runner.models`` config types imported there
remain permitted; the guard's ``else:`` branch DOES run at runtime and
is walked normally. A runtime import of ``runner_pb2`` /
``runner_pb2_grpc`` from any module in the package fails — including
the historical ``from tolokaforge.runner import runner_pb2 as pb2``
idiom the pre-split composite used, which the walker catches by
emitting both ``X`` and the fully-qualified ``X.Y`` name for each
``from X import Y`` alias.

A companion assertion verifies the walker actually visited every module
under ``composite/`` — a new module added to the package cannot bypass
the fence by not being read.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical


_COMPOSITE_PKG = (
    Path(__file__).resolve().parents[2] / "tolokaforge" / "core" / "grading" / "composite"
)

_FORBIDDEN_MODULES: frozenset[str] = frozenset(
    {
        "tolokaforge.runner.runner_pb2",
        "tolokaforge.runner.runner_pb2_grpc",
    }
)

# Package modules the fence must see. Adding a new module to
# ``composite/`` requires extending this set — the coverage assertion
# below is the tripwire that catches a silent bypass.
_EXPECTED_MODULE_NAMES: frozenset[str] = frozenset(
    {
        "__init__.py",
        "custom_checks.py",
        "llm_judge.py",
        "state_checks.py",
        "trace_checks.py",
        "transcript_rules.py",
    }
)


def _is_type_checking_guard(test: ast.expr) -> bool:
    """Match ``if TYPE_CHECKING:`` or ``if typing.TYPE_CHECKING:``."""
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _runtime_imports(module_source: str) -> list[tuple[str, int]]:
    """Return every dotted module name reachable OUTSIDE ``TYPE_CHECKING``.

    Walks the AST, skipping the body of any ``if TYPE_CHECKING:`` block —
    those imports never resolve at runtime and cannot re-collapse the
    seam. The ``else:`` branch of such a guard runs at runtime, so its
    statements are still processed. For each ``from X import Y``
    statement the walker emits BOTH ``X`` and ``X.Y``: the
    fully-qualified form is what catches the historical
    ``from tolokaforge.runner import runner_pb2`` idiom (``child.module``
    alone is only ``tolokaforge.runner``). Returns ``(module_name,
    lineno)`` pairs so a failure names the exact site.
    """
    tree = ast.parse(module_source)
    imports: list[tuple[str, int]] = []

    def _record(node: ast.AST) -> None:
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.append((node.module, node.lineno))
            for alias in node.names:
                imports.append((f"{node.module}.{alias.name}", node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append((alias.name, node.lineno))

    def _walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and _is_type_checking_guard(child.test):
                for else_stmt in child.orelse:
                    _record(else_stmt)
                    _walk(else_stmt)
                continue
            _record(child)
            _walk(child)

    _walk(tree)
    return imports


def _all_composite_modules() -> list[Path]:
    return sorted(_COMPOSITE_PKG.rglob("*.py"))


def test_the_walker_covers_every_module_in_the_composite_package() -> None:
    """The pb2-import assertion below is only meaningful if every module
    was walked. A new module added to ``composite/`` MUST extend
    :data:`_EXPECTED_MODULE_NAMES` — a silent bypass here would leave the
    fence blind.
    """
    seen = {p.name for p in _all_composite_modules()}
    missing_from_expected = seen - _EXPECTED_MODULE_NAMES
    missing_from_disk = _EXPECTED_MODULE_NAMES - seen
    assert not missing_from_expected, (
        f"Composite package has module(s) the fence does not enumerate: "
        f"{sorted(missing_from_expected)!r}. Add them to _EXPECTED_MODULE_NAMES "
        f"in this test file so the pb2-import fence sees them."
    )
    assert not missing_from_disk, (
        f"Enumerated composite module(s) not on disk: {sorted(missing_from_disk)!r}. "
        f"Rename / removal without a fence update is the failure mode this "
        f"assertion catches."
    )


def test_no_pb2_import_under_composite_at_runtime() -> None:
    """No composite module reaches ``runner_pb2`` / ``runner_pb2_grpc``.

    The wire encoder lives runner-side
    (:func:`tolokaforge.runner.grading.project_check_result_to_runner_wire`);
    the composite returns pure core types. A runtime import of a pb2
    module here re-collapses the substrate seam ADR-0040 makes the
    boundary of.
    """
    leaks: list[tuple[str, str, int]] = []
    for module_path in _all_composite_modules():
        for imported_module, lineno in _runtime_imports(module_path.read_text()):
            if imported_module in _FORBIDDEN_MODULES:
                leaks.append((module_path.name, imported_module, lineno))
    assert not leaks, (
        f"Composite package modules import forbidden pb2 modules at runtime: "
        f"{leaks!r}. Wire encoding lives runner-side in "
        f"tolokaforge.runner.grading.project_check_result_to_runner_wire; the "
        f"composite must return pure core types."
    )


def test_no_runner_service_or_runner_grading_import_under_composite() -> None:
    """Belt-and-braces around the ``.importlinter`` fence.

    ``runner.service`` (the grade RPC) and ``runner.grading`` (the narrow
    runner-wire helpers) are also fenced by the
    ``no-runner-reach-from-core-grading`` contract; this test locks them
    at pytest-collection speed for the composite package specifically.
    """
    forbidden: frozenset[str] = frozenset(
        {"tolokaforge.runner.service", "tolokaforge.runner.grading"}
    )
    leaks: list[tuple[str, str, int]] = []
    for module_path in _all_composite_modules():
        for imported_module, lineno in _runtime_imports(module_path.read_text()):
            if imported_module in forbidden:
                leaks.append((module_path.name, imported_module, lineno))
    assert not leaks, f"Composite package modules reach fenced runner modules: {leaks!r}."
