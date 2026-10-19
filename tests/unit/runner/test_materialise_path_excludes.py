"""Cross-container materialisation: exclude pruning, ordering and readiness.

``RunnerServiceImpl._materialise_path`` empties a path in the verifier container
and streams the agent's version across before the separate verifier grades it.
This file locks the behaviours a half-materialised or wrongly-pruned tree would
silently mis-grade:

* an exclude glob with a separator (``build/*.o``) is path-relative — matched
  with ``find -path`` so it takes effect — while a bare name (``__pycache__``)
  still matches at every depth with ``find -name``;
* a failed exclude prune books the trial ungradeable rather than scoring the
  verifier against files the task declared excluded;
* the source is probed before the destination is wiped, so an unreachable
  source never destroys the verifier's baked tree;
* an env tool that has not started surfaces as the ungradeable path rather than
  letting ``for_service`` raise an unhandled error.

The method is bound onto a minimal stub: the real constructor spins up a gRPC
server, a DB client and an LLM stack that this path never touches. ``docker``
calls are faked at the ``subprocess`` seam.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tolokaforge.core.grading.substrate import SubstrateUnreachableError
from tolokaforge.runner import service as service_module
from tolokaforge.runner.models import ToolSchema, ToolSource
from tolokaforge.runner.tool_factory import DockerComposeExecToolWrapper

pytestmark = pytest.mark.unit


class _StubRunnerServiceImpl:
    """Bind just ``_materialise_path`` onto a minimal instance with a trial map."""

    def __init__(self) -> None:
        self.trials: dict[str, object] = {}

    _materialise_path = service_module.RunnerServiceImpl._materialise_path


def _exec_wrapper(*, started: bool) -> DockerComposeExecToolWrapper:
    schema = ToolSchema(
        name="bash",
        description="stub",
        parameters={"type": "object", "properties": {}},
        category="compute",
        timeout_s=30.0,
        source=ToolSource(
            toolset="terminal_bench",
            module_path="",
            class_name="bash",
            invocation_style="docker_compose_exec",
            extra={"service": "agent", "compose_project_prefix": "tbench"},
        ),
    )
    wrapper = DockerComposeExecToolWrapper(schema, service="agent", compose_project_prefix="tbench")
    if started:
        wrapper._trial_id = "t-1"
        wrapper._container = "tbench_t-1_agent"
    return wrapper


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeProc:
    """A ``docker cp`` child that always succeeds for the copy stage."""

    def __init__(self) -> None:
        self.stdout = MagicMock()

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002 — scripted
        return 0

    def poll(self) -> int:
        return 0

    def kill(self) -> None:  # pragma: no cover — only on the timeout path
        pass


def _install_fake_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    probe_stdout: str = "__TF_PRESENT__\n",
    probe_rc: int = 0,
    prune_rc: int = 0,
    prune_stderr: str = "",
) -> tuple[list[str], list[str]]:
    """Fake ``subprocess.run``/``Popen`` in the service module.

    Returns ``(run_commands, prune_commands)`` captured in call order.
    """
    run_commands: list[str] = []
    prune_commands: list[str] = []

    def fake_run(argv: list[str], **_: object) -> _FakeCompleted:
        command = argv[-1]
        run_commands.append(command)
        if "__TF_PRESENT__" in command:
            return _FakeCompleted(returncode=probe_rc, stdout=probe_stdout)
        if "mkdir" in command:
            return _FakeCompleted(returncode=0)
        if "-prune" in command:
            prune_commands.append(command)
            return _FakeCompleted(returncode=prune_rc, stderr=prune_stderr)
        return _FakeCompleted(returncode=0)

    def fake_popen(argv: list[str], **_: object) -> _FakeProc:  # noqa: ARG001 — scripted
        return _FakeProc()

    monkeypatch.setattr(service_module.subprocess, "run", fake_run)
    monkeypatch.setattr(service_module.subprocess, "Popen", fake_popen)
    return run_commands, prune_commands


def _service_with_started_tool() -> _StubRunnerServiceImpl:
    svc = _StubRunnerServiceImpl()
    svc.trials["t-1"] = SimpleNamespace(agent_tools={"bash": _exec_wrapper(started=True)})
    return svc


def test_bare_name_exclude_matches_by_basename(monkeypatch: pytest.MonkeyPatch) -> None:
    _run_commands, prune_commands = _install_fake_subprocess(monkeypatch)
    svc = _service_with_started_tool()

    svc._materialise_path(
        src_service=None,
        dst_service="verifier",
        path="/app/output",
        exclude=("__pycache__",),
        trial_id="t-1",
    )

    assert len(prune_commands) == 1
    assert "-name __pycache__" in prune_commands[0]
    assert "-path" not in prune_commands[0]


def test_separator_exclude_matches_path_relative(monkeypatch: pytest.MonkeyPatch) -> None:
    _run_commands, prune_commands = _install_fake_subprocess(monkeypatch)
    svc = _service_with_started_tool()

    svc._materialise_path(
        src_service=None,
        dst_service="verifier",
        path="/app/output",
        exclude=("build/*.o",),
        trial_id="t-1",
    )

    assert len(prune_commands) == 1
    # A glob with a separator anchors at the materialised root via -path, so it
    # actually matches; -name (basename only) would silently prune nothing.
    assert "-path '/app/output/build/*.o'" in prune_commands[0]
    assert "-name" not in prune_commands[0]


def test_failed_prune_books_trial_ungradeable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_subprocess(monkeypatch, prune_rc=1, prune_stderr="find: boom")
    svc = _service_with_started_tool()

    with pytest.raises(SubstrateUnreachableError, match="pruning exclude"):
        svc._materialise_path(
            src_service=None,
            dst_service="verifier",
            path="/app/output",
            exclude=("__pycache__",),
            trial_id="t-1",
        )


def test_source_probed_before_destination_is_wiped(monkeypatch: pytest.MonkeyPatch) -> None:
    run_commands, _prune = _install_fake_subprocess(monkeypatch)
    svc = _service_with_started_tool()

    svc._materialise_path(
        src_service=None,
        dst_service="verifier",
        path="/app/output",
        exclude=(),
        trial_id="t-1",
    )

    probe_idx = next(i for i, c in enumerate(run_commands) if "__TF_PRESENT__" in c)
    wipe_idx = next(i for i, c in enumerate(run_commands) if "mkdir" in c)
    assert probe_idx < wipe_idx


def test_unreachable_source_probe_does_not_wipe_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Neither token in stdout => the probe could not read the source container.
    run_commands, _prune = _install_fake_subprocess(monkeypatch, probe_stdout="")
    svc = _service_with_started_tool()

    with pytest.raises(SubstrateUnreachableError, match="could not probe"):
        svc._materialise_path(
            src_service=None,
            dst_service="verifier",
            path="/app/output",
            exclude=(),
            trial_id="t-1",
        )

    assert not any("mkdir" in c for c in run_commands)


def test_absent_source_still_empties_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    run_commands, _prune = _install_fake_subprocess(monkeypatch, probe_stdout="__TF_ABSENT__\n")
    svc = _service_with_started_tool()

    svc._materialise_path(
        src_service=None,
        dst_service="verifier",
        path="/app/output",
        exclude=(),
        trial_id="t-1",
    )

    # Destination emptied (reward 0), but no copy was attempted.
    assert any("mkdir" in c for c in run_commands)


def test_probe_subprocess_exception_books_trial_ungradeable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operational subprocess failure during the source probe (a stalled
    daemon raising ``TimeoutExpired``) becomes a clean ungradeable, not a raw
    escape — same wrapping the exec seam applies."""

    def boom_run(argv: list[str], **_: object) -> _FakeCompleted:  # noqa: ARG001 — scripted
        raise subprocess.TimeoutExpired(cmd="docker", timeout=30.0)

    monkeypatch.setattr(service_module.subprocess, "run", boom_run)
    svc = _service_with_started_tool()

    with pytest.raises(SubstrateUnreachableError, match="materialising"):
        svc._materialise_path(
            src_service=None,
            dst_service="verifier",
            path="/app/output",
            exclude=(),
            trial_id="t-1",
        )


