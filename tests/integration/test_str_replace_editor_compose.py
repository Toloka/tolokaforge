"""Behaviour-parity lock for the compose ``str_replace_editor`` engine (#567, #637).

Runs the four editor commands against a real ``docker exec`` into a running
container — proving the compose provider is behaviour-identical to the local one
(line-numbered view, unique str_replace, insert, fail-loud create/str_replace,
container-side path containment). The behaviour battery runs once per root
(``/work`` and a non-default absolute root) so a configured ``working_root`` is
proven equivalent to the default; escape rejection is checked per command against
the custom root, and a root absent from the container fails loud naming the root.

No mocks: a one-service container is brought up in the fixture and torn down
after. Skips cleanly when the Docker daemon is unavailable; a failed assertion
is a real failure, never a silent skip.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge.runner.models import ToolSchema
from tolokaforge.runner.tool_factory import StrReplaceEditorToolWrapper
from tolokaforge.tools.str_replace_editor import DockerComposeEditor, EditorError

pytestmark = [pytest.mark.integration, pytest.mark.requires_docker]

_IMAGE = "alpine:latest"
_PREFIX = "tf_test_editor_"
# Worker-scoped trial id so each xdist worker owns a distinct container; without
# this the shared name collides under -n auto and all-but-one worker skip.
_TRIAL_ID = f"s2parity_{os.environ.get('PYTEST_XDIST_WORKER', 'serial')}"
_SERVICE = "app"
_CONTAINER = StrReplaceEditorToolWrapper._resolve_container_name(_TRIAL_ID, _SERVICE, _PREFIX)
_WORK = "/work"
_CUSTOM_ROOT = "/srv/agent"
# The behaviour battery runs once per root so the compose engine is proven
# behaviour-identical at the default /work and at a non-default absolute root.
_ROOTS = [_WORK, _CUSTOM_ROOT]


@pytest.fixture(scope="module")
def running_container():
    """Bring up a single container with each editable root created inside it."""
    if not is_docker_daemon_available():
        pytest.skip("Docker daemon not available")
    subprocess.run(["docker", "rm", "-f", _CONTAINER], capture_output=True, check=False)
    up = subprocess.run(
        ["docker", "run", "-d", "--name", _CONTAINER, _IMAGE, "sleep", "3600"],
        capture_output=True,
        text=True,
    )
    if up.returncode != 0:
        pytest.skip(f"could not start test container: {up.stderr.strip()}")
    subprocess.run(
        ["docker", "exec", _CONTAINER, "mkdir", "-p", *_ROOTS], capture_output=True, check=True
    )
    try:
        yield _CONTAINER
    finally:
        subprocess.run(["docker", "rm", "-f", _CONTAINER], capture_output=True, check=False)


@pytest.fixture(params=_ROOTS)
def root(request) -> str:
    return request.param


@pytest.fixture
def editor(running_container, root) -> DockerComposeEditor:
    return DockerComposeEditor(running_container, base_path=root)


def test_create_view_str_replace_insert_round_trip(editor):
    path = f"{editor.base_path}/story.txt"
    assert "created successfully" in editor.create(path, "the quick brown fox\ntail\n")

    assert editor.view(path) == "     1\tthe quick brown fox\n     2\ttail\n"

    editor.str_replace(path, "quick brown", "slow red")
    assert editor.view(path, view_range=[1, 1]) == "     1\tthe slow red fox\n"

    editor.insert(path, 1, "INSERTED")
    assert editor.view(path) == ("     1\tthe slow red fox\n     2\tINSERTED\n     3\ttail\n")


def test_create_on_existing_path_fails_loud(editor):
    path = f"{editor.base_path}/existing.txt"
    editor.create(path, "original\n")
    with pytest.raises(EditorError):
        editor.create(path, "overwrite\n")
    assert editor.view(path) == "     1\toriginal\n"


def test_str_replace_non_unique_fails_loud(editor):
    path = f"{editor.base_path}/dup.txt"
    editor.create(path, "foo and foo\n")
    with pytest.raises(EditorError) as exc:
        editor.str_replace(path, "foo", "bar")
    assert "2" in str(exc.value)
    assert editor.view(path) == "     1\tfoo and foo\n"


def test_absolute_path_outside_root_fails_loud(editor):
    with pytest.raises(EditorError):
        editor.view("/etc/hostname")


def test_symlink_escape_fails_loud(running_container):
    """A symlink inside the custom root pointing outside it is rejected: the
    in-container ``realpath`` follows it, so containment binds to the target."""
    ed = DockerComposeEditor(running_container, base_path=_CUSTOM_ROOT)
    subprocess.run(
        [
            "docker",
            "exec",
            running_container,
            "ln",
            "-sf",
            "/etc/hostname",
            f"{_CUSTOM_ROOT}/escape.txt",
        ],
        capture_output=True,
        check=True,
    )
    with pytest.raises(EditorError) as exc:
        ed.view(f"{_CUSTOM_ROOT}/escape.txt")
    assert "escapes the working root" in str(exc.value)
    assert _CUSTOM_ROOT in str(exc.value)


def test_parent_traversal_escape_fails_loud(running_container):
    ed = DockerComposeEditor(running_container, base_path=_CUSTOM_ROOT)
    with pytest.raises(EditorError) as exc:
        ed.view(f"{_CUSTOM_ROOT}/../../etc/hostname")
    assert "escapes the working root" in str(exc.value)
    assert _CUSTOM_ROOT in str(exc.value)


def test_missing_compose_root_fails_loud(running_container):
    """A configured root absent from the container fails loud on first command,
    naming the root — not a misleading 'file not found'."""
    ed = DockerComposeEditor(running_container, base_path="/srv/missing")
    with pytest.raises(EditorError) as exc:
        ed.view("anything.txt")
    msg = str(exc.value)
    assert "/srv/missing" in msg
    assert "does not exist or is not a directory" in msg


async def test_wrapper_drives_compose_backend_end_to_end(running_container):
    """Full wrapper path: config selects the compose backend, resolves the
    running container's name, and edits a file against the real daemon."""
    schema = ToolSchema(
        name="str_replace_editor",
        description="x",
        parameters={"type": "object", "properties": {}},
        tool_config={"service": _SERVICE, "compose_project_prefix": _PREFIX},
    )
    wrapper = StrReplaceEditorToolWrapper(schema, trial_id=_TRIAL_ID)
    path = f"{_WORK}/wrapper.txt"
    assert "created successfully" in await wrapper.execute(
        {"command": "create", "path": path, "file_text": "hello\n"}
    )
    view = await wrapper.execute({"command": "view", "path": path})
    assert view == "     1\thello\n"


async def test_wrapper_drives_compose_backend_with_custom_working_root(running_container):
    """Full wrapper path with ``working_root`` set: the edit lands under the
    custom root, not ``/work``."""
    schema = ToolSchema(
        name="str_replace_editor",
        description="x",
        parameters={"type": "object", "properties": {}},
        tool_config={
            "service": _SERVICE,
            "compose_project_prefix": _PREFIX,
            "working_root": _CUSTOM_ROOT,
        },
    )
    wrapper = StrReplaceEditorToolWrapper(schema, trial_id=_TRIAL_ID)
    path = f"{_CUSTOM_ROOT}/wrapper_custom.txt"
    assert "created successfully" in await wrapper.execute(
        {"command": "create", "path": path, "file_text": "hello\n"}
    )
    view = await wrapper.execute({"command": "view", "path": path})
    assert view == "     1\thello\n"
