"""Reading a harness trial's native artifacts out of the still-up container.

Three surfaces meet here. The :class:`BaseAdapter` hook names which in-container
paths hold the harness's own artifacts; its default is empty, so the engine-loop
path and any adapter without native artifacts preserve nothing. The
:class:`CodingHarnessAdapterMixin` override names ``/logs``, the Harbor /
terminal-bench artifact root. And :meth:`TrialRunner._read_container_artifacts`
copies those paths out of the container via the exec seam — ``tar | base64`` in,
a ``relative path -> bytes`` mapping out — while never failing a trial on a read
error.

The container read is driven over a scripted executor so the assertions are
about the staging, not about a real container.
"""

from __future__ import annotations

import base64
import io
import tarfile
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.adapters.base import AdapterEnvironment, BaseAdapter
from tolokaforge.core.models import GradingConfig, TaskConfig
from tolokaforge.core.runner import TrialRunner
from tolokaforge.tools.registry import ToolResult
from tolokaforge_coding_harnesses import CodingHarnessAdapterMixin

pytestmark = pytest.mark.unit


class _PlainAdapter(BaseAdapter):
    """A concrete adapter that overrides none of the optional hooks.

    Every abstract method raises — none is reached here — so the only behaviour
    under test is the inherited :meth:`native_artifact_container_paths` default.
    """

    def get_task_ids(self) -> list[str]:  # pragma: no cover - never called
        raise NotImplementedError

    def get_task(self, task_id: str) -> TaskConfig:  # pragma: no cover
        raise NotImplementedError

    def get_task_dir(self, task_id: str) -> Path:  # pragma: no cover
        raise NotImplementedError

    def create_environment(self, task_id: str) -> AdapterEnvironment:  # pragma: no cover
        raise NotImplementedError

    def get_tools(self, task_id: str) -> list[Any]:  # pragma: no cover
        raise NotImplementedError

    def get_registry_tools(
        self, task_id: str, env: AdapterEnvironment
    ) -> list[Any]:  # pragma: no cover
        raise NotImplementedError

    def get_system_prompt(self, task_id: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def get_grading_config(self, task_id: str) -> GradingConfig:  # pragma: no cover
        raise NotImplementedError

    def reset_environment(self, env: AdapterEnvironment) -> None:  # pragma: no cover
        raise NotImplementedError

    def compute_golden_hash(
        self, task_id: str, env: AdapterEnvironment
    ) -> str | None:  # pragma: no cover
        raise NotImplementedError

    def to_task_description(self, task_id: str) -> Any:  # pragma: no cover
        raise NotImplementedError


def test_base_adapter_preserves_nothing_by_default() -> None:
    """The default hook is a no-op empty list: no native artifacts to preserve."""
    adapter = _PlainAdapter({})
    assert adapter.native_artifact_container_paths("any-task") == []


def test_coding_harness_mixin_names_the_logs_root() -> None:
    """Harbor / terminal-bench adapters inherit ``/logs`` as the artifact root."""
    assert CodingHarnessAdapterMixin().native_artifact_container_paths("any-task") == ["/logs"]


def _tar_base64(members: dict[str, bytes]) -> str:
    """A base64-encoded tar carrying *members* — what ``tar | base64`` prints."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return base64.b64encode(buffer.getvalue()).decode("ascii")


class _ScriptedExecutor:
    """A ``docker exec`` bash tool scripted to answer the artifact read.

    *archives* maps a container path to the base64 tar its ``tar ... | base64``
    command returns. A path absent from the map answers like ``tar`` on a
    missing file: a non-zero status. *raises* makes the executor itself fail —
    a container already gone — which must cost the read nothing.
    """

    def __init__(
        self,
        archives: dict[str, str] | None = None,
        *,
        raises: Exception | None = None,
    ) -> None:
        self._archives = archives or {}
        self._raises = raises
        self.commands: list[str] = []

    def execute(self, tool_name: str, arguments: dict[str, Any], **kwargs: Any) -> ToolResult:
        command = arguments["command"]
        self.commands.append(command)
        if self._raises is not None:
            raise self._raises
        for container_path, encoded in self._archives.items():
            if container_path.lstrip("/") in command:
                return ToolResult(success=True, output=encoded)
        return ToolResult(
            success=False,
            output="",
            error="tar: no such file or directory",
        )


def _runner(executor: _ScriptedExecutor) -> TrialRunner:
    runner = TrialRunner(
        task_id="native-artifacts",
        trial_index=0,
        agent_client=_StubAgentClient(),  # type: ignore[arg-type]
        user_simulator=None,
        tool_executor=executor,
        tool_schemas=[],
        episode_timeout_s=600,
    )
    # ``_read_container_artifacts`` logs on every path; the helper methods expect
    # a live logger, which ``run_harness`` would have built. Build it directly so
    # the helper is drivable without the full trial.
    from tolokaforge.core.logging import init_trial_logger

    runner.logger = init_trial_logger("native-artifacts:0", verbose=False, strict=False)
    return runner


class _StubAgentClient:
    """The CLI ran the model, so the client issues nothing — only identity is read."""

    model_name = "openrouter/anthropic/claude-sonnet-4.6"


def test_read_container_artifacts_stages_the_subtree() -> None:
    """A directory path's members land as ``relative path -> bytes``, subtree kept."""
    tree = {
        "logs/verifier/reward.txt": b"1.0\n",
        "logs/agent/session.log": b"agent did a thing\n",
    }
    executor = _ScriptedExecutor({"/logs": _tar_base64(tree)})
    staged = _runner(executor)._read_container_artifacts("bash", ["/logs"])

    assert staged == tree
    # The read is one ``tar | base64`` rooted at ``/`` so member names keep the
    # container subtree.
    assert executor.commands == ["tar -cf - -C / logs | base64"]


def test_read_container_artifacts_merges_multiple_paths() -> None:
    """Several named paths merge into one mapping."""
    executor = _ScriptedExecutor(
        {
            "/logs/verifier/reward.txt": _tar_base64({"reward.txt": b"0.5\n"}),
            "/results": _tar_base64({"results/out.json": b"{}\n"}),
        }
    )
    staged = _runner(executor)._read_container_artifacts(
        "bash", ["/logs/verifier/reward.txt", "/results"]
    )

    assert staged == {"reward.txt": b"0.5\n", "results/out.json": b"{}\n"}


def test_absent_path_contributes_nothing_and_does_not_raise() -> None:
    """A path the container never held is skipped; the read yields ``None``."""
    executor = _ScriptedExecutor({})  # every command answers non-zero
    assert _runner(executor)._read_container_artifacts("bash", ["/logs"]) is None


def test_executor_failure_does_not_raise() -> None:
    """An executor that raises — a container already gone — costs the read nothing."""
    executor = _ScriptedExecutor(raises=RuntimeError("container gone"))
    assert _runner(executor)._read_container_artifacts("bash", ["/logs"]) is None
