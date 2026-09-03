"""``tolokaforge --help`` surfaces the ``grade`` + ``grade-run`` verbs under Runs.

Structural lock on the CLI's public help surface:

* the "Runs" section carries rows whose first tokens are ``grade`` and
  ``grade-run``,
* ``grade`` short-help mentions ``regrade`` and ``bundle``; ``grade-run``
  short-help mentions ``regrade`` and ``run`` (readers scan short-help
  to pick the right verb),
* ``_GroupedCommandsGroup.COMMAND_GROUPS`` maps both to ``"Runs"``
  (a rename without the map update raises ``RuntimeError`` at help time;
  these assertions pin the mapping directly).
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from tolokaforge.dx.cli.main import _GroupedCommandsGroup, cli

pytestmark = pytest.mark.canonical


def _runs_section(stdout: str) -> str:
    start = stdout.index("Runs:") + len("Runs:")
    later_headings = ("Tasks:", "Docker:", "Config:", "Assets:", "Adapters:")
    offsets = [stdout.index(h) for h in later_headings if h in stdout and stdout.index(h) > start]
    end = min(offsets) if offsets else len(stdout)
    return stdout[start:end]


def _command_names(section: str) -> list[str]:
    names: list[str] = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        names.append(stripped.split()[0])
    return names


def test_grade_verb_listed_under_runs_heading() -> None:
    result = CliRunner(mix_stderr=False).invoke(cli, ["--help"])
    assert result.exit_code == 0, result.stderr

    section = _runs_section(result.stdout)
    names = _command_names(section)
    assert "grade" in names
    assert "grade-run" in names


def test_grade_short_help_mentions_regrade_and_bundle() -> None:
    result = CliRunner(mix_stderr=False).invoke(cli, ["--help"])
    assert result.exit_code == 0, result.stderr

    section = _runs_section(result.stdout)
    grade_line = next(line for line in section.splitlines() if line.strip().startswith("grade "))
    assert "Regrade" in grade_line or "regrade" in grade_line
    assert "bundle" in grade_line


def test_grade_run_short_help_mentions_regrade_and_run() -> None:
    result = CliRunner(mix_stderr=False).invoke(cli, ["--help"])
    assert result.exit_code == 0, result.stderr

    section = _runs_section(result.stdout)
    grade_run_line = next(
        line for line in section.splitlines() if line.strip().startswith("grade-run ")
    )
    assert "Regrade" in grade_run_line or "regrade" in grade_run_line
    assert "run" in grade_run_line


def test_grade_and_grade_run_in_command_groups_under_runs() -> None:
    assert _GroupedCommandsGroup.COMMAND_GROUPS["grade"] == "Runs"
    assert _GroupedCommandsGroup.COMMAND_GROUPS["grade-run"] == "Runs"
