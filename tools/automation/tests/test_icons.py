"""Message icons addressed by ROLE, one override parameter per icon."""

from __future__ import annotations

import json
import re
from pathlib import Path

import automation.icons as icons
import pytest

pytestmark = pytest.mark.unit

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github/workflows"


class TestTheRegistry:
    def test_every_role_defaults_to_a_shortcode(self):
        for role, emoji in icons.DEFAULT_ICONS.items():
            assert emoji.startswith(":") and emoji.endswith(":"), role
            assert " " not in emoji, role

    @pytest.mark.parametrize(
        ("first", "second"),
        [("integrated", "integrated_merged"), ("observe_clean", "integrated")],
    )
    def test_roles_sharing_a_default_are_separately_overridable(self, first: str, second: str):
        """The reason for role keys. These render the same glyph today, so a map
        keyed on the standard NAME could not tell them apart - one entry would
        have restyled both."""
        assert icons.DEFAULT_ICONS[first] == icons.DEFAULT_ICONS[second]
        overrides = icons.load_icon_overrides(json.dumps({second: ":tf-one:"}))
        assert icons.icon(second, overrides) == ":tf-one:"
        assert icons.icon(first, overrides) == icons.DEFAULT_ICONS[first]

    def test_the_defaults_reproduce_what_the_flow_sends_today(self):
        """An unset variable must change nothing, so these are pinned."""
        assert icons.DEFAULT_ICONS["observe_started"] == ":arrow_forward:"
        assert icons.DEFAULT_ICONS["resolve_started"] == ":wrench:"
        assert icons.DEFAULT_ICONS["needs_human"] == ":warning:"
        assert icons.DEFAULT_ICONS["needs_human_agent"] == ":raising_hand:"
        assert icons.DEFAULT_ICONS["pipeline_error"] == ":rotating_light:"


class TestLoading:
    def test_a_role_map_loads_with_colons_normalised(self):
        assert icons.load_icon_overrides('{"observe_started": "tf-observe"}') == {
            "observe_started": ":tf-observe:"
        }

    def test_an_unknown_role_is_reported_loudly(self, capsys):
        assert icons.load_icon_overrides('{"observe_startd": ":x:"}') == {}
        err = capsys.readouterr().err
        assert "observe_startd" in err
        assert "observe_started" in err  # the known roles are listed

    def test_one_bad_entry_does_not_discard_the_good_ones(self):
        assert icons.load_icon_overrides(
            '{"integrated": ":a:", "nope": ":b:", "needs_human": "has space"}'
        ) == {"integrated": ":a:"}

    @pytest.mark.parametrize("raw", ["", "  ", "{bad", "[1]", '"s"', "null", "3"])
    def test_an_unusable_payload_degrades_to_no_overrides(self, raw: str):
        assert icons.load_icon_overrides(raw) == {}

    def test_the_env_var_is_the_default_source(self, monkeypatch):
        monkeypatch.setenv(icons.ICON_OVERRIDES_ENV, '{"needs_human": ":tf-help:"}')
        assert icons.icon("needs_human") == ":tf-help:"

    def test_an_absent_env_var_leaves_the_defaults(self, monkeypatch):
        monkeypatch.delenv(icons.ICON_OVERRIDES_ENV, raising=False)
        assert icons.icon("needs_human") == ":warning:"


class TestResolution:
    def test_an_unknown_role_raises_because_roles_come_from_this_codebase(self):
        with pytest.raises(ValueError, match="Unknown icon role"):
            icons.icon("not_a_role")

    def test_prefix_prepends_the_icon(self):
        assert icons.prefix("observe_started", "Observe started", {}) == (
            ":arrow_forward: Observe started"
        )

    def test_an_empty_role_is_the_no_icon_case_not_an_error(self):
        assert icons.prefix("", "already led") == "already led"


