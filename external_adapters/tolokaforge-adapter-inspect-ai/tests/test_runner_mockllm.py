"""End-to-end run of a task pack through the runner, at $0 with mockllm."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from tolokaforge_adapter_inspect_ai.runner import run_pack

pytestmark = pytest.mark.integration

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
pytest.importorskip("inspect_ai")
_INSPECT_CLI = shutil.which("inspect")


@pytest.mark.skipif(_INSPECT_CLI is None, reason="`inspect` CLI not on PATH")
def test_run_pack_mockllm(tmp_path):
    results = run_pack(_FIXTURES, model="mockllm/model", output_dir=tmp_path, timeout=300)

    assert results
    result = next(r for r in results if r.task_id == "poc_smoke")
    assert result.grade.binary_pass is True
    assert result.grade.score == 1.0
    assert result.trajectories and all(t.messages for t in result.trajectories)

    assert (tmp_path / "summary.json").exists()
    assert (tmp_path / "poc_smoke.json").exists()
