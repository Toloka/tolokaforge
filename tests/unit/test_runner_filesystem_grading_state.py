"""Runner reads back the agent-visible tree at grading time.

Task authors write jsonpath state_checks like::

    state_checks:
      jsonpaths:
        - path: "$.filesystem['/env/fs/agent-visible/buggy_math.py']"
          contains: "amount * (1 + tax_rate)"

For that to match, the runner exposes the on-disk contents of the agent's
edit surface back at its logical path. Two routes:

* **Engine-loop trial** — the runner service walks its own ``AGENT_WORK_DIR``,
  the container it registered the trial into. Keys land under
  ``/env/fs/agent-visible/<rel>``.
* **Harness-mode trial** — the CLI edits inside a separate container reached
  via the exec-wrapper; the runner service execs ``tar | base64`` there and
  decodes the tree in-process. Keys land under the container's declared
  ``agent_visible_dir`` (e.g. ``/work/factorial.py``).

The result feeds ``composite.grade_state_checks_reads`` through the
substrate's ``filesystem_state`` accessor — see
``tests/canonical/test_grading_composite_state_checks.py`` and
``tests/unit/grading/test_composite_state_checks_gating.py`` for the
composite's own behaviour locks over the merged ``$.db`` / ``$.tables`` /
``$.filesystem`` state.
"""

from __future__ import annotations

import base64
import io
import tarfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tolokaforge.runner import service as service_module
from tolokaforge.runner.tool_factory import DockerComposeExecToolWrapper

pytestmark = pytest.mark.unit


class _StubRunnerServiceImpl:
    """Bind just the method under test onto a minimal instance.

    The full RunnerServiceImpl constructor spins up a gRPC server, a DB client,
    an LLM stack and an OTEL exporter — none of which the filesystem-read
    helper touches. Binding the method directly to a plain object keeps the
    test hermetic.
    """

    def __init__(self, db_client) -> None:  # noqa: ANN001 — test stub
        self.db_client = db_client
        self.trials: dict[str, object] = {}

    _read_agent_visible_filesystem = service_module.RunnerServiceImpl._read_agent_visible_filesystem
    _read_filesystem_for_state = service_module.RunnerServiceImpl._read_filesystem_for_state


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


# ---------------------------------------------------------------------------
# Harness-mode routing — the runner execs into the trial container rather
# than reading its own /work/. The metadata handshake carries both the CLI's
# invocation command (which flags harness mode) and the container path the
# runner mirrors back into ``state["filesystem"]``.
# ---------------------------------------------------------------------------


def _tarball_b64(files: dict[str, bytes]) -> str:
    """Encode ``{./path: bytes}`` as ``tar | base64`` would emit inside a container."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode("ascii")


class _StubBashTool(DockerComposeExecToolWrapper):
    """Real subclass so :func:`isinstance` recognises it; scripted ``_exec_sync``.

    Inheriting instead of duck-typing keeps the routing logic honest: the
    runner service looks for the concrete wrapper type, not just anything
    that answers ``_exec_sync``.
    """

    def __init__(self, responses: list[str]) -> None:
        # Skip the base ``__init__`` — its ToolSchemaModel path is out of scope
        # here. The fields the routing consults (``_container`` for start(),
        # ``_exec_sync`` for reads) are set explicitly.
        self._responses = list(responses)
        self.calls: list[str] = []
        self._container = "stub_container"
        self._trial_id = "t-1"

    def _exec_sync(self, command: str, timeout: float) -> str:  # noqa: ARG002 — matches base
        self.calls.append(command)
        if not self._responses:
            raise AssertionError(f"unexpected exec call: {command!r}")
        return self._responses.pop(0)


def _harness_trial_context(
    *,
    agent_harness_command: str | None,
    agent_visible_dir: str | None,
    bash_tool: DockerComposeExecToolWrapper | None,
) -> MagicMock:
    """Minimal ``TrialContextRuntime`` stand-in with the two consulted attrs."""
    ctx = MagicMock()
    metadata: dict[str, object] = {}
    if agent_harness_command is not None:
        metadata["agent_harness_command"] = agent_harness_command
    if agent_visible_dir is not None:
        metadata["agent_visible_dir"] = agent_visible_dir
    ctx.task_description.metadata = metadata
    ctx.agent_tools = {"bash": bash_tool} if bash_tool is not None else {}
    return ctx


def test_harness_trial_reads_filesystem_via_exec_wrapper() -> None:
    """When metadata carries ``agent_harness_command`` + ``agent_visible_dir``
    and an exec-capable tool is registered, the runner execs ``tar | base64``
    inside the trial container and decodes the tree in-process — keys land
    under the container's declared path."""
    bash_tool = _StubBashTool(
        [
            "512\t/work\n",  # du probe
            _tarball_b64({"./factorial.py": b"def factorial(n): return 1\n"}),
        ]
    )

    svc = _StubRunnerServiceImpl(db_client=AsyncMock())
    svc.trials["t-1"] = _harness_trial_context(
        agent_harness_command="claude --print 'fix it'",
        agent_visible_dir="/work",
        bash_tool=bash_tool,
    )

    fs = svc._read_filesystem_for_state("t-1")

    assert fs == {"/work/factorial.py": "def factorial(n): return 1\n"}
    # Both container-side commands actually issued.
    assert any("du -sb" in cmd for cmd in bash_tool.calls)
    assert any("tar --exclude=./.git" in cmd for cmd in bash_tool.calls)


