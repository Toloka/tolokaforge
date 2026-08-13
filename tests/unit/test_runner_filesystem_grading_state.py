"""Runner reads /work/ back at grading time so $.filesystem jsonpaths resolve.

Task authors write jsonpath state_checks like::

    state_checks:
      jsonpaths:
        - path: "$.filesystem['/env/fs/agent-visible/buggy_math.py']"
          contains: "amount * (1 + tax_rate)"

For that to match, the runner exposes the on-disk contents of /work/
back out under the logical /env/fs/agent-visible/ layout the task YAML
wrote against — the same layout the RegisterTrial provisioner accepts.

The runner catches ``TrialNotFoundError`` from the DB client so a
filesystem-only trial (one whose task never provisions ``initial_state.tables``
and therefore never calls ``db_client.init_trial()``) still assembles a
jsonpath state rooted at ``$.filesystem[…]``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tolokaforge.runner import service as service_module
from tolokaforge.runner.db_client import TrialNotFoundError

pytestmark = pytest.mark.unit


class _StubRunnerServiceImpl:
    """Bind just the methods under test onto a minimal instance.

    The full RunnerServiceImpl constructor spins up a gRPC server, a DB client,
    an LLM stack and an OTEL exporter — none of which the filesystem-read
    helper touches. Binding the two methods directly to a plain object keeps
    the test hermetic.
    """

    def __init__(self, db_client) -> None:  # noqa: ANN001 — test stub
        self.db_client = db_client

    _read_agent_visible_filesystem = service_module.RunnerServiceImpl._read_agent_visible_filesystem
    _assemble_jsonpath_state = service_module.RunnerServiceImpl._assemble_jsonpath_state


@pytest.fixture
def redirect_work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # A dedicated subdirectory keeps the unit-conftest's autouse fake-wheel
    # (planted at ``tmp_path/tolokaforge-*.whl``) out of the walk.
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(service_module, "AGENT_WORK_DIR", str(work))
    return work


def test_read_agent_visible_filesystem_relabels_files_under_logical_root(
    redirect_work_dir: Path,
) -> None:
    (redirect_work_dir / "buggy_math.py").write_text("amount * (1 + tax_rate)\n")
    (redirect_work_dir / "sub").mkdir()
    (redirect_work_dir / "sub" / "helper.py").write_text("def x(): return 1\n")

    svc = _StubRunnerServiceImpl(db_client=AsyncMock())
    fs = svc._read_agent_visible_filesystem()

    assert fs == {
        "/env/fs/agent-visible/buggy_math.py": "amount * (1 + tax_rate)\n",
        "/env/fs/agent-visible/sub/helper.py": "def x(): return 1\n",
    }


def test_read_agent_visible_filesystem_skips_binary_and_returns_empty_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point AGENT_WORK_DIR at a directory that will not exist for the empty case.
    monkeypatch.setattr(service_module, "AGENT_WORK_DIR", str(tmp_path / "nope"))
    svc = _StubRunnerServiceImpl(db_client=AsyncMock())
    assert svc._read_agent_visible_filesystem() == {}

    # Then repoint at a fresh subdirectory (dodging the unit-conftest's
    # autouse fake wheel at tmp_path root) with a binary file: it is skipped,
    # not raised.
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(service_module, "AGENT_WORK_DIR", str(work))
    (work / "image.bin").write_bytes(b"\x89PNG\x00\x01\x02\x03\xff\xfe")
    (work / "readme.txt").write_text("hello")
    fs = svc._read_agent_visible_filesystem()
    assert fs == {"/env/fs/agent-visible/readme.txt": "hello"}


def test_read_agent_visible_filesystem_skips_symlinks(
    redirect_work_dir: Path,
) -> None:
    # A symlink under /work/ could point at any container-readable path
    # (e.g. /etc/hostname). The assertion vocabulary is not a general-purpose
    # container filesystem probe, so the walk must ignore the link even
    # though ``is_file()`` returns True for a file-target symlink.
    outside = redirect_work_dir.parent / "outside.txt"
    outside.write_text("must not leak")
    (redirect_work_dir / "readme.txt").write_text("ok")
    (redirect_work_dir / "link").symlink_to(outside)

    svc = _StubRunnerServiceImpl(db_client=AsyncMock())
    fs = svc._read_agent_visible_filesystem()

    assert fs == {"/env/fs/agent-visible/readme.txt": "ok"}


@pytest.mark.asyncio
async def test_assemble_jsonpath_state_handles_missing_db_trial(
    redirect_work_dir: Path,
) -> None:
    """Filesystem-only tasks never call db_client.init_trial(), so
    ``get_stable_state`` raises ``TrialNotFoundError``. The assembler must
    treat that as an empty DB state instead of propagating the error —
    otherwise the outer catch-all in GradeTrial rewrites it as
    ``Grading error: TrialNotFoundError`` and the trial fails to grade at
    all, even for assertions that only need $.filesystem.
    """
    (redirect_work_dir / "buggy_math.py").write_text("amount * (1 + tax_rate)\n")

    db_client = AsyncMock()
    db_client.get_stable_state = AsyncMock(side_effect=TrialNotFoundError("t-1"))
    svc = _StubRunnerServiceImpl(db_client=db_client)

    state = await svc._assemble_jsonpath_state("t-1")

    assert state["db"] == {}
    assert state["tables"] == {}
    assert state["filesystem"] == {
        "/env/fs/agent-visible/buggy_math.py": "amount * (1 + tax_rate)\n",
    }


@pytest.mark.asyncio
async def test_assemble_jsonpath_state_merges_db_and_filesystem(
    redirect_work_dir: Path,
) -> None:
    (redirect_work_dir / "buggy_math.py").write_text("amount * (1 + tax_rate)\n")

    stable_response = type("R", (), {"data": {"users": [{"id": 1, "name": "alice"}]}})()
    db_client = AsyncMock()
    db_client.get_stable_state = AsyncMock(return_value=stable_response)
    svc = _StubRunnerServiceImpl(db_client=db_client)

    state = await svc._assemble_jsonpath_state("t-1")

    assert state["db"] == {"users": [{"id": 1, "name": "alice"}]}
    assert state["tables"] == state["db"]
    assert state["filesystem"] == {
        "/env/fs/agent-visible/buggy_math.py": "amount * (1 + tax_rate)\n",
    }
