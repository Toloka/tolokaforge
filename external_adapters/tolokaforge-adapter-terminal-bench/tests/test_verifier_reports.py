"""The verifier reports a v4 task's grading config asks the engine to read.

A v4 suite prints its reward and nothing else, recording which criteria failed
in files beside it. Only the v4 shape ships them, and only the v4 shape has a
``/logs/verifier`` mount to read them from, so the shape decides whether the
paths are named at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tolokaforge_adapter_terminal_bench.adapter import TerminalBenchAdapter
from tolokaforge_adapter_terminal_bench.compose_synthesis import CONTAINER_VERIFIER_LOGS_DIR

from tolokaforge.core.grading.kinds import TestExecutionKindConfig

pytestmark = pytest.mark.unit

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_LEGACY_DIR = Path(__file__).resolve().parents[3] / "examples" / "terminal_bench"

_EXPECTED_V4_REPORTS = [
    "/logs/verifier/ctrf.json",
    "/logs/verifier/reward-details.json",
]


def _grading_method_config(terminal_bench_dir: Path, task_id: str, staging_root: Path) -> dict:
    adapter = TerminalBenchAdapter(
        {
            "terminal_bench_dir": str(terminal_bench_dir),
            "task_ids": [task_id],
            "staging_root": str(staging_root),
        }
    )
    grading = adapter.to_task_description(task_id).grading
    assert grading is not None
    return dict(grading.grading_method_config or {})


@pytest.mark.parametrize(
    ("terminal_bench_dir", "task_id", "expected"),
    [
        pytest.param(_FIXTURES, "v4_echo", _EXPECTED_V4_REPORTS, id="v4"),
        # A legacy pack writes no CTRF and mounts no /logs/verifier; naming one
        # would make the engine ``cat`` a path that never exists and note it as
        # unavailable on every grade.
        pytest.param(_LEGACY_DIR, "fix-airline-segmentation", None, id="legacy"),
    ],
)
def test_only_the_v4_shape_names_its_verifier_reports(
    terminal_bench_dir: Path, task_id: str, expected: list[str] | None, tmp_path: Path
) -> None:
    config = _grading_method_config(terminal_bench_dir, task_id, tmp_path)

    assert config.get("detail_paths") == expected


def test_the_reports_are_named_where_the_engine_reads_the_reward() -> None:
    """The adapter's log-dir constant and the engine's reward default agree.

    The paths are built from ``CONTAINER_VERIFIER_LOGS_DIR`` in this package
    while the reward is read from ``reward_path``'s default in the engine's.
    Two packages are free to disagree, and if they drifted the engine would read
    reports from an empty directory while still scoring the trial — green tests,
    no audit trail. Nothing else pins them together.
    """
    reward_dir = str(Path(TestExecutionKindConfig().reward_path).parent)

    assert reward_dir == CONTAINER_VERIFIER_LOGS_DIR
    assert [str(Path(p).parent) for p in _EXPECTED_V4_REPORTS] == [reward_dir, reward_dir]
