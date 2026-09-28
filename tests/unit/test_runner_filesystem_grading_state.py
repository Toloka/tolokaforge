"""Runner routes the filesystem-state read at grading time.

Task authors write jsonpath state_checks like::

    state_checks:
      jsonpaths:
        - path: "$.filesystem['/env/fs/agent-visible/buggy_math.py']"
          contains: "amount * (1 + tax_rate)"

For that to match, the runner exposes the on-disk contents of the agent's
edit surface back at its logical path. Two routes, both exercised here:

* **Engine-loop trial** — the runner service walks its own ``AGENT_WORK_DIR``
  via :func:`tolokaforge.core.grading.filesystem_view.read_agent_visible_filesystem`
  (whose behaviour is locked in ``tests/unit/grading/test_filesystem_view.py``).
  Keys land under ``/env/fs/agent-visible/<rel>``.
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

from tolokaforge.core.grading.filesystem_view import AGENT_VISIBLE_EXCLUDES
from tolokaforge.runner import service as service_module
from tolokaforge.runner.tool_factory import DockerComposeExecToolWrapper

pytestmark = pytest.mark.unit


class _StubRunnerServiceImpl:
    """Bind just the routing method under test onto a minimal instance.

    The full RunnerServiceImpl constructor spins up a gRPC server, a DB client,
    an LLM stack and an OTEL exporter — none of which
    :meth:`_read_filesystem_for_state` touches on the routing branches this
    file exercises. Binding the method directly to a plain object keeps the
    test hermetic.
    """

    def __init__(self, db_client) -> None:  # noqa: ANN001 — test stub
        self.db_client = db_client
        self.trials: dict[str, object] = {}

    _read_filesystem_for_state = service_module.RunnerServiceImpl._read_filesystem_for_state


@pytest.fixture
def redirect_work_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # A dedicated subdirectory keeps the unit-conftest's autouse fake-wheel
    # (planted at ``tmp_path/tolokaforge-*.whl``) out of the walk.
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(service_module, "AGENT_WORK_DIR", str(work))
    return work


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


class _StubBashTool:
    """Plain object exposing the env-exec capability; scripted responses.

    Inherits nothing: the runner selects its grading executor on the two
    ``SupportsEnvExec`` methods, so answering them is the whole contract.
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []

    def exec_in_env(self, command: str, timeout_s: float) -> str:  # noqa: ARG002 — scripted
        self.calls.append(command)
        if not self._responses:
            raise AssertionError(f"unexpected exec call: {command!r}")
        return self._responses.pop(0)

    def exec_in_env_with_exit_code(self, command: str, timeout_s: float) -> tuple[int, str]:
        return 0, self.exec_in_env(command, timeout_s)


def _harness_trial_context(
    *,
    agent_harness_command: str | None,
    agent_visible_dir: str | None,
    bash_tool: _StubBashTool | None,
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
    tar_cmds = [cmd for cmd in bash_tool.calls if "tar " in cmd and "base64" in cmd]
    assert len(tar_cmds) == 1
    tar_cmd = tar_cmds[0]
    for name in AGENT_VISIBLE_EXCLUDES:
        assert f"--exclude={name}" in tar_cmd


def test_exec_capable_object_outside_the_wrapper_class_drives_the_read(
    redirect_work_dir: Path,
) -> None:
    """Any tool that can exec in the trial environment serves the read.

    The stub inherits nothing from
    :class:`~tolokaforge.runner.tool_factory.DockerComposeExecToolWrapper`, so
    the container-side ``tar | base64`` it answers proves selection runs on the
    capability. The decoy file under the runner's own workdir is what a
    class-gated selection would have returned instead.
    """
    (redirect_work_dir / "decoy.py").write_text("runner-side fallback\n")
    bash_tool = _StubBashTool(
        [
            "512\t/work\n",  # du probe
            _tarball_b64({"./factorial.py": b"def factorial(n): return 1\n"}),
        ]
    )
    assert not isinstance(bash_tool, DockerComposeExecToolWrapper)

    svc = _StubRunnerServiceImpl(db_client=AsyncMock())
    svc.trials["t-1"] = _harness_trial_context(
        agent_harness_command="claude --print 'fix it'",
        agent_visible_dir="/work",
        bash_tool=bash_tool,
    )

    fs = svc._read_filesystem_for_state("t-1")

    assert fs == {"/work/factorial.py": "def factorial(n): return 1\n"}
    assert bash_tool.calls


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
