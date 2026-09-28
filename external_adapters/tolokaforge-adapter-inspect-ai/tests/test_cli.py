"""CLI tests for `tolokaforge-inspect`, at $0 with mockllm."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from tolokaforge_adapter_inspect_ai.cli import app
from typer.testing import CliRunner

pytestmark = pytest.mark.integration

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
pytest.importorskip("inspect_ai")
_INSPECT_CLI = shutil.which("inspect")
_runner = CliRunner()


@pytest.mark.skipif(_INSPECT_CLI is None, reason="`inspect` CLI not on PATH")
def test_cli_list():
    result = _runner.invoke(app, ["list", str(_FIXTURES)])
    assert result.exit_code == 0, result.output
    assert "poc_smoke" in result.output


@pytest.mark.skipif(_INSPECT_CLI is None, reason="`inspect` CLI not on PATH")
def test_cli_run(tmp_path):
    result = _runner.invoke(
        app,
        ["run", str(_FIXTURES), "--model", "mockllm/model", "--output-dir", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "poc_smoke: PASS" in result.output
    assert (tmp_path / "summary.json").exists()
