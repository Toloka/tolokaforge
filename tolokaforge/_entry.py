"""Console-script shim for the ``tolokaforge`` command.

Kept stdlib-only so the ``tolokaforge`` binary resolves on any install of
``tolokaforge`` — including headless ``pip install tolokaforge`` without
the ``[dx]`` extras. When the extras are absent, the shim prints an
install hint and exits ``1``.
"""

from __future__ import annotations

import sys


def main() -> int:
    """Delegate to :func:`tolokaforge.dx.cli.main.cli` when available.

    Wrapping the import in ``try / except ImportError`` is what lets the
    ``tolokaforge`` command remain installable on a headless-server
    profile that never wants Rich in its dependency graph.
    """
    try:
        from tolokaforge.dx.cli.main import cli
    except ImportError as exc:
        sys.stderr.write(
            "The tolokaforge CLI needs the `dx` extras.\n"
            "Install with:  pip install 'tolokaforge[dx]'\n"
            f"(underlying error: {exc})\n"
        )
        return 1
    cli()
    return 0
