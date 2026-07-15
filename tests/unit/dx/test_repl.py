"""Unit tests for the interactive tolokaforge shell.

Locks the wiring — no subcommand or explicit ``repl`` verb enters
:func:`tolokaforge.dx.repl.enter_repl`, the ``repl`` command is
registered under the ``Interactive`` heading, and ``--help`` renders
it there. ``click_repl.repl`` is patched to a stub so tests never
touch stdin or ``prompt_toolkit`` — that path is exercised end-to-end
via the manual smoke command documented in ``docs/CLI.md``.
"""

from __future__ import annotations

import inspect
import typing
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from tolokaforge.dx import repl as repl_module
from tolokaforge.dx.cli.main import _GroupedCommandsGroup, cli

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner(mix_stderr=False)


def test_enter_repl_signature_stable() -> None:
    signature = inspect.signature(repl_module.enter_repl)
    parameters = list(signature.parameters.values())
    assert len(parameters) == 1
    (ctx_param,) = parameters
    assert ctx_param.name == "ctx"
    hints = typing.get_type_hints(repl_module.enter_repl)
    assert hints["ctx"] is click.Context
    assert hints["return"] is type(None)


def test_bare_tolokaforge_enters_repl(runner: CliRunner) -> None:
    with patch("tolokaforge.dx.repl._click_repl") as click_repl_stub:
        result = runner.invoke(cli, [])

    assert result.exit_code == 0, result.stderr
    click_repl_stub.assert_called_once()
    (ctx_arg,), _ = click_repl_stub.call_args
    assert isinstance(ctx_arg, click.Context)


def test_repl_subcommand_dispatches(runner: CliRunner) -> None:
    with patch("tolokaforge.dx.repl._click_repl") as click_repl_stub:
        result = runner.invoke(cli, ["repl"])

    assert result.exit_code == 0, result.stderr
    click_repl_stub.assert_called_once()


def test_repl_registered_in_command_groups() -> None:
    assert _GroupedCommandsGroup.COMMAND_GROUPS["repl"] == "Interactive"
    assert "Interactive" in _GroupedCommandsGroup.GROUP_ORDER


def test_repl_renders_under_interactive_heading(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["--help"])

    assert result.exit_code == 0, result.stderr
    assert "Interactive:" in result.stdout
    interactive_offset = result.stdout.index("Interactive:")
    runs_offset = result.stdout.index("Runs:")
    assert interactive_offset < runs_offset
    section_body = result.stdout[interactive_offset : interactive_offset + 200]
    assert "repl" in section_body
