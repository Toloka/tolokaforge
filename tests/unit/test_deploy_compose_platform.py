"""Platform selection in the deploy suite's compose env assembly."""

import pytest

from tests.integration.deploy import conftest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("arm64", "linux/arm64"),
        ("aarch64", "linux/arm64"),
        ("ARM64", "linux/arm64"),
        ("x86_64", "linux/amd64"),
        ("amd64", "linux/amd64"),
    ],
)
def test_host_platform_maps_machine(
    monkeypatch: pytest.MonkeyPatch, machine: str, expected: str
) -> None:
    monkeypatch.setattr(conftest.platform, "machine", lambda: machine)
    assert conftest._host_platform() == expected


def test_host_platform_rejects_unknown_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conftest.platform, "machine", lambda: "sparc")
    with pytest.raises(RuntimeError, match="unsupported host architecture"):
        conftest._host_platform()


def test_local_tag_pins_host_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOLOKAFORGE_PLATFORM", raising=False)
    monkeypatch.setattr(conftest.platform, "machine", lambda: "arm64")
    env = conftest._compose_env("local")
    assert env["TOLOKAFORGE_IMAGE_TAG"] == "local"
    assert env["TOLOKAFORGE_PLATFORM"] == "linux/arm64"


def test_local_tag_preserves_explicit_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOLOKAFORGE_PLATFORM", "linux/amd64")
    monkeypatch.setattr(conftest.platform, "machine", lambda: "arm64")
    env = conftest._compose_env("local")
    assert env["TOLOKAFORGE_PLATFORM"] == "linux/amd64"


def test_published_tag_leaves_platform_to_recipe_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOLOKAFORGE_PLATFORM", raising=False)
    monkeypatch.setattr(conftest.platform, "machine", lambda: "arm64")
    env = conftest._compose_env("latest")
    assert "TOLOKAFORGE_PLATFORM" not in env
