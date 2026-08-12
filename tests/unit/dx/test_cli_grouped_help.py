"""Unit tests locking ``tolokaforge --version`` and the grouped ``--help`` layout.

Root ``--help`` renders top-level commands under six fixed-order headings —
``Runs``, ``Tasks``, ``Docker``, ``Config``, ``Assets``, ``Adapters`` —
alphabetical within each section. A registered top-level command missing from
``_GroupedCommandsGroup.COMMAND_GROUPS`` fails loudly at help time. Per-command
``--help`` (``run``, ``docker``, ``docker up``, …) still renders Click's
default flat listing — grouped formatting is confined to the root group.
``tolokaforge --version`` prints ``tolokaforge, version <installed-version>``
on ``sys.stdout`` (machine-friendly artifact, same discipline as the
``stdout is artifact`` rule).
"""

from __future__ import annotations

import importlib.metadata

import click
import pytest
from click.testing import CliRunner

from tolokaforge.dx.cli.main import _GroupedCommandsGroup, cli

pytestmark = pytest.mark.unit


_HEADINGS = ("Runs:", "Tasks:", "Docker:", "Config:", "Assets:", "Adapters:")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


def _section_body(help_output: str, heading: str) -> str:
    """Return the substring between ``heading`` and the next heading (or end)."""
    start = help_output.index(heading) + len(heading)
    later_offsets = [
        help_output.index(other) for other in _HEADINGS if other != heading and other in help_output
    ]
    later_offsets = [offset for offset in later_offsets if offset > start]
    end = min(later_offsets) if later_offsets else len(help_output)
    return help_output[start:end]


def _command_names_in(section_body: str) -> list[str]:
    """Extract command names (first token on each non-blank line) from a section body."""
    names: list[str] = []
    for line in section_body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        names.append(stripped.split()[0])
    return names


class TestVersionOption:
    def test_version_option_matches_importlib_metadata(self, runner: CliRunner) -> None:
        installed_version = importlib.metadata.version("tolokaforge")

        result = runner.invoke(cli, ["--version"], prog_name="tolokaforge")

        assert result.exit_code == 0, result.stderr
        assert result.stdout.strip() == f"tolokaforge, version {installed_version}"

    def test_version_writes_to_stdout(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--version"], prog_name="tolokaforge")

        assert result.exit_code == 0, result.stderr
        assert "tolokaforge, version" in result.stdout
        assert "tolokaforge, version" not in result.stderr


class TestGroupedHelpLayout:
    def test_help_output_contains_group_headings(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0, result.stderr

        offsets = [result.stdout.index(heading) for heading in _HEADINGS]
        ordered_pairs = list(zip(_HEADINGS, offsets, strict=True))
        assert offsets == sorted(offsets), f"Section headings out of order: {ordered_pairs}"

    def test_help_output_places_commands_under_correct_headings(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0, result.stderr

        expected: dict[str, set[str]] = {
            "Runs:": {"run", "prepare", "worker", "status", "analyze"},
            "Tasks:": {"validate"},
            "Docker:": {"docker"},
            "Config:": {"config"},
            "Assets:": {"assets"},
            "Adapters:": {"adapter"},
        }
        for heading, commands in expected.items():
            body = _section_body(result.stdout, heading)
            names_in_section = set(_command_names_in(body))
            missing = commands - names_in_section
            assert not missing, f"Missing commands in {heading!r}: {missing}"

        # And every command lives in exactly one section — the union of all
        # other sections must be disjoint from any given section's commands.
        for heading, commands in expected.items():
            for other_heading in expected:
                if other_heading == heading:
                    continue
                other_body = _section_body(result.stdout, other_heading)
                other_names = set(_command_names_in(other_body))
                leaked = commands & other_names
                assert not leaked, f"{leaked} leaked from {heading!r} into {other_heading!r}"

    def test_commands_within_section_are_alphabetical(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--help"])

        assert result.exit_code == 0, result.stderr

        runs_body = _section_body(result.stdout, "Runs:")
        assert _command_names_in(runs_body) == [
            "analyze",
            "browse",
            "curate",
            "prepare",
            "reconcile",
            "rejudge",
            "retrace",
            "run",
            "status",
            "worker",
        ]


class TestGroupedCommandsGroupContract:
    def test_all_registered_commands_are_mapped(self) -> None:
        registered = set(cli.commands.keys())
        mapped = set(_GroupedCommandsGroup.COMMAND_GROUPS.keys())
        unmapped = registered - mapped
        assert not unmapped, f"Unmapped top-level commands: {sorted(unmapped)}"

    def test_registered_but_unmapped_command_raises_runtime_error(self, runner: CliRunner) -> None:
        probe = _GroupedCommandsGroup(name="probe")
        probe.add_command(click.Command("nope", help="probe command"))

        result = runner.invoke(probe, ["--help"], standalone_mode=False)

        assert isinstance(result.exception, RuntimeError)
        assert "no group heading for command 'nope'" in str(result.exception)


class TestSubcommandHelpUnchanged:
    """Grouped formatting is confined to the root group. Every per-command
    ``--help`` renders Click's default flat listing with no section headings.
    """

    @pytest.mark.parametrize(
        "argv",
        [
            ["run", "--help"],
            ["docker", "--help"],
            ["docker", "status", "--help"],
            ["config", "--help"],
            ["adapter", "convert", "--help"],
            ["assets", "stamp", "--help"],
        ],
    )
    def test_subcommand_help_has_no_group_headings(
        self, runner: CliRunner, argv: list[str]
    ) -> None:
        result = runner.invoke(cli, argv)

        assert result.exit_code == 0, result.stderr
        assert result.stdout.startswith("Usage: ")
        argv_str = " ".join(argv)
        headings_present = [h for h in _HEADINGS if h in result.stdout]
        assert not headings_present, f"Headings leaked into {argv_str!r}: {headings_present}"
