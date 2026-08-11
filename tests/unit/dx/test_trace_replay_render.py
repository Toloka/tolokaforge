"""``tolokaforge retrace``'s console — what it says about a bundle with no task snapshot.

Driven through the real command, because the three places a disposition has to
appear are three different functions and a count that reaches one of them alone
is a batch whose size does not add up: the per-bundle line, the totals beside the
other dispositions, and the run-level ``Evidence:`` block the report carries.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from tests.utils.provision_failure import write_provision_failure_bundle
from tolokaforge.core.grading.replay_layout import TRACE_REPLAY_DIRNAME
from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.unit


def test_a_task_less_abort_is_named_on_its_line_in_the_totals_and_in_the_evidence(
    tmp_path: Path,
) -> None:
    """The disposition reaches all three, and the batch still exits ``0``.

    A skip is not a failure, so a run whose only non-eligible bundle is one the
    substrate killed must not turn a CI step red. The evidence count is its own
    number rather than a widened ``skipped``: what an aborted trial could not say
    about a pack and what a pack chose not to declare are two facts.
    """
    source = tmp_path / "run"
    bundle = write_provision_failure_bundle(source)

    result = CliRunner().invoke(
        cli,
        ["retrace", "--source", str(source), "--trial", str(bundle), "--replay-id", "r1"],
        env={"COLUMNS": "200"},
    )

    assert result.exit_code == 0, result.output
    printed = " ".join(result.output.split())
    assert "skip (no task)" in printed
    assert (
        "0 eligible, 0 skipped-not-applicable, 1 skipped-no-task, 0 failed-with-reason" in printed
    )
    assert "0 skipped, 1 with no task snapshot, 0 failed" in printed
    assert (source / TRACE_REPLAY_DIRNAME / "r1" / "trace_replay_report.yaml").is_file()
