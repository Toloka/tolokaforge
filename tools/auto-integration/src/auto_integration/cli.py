"""``auto-integration`` CLI: one typer app aggregating the observe / resolve / finalize
subcommands. Each command is a thin wrapper over a module's ``run`` (or pure) logic so
the logic stays unit-testable and the GitHub Actions workflow is a thin caller.
"""

from __future__ import annotations

import typer

from auto_integration import cert

app = typer.Typer(
    help="Arena model auto-integration: observe quirks, resolve a policy + cert, finalize the PR.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command("reconcile-cert")
def reconcile_cert(
    model_id: str = typer.Option(..., "--model-id", help="candidate model_id slug"),
    findings: str = typer.Option(..., "--findings", help="path to observe findings.json"),
) -> None:
    """Reconcile the staged ModelCertificate against the observe baseline (finalize gate)."""
    raise typer.Exit(cert.run(model_id, findings))


if __name__ == "__main__":
    app()
