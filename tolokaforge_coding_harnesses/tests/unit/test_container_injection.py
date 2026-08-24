"""What ``DockerExecInjector`` hands the docker binary, and what it never does.

Every test here runs a **real** executable: a recording stand-in for ``docker``
written to ``tmp_path``, invoked through ``subprocess`` exactly as the real
binary would be. Patching ``subprocess.run`` would let the test assert the call
the implementation happens to make, which is the one thing worth doubting — a
recorder proves what a process actually received on its argv and its stdin.

The real container-side behaviour (quoting through a shell, ``mkdir -p``,
modes) is not provable here, because the stand-in never executes anything. That
lives in ``tests/integration/coding_harnesses/test_container_injection_docker.py``.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from tolokaforge_coding_harnesses import (
    ContainerFileInjector,
    ContainerInjectionError,
    DockerExecInjector,
    FileSpec,
)

pytestmark = pytest.mark.unit

SECRET = "sk-test-secret-value"


@dataclass(frozen=True)
class FakeDocker:
    """A recording stand-in for the docker binary, plus what it recorded."""

    path: Path
    record_dir: Path

    def argv(self, invocation: int = 0) -> list[str]:
        return json.loads(self.record_dir.joinpath(f"argv.{invocation}").read_text())

    def stdin(self, invocation: int = 0) -> bytes:
        return self.record_dir.joinpath(f"stdin.{invocation}").read_bytes()

    @property
    def invocations(self) -> int:
        return len(list(self.record_dir.glob("argv.*")))


def _fake_docker(tmp_path: Path, *, exit_code: int = 0, stderr: str = "") -> FakeDocker:
    record_dir = tmp_path / f"record-{exit_code}"
    record_dir.mkdir()
    binary = tmp_path / f"docker-{exit_code}"
    binary.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        f"record = pathlib.Path({str(record_dir)!r})\n"
        "n = len(list(record.glob('argv.*')))\n"
        "record.joinpath(f'argv.{n}').write_text(json.dumps(sys.argv[1:]))\n"
        "record.joinpath(f'stdin.{n}').write_bytes(sys.stdin.buffer.read())\n"
        f"sys.stderr.write({stderr!r})\n"
        f"sys.exit({exit_code})\n"
    )
    binary.chmod(0o755)
    return FakeDocker(path=binary, record_dir=record_dir)


def _hanging_docker(tmp_path: Path) -> Path:
    """A stand-in that never returns — a wedged daemon, or a container in
    restart backoff."""
    binary = tmp_path / "docker-hangs"
    binary.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n")
    binary.chmod(0o755)
    return binary


def _parameters(method: Callable[..., None]) -> list[tuple[str, str]]:
    return [(p.name, p.kind.name) for p in inspect.signature(method).parameters.values()]


def test_the_shipped_implementation_satisfies_the_contract():
    """The Protocol is what a second transport (``kubectl exec``) is written
    against, and its only consumer is in another repo: unlocked, a rename here
    ships green and surfaces there as a ``TypeError`` on a keyword argument.

    ``@runtime_checkable`` only proves the method exists, so the parameter names
    and kinds are pinned separately. Annotations and defaults are not compared.
    """
    injector: ContainerFileInjector = DockerExecInjector()

    assert isinstance(injector, ContainerFileInjector)

    expected = [
        ("self", "POSITIONAL_OR_KEYWORD"),
        ("container", "POSITIONAL_OR_KEYWORD"),
        ("files", "POSITIONAL_OR_KEYWORD"),
    ]
    assert _parameters(ContainerFileInjector.inject) == expected
    assert _parameters(DockerExecInjector.inject) == expected


class TestTheCredentialNeverLeavesStdin:
    """The module's whole premise: a resolved credential reaches the container
    down one pipe and is visible nowhere else a process or a log can read."""

    def test_content_arrives_on_stdin_byte_for_byte(self, tmp_path):
        """Trailing newlines, embedded quotes, ``$`` and backticks all survive:
        a config file the CLI parses is wrong if any of them is rewritten."""
        fake = _fake_docker(tmp_path)
        content = 'key = "$LITELLM_API_KEY"\nshell = `whoami`\n\n'

        DockerExecInjector(docker_binary=str(fake.path)).inject(
            "trial-container", [FileSpec(container_path="/root/.cli/config.toml", content=content)]
        )

        assert fake.stdin() == content.encode("utf-8")

    def test_the_credential_appears_in_no_argument(self, tmp_path):
        """``ps`` on the host, and inside the container, must not show it."""
        fake = _fake_docker(tmp_path)

        DockerExecInjector(docker_binary=str(fake.path)).inject(
            "trial-container", [FileSpec(container_path="/root/.cli/auth.json", content=SECRET)]
        )

        assert not any(SECRET in argument for argument in fake.argv())

    def test_the_generated_repr_omits_the_content(self):
        """One line, and the only thing between a resolved credential and every
        traceback, pytest assertion diff and debug log this module can produce."""
        assert SECRET not in repr(FileSpec(container_path="/x", content=SECRET))


class TestOneExecPerFile:
    def test_three_files_are_three_invocations(self, tmp_path):
        """A batched write cannot name the path that failed; the per-file exec
        is what makes ``ContainerInjectionError`` specific."""
        fake = _fake_docker(tmp_path)
        specs = [FileSpec(container_path=f"/root/f{i}", content=f"content {i}") for i in range(3)]

        DockerExecInjector(docker_binary=str(fake.path)).inject("trial-container", specs)

        assert fake.invocations == 3
        assert [fake.stdin(i) for i in range(3)] == [b"content 0", b"content 1", b"content 2"]

    def test_a_failing_exec_raises_naming_the_container_and_the_path(self, tmp_path):
        fake = _fake_docker(tmp_path, exit_code=1, stderr="chmod: /root/f: Read-only file system")

        with pytest.raises(ContainerInjectionError) as excinfo:
            DockerExecInjector(docker_binary=str(fake.path)).inject(
                "trial-container", [FileSpec(container_path="/root/f", content="x")]
            )

        error = excinfo.value
        assert error.container == "trial-container"
        assert error.container_path == "/root/f"
        assert error.returncode == 1
        message = str(error)
        assert "trial-container" in message
        assert "/root/f" in message
        assert "Read-only file system" in message

    def test_a_failing_exec_stops_before_the_next_file(self, tmp_path):
        """No partial-success return value and no logging-and-continuing: the
        caller learns about the first failure, with the rest not attempted."""
        fake = _fake_docker(tmp_path, exit_code=1, stderr="boom")
        specs = [FileSpec(container_path=f"/root/f{i}", content="x") for i in range(3)]

        with pytest.raises(ContainerInjectionError):
            DockerExecInjector(docker_binary=str(fake.path)).inject("trial-container", specs)

        assert fake.invocations == 1

    def test_an_exec_that_never_returns_raises_naming_the_container_and_the_path(self, tmp_path):
        """A wedged daemon produces no output under ``capture_output``, so
        without the bound the provisioning call blocks with nothing to read.

        The error carries no returncode at all: a caller reading a negative code
        as a signal death would report a killed exec that never ran.
        """
        with pytest.raises(ContainerInjectionError) as excinfo:
            DockerExecInjector(docker_binary=str(_hanging_docker(tmp_path)), timeout_s=0.5).inject(
                "trial-container", [FileSpec(container_path="/root/f", content=SECRET)]
            )

        error = excinfo.value
        assert isinstance(error.__cause__, subprocess.TimeoutExpired)
        assert error.container == "trial-container"
        assert error.container_path == "/root/f"
        assert error.returncode is None
        assert "did not return within 0.5s" in str(error)
        assert "exit status" not in str(error)
        assert SECRET not in str(error)


class TestThePathIsDataNotProgram:
    """A ``container_path`` is a filename, never a fragment of a command.

    The obvious phrasing of this test — "assert the path arrived as one
    argument" — is true of *every* implementation, including
    ``subprocess.run([docker, "exec", c, "sh", "-c", f"cat > {path}"])``: that
    also passes one argv element, and a stand-in that records and exits never
    runs it. What is asserted instead is that the argv for a hostile path
    differs from the argv for a benign one **only in the path element itself**.
    A shell program built by concatenation moves with its input; a fixed
    literal does not.
    """

    BENIGN = "/root/.cli/config.toml"
    HOSTILE = "/root/.cli/x; touch /tmp/pwned"

    def _argv_for(self, tmp_path: Path, name: str, container_path: str) -> list[str]:
        fake = _fake_docker(tmp_path / name)
        DockerExecInjector(docker_binary=str(fake.path)).inject(
            "trial-container", [FileSpec(container_path=container_path, content="x")]
        )
        return fake.argv()

    @pytest.fixture(autouse=True)
    def _argv_dirs(self, tmp_path):
        (tmp_path / "benign").mkdir()
        (tmp_path / "hostile").mkdir()

    def test_only_the_path_element_moves_between_a_benign_and_a_hostile_path(self, tmp_path):
        benign = self._argv_for(tmp_path, "benign", self.BENIGN)
        hostile = self._argv_for(tmp_path, "hostile", self.HOSTILE)

        assert len(benign) == len(hostile)
        differing = [(b, h) for b, h in zip(benign, hostile, strict=True) if b != h]
        assert differing == [(self.BENIGN, self.HOSTILE)], (
            "an argv element other than the path itself changed with the path, so the "
            f"program is being built from FileSpec data: {differing!r}"
        )

    def test_the_hostile_path_is_a_whole_argument_and_not_embedded_in_one(self, tmp_path):
        hostile = self._argv_for(tmp_path, "hostile", self.HOSTILE)

        assert self.HOSTILE in hostile
        assert not [
            argument
            for argument in hostile
            if self.HOSTILE in argument and argument != self.HOSTILE
        ]
