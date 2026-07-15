"""Unit tests for the per-service compose-log capture primitive."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tolokaforge.core.compose_materialisation import capture_compose_service_logs

pytestmark = pytest.mark.unit


class _FakeCompose:
    """Pins the command shape capture_compose_service_logs must build.

    Exposes the same ``compose_command_property`` base list and ``context``
    attribute the real testcontainers ``DockerCompose`` carries. Records the
    argv of every ``docker compose logs`` invocation and returns canned bytes
    per service; a service listed in ``raising`` raises instead.
    """

    def __init__(self, context: Path, outputs: dict[str, bytes], raising: set[str] | None = None):
        self.compose_command_property = ["docker", "compose", "-f", "compose.yaml"]
        self.context = context
        self._outputs = outputs
        self._raising = raising or set()
        self.calls: list[list[str]] = []

    def run(self, command: list[str], *, cwd: str, capture_output: bool, check: bool):
        self.calls.append(command)
        service = command[-1]
        if service in self._raising:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, stdout=self._outputs.get(service, b""))


def test_writes_one_log_file_per_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "services"
    fake = _FakeCompose(
        context=tmp_path / "ctx",
        outputs={"db": b"db log line\n", "api": b"api boot\napi ready\n"},
    )
    monkeypatch.setattr(subprocess, "run", fake.run)

    result = capture_compose_service_logs(fake, ["db", "api"], dest, tail=250)

    assert result == {"db": len(b"db log line\n"), "api": len(b"api boot\napi ready\n")}
    assert (dest / "db.log").read_bytes() == b"db log line\n"
    assert (dest / "api.log").read_bytes() == b"api boot\napi ready\n"


def test_command_shape_and_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = tmp_path / "ctx"
    fake = _FakeCompose(context=ctx, outputs={"db": b"x"})
    monkeypatch.setattr(subprocess, "run", fake.run)

    capture_compose_service_logs(fake, ["db"], tmp_path / "services", tail=99)

    assert fake.calls == [
        [
            "docker",
            "compose",
            "-f",
            "compose.yaml",
            "logs",
            "--no-color",
            "--no-log-prefix",
            "--tail",
            "99",
            "db",
        ]
    ]


def test_raising_service_is_omitted_without_propagating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "services"
    fake = _FakeCompose(
        context=tmp_path / "ctx",
        outputs={"db": b"db log\n"},
        raising={"api"},
    )
    monkeypatch.setattr(subprocess, "run", fake.run)

    result = capture_compose_service_logs(fake, ["db", "api"], dest, tail=100)

    assert result == {"db": len(b"db log\n")}
    assert (dest / "db.log").exists()
    assert not (dest / "api.log").exists()


def test_empty_output_service_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dest = tmp_path / "services"
    fake = _FakeCompose(context=tmp_path / "ctx", outputs={"db": b"", "api": b"api\n"})
    monkeypatch.setattr(subprocess, "run", fake.run)

    result = capture_compose_service_logs(fake, ["db", "api"], dest, tail=100)

    assert result == {"api": len(b"api\n")}
    assert not (dest / "db.log").exists()


def test_none_compose_returns_empty_and_creates_no_dir(tmp_path: Path) -> None:
    dest = tmp_path / "services"

    result = capture_compose_service_logs(None, ["db"], dest, tail=100)

    assert result == {}
    assert not dest.exists()


def test_empty_service_names_creates_no_dir(tmp_path: Path) -> None:
    ctx = tmp_path / "ctx"
    dest = tmp_path / "services"
    fake = _FakeCompose(context=ctx, outputs={})

    result = capture_compose_service_logs(fake, [], dest, tail=100)

    assert result == {}
    assert not dest.exists()