class TestTheWorkflowsAndTheRegistryAgree:
    """Every role the workflows name must exist, and every declared role should be
    reachable - a role nothing uses is either a typo or dead weight."""

    @staticmethod
    def _used() -> set[str]:
        used: set[str] = set()
        for name in ("slack-integrate.yml", "integrate-model.yml"):
            text = (WORKFLOWS / name).read_text()
            used |= set(re.findall(r"--icon ([a-z_]+)", text))
            used |= set(re.findall(r"ICON_ROLE=([a-z_]+)", text))
        return used

    def test_no_workflow_names_an_unknown_role(self):
        assert self._used() - set(icons.DEFAULT_ICONS) == set()

    def test_every_declared_role_is_used(self):
        assert set(icons.DEFAULT_ICONS) - self._used() == set()

    def test_no_message_still_hardcodes_an_emoji(self):
        """The emoji left the text when the role arrived; a leftover would be an
        icon no override can reach."""
        for name in ("slack-integrate.yml", "integrate-model.yml"):
            text = (WORKFLOWS / name).read_text()
            assert not re.search(r"""--text ['"]:[a-z_]+:""", text), name
            assert not re.search(r"""(MSG|SLACK)=['"]?:[a-z_]+:""", text), name


class TestTheReplyPathAppliesTheRole:
    def test_the_role_reaches_the_posted_body(self, monkeypatch):
        import automation.slack as slack

        monkeypatch.setenv(icons.ICON_OVERRIDES_ENV, '{"integrated": ":tf-done:"}')
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        sent: dict = {}
        monkeypatch.setattr(slack, "_ready", lambda channel: "xoxb-test")
        monkeypatch.setattr(slack, "_find_or_create_root", lambda *a, **k: "1.0")
        monkeypatch.setattr(
            slack, "_post_message", lambda ch, text, tok, thread_ts=None: sent.setdefault("t", text)
        )
        slack.cmd_reply("C1", 42, "Integrated: preset committed.", "m", False, role="integrated")
        assert sent["t"].startswith(":tf-done: Integrated:")


class TestEveryIconCallSiteReachesACommandThatAcceptsIt:
    """A role string that matches the registry proves nothing about the command
    receiving it.

    `post-thread` was wired with `--icon pr_opened` and `--icon dispatch_failed`
    while only `reply` declared the option, so Click rejected the flag with a
    usage error and the call site's `|| true` swallowed it: two notifications
    silently stopped posting, one of them the failure report. The registry-
    agreement test passed the whole time, because it compares strings.
    """

    @staticmethod
    def _call_sites() -> set[tuple[str, str]]:
        """(subcommand, role) for every `--icon` in the workflows.

        Backslash-continued commands are reassembled, since the subcommand and
        the flag routinely sit on different lines.
        """
        found: set[tuple[str, str]] = set()
        for name in ("slack-integrate.yml", "integrate-model.yml"):
            lines = (WORKFLOWS / name).read_text().splitlines()
            for index, line in enumerate(lines):
                if "automation slack " not in line:
                    continue
                subcommand = line.split("automation slack ")[1].split()[0]
                cursor, block = index, [line]
                while lines[cursor].rstrip().endswith("\\") and cursor + 1 < len(lines):
                    cursor += 1
                    block.append(lines[cursor])
                for role in re.findall(r"--icon ([a-z_]+)", "\n".join(block)):
                    found.add((subcommand, role))
        return found

    def test_the_workflows_do_use_the_flag(self):
        """Guards the guard: an empty set would make the next test vacuous."""
        assert self._call_sites()

    def test_each_receiving_command_declares_icon(self):
        import automation.slack as slack
        import typer.main

        commands = typer.main.get_command(slack.app).commands
        offenders = []
        for subcommand, role in sorted(self._call_sites()):
            command = commands.get(subcommand)
            if command is None:
                offenders.append(f"{subcommand} (no such subcommand) for role {role}")
                continue
            if not any("--icon" in (param.opts or []) for param in command.params):
                offenders.append(f"{subcommand} does not declare --icon (role {role})")
        assert not offenders, offenders

    def test_post_thread_actually_applies_the_role(self, monkeypatch):
        """The end-to-end half: the flag is accepted AND the icon reaches the body."""
        import automation.slack as slack
        from typer.testing import CliRunner

        monkeypatch.setenv(icons.ICON_OVERRIDES_ENV, '{"pr_opened": ":tf-pr:"}')
        monkeypatch.setattr(slack, "_ready", lambda channel: "xoxb-test")
        sent: dict = {}
        monkeypatch.setattr(
            slack,
            "_post_message",
            lambda ch, text, tok, thread_ts=None: sent.setdefault("text", text),
        )
        result = CliRunner().invoke(
            slack.app,
            [
                "post-thread",
                "--channel",
                "C1",
                "--thread-ts",
                "1.0",
                "--text",
                "Opened PR #42",
                "--icon",
                "pr_opened",
            ],
        )
        assert result.exit_code == 0
        assert sent["text"] == ":tf-pr: Opened PR #42"
