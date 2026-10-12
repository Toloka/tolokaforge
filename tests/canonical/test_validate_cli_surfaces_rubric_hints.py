"""Canonical lock: ``tolokaforge validate`` surfaces rubric-anchor hints in stdout.

Runs the real ``tolokaforge validate`` CLI in-process (``click.testing.CliRunner``)
against a real pack shipped under ``examples/native/`` and asserts:
    - exit code is 0 (a hint never marks the task invalid);
    - each ``kind: graded`` criterion without ``expected:`` is printed once,
      with the reserved ``⚠`` prefix that distinguishes hints from unchecked
      skips (which print with ``?``);
    - the summary still reads ``1 valid``.

Pinned at the canonical tier because the CLI's stdout is a shipped contract:
downstream task-pack repositories parse the summary line, and pack owners
scan yellow-prefixed lines to spot issues before spending run budget. A
regression that dropped either the hint line or the summary would surface
here rather than silently.

Uses ``multi_service_helpdesk_workflow/dataset/tasks/helpdesk_01`` because
its shipped ``grading.yaml`` carries exactly two unanchored graded criteria
(``policy_reasoning`` and ``tone_completeness``) — the intended nudge shape
in the wild, verified at pin time by inspection of the pack's YAML.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from tolokaforge.dx.cli.main import cli

pytestmark = pytest.mark.canonical


_HELPDESK_TASK = (
    Path(__file__).resolve().parents[2]
    / "examples/native/multi_service_helpdesk_workflow/dataset/tasks/helpdesk_01/task.yaml"
)


def test_validate_prints_one_hint_line_per_unanchored_graded_criterion() -> None:
    """Both unanchored graded criteria in the shipped pack appear as hint lines.

    The pack ships three criteria: ``names_customer_delivery`` (binary, silent),
    ``policy_reasoning`` (graded, no ``expected:``, hinted), and
    ``tone_completeness`` (graded, no ``expected:``, hinted). Two hints, one
    ``✓`` line, exit code 0, ``1 valid`` in the summary.
    """
    runner = CliRunner(mix_stderr=False)

    result = runner.invoke(cli, ["validate", "--tasks", str(_HELPDESK_TASK)])

    # The CLI's ``rich.Console`` writes to stderr (see ``tolokaforge/dx/_display.py``
    # — the shared ``console`` is constructed with ``stderr=True``), so the
    # validate summary and hint lines land in ``result.stderr``, not stdout.
    stderr = result.stderr
    assert result.exit_code == 0, stderr
    assert "✓" in stderr
    assert stderr.count("⚠") == 2, stderr
    assert "llm_judge.rubric.criteria.policy_reasoning" in stderr
    assert "llm_judge.rubric.criteria.tone_completeness" in stderr
    assert "kind: graded with no 'expected:' anchor" in stderr
    assert "1 valid" in stderr
