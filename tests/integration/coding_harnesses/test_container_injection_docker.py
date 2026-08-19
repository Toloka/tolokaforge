"""``DockerExecInjector`` against a live container.

The injector's whole contract is what a real ``docker exec`` does with stdin,
quoting and modes, so the unit tier — where a recording stand-in for ``docker``
never executes anything — can only prove what was *handed* to the binary. This
is the tier that runs a container-side shell, and therefore the only one where
"a hostile ``container_path`` is a filename, not a command" is an observation
rather than an inference.
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Iterator

import pytest

from tests.utils.docker_helpers import is_docker_daemon_available
from tolokaforge_coding_harnesses import DockerExecInjector, FileSpec

pytestmark = [pytest.mark.integration, pytest.mark.docker]

IMAGE = "alpine:3.20"

NESTED_PATH = "/root/.config/deeply/nested/settings.json"
NESTED_CONTENT = '{"security":{"auth":{"useExternal":true}}}\n'

CREDENTIAL_PATH = "/root/.cli/auth.json"
CREDENTIAL_CONTENT = '{"key": "sk-$LITELLM `whoami` \'quoted\'"}\n'

INJECTION_PATH = "/tmp/inj/a b;touch /tmp/pwned"
INJECTION_CONTENT = "not a command\n"


@pytest.fixture
def running_container() -> Iterator[str]:
    name = f"tf-injection-{uuid.uuid4().hex[:12]}"
    subprocess.run(
        ["docker", "run", "-d", "--name", name, IMAGE, "sleep", "300"],
        check=True,
        capture_output=True,
        timeout=300,
    )
    try:
        yield name
    finally:
        subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True, timeout=180)


def _exec(container: str, *argv: str) -> subprocess.CompletedProcess[bytes]:
    """Run *argv* in *container* with no host or container shell in the way."""
    return subprocess.run(
        ["docker", "exec", container, *argv], capture_output=True, check=False, timeout=120
    )


@pytest.mark.skipif(
    not is_docker_daemon_available(),
    reason="Docker daemon not available (file injection needs a real container)",
)
def test_three_files_land_intact_and_the_hostile_path_stays_a_filename(running_container):
    DockerExecInjector().inject(
        running_container,
        [
            FileSpec(container_path=NESTED_PATH, content=NESTED_CONTENT),
            FileSpec(container_path=CREDENTIAL_PATH, content=CREDENTIAL_CONTENT),
            FileSpec(container_path=INJECTION_PATH, content=INJECTION_CONTENT),
        ],
    )

    # The parent of NESTED_PATH does not exist in the image, so this also
    # locks that `mkdir -p` shares the write's exec.
    for path, content in (
        (NESTED_PATH, NESTED_CONTENT),
        (CREDENTIAL_PATH, CREDENTIAL_CONTENT),
        (INJECTION_PATH, INJECTION_CONTENT),
    ):
        read_back = _exec(running_container, "cat", path)
        assert read_back.returncode == 0, f"{path} did not land: {read_back.stderr!r}"
        assert read_back.stdout == content.encode("utf-8")

        mode = _exec(running_container, "stat", "-c", "%a", path)
        assert mode.stdout.decode().strip() == "600"

    # The claim the unit tier structurally cannot make: the `;` in the path was
    # never a command separator, so nothing ran and nothing was created at the
    # path the payload named.
    assert _exec(running_container, "test", "-e", "/tmp/pwned").returncode != 0


@pytest.mark.skipif(
    not is_docker_daemon_available(),
    reason="Docker daemon not available (file injection needs a real container)",
)
def test_an_unplaceable_target_raises_naming_that_file_and_writes_nothing(running_container):
    """A parent directory that cannot be created fails *that* file's exec,
    carrying the container's own stderr — and leaves the thing in the way
    untouched, which is what sharing one exec with the write buys."""
    from tolokaforge_coding_harnesses import ContainerInjectionError

    assert _exec(running_container, "sh", "-c", "echo occupied > /root/afile").returncode == 0

    with pytest.raises(ContainerInjectionError) as excinfo:
        DockerExecInjector().inject(
            running_container,
            [FileSpec(container_path="/root/afile/child.json", content="x")],
        )

    error = excinfo.value
    assert error.container_path == "/root/afile/child.json"
    assert error.container == running_container
    assert error.returncode != 0
    assert "/root/afile" in error.stderr
    assert _exec(running_container, "cat", "/root/afile").stdout == b"occupied\n"
