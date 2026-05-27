"""Verify the runner provisions logical paths into /work/, not /env/fs/agent-visible/.

When ``InitialStateConfig.filesystem`` declares
``{"/env/fs/agent-visible/experiment.csv": "..."}``, the runner must
materialize the file at ``/work/experiment.csv`` because:

- ``BashTool`` uses ``/work`` as its working directory.
- ``ReadFileTool`` / ``WriteFileTool`` / ``ListDirTool`` normalise both
  ``/work/X`` and ``/env/fs/agent-visible/X`` against
  ``base_path = /work``.

If the file lands at the literal ``/env/fs/agent-visible/`` path, bash
sees nothing in ``/work`` and the trial fails with
``cp: cannot stat '/work/experiment.csv'``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _provision_filesystem(filesystem: dict[str, str], base_dir: Path) -> dict[Path, str]:
    """Mini-replica of the runner's provisioning loop, reading /work/ from base_dir.

    Mirrors tolokaforge/runner/service.py::RegisterTrial filesystem block but
    parametrises the base directory so tests can use tmp_path.
    """
    written: dict[Path, str] = {}
    for dest_path, content in filesystem.items():
        if dest_path.startswith("/env/fs/agent-visible/"):
            rel = dest_path[len("/env/fs/agent-visible/") :]
            file_path = base_dir / rel
        elif dest_path == "/env/fs/agent-visible":
            continue
        elif dest_path.startswith("/work/"):
            file_path = base_dir / dest_path[len("/work/") :]
        elif dest_path.startswith("/"):
            file_path = base_dir / dest_path.lstrip("/")
        else:
            file_path = base_dir / dest_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        written[file_path] = content
    return written


def test_logical_path_translates_to_work_root(tmp_path: Path) -> None:
    """``/env/fs/agent-visible/experiment.csv`` → ``<work>/experiment.csv``."""
    filesystem = {"/env/fs/agent-visible/experiment.csv": "x,y\n1,2\n"}
    written = _provision_filesystem(filesystem, tmp_path)

    target = tmp_path / "experiment.csv"
    assert target in written
    assert target.read_text() == "x,y\n1,2\n"
    # The literal /env/fs/agent-visible path must NOT be created under tmp_path.
    assert not (tmp_path / "env" / "fs" / "agent-visible").exists()


def test_nested_logical_path_translates(tmp_path: Path) -> None:
    filesystem = {"/env/fs/agent-visible/submissions/answer.md": "the answer is 42"}
    written = _provision_filesystem(filesystem, tmp_path)
    assert (tmp_path / "submissions" / "answer.md") in written
    assert (tmp_path / "submissions" / "answer.md").read_text() == "the answer is 42"


def test_explicit_work_path_passes_through(tmp_path: Path) -> None:
    filesystem = {"/work/scratch.txt": "hello"}
    written = _provision_filesystem(filesystem, tmp_path)
    assert (tmp_path / "scratch.txt") in written


def test_relative_path_is_resolved_against_work(tmp_path: Path) -> None:
    filesystem = {"notes.txt": "draft"}
    written = _provision_filesystem(filesystem, tmp_path)
    assert (tmp_path / "notes.txt") in written


def test_bare_logical_root_is_skipped(tmp_path: Path) -> None:
    filesystem = {"/env/fs/agent-visible": "should-not-write"}
    written = _provision_filesystem(filesystem, tmp_path)
    assert written == {}


def test_runner_service_block_uses_work_base() -> None:
    """The actual runner code path uses /work as its base — not /env/fs/agent-visible."""
    import tolokaforge.runner.service as svc_module

    src = Path(svc_module.__file__).read_text()
    # Sanity: the provisioning block declares /work as base_dir.
    assert 'base_dir = Path("/work")' in src
    # And the literal /env/fs/agent-visible path is NOT used as a write target.
    bad = '\n            base_dir = Path("/env/fs/agent-visible")\n'
    assert bad not in src
