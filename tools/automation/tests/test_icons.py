"""Message icons addressed by ROLE, one override parameter per icon."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import automation.icons as icons
import pytest
from automation import gateway_catalog

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


#: Every module that can put text into a Slack message. `icons.py` is excluded: it is the
#: registry, so the defaults live there by definition.
SOURCES = Path(icons.__file__).parent
SOURCE_FILES = sorted(p for p in SOURCES.glob("*.py") if p.name != "icons.py")

#: A Slack emoji shortcode. Two exclusions keep it honest: a colon-pair on either side is a
#: GitHub Actions annotation (`::error::`), and a name must contain a LETTER, so a time or a ratio
#: in a message ("12:30:45", "1:2") is not mistaken for an emoji.
_EMOJI_LITERAL_RE = re.compile(r"(?<!:):([a-z0-9_+-]*[a-z][a-z0-9_+-]*):(?!:)")

#: Unicode ranges that RENDER as emoji in Slack, for the literals a shortcode sweep cannot see.
#: Deliberately narrow - the pictographic and dingbat blocks - so the typographic characters these
#: messages legitimately use (the U+25E6 bullet in a reply) are not swept up.
_EMOJI_CHAR_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27bf\ufe0f]")


def _emoji_in_code(path: Path) -> list[str]:
    """Emoji shortcodes in the STRING LITERALS of *path*, docstrings excluded.

    An AST walk rather than a line scan, because docstrings are full of Sphinx cross-reference
    roles (``:func:``, ``:data:``) that look exactly like a shortcode and can never reach Slack.
    What CAN reach Slack is a literal in executable code, including the constant parts of an
    f-string, which is what this collects.
    """
    tree = ast.parse(path.read_text())
    scopes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, scopes) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(
                getattr(first, "value", None), ast.Constant
            ):
                docstrings.add(id(first.value))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings:
            continue
        for match in _EMOJI_LITERAL_RE.finditer(node.value):
            found.append(f"{path.name}:{node.lineno}: {match.group(0)}")
        for match in _EMOJI_CHAR_RE.finditer(node.value):
            found.append(f"{path.name}:{node.lineno}: {match.group(0)!r} (unicode emoji)")
    return found


class TestTheWorkflowsAndTheRegistryAgree:
    """Every role a caller names must exist, and every declared role should be
    reachable - a role nothing uses is either a typo or dead weight."""

    @staticmethod
    def _used() -> set[str]:
        used: set[str] = set()
        for name in ("slack-integrate.yml", "integrate-model.yml"):
            text = (WORKFLOWS / name).read_text()
            used |= set(re.findall(r"--icon ([a-z_]+)", text))
            used |= set(re.findall(r"ICON_ROLE=([a-z_]+)", text))
        # Roles named in Python too: a message built in code (the poller's reply to a request)
        # names its role at the call site, not through a CLI flag.
        for source in SOURCE_FILES:
            used |= set(
                re.findall(
                    r"""icons\.(?:icon|prefix)\(\s*["']([a-z0-9_]+)["']""", source.read_text()
                )
            )
        return used

    def test_no_caller_names_an_unknown_role(self):
        assert self._used() - set(icons.DEFAULT_ICONS) == set()

    def test_every_declared_role_is_used(self):
        assert set(icons.DEFAULT_ICONS) - self._used() == set()

    def test_no_workflow_message_still_hardcodes_an_emoji(self):
        """The emoji left the text when the role arrived; a leftover would be an
        icon no override can reach."""
        for name in ("slack-integrate.yml", "integrate-model.yml"):
            text = (WORKFLOWS / name).read_text()
            assert not re.search(r"""--text ['"]:[a-z_]+:""", text), name
            assert not re.search(r"""(MSG|SLACK)=['"]?:[a-z_]+:""", text), name

    def test_no_python_built_message_still_hardcodes_an_emoji(self):
        """The same guard over the SOURCES, not just the workflows.

        A message built in Python is as Slack-visible as one built in YAML, so an emoji literal
        in a source file is an icon no override can reach.

        A tripwire, not a proof: it sees literals (including the constant parts of an f-string)
        and unicode emoji characters, so an emoji assembled at runtime - `":" + name + ":"`,
        `f":{name}:"`, `.format()` - still gets through. Those are worth catching by review;
        this catches the shape that actually shipped.
        """
        offenders = [item for source in SOURCE_FILES for item in _emoji_in_code(source)]
        assert offenders == [], (
            "hardcoded Slack emoji found; give each one a role in icons.DEFAULT_ICONS and emit "
            f"it with icons.icon(role, overrides): {offenders}"
        )


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


