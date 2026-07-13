"""``automation`` CLI: one typer app aggregating the observe / resolve / finalize
subcommands. Each command is a thin wrapper over a module's ``run`` (or pure) logic so
the logic stays unit-testable and the GitHub Actions workflow is a thin caller.
"""

from __future__ import annotations

import json

import typer

from automation import (
    cert,
    greencheck,
    model_resolver,
    observe,
    poller,
    pricing,
    probes,
    reprobe,
    slack,
)

app = typer.Typer(
    help="Arena model automation: observe quirks, resolve a policy + cert, finalize the PR.",
    no_args_is_help=True,
    add_completion=False,
)

# Slack thread notifications are their own sub-app (`automation slack ...`).
app.add_typer(slack.app, name="slack")

# The Slack-triggered integration poller (`automation slack-poll ...`).
app.command("slack-poll")(poller.cli)


@app.command("reconcile-cert")
def reconcile_cert(
    model_id: str = typer.Option(..., "--model-id", help="candidate model_id slug"),
    findings: str = typer.Option(..., "--findings", help="path to observe findings.json"),
) -> None:
    """Reconcile the staged ModelCertificate against the observe baseline (finalize gate)."""
    raise typer.Exit(cert.run(model_id, findings))


@app.command("ensure-pricing")
def ensure_pricing(
    name: str = typer.Option(..., "--name", help="litellm model name, e.g. xiaomi/mimo-v2.5-pro"),
    pricing_file: str = typer.Option(pricing.DEFAULT_PRICING_FILE, "--pricing-file"),
    check: bool = typer.Option(False, "--check", help="exit 0 if priced, 1 if not; no fetch/write"),
) -> None:
    """Ensure the candidate has a pricing.json entry (best-effort, minimal diff)."""
    raise typer.Exit(pricing.run(name, pricing_file=pricing_file, check=check))


@app.command("greencheck")
def greencheck_cmd(
    decision: str = typer.Argument(..., help="path to the compose step's decision.json"),
    reprobe_findings: str = typer.Argument(..., help="path to the reprobe findings.json"),
) -> None:
    """Print the resolve fix-loop green-check token (CONVERGED / RED:... / NO_TARGETS)."""
    raise typer.Exit(greencheck.run(decision, reprobe_findings))


@app.command("run-probes")
def run_probes(
    k_expr: str = typer.Option(..., "--k-expr", help="the pytest -k selection for the candidate"),
    out: str = typer.Option(..., "--out", help="junit output dir (e.g. observation/capability)"),
    reps: int = typer.Option(15, "--reps", help="repeats per node (CAPABILITY_K)"),
    workers: int = typer.Option(10, "--workers", help="flat-pool width (node x rep)"),
    path: str = typer.Option(probes.DEFAULT_PATH, "--path", help="test root to collect from"),
) -> None:
    """Flat (node x rep) parallel probe runner for the OBSERVE stage."""
    raise typer.Exit(probes.run(k_expr, out, reps=reps, workers=workers, path=path))


@app.command("reprobe")
def reprobe_cmd(
    baseline: str = typer.Option(
        ..., "--baseline", help="observe findings.json to read failures from"
    ),
    overlay: str = typer.Option(..., "--overlay", help="policy preset overlay YAML (the fix)"),
    provider: str = typer.Option(..., "--provider", help="candidate provider (e.g. openrouter)"),
    name: str = typer.Option(..., "--name", help="candidate model slug (e.g. minimax/minimax-m3)"),
    out: str = typer.Option(..., "--out", help="output dir for the re-probe observation"),
    dataset: str = typer.Option(reprobe.WIRE_DATASET, "--dataset", help="wire task-pack root"),
    capability_k: int = typer.Option(15, "--capability-k"),
    wire_k: int = typer.Option(10, "--wire-k"),
    workers: int = typer.Option(10, "--workers"),
    cap_parallel: int = typer.Option(10, "--cap-parallel"),
    targets: str | None = typer.Option(
        None,
        "--targets",
        help="comma-separated probe names to reprobe (the agent's fix_targets); default = "
        "ALL failed probes from the baseline. Restricting to fix_targets skips the slow, "
        "un-fixable ceiling probes (thinking/caching) each iteration.",
    ),
    skip_wire: bool = typer.Option(
        False, "--skip-wire", help="capability-only (the agent's inner loop)"
    ),
    run_url: str | None = typer.Option(None, "--run-url"),
) -> None:
    """Re-run only the failed probes under a policy overlay and emit findings (RESOLVE)."""
    raise typer.Exit(
        reprobe.run(
            baseline=baseline,
            overlay=overlay,
            provider=provider,
            name=name,
            out=out,
            dataset=dataset,
            capability_k=capability_k,
            wire_k=wire_k,
            workers=workers,
            cap_parallel=cap_parallel,
            targets=targets,
            skip_wire=skip_wire,
            run_url=run_url,
        )
    )


@app.command("observe-findings")
def observe_findings(
    obs_dir: str = typer.Argument(..., help="the observation artifact directory"),
    out: str | None = typer.Option(
        None, "--out", help="findings JSON output path (default: <obs_dir>/findings.json)"
    ),
    summary_out: str | None = typer.Option(
        None, "--summary-out", help="optional markdown summary output path"
    ),
    run_url: str | None = typer.Option(
        None, "--run-url", help="workflow run URL to link in the summary"
    ),
) -> None:
    """Emit deterministic observe-stage findings.json (raw stats) from an obs dir."""
    raise typer.Exit(observe.run(obs_dir, out=out, summary_out=summary_out, run_url=run_url))


@app.command("resolve-models")
def resolve_models(
    request: str = typer.Argument(
        ..., help="free-text integrate request, e.g. 'integrate Grok 4.5 and GPT 5.6'"
    ),
) -> None:
    """Deterministically resolve the model phrases in a Slack request to OpenRouter slugs.
    Prints a JSON list of {query, status, slug, candidates} for the poller to act on."""
    catalog = model_resolver.fetch_openrouter_catalog()
    resolutions = model_resolver.resolve_all(request, catalog)
    typer.echo(json.dumps([model_resolver.as_dict(r) for r in resolutions], indent=2))


if __name__ == "__main__":
    app()
