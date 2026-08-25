"""``__main__.py`` structural lock — the standalone grader wires the composite.

AST-parses :mod:`tolokaforge.grader.__main__` and asserts:

1. The historical ``_unwired_judge_fn`` symbol is gone (a
   ``NotImplementedError`` stub that surfaced on every ``Grade`` RPC).
2. :class:`~tolokaforge.grader.composite_dispatch.GraderCompositeDispatch`
   is instantiated at boot AND its bound method is passed as
   ``judge_fn`` to :class:`GraderServiceImpl`.

The lock catches a rewire regression that a docstring rewrite alone
would leave silent — a service still mounting a placeholder callable
would ``NotImplementedError`` every request, and no runtime test can
prove absence of that path without also spinning up a real gRPC server.
An AST parse decides the shape in milliseconds.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical


_GRADER_MAIN_PATH = (Path(__file__).parents[2] / "tolokaforge" / "grader" / "__main__.py").resolve()


def _module_tree() -> ast.Module:
    return ast.parse(_GRADER_MAIN_PATH.read_text())


def test_unwired_judge_fn_symbol_is_gone() -> None:
    """No function OR name assignment binds ``_unwired_judge_fn`` any more."""
    tree = _module_tree()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name != "_unwired_judge_fn", (
                f"{_GRADER_MAIN_PATH} still defines _unwired_judge_fn — "
                "the grader boot must wire GraderCompositeDispatch instead"
            )
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assert (
                        target.id != "_unwired_judge_fn"
                    ), f"{_GRADER_MAIN_PATH} still binds _unwired_judge_fn"


def test_grader_composite_dispatch_is_instantiated_and_wired() -> None:
    """``GraderCompositeDispatch(logger)`` is constructed AND ``.grade`` reaches
    :class:`GraderServiceImpl` as ``judge_fn``.

    The two halves are asserted separately so a rewire regression that
    drops the seam surfaces the specific gap — a boot that instantiates
    the dispatch but forgets to pass its ``.grade`` on to
    :class:`GraderServiceImpl` would call the wrong seam.
    """
    tree = _module_tree()

    instantiations = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "GraderCompositeDispatch"
    ]
    assert instantiations, (
        "grader.__main__ does not instantiate GraderCompositeDispatch — the "
        "standalone service would run without its composite dispatch"
    )

    service_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "GraderServiceImpl"
    ]
    assert service_calls, "grader.__main__ never constructs GraderServiceImpl"

    judge_fn_kwargs = [kw for call in service_calls for kw in call.keywords if kw.arg == "judge_fn"]
    assert judge_fn_kwargs, "GraderServiceImpl(...) was called without a judge_fn kwarg"

    wired_from_dispatch = any(
        isinstance(kw.value, ast.Attribute)
        and kw.value.attr == "grade"
        and isinstance(kw.value.value, ast.Name)
        for kw in judge_fn_kwargs
    )
    assert wired_from_dispatch, (
        "GraderServiceImpl(judge_fn=...) is not wired to a "
        "GraderCompositeDispatch instance's .grade method"
    )
