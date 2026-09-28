"""End-to-end delegation on the local path, at $0.

Discovers the fixture Inspect task, runs it through ``inspect eval`` with the
offline ``mockllm`` provider, then normalizes the produced ``.eval`` log into a
tolokaforge Grade + Trajectory. No network, no model spend.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from tolokaforge_adapter_inspect_ai import normalize
from tolokaforge_adapter_inspect_ai.adapter import InspectAiAdapter
from tolokaforge_adapter_inspect_ai.bridge import run_inspect_eval

pytestmark = pytest.mark.integration

_FIXTURES = Path(__file__).resolve().parent / "fixtures"

pytest.importorskip("inspect_ai")
_INSPECT_CLI = shutil.which("inspect")


@pytest.mark.skipif(_INSPECT_CLI is None, reason="`inspect` CLI not on PATH")
def test_delegated_run_end_to_end(tmp_path):
    adapter = InspectAiAdapter({"inspect_task_dir": str(_FIXTURES), "agent_model": "mockllm/model"})
    assert "poc_smoke" in adapter.get_task_ids()
    task_info = adapter._tasks["poc_smoke"]

    result = run_inspect_eval(
        task_file=task_info.file,
        task_name=task_info.name,
        model="mockllm/model",
        log_dir=tmp_path,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    assert result.log_path is not None and result.log_path.exists()

    from inspect_ai.log import read_eval_log

    log = read_eval_log(str(result.log_path))
    grade = normalize.run_grade(log)
    # mockllm's default completion contains "output", so includes() marks both CORRECT
    assert grade.binary_pass is True
    assert grade.score == 1.0

    trajectories = [
        normalize.sample_trajectory(s, task_id="poc_smoke") for s in (log.samples or [])
    ]
    assert trajectories and all(t.messages for t in trajectories)
