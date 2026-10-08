"""Read the per-file ruff target versions that declare a task-sandbox Python."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_RUFF_TARGET_RE = re.compile(r"^py3(\d+)$")


def per_file_target_versions() -> dict[str, str]:
    """Map each ``[tool.ruff.per-file-target-version]`` pattern to its ``major.minor``."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    table = pyproject.get("tool", {}).get("ruff", {}).get("per-file-target-version", {})
    versions: dict[str, str] = {}
    for pattern, target in table.items():
        match = _RUFF_TARGET_RE.match(target)
        if match is None:
            raise ValueError(f"per-file-target-version {pattern!r} = {target!r} is not py3N")
        versions[pattern] = f"3.{match.group(1)}"
    return versions