def test_docker_cp_transport_failure_books_trial_ungradeable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``docker cp`` pipe raising (daemon gone, ``docker`` binary missing)
    is wrapped as ``SubstrateUnreachableError`` rather than escaping the
    executor. The probe passes, so the failure is isolated to the copy stage."""
    _install_fake_subprocess(monkeypatch)  # probe PRESENT, rm/mkdir succeed

    def boom_popen(argv: list[str], **_: object) -> _FakeProc:  # noqa: ARG001 — scripted
        raise FileNotFoundError("docker: command not found")

    monkeypatch.setattr(service_module.subprocess, "Popen", boom_popen)
    svc = _service_with_started_tool()

    with pytest.raises(SubstrateUnreachableError, match="materialising"):
        svc._materialise_path(
            src_service=None,
            dst_service="verifier",
            path="/app/output",
            exclude=(),
            trial_id="t-1",
        )


def test_dst_popen_failure_reaps_started_src_proc(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the first ``docker cp`` (src) starts but the second (dst) ``Popen``
    construction raises, the already-running src child must be killed and reaped,
    not orphaned. Locks the partial-start cleanup: the failure still surfaces as
    an ungradeable, and the src proc saw kill()+wait()."""
    _install_fake_subprocess(monkeypatch)  # probe PRESENT, rm/mkdir succeed

    class _TrackedSrcProc:
        def __init__(self) -> None:
            self.stdout = MagicMock()
            self.killed = False
            self.waited = False

        def poll(self) -> int | None:
            # Still running until killed, so the finally kill-branch fires.
            return 0 if self.killed else None

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002 — scripted
            self.waited = True
            return 0

    src_proc = _TrackedSrcProc()
    calls = {"n": 0}

    def popen_src_then_raise(argv: list[str], **_: object) -> object:  # noqa: ARG001 — scripted
        calls["n"] += 1
        if calls["n"] == 1:
            return src_proc
        raise OSError("dst docker cp could not spawn")

    monkeypatch.setattr(service_module.subprocess, "Popen", popen_src_then_raise)
    svc = _service_with_started_tool()

    with pytest.raises(SubstrateUnreachableError, match="materialising"):
        svc._materialise_path(
            src_service=None,
            dst_service="verifier",
            path="/app/output",
            exclude=(),
            trial_id="t-1",
        )

    assert calls["n"] == 2, "the dst Popen must have been attempted"
    assert src_proc.killed and src_proc.waited, "the started src proc must be killed and reaped"


