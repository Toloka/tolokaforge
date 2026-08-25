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
X`` and every adapter's own tool package resolves. Cleanup — path removal
and ``rmtree`` — is the caller's responsibility; the runner tracks the
directory on ``self._artifact_dirs[trial_id]`` for teardown, the grader
scopes it to a local ``try/finally`` on the grade call.
"""

from __future__ import annotations

import base64
import logging
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["extract_tool_artifacts"]


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
