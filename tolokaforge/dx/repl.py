"""Interactive tolokaforge shell — reference implementation.

Instantiated by the CLI when the user runs ``tolokaforge`` with no
subcommand (or ``tolokaforge repl`` explicitly). Session-scopes root
flags (``-v``, ``-q``, ``--display``, ``--log-format``) so they apply to
every command entered until the REPL exits.

click-repl hardcodes ``:`` as the internal-command prefix (``:help``,
``:exit`` etc.). Import-time patch below swaps that for ``/`` — matches
Slack / Discord conventions users are already familiar with and reads
better in copy-pasted shell logs where ``:`` collides with paths and
scope operators.
"""

from __future__ import annotations

import re
from pathlib import Path

import click
import click_repl.utils as _click_repl_utils
from click_repl import repl as _click_repl
from prompt_toolkit.history import FileHistory

from tolokaforge.dx._display import console

_INTERNAL_COMMAND_PREFIX = "/"


def _handle_internal_commands(command: str) -> object:
    """Slash-prefixed replacement for ``click_repl.utils.handle_internal_commands``.

    Recognises ``/help``, ``/exit`` etc. instead of the upstream ``:help``
    / ``:exit``. Returns the target's result (or the ExitReplException
    the exit-target raises) when the command matches; returns ``None``
    otherwise so the dispatcher falls through to normal Click execution.
    """
    if command.startswith(_INTERNAL_COMMAND_PREFIX):
        target = _click_repl_utils._get_registered_target(
            command[len(_INTERNAL_COMMAND_PREFIX) :], default=None
        )
        if target:
            return target()
    return None


def _patch_click_repl_prefix() -> None:
    """Rewrite click-repl's help text and dispatcher to use ``/`` in
    place of the upstream-hardcoded ``:``.

    Idempotent — safe to call more than once (subsequent calls no-op
    on the already-patched dispatcher). Applied at module import so a
    ``repl`` invocation from anywhere in the process sees the swap.
    """
    _click_repl_utils.handle_internal_commands = _handle_internal_commands
    # ``_help_internal`` bakes ``:`` into the rendered help string. Patch
    # the rendered text in-place with a wrapper that swaps the prefix
    # column and the hint sentence.
    original_help = _click_repl_utils._help_internal

    def _slash_help() -> str:
        text = original_help()
        text = text.replace(
            'prefix internal commands with ":"', 'prefix internal commands with "/"'
        )
        # Rewrite every ``:mnemonic`` token — including those after commas
        # in the "``/exit, :q, :quit``" line — to their slash form. The
        # negative-lookbehind stops URL colons (``file://…``) from being
        # rewritten if they ever land in the help text.
        text = re.sub(r"(?<![a-zA-Z0-9]):([a-zA-Z?])", r"/\1", text)
        return text

    _click_repl_utils._help_internal = _slash_help
    # ``_internal_commands`` maps mnemonic → (target, description). Re-register
    # ``help`` to the wrapper so ``/help`` returns the slash-prefixed listing.
    _click_repl_utils._internal_commands["help"] = (
        _slash_help,
        _click_repl_utils._internal_commands["help"][1],
    )
    for alias in ("h", "?"):
        if alias in _click_repl_utils._internal_commands:
            _click_repl_utils._internal_commands[alias] = (
                _slash_help,
                _click_repl_utils._internal_commands[alias][1],
            )


_patch_click_repl_prefix()


def enter_repl(ctx: click.Context) -> None:
    """Enter the interactive tolokaforge shell.

    Type ``/help`` for a grouped command list, ``/exit`` (or Ctrl-D) to
    quit. Root flags supplied at REPL entry (``-v``, ``-q``,
    ``--display``, ``--log-format``) apply to every command inside the
    session until exit — they mutate global logging + console state
    once via the ``cli()`` group callback and stay in effect.
    """
    history_path = Path.home() / ".tolokaforge_history"
    console.print(
        "[info]tolokaforge[/info] interactive shell. "
        "Type `/help` for commands, `/exit` (or Ctrl-D) to quit. "
        "Subcommands work as usual: `run --config …`, `status`, etc."
    )
    _click_repl(
        ctx,
        prompt_kwargs={
            "message": "tolokaforge> ",
            "history": FileHistory(str(history_path)),
            "complete_while_typing": True,
        },
    )


__all__ = ["enter_repl"]
