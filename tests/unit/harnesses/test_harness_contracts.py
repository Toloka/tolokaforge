"""BYOH adapter metadata, flags, and container I/O contracts."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tolokaforge.docker.container import Container, ExecResult
from tolokaforge.harnesses import AgentContainerEnvironment, get_harness_spec, list_harness_specs

pytestmark = pytest.mark.unit


def test_claude_capabilities_and_credentials_are_declared() -> None:
    spec = get_harness_spec("claude-code")

    assert spec.default_version == "2.1.203"
    assert spec.required_secret_keys == ("ANTHROPIC_API_KEY",)
    assert spec.capabilities.supports_resume is True
    assert spec.capabilities.supports_mcp_headers is True
    assert spec.capabilities.supports_atif is True
    assert spec.capabilities.requires_interactive_auth is False


def test_registry_reports_documentation_only_adapters() -> None:
    specs = {spec.type: spec for spec in list_harness_specs()}

    for name in ("cursor", "copilot", "gemini", "opencode"):
        assert specs[name].capabilities.requires_interactive_auth is True
        assert specs[name].capabilities.runtime_available is False


def test_adapter_flags_reject_unknown_keys() -> None:
    with pytest.raises(ValueError, match="extra_forbidden"):
        get_harness_spec("claude-code").validate_flags({"unsafe_untracked_flag": True})


def test_agent_container_environment_delegates_exec_and_file_transfer(tmp_path: Path) -> None:
    container = MagicMock(spec=Container)
    container.exec.return_value = ExecResult(exit_code=0, stdout="ok\n", stderr="")
    container.read_file.return_value = b"result"
    environment = AgentContainerEnvironment(container)
    upload = tmp_path / "input.txt"
    upload.write_bytes(b"payload")

    result = environment.exec(["sh", "-lc", "true"])
    environment.upload(upload, "/work/input.txt")
    downloaded = environment.download("/work/output.txt", tmp_path / "nested" / "output.txt")

    assert result.stdout == "ok\n"
    container.write_file.assert_called_once_with("/work/input.txt", b"payload")
    container.read_file.assert_called_once_with("/work/output.txt")
    assert downloaded.read_bytes() == b"result"


def test_agent_container_environment_rejects_missing_upload(tmp_path: Path) -> None:
    environment = AgentContainerEnvironment(MagicMock(spec=Container))

    with pytest.raises(FileNotFoundError, match="Upload source"):
        environment.upload(tmp_path / "missing", "/work/missing")
