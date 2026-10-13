"""``tolokaforge trial-node`` — serve whole trials over gRPC.

A thin launcher for :mod:`tolokaforge.trial_node.__main__`: the options, the
environment variables and their defaults are defined once there, and ``--help``
prints that parser's reference. The service imports load only when the command
runs, so the rest of the CLI does not pay for them.
"""

from __future__ import annotations

import click


@click.command(
    name="trial-node",
    context_settings={"ignore_unknown_options": True, "help_option_names": []},
    add_help_option=False,
)
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def trial_node(args: tuple[str, ...]) -> None:
    """Run a trial node: a gRPC service that runs one whole trial per call.

    The engine's `--runtime remote` ships each trial to a node started with this
    command. Pass --help after the command name for the node's options.
    """
    from tolokaforge.trial_node.__main__ import main

    raise SystemExit(main(list(args)))
