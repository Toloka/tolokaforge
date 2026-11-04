"""Lock the import direction of the judge-evidence modules.

``runner/models.py`` declares the evidence config and its one part-to-input mapping
(``JudgeEvidence.needs``); the parts that read it live in
``core/grading/judge_evidence.py``. The direction runs one way —
``judge_evidence`` → ``tool_effects`` → ``argument_predicates`` → (type checking only)
``runner.models`` — so ``runner.models`` reaching any of the three would close a cycle.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical

_REPO = Path(__file__).resolve().parents[2]

_DOWNSTREAM = (
    "tolokaforge.core.grading.judge_evidence",
    "tolokaforge.core.tool_effects",
    "tolokaforge.core.grading.argument_predicates",
)


def _imported_modules(path: Path, *, runtime_only: bool) -> set[str]:
    tree = ast.parse(path.read_text())
    type_checking_nodes = {
        id(node)
        for block in ast.walk(tree)
        if isinstance(block, ast.If) and ast.unparse(block.test) == "TYPE_CHECKING"
        for statement in block.body
        for node in ast.walk(statement)
    }
    modules: set[str] = set()
    for node in ast.walk(tree):
        if runtime_only and id(node) in type_checking_nodes:
            continue
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def test_runner_models_imports_none_of_the_evidence_modules():
    imported = _imported_modules(_REPO / "tolokaforge/runner/models.py", runtime_only=False)
    assert not imported & set(_DOWNSTREAM)


def test_argument_predicates_reads_runner_models_only_for_type_checking():
    path = _REPO / "tolokaforge/core/grading/argument_predicates.py"
    assert "tolokaforge.runner.models" in _imported_modules(path, runtime_only=False)
    assert "tolokaforge.runner.models" not in _imported_modules(path, runtime_only=True)


def test_importing_runner_models_loads_none_of_the_evidence_modules():
    probe = (
        "import sys, tolokaforge.runner.models; "
        f"print(sorted(set({list(_DOWNSTREAM)!r}) & set(sys.modules)))"
    )
    loaded = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True, cwd=_REPO
    ).stdout.strip()
    assert loaded == "[]"
