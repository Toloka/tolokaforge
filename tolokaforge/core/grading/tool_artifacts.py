"""Tool-artifact extraction shared between runner and grader dispatch.

The pack ships every artefact module the trial imports — ``checks.py`` for
custom checks, MCP-server scripts, sibling helpers — as base64-encoded
entries on :attr:`TaskDescription.tool_artifacts` (``{rel_path: b64_content}``).
Both dispatch topologies materialise the same bundle: the runner side
during ``RegisterTrial`` (so tool reconstruction can import the modules
during agent execution), the grader side at grade time (so
``grade_custom_checks`` and any sibling artefact modules resolve).

:func:`extract_tool_artifacts` writes each entry to a fresh temp directory
and prepends both ``extract_dir`` and ``extract_dir / "tools"`` (when the
subdir exists) to ``sys.path`` so ``checks.py``'s ``from mcp_core import
X`` and every adapter's own tool package resolves. Cleanup — path removal,
:func:`purge_modules_under` and ``rmtree`` — is the caller's responsibility;
the runner tracks the directory on ``self._artifact_dirs[trial_id]`` for
teardown, the grader scopes it to a local ``try/finally`` on the grade call.
"""

from __future__ import annotations

import base64
import logging
import os
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["extract_tool_artifacts", "purge_modules_under"]


def extract_tool_artifacts(trial_id: str, artifacts: dict[str, str]) -> Path:
    """Materialise ``artifacts`` to a temp directory and register it on ``sys.path``.

    Each ``artifacts`` entry maps a relative path to base64-encoded file
    content; ``extract_tool_artifacts`` decodes, writes each to a sibling of
    the returned dir, and prepends ``extract_dir`` (for packages living at
    the artefact root, e.g. ``mcp_core/``) plus ``extract_dir / "tools"``
    (for adapter layouts placing their packages under a ``tools/`` subdir)
    to ``sys.path`` when non-empty. Callers must remove the entries from
    ``sys.path`` and ``shutil.rmtree`` the directory when the trial's
    artefacts are no longer needed.
    """
    safe_trial_id = trial_id.replace(":", "_").replace("/", "_")
    extract_dir = Path(tempfile.mkdtemp(prefix=f"tolokaforge-artifacts-{safe_trial_id}-"))

    for rel_path, b64_content in artifacts.items():
        out_path = extract_dir / rel_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        content = base64.b64decode(b64_content)
        out_path.write_bytes(content)

    extract_str = str(extract_dir)
    if extract_str not in sys.path:
        sys.path.insert(0, extract_str)

    tools_dir = extract_dir / "tools"
    if tools_dir.exists():
        tools_str = str(tools_dir)
        if tools_str not in sys.path:
            sys.path.insert(0, tools_str)

    logger.info(
        "Extracted %d artifacts to %s, added to sys.path",
        len(artifacts),
        extract_dir,
    )

    return extract_dir


def purge_modules_under(root: Path) -> list[str]:
    """Drop from ``sys.modules`` every module loaded from under ``root``; their names, sorted.

    A module the trial imported in process — a tool module, a helper of
    ``checks.py``, a pinned tool library (ADR-0060) — stays cached after its
    directory is removed, and a later trial that ships another version of the
    same module under a fresh directory would be served the stale one. Called
    wherever an extracted directory is torn down, before the ``rmtree``.

    Only a module whose ``__file__`` or ``__path__`` lies under ``root`` — as
    given, or resolved through symlinks — qualifies; nothing loaded from
    elsewhere is touched.
    """
    prefixes = tuple({f"{os.fspath(root).rstrip(os.sep)}{os.sep}", f"{root.resolve()}{os.sep}"})
    purged: list[str] = []
    for name, module in list(sys.modules.items()):
        locations: list[str] = []
        file = getattr(module, "__file__", None)
        if isinstance(file, str):
            locations.append(file)
        path = getattr(module, "__path__", None)
        if path is not None:
            try:
                locations.extend(os.fspath(entry) for entry in path)
            except TypeError:
                pass
        if any(location.startswith(prefixes) for location in locations):
            del sys.modules[name]
            purged.append(name)
    if purged:
        logger.debug("Purged %d module(s) loaded from %s: %s", len(purged), root, purged)
    return sorted(purged)
