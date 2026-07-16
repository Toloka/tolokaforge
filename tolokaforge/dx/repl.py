"""Interactive tolokaforge shell — reference implementation.

Instantiated by the CLI when the user runs ``tolokaforge`` with no
subcommand (or ``tolokaforge repl`` explicitly). Session-scopes root
flags (``-v``, ``-q``, ``--display``, ``--log-format``) so they apply to
every command entered until the REPL exits.
"""

from __future__ import annotations

from pathlib import Path

import click
from click_repl import repl as _click_repl
from prompt_toolkit.history import FileHistory

from tolokaforge.dx._display import console


def enter_repl(ctx: click.Context) -> None:
    """Enter the interactive tolokaforge shell.

    Type ``help`` for a grouped command list, ``exit`` (or Ctrl-D) to
    quit. Root flags supplied at REPL entry (``-v``, ``-q``,
    ``--display``, ``--log-format``) apply to every command inside the
    session until exit — they mutate global logging + console state
    once via the ``cli()`` group callback and stay in effect.
    """
    history_path = Path.home() / ".tolokaforge_history"
    console.print(
        "[info]tolokaforge[/info] interactive shell. "
        "Type `:help` for commands, `:exit` (or Ctrl-D) to quit. "
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
