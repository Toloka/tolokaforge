"""Milestone capstone for the persistent shell + editor tools (#569).

Brings up a real ``docker compose`` stack with one ``bash:5`` service and drives
BOTH runner wrappers — :class:`PersistentShellToolWrapper` and
:class:`StrReplaceEditorToolWrapper` — against it under a single shared
``trial_id``. This locks the milestone-level contract A3/A4 do not cover: both
wrappers resolve to the same container, they share that container's ``/work``
filesystem (cross-tool observation), the shell's ``tool_config.timeout_s`` is
kill-safe without corrupting the shared container, and the editor view is
deterministic.

No mocks: the compose stack is a real subprocess-managed daemon lifecycle, torn
down in a ``finally`` even on assertion failure. Skips cleanly when the Docker
daemon is unavailable; a failed assertion is a real failure, never a silent skip.
"""

from __future__ import annotations

import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge.runner.models import ToolSchema
from tolokaforge.runner.tool_factory import (
    PersistentShellToolWrapper,
    StrReplaceEditorToolWrapper,
    ToolLifecycleContext,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]

_IMAGE = "bash:5"
_PREFIX = "tf_capstone_"
_SERVICE = "app"
_WORK = "/work"

_COMPOSE_TEMPLATE = """\
services:
  {service}:
    image: {image}
    container_name: {container}
    command: ["sh", "-c", "mkdir -p {work} && exec sleep infinity"]
"""


@dataclass(frozen=True)
class ComposeStack:
    """Identity shared by the fixture and both wrappers, by construction."""

    trial_id: str
    service: str
    prefix: str
    container: str


def _compose(compose_file: Path, project: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-f", str(compose_file), "-p", project, *args],
        capture_output=True,
        text=True,
    )


def _wait_running(container: str, attempts: int = 30) -> bool:
    for _ in range(attempts):
        inspect = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container],
            capture_output=True,
            text=True,
        )
        if inspect.returncode == 0 and inspect.stdout.strip() == "true":
            return True
        time.sleep(0.5)
    return False


@pytest.fixture(scope="module")
def compose_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ComposeStack]:
    """Own the compose stack lifecycle for the module.

    Unique per run (``trial_id`` + project + container name) so xdist workers and
    re-runs never collide. The container name is resolved via the wrapper's own
    helper, so the fixture and the tools agree by construction — never a
    hand-copied string.
    """
    if not is_docker_daemon_available():
        pytest.skip("Docker daemon not available")

    trial_id = f"capstone_{uuid.uuid4().hex[:8]}"
    container = PersistentShellToolWrapper._resolve_container_name(trial_id, _SERVICE, _PREFIX)
    project = f"{_PREFIX}{trial_id}"

    compose_file = tmp_path_factory.mktemp("capstone") / "docker-compose.yml"
    compose_file.write_text(
        _COMPOSE_TEMPLATE.format(service=_SERVICE, image=_IMAGE, container=container, work=_WORK)
    )

    subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)
    up = _compose(compose_file, project, "up", "-d", "--wait")
    if up.returncode != 0:
        # A healthcheck-less --wait can be rejected on some compose builds; fall
        # back to a plain up plus a running-state poll.
        up = _compose(compose_file, project, "up", "-d")
        if up.returncode != 0:
            pytest.skip(f"could not start compose stack: {up.stderr.strip()}")
        if not _wait_running(container):
            _compose(compose_file, project, "down", "-v", "--remove-orphans")
            pytest.skip(f"compose service {container} never reached running state")

    try:
        yield ComposeStack(trial_id=trial_id, service=_SERVICE, prefix=_PREFIX, container=container)
    finally:
        _compose(compose_file, project, "down", "-v", "--remove-orphans")


def _tool_config(stack: ComposeStack, **extra: object) -> dict[str, object]:
    return {"service": stack.service, "compose_project_prefix": stack.prefix, **extra}


@contextmanager
def _shell(
    stack: ComposeStack, timeout_s: float | None = None
) -> Iterator[PersistentShellToolWrapper]:
    config = (
        _tool_config(stack, timeout_s=timeout_s) if timeout_s is not None else _tool_config(stack)
    )
    schema = ToolSchema(
        name="bash_session",
        description="x",
        parameters={"type": "object", "properties": {}},
        tool_config=config,
    )
    wrapper = PersistentShellToolWrapper(schema)
    wrapper.start(ToolLifecycleContext(trial_id=stack.trial_id))
    try:
        yield wrapper
    finally:
        wrapper.stop()