def test_engine_loop_trial_still_walks_the_runner_workdir(
    redirect_work_dir: Path,
) -> None:
    """A non-harness trial reads back the runner's own ``AGENT_WORK_DIR`` —
    no exec into any other container. This is the byte-identical path the
    filesystem-only trials in this file already lock."""
    (redirect_work_dir / "buggy_math.py").write_text("amount * (1 + tax_rate)\n")

    svc = _StubRunnerServiceImpl(db_client=AsyncMock())
    # Engine-loop trial: metadata carries no harness command.
    svc.trials["t-1"] = _harness_trial_context(
        agent_harness_command=None,
        agent_visible_dir=None,
        bash_tool=None,
    )

    fs = svc._read_filesystem_for_state("t-1")

    assert fs == {
        "/env/fs/agent-visible/buggy_math.py": "amount * (1 + tax_rate)\n",
    }


def test_harness_trial_without_exec_tool_falls_back_to_workdir(
    redirect_work_dir: Path,
) -> None:
    """A harness trial that registered no exec-capable tool falls back to
    the runner's own /work/ walk with a warning — grading proceeds rather
    than failing on a missing exec surface."""
    (redirect_work_dir / "left_behind.py").write_text("still here\n")

    svc = _StubRunnerServiceImpl(db_client=AsyncMock())
    svc.trials["t-1"] = _harness_trial_context(
        agent_harness_command="claude --print 'fix it'",
        agent_visible_dir="/work",
        bash_tool=None,
    )

    fs = svc._read_filesystem_for_state("t-1")

    assert fs == {"/env/fs/agent-visible/left_behind.py": "still here\n"}


def test_harness_trial_without_agent_visible_dir_falls_back(
    redirect_work_dir: Path,
) -> None:
    """A harness trial whose adapter omitted ``agent_visible_dir`` falls back
    to the /work/ walk — the runner has nothing to enumerate into, so it
    reads its own workdir rather than execing ``tar`` against ``/``."""
    (redirect_work_dir / "runner_side.py").write_text("still here\n")

    svc = _StubRunnerServiceImpl(db_client=AsyncMock())
    svc.trials["t-1"] = _harness_trial_context(
        agent_harness_command="claude --print 'fix it'",
        agent_visible_dir=None,
        bash_tool=_StubBashTool([]),  # would fail loud on exec attempt
    )

    fs = svc._read_filesystem_for_state("t-1")

    assert fs == {"/env/fs/agent-visible/runner_side.py": "still here\n"}


def test_unregistered_trial_walks_workdir(
    redirect_work_dir: Path,
) -> None:
    """A read for a trial no longer in ``self.trials`` falls back to the
    /work/ walk without raising — the state assembly runs against an
    empty trial state rather than a KeyError."""
    (redirect_work_dir / "orphan.py").write_text("orphan\n")

    svc = _StubRunnerServiceImpl(db_client=AsyncMock())

    fs = svc._read_filesystem_for_state("does-not-exist")

    assert fs == {"/env/fs/agent-visible/orphan.py": "orphan\n"}


def test_harness_state_composes_with_jsonpath_check() -> None:
    """The composition claim: a harness trial's edits are seen by state_checks.

    Threads the harness-mode filesystem read into the ``{db, tables, filesystem}``
    shape ``composite.grade_state_checks_reads`` hands the :class:`StateChecker`,
    then drives one JSONPath assertion against a file the "CLI" wrote inside
    the container. This is the whole point of the lift: any adapter's
    harness-mode trial can grade under any state-based grading mode.
    """
    from tolokaforge.core.grading.state_checks import StateChecker

    bash_tool = _StubBashTool(
        [
            "128\t/work\n",
            _tarball_b64({"./factorial.py": b"def factorial(n): return 1\n"}),
        ]
    )

    svc = _StubRunnerServiceImpl(db_client=AsyncMock())
    svc.trials["t-1"] = _harness_trial_context(
        agent_harness_command="claude --print 'fix it'",
        agent_visible_dir="/work",
        bash_tool=bash_tool,
    )

    filesystem = svc._read_filesystem_for_state("t-1")
    state = {"db": {}, "tables": {}, "filesystem": filesystem}
    score, reasons = StateChecker().check_jsonpaths(
        state,
        [
            {
                "path": "$.filesystem['/work/factorial.py']",
                "contains": "def factorial",
                "description": "the CLI wrote a factorial definition",
            }
        ],
    )

    assert score == 1.0, reasons
    assert reasons == []
