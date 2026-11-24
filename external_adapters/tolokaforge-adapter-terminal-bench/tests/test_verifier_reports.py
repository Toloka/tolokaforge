"""The verifier reports a v4 task's grading config asks the engine to read.

A v4 suite prints its reward and nothing else: the per-criterion verdicts go to
a CTRF report and a details JSON beside the reward, which ``test.sh`` chmods
open to the harness on its way out. They live in the trial's compose staging,
removed at teardown, so a report the grading config does not name is gone — and
a 0.0 is then auditable only by running the task again.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_LEGACY_DIR = Path(__file__).resolve().parents[3] / "examples" / "terminal_bench"


def _grading_method_config(terminal_bench_dir: Path, task_id: str) -> dict:
    adapter = TerminalBenchAdapter(
        {"terminal_bench_dir": str(terminal_bench_dir), "task_ids": [task_id]}
    )
    grading = adapter.to_task_description(task_id).grading
    assert grading is not None
    return dict(grading.grading_method_config or {})


def test_a_v4_task_asks_for_the_ctrf_and_details_reports() -> None:
    config = _grading_method_config(_FIXTURES, "v4_echo")

    assert config["detail_paths"] == [
        "/logs/verifier/ctrf.json",
        "/logs/verifier/reward-details.json",
    ]


def test_the_reports_sit_beside_the_reward_the_same_config_reads() -> None:
    """Both reports are named in the directory the reward is read from.

    The pack writes all three into one directory and opens all three together;
    a report path that drifted away from the reward path would read an empty
    location and report every grade as unauditable.
    """
    config = _grading_method_config(_FIXTURES, "v4_echo")
    reward_dir = str(Path(config.get("reward_path", "/logs/verifier/reward.txt")).parent)

    assert [str(Path(p).parent) for p in config["detail_paths"]] == [reward_dir, reward_dir]


def test_a_legacy_task_asks_for_no_reports() -> None:
    """Only the v4 shape ships the reports, so only it names them.

    A legacy pack writes no CTRF; naming one would make the engine ``cat`` a
    path that never exists and note it as unavailable on every single grade.
    """
    config = _grading_method_config(_LEGACY_DIR, "fix-airline-segmentation")

    assert "detail_paths" not in config
