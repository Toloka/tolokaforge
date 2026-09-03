"""``tolokaforge --help`` surfaces the ``grade`` verb under Runs.

Structural lock on the CLI's public help surface:

* the "Runs" section carries a row whose first token is ``grade``,
* ``grade`` short-help mentions ``regrade`` and ``bundle`` (readers
  scan short-help to pick the right verb),
* ``_GroupedCommandsGroup.COMMAND_GROUPS["grade"] == "Runs"`` (a
  rename without the map update raises ``RuntimeError`` at help time,
  but this assertion pins the mapping directly).
* ``"grade-run"`` is NOT yet in ``COMMAND_GROUPS`` — Stage 2 registers
  only the single-trial verb; if Stage 3 is rolled back the surface is
  left half-registered otherwise.
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
    assert "grade" in _command_names(section)


def test_grade_short_help_mentions_regrade_and_bundle() -> None:
    result = CliRunner(mix_stderr=False).invoke(cli, ["--help"])
    assert result.exit_code == 0, result.stderr

    section = _runs_section(result.stdout)
    grade_line = next(line for line in section.splitlines() if line.strip().startswith("grade "))
    assert "Regrade" in grade_line or "regrade" in grade_line
    assert "bundle" in grade_line


def test_grade_in_command_groups_under_runs() -> None:
    assert _GroupedCommandsGroup.COMMAND_GROUPS["grade"] == "Runs"


def test_grade_run_not_yet_registered() -> None:
    """Stage 3 implements + registers ``grade-run`` in one commit.

    Until then, the half-registration would leave ``tolokaforge --help``
    with a broken row if ``grade-run`` were mapped but not implemented.
    Locking absence here keeps the Stage 2 surface honest.
    """
    assert "grade-run" not in _GroupedCommandsGroup.COMMAND_GROUPS