class TestAnUnknownRoleDoesNotCostTheMessage:
    """A role typo is a bug in this codebase; losing the notification is a worse one.

    `icon()` raises and the CLI wrappers catch everything then exit 0, so an escaping raise
    costs the whole message on a green step - and in `reply`, where the thread root is posted
    first, leaves a root with nothing under it.

    Driven through the COMMANDS rather than through the helper: a test that calls `_prefixed`
    directly still passes with both call sites reverted to `icons.prefix`, which is exactly the
    regression it is supposed to catch.
    """

    @staticmethod
    def _capture(monkeypatch) -> tuple[dict, list[str]]:
        import automation.slack as slack

        sent: dict = {}
        logged: list[str] = []
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setattr(slack, "_ready", lambda channel: "xoxb-test")
        monkeypatch.setattr(slack, "_find_or_create_root", lambda *a, **k: "1.0")
        monkeypatch.setattr(slack, "_log", logged.append)
        monkeypatch.setattr(
            slack,
            "_post_message",
            lambda ch, text, tok, thread_ts=None: sent.setdefault("t", text) or True,
        )
        return sent, logged

    def test_reply_still_posts_under_its_root(self, monkeypatch):
        import automation.slack as slack

        sent, logged = self._capture(monkeypatch)
        slack.cmd_reply("C1", 42, "Integrated: preset committed.", "m", False, role="not_a_role")
        assert sent.get("t", "").startswith("Integrated: preset committed.")
        assert any("not_a_role" in line for line in logged)

    def test_post_thread_still_posts(self, monkeypatch):
        import automation.slack as slack
        from typer.testing import CliRunner

        sent, logged = self._capture(monkeypatch)
        result = CliRunner().invoke(
            slack.app,
            [
                "post-thread",
                "--channel",
                "C1",
                "--thread-ts",
                "1.0",
                "--text",
                "Dispatch failed",
                "--icon",
                "not_a_role",
            ],
        )
        assert result.exit_code == 0
        assert sent.get("t", "").startswith("Dispatch failed")
        assert any("not_a_role" in line for line in logged)

    def test_a_good_role_is_still_applied(self, monkeypatch):
        import automation.slack as slack

        sent, _ = self._capture(monkeypatch)
        slack.cmd_reply("C1", 42, "Observe started", "m", False, role="observe_started")
        assert sent["t"].startswith(":arrow_forward: Observe started")


class TestAFullOverrideRestylesEveryMessage:
    """The requirement in one assertion: with the full role map set, NO stock emoji survives.

    Built over the poller's request reply because that is where the coverage gap was - it is the
    first message the flow ever sends, it carries all three intake outcomes plus a route warning,
    and every one of those icons used to be a literal.
    """

    CATALOG = ["x-ai/grok-4.5", "openai/gpt-5.6-sol", "openai/gpt-5.6-terra"]

    @staticmethod
    def _full_override() -> str:
        return json.dumps(
            {role: f":arena-{role.replace('_', '-')}:" for role in icons.DEFAULT_ICONS}
        )

    def _reply_with_every_outcome(self) -> str:
        import automation.model_resolver as mr
        import automation.poller as poller

        # resolved + ambiguous + unknown in one message, so all three intake icons appear.
        resolutions = mr.resolve_all(
            "<@U1> integrate x-ai/grok-4.5, gpt 5.6 and nope/nothing-9", self.CATALOG
        )
        availability = {
            r.slug: gateway_catalog.lookup(r.slug, [])  # catalog read, nothing in it
            for r in resolutions
            if r.status == "resolved" and r.slug
        }
        plan = poller.route_plan(resolutions, availability, gateway_catalog.ROUTE_GATEWAY)
        reply = mr.format_resolution_reply(
            "U1", resolutions, availability, plan.requested_route, gateway_searched=True
        )
        assert plan.warnings, "expected a route warning to be part of the message under test"
        return "\n\n".join([reply, *plan.warnings])

    def test_stock_glyphs_are_present_without_an_override(self, monkeypatch):
        monkeypatch.delenv(icons.ICON_OVERRIDES_ENV, raising=False)
        found = set(_EMOJI_LITERAL_RE.findall(self._reply_with_every_outcome()))
        # The unset case must be unchanged: today's glyphs, in all three intake states.
        assert {"white_check_mark", "warning", "x"} <= found

    def test_no_stock_glyph_survives_the_full_override(self, monkeypatch):
        monkeypatch.setenv(icons.ICON_OVERRIDES_ENV, self._full_override())
        text = self._reply_with_every_outcome()
        found = sorted(set(_EMOJI_LITERAL_RE.findall(text)))
        assert found, "the message under test must contain icons"
        assert [name for name in found if not name.startswith("arena-")] == [], text
