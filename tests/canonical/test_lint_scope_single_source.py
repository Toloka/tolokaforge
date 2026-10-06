"""Guards that the dev MCP lint/format tools lint the same directories as ``make lint``."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.canonical

REPO_ROOT = Path(__file__).resolve().parents[2]

_MAKEFILE = REPO_ROOT / "Makefile"
_DEV_MCP_SERVER = REPO_ROOT / "tools" / "dev-mcp" / "src" / "dev_mcp" / "server.py"
_LINT_DIRS_RE = re.compile(r"^LINT_DIRS\s*:?=\s*(.+?)\s*$", re.MULTILINE)


def _makefile_lint_dirs() -> list[str]:
    match = _LINT_DIRS_RE.search(_MAKEFILE.read_text())
    assert match is not None, f"{_MAKEFILE.name}: no `LINT_DIRS = ...` assignment"
    return match.group(1).split()


def _dev_mcp_lint_dirs() -> list[str]:
    module = ast.parse(_DEV_MCP_SERVER.read_text())
    for node in module.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "DEFAULT_LINT_DIRS"
        ):
            return ast.literal_eval(node.value).split()
    raise AssertionError(f"{_DEV_MCP_SERVER.relative_to(REPO_ROOT)}: no DEFAULT_LINT_DIRS")


def test_dev_mcp_lint_dirs_match_the_makefile() -> None:
    assert _dev_mcp_lint_dirs() == _makefile_lint_dirs(), (
        f"{_DEV_MCP_SERVER.relative_to(REPO_ROOT)}: DEFAULT_LINT_DIRS diverges from the "
        "Makefile's LINT_DIRS — the dev MCP lint/format tools and `make lint` would check "
        "different trees"
    )