def test_programming_bug_in_materialise_propagates_not_masked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A programming bug (``AttributeError``) is NOT swallowed into the
    ungradeable path — only operational exec/transport failures are. Locks the
    'do not over-catch' boundary."""

    def buggy_run(argv: list[str], **_: object) -> _FakeCompleted:  # noqa: ARG001 — scripted
        raise AttributeError("internal bug, not an infrastructure condition")

    monkeypatch.setattr(service_module.subprocess, "run", buggy_run)
    svc = _service_with_started_tool()

    with pytest.raises(AttributeError, match="internal bug"):
        svc._materialise_path(
            src_service=None,
            dst_service="verifier",
            path="/app/output",
            exclude=(),
            trial_id="t-1",
        )


def test_unstarted_env_tool_surfaces_as_ungradeable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_subprocess(monkeypatch)
    svc = _StubRunnerServiceImpl()
    svc.trials["t-1"] = SimpleNamespace(agent_tools={"bash": _exec_wrapper(started=False)})

    with pytest.raises(SubstrateUnreachableError, match="has not started"):
        svc._materialise_path(
            src_service=None,
            dst_service="verifier",
            path="/app/output",
            exclude=(),
            trial_id="t-1",
        )


def _capture_docker_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[list[str]], list[list[str]]]:
    """Fake the subprocess seam capturing FULL argv for ``run`` and ``Popen``.

    Returns ``(run_argvs, popen_argvs)`` so a test can assert exactly which
    ``docker`` commands were issued (the probe path, the ``docker cp`` source and
    destination, the prune), not just their trailing shell string.
    """
    run_argvs: list[list[str]] = []
    popen_argvs: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> _FakeCompleted:
        run_argvs.append(list(argv))
        command = argv[-1]
        if "__TF_PRESENT__" in command:
            return _FakeCompleted(returncode=0, stdout="__TF_PRESENT__\n")
        return _FakeCompleted(returncode=0)

    def fake_popen(argv: list[str], **_: object) -> _FakeProc:
        popen_argvs.append(list(argv))
        return _FakeProc()

    monkeypatch.setattr(service_module.subprocess, "run", fake_run)
    monkeypatch.setattr(service_module.subprocess, "Popen", fake_popen)
    return run_argvs, popen_argvs


def test_trailing_slash_source_copies_like_non_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    """A source path with a trailing slash must materialise the same tree as the
    bare form. ``docker cp src:/app/data/ -`` would stream the directory's
    *contents* (no top-level ``data/`` member), extracting them loose under the
    parent (``/app`` instead of ``/app/data``) while still reporting ok — a silent
    wrong grade. The path is normalised so both forms issue byte-identical docker
    commands: probe, destination wipe, ``docker cp`` source and destination."""

    def _materialise(path: str) -> tuple[list[list[str]], list[list[str]]]:
        run_argvs, popen_argvs = _capture_docker_argv(monkeypatch)
        svc = _service_with_started_tool()
        svc._materialise_path(
            src_service=None,
            dst_service="verifier",
            path=path,
            exclude=(),
            trial_id="t-1",
        )
        return run_argvs, popen_argvs

    slash_run, slash_popen = _materialise("/app/data/")
    bare_run, bare_popen = _materialise("/app/data")

    # The trailing-slash run issues exactly the commands the bare run does.
    assert slash_run == bare_run
    assert slash_popen == bare_popen

    # And those commands target the directory itself, not its contents: the copy
    # source is ``…:/app/data`` (no slash) and the destination parent is ``/app``.
    cp_source = next(a for a in slash_popen if a[:2] == ["docker", "cp"] and a[-1] == "-")
    cp_dest = next(a for a in slash_popen if a[:2] == ["docker", "cp"] and a[2] == "-")
    assert cp_source[2] == "tbencht-1_agent:/app/data"
    assert cp_dest[3] == "tbencht-1_verifier:/app"
    # The destination wipe/parent-make names the directory and its parent exactly.
    wipe = next(a for a in slash_run if "mkdir" in a[-1])
    assert "rm -rf /app/data " in wipe[-1]
    assert "mkdir -p /app" in wipe[-1]


def test_non_compose_env_tool_surfaces_clear_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cross-container copy hard-requires a compose-backed executor. A trial whose
    env tool is NOT compose-backed fails with a CLEAR, actionable
    SubstrateUnreachableError naming the requirement — not a bare RuntimeError or
    AttributeError from reaching for a sibling resolver the tool does not have."""
    _install_fake_subprocess(monkeypatch)
    svc = _StubRunnerServiceImpl()

    class _NonComposeExecTool:
        """Satisfies SupportsEnvExec but is not a DockerComposeExecToolWrapper."""

        def exec_in_env(self, command: str, timeout_s: float) -> str:  # noqa: ARG002
            return ""

        def exec_in_env_with_exit_code(self, command: str, timeout_s: float):  # noqa: ARG002
            return (0, "")

    svc.trials["t-1"] = SimpleNamespace(agent_tools={"bash": _NonComposeExecTool()})

    with pytest.raises(SubstrateUnreachableError, match="compose-backed"):
        svc._materialise_path(
            src_service="main",
            dst_service="verifier",
            path="/app/output",
            exclude=(),
            trial_id="t-1",
        )