def _editor(stack: ComposeStack) -> StrReplaceEditorToolWrapper:
    schema = ToolSchema(
        name="str_replace_editor",
        description="x",
        parameters={"type": "object", "properties": {}},
        tool_config=_tool_config(stack),
    )
    return StrReplaceEditorToolWrapper(schema, trial_id=stack.trial_id)


def test_both_wrappers_target_the_same_container(compose_stack: ComposeStack):
    shell_name = PersistentShellToolWrapper._resolve_container_name(
        compose_stack.trial_id, compose_stack.service, compose_stack.prefix
    )
    editor_name = StrReplaceEditorToolWrapper._resolve_container_name(
        compose_stack.trial_id, compose_stack.service, compose_stack.prefix
    )
    assert shell_name == editor_name == compose_stack.container


async def test_shell_state_persists_across_wrapper_calls(compose_stack: ComposeStack):
    with _shell(compose_stack) as shell:
        await shell.execute({"command": "cd /work"})
        await shell.execute({"command": "export FOO=bar"})
        await shell.execute({"command": "mkdir -p sub"})
        await shell.execute({"command": "cd sub"})
        assert (await shell.execute({"command": "pwd"})).strip() == "/work/sub"
        assert (await shell.execute({"command": "echo $FOO"})).strip() == "bar"

        assert "restarted" in await shell.execute({"restart": True})
        assert (await shell.execute({"command": "echo $FOO"})).strip() == ""
        assert (await shell.execute({"command": "pwd"})).strip() != "/work/sub"


async def test_editor_four_command_round_trip_through_wrapper(compose_stack: ComposeStack):
    editor = _editor(compose_stack)
    path = f"{_WORK}/story.txt"

    assert "created successfully" in await editor.execute(
        {"command": "create", "path": path, "file_text": "the quick brown fox\ntail\n"}
    )
    assert (
        await editor.execute({"command": "view", "path": path})
        == "     1\tthe quick brown fox\n     2\ttail\n"
    )
    await editor.execute(
        {"command": "str_replace", "path": path, "old_str": "quick brown", "new_str": "slow red"}
    )
    assert (
        await editor.execute({"command": "view", "path": path, "view_range": [1, 1]})
        == "     1\tthe slow red fox\n"
    )
    await editor.execute(
        {"command": "insert", "path": path, "insert_line": 1, "insert_text": "INSERTED"}
    )
    assert await editor.execute({"command": "view", "path": path}) == (
        "     1\tthe slow red fox\n     2\tINSERTED\n     3\ttail\n"
    )


async def test_shell_and_editor_share_the_container_filesystem(compose_stack: ComposeStack):
    editor = _editor(compose_stack)
    from_editor = f"{_WORK}/from_editor.txt"
    from_shell = f"{_WORK}/from_shell.txt"

    await editor.execute(
        {"command": "create", "path": from_editor, "file_text": "written-by-editor\n"}
    )
    with _shell(compose_stack) as shell:
        assert (
            await shell.execute({"command": f"cat {from_editor}"})
        ).strip() == "written-by-editor"

        await shell.execute({"command": f"printf 'written-by-shell\\n' > {from_shell}"})

    assert (
        await editor.execute({"command": "view", "path": from_shell})
        == "     1\twritten-by-shell\n"
    )
    await editor.execute(
        {
            "command": "str_replace",
            "path": from_shell,
            "old_str": "written-by-shell",
            "new_str": "edited-by-editor",
        }
    )
    with _shell(compose_stack) as shell:
        assert (await shell.execute({"command": f"cat {from_shell}"})).strip() == "edited-by-editor"


async def test_shell_timeout_is_kill_safe_and_editor_unaffected(compose_stack: ComposeStack):
    editor = _editor(compose_stack)
    path = f"{_WORK}/after_timeout.txt"

    with _shell(compose_stack, timeout_s=3) as shell:
        result = await shell.execute({"command": "sleep 300"})
        assert "timed out" in result

        assert "created successfully" in await editor.execute(
            {"command": "create", "path": path, "file_text": "still-here\n"}
        )
        assert await editor.execute({"command": "view", "path": path}) == "     1\tstill-here\n"

        assert (await shell.execute({"command": "echo alive"})).strip() == "alive"


async def test_repeated_view_is_byte_identical(compose_stack: ComposeStack):
    editor = _editor(compose_stack)
    path = f"{_WORK}/stable.txt"
    await editor.execute({"command": "create", "path": path, "file_text": "line one\nline two\n"})
    first = await editor.execute({"command": "view", "path": path})
    second = await editor.execute({"command": "view", "path": path})
    assert first == second
