"""``automation`` CLI: one typer app aggregating the observe / resolve / finalize
subcommands. Each command is a thin wrapper over a module's ``run`` (or pure) logic so
the logic stays unit-testable and the GitHub Actions workflow is a thin caller.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import typer

from automation import (
    bucket_classifier,
    cert,
    cost_summary,
    gateway_catalog,
    greencheck,
    langfuse_upload,
    model_resolver,
    observe,
    poller,
    pricing,
    probes,
    reprobe,
    resolve_report,
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

# The needs-human "why it did not converge" report composer.
app.command("resolve-report")(resolve_report.cli)


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
    input_usd: float | None = typer.Option(
        None, "--input-usd", help="declared input price per million tokens (gateway-only models)"
    ),
    output_usd: float | None = typer.Option(None, "--output-usd", help="declared output price"),
) -> None:
    """Ensure the candidate has a pricing.json entry (best-effort, minimal diff)."""
    if (input_usd is None) != (output_usd is None):
        raise typer.BadParameter("--input-usd and --output-usd go together, or neither")
    declared = None if input_usd is None else (input_usd, output_usd)
    raise typer.Exit(
        pricing.run(name, pricing_file=pricing_file, check=check, declared=declared)  # type: ignore[arg-type]
    )


@app.command("cert-env-gate")
def cert_env_gate(
    model_id: str = typer.Option(..., "--model-id", help="filesystem-safe certificate slug"),
) -> None:
    """Print the variable gating this model's live capability probes, if it needs opening."""
    raise typer.Exit(cert.env_gate(model_id))


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
    pyargs: str = typer.Option(
        probes.DEFAULT_PYARGS,
        "--pyargs",
        help="pytest --pyargs target package for collection (e.g. tolokaforge.testing.certify.suite)",
    ),
) -> None:
    """Flat (node x rep) parallel probe runner for the OBSERVE stage."""
    raise typer.Exit(probes.run(k_expr, out, reps=reps, workers=workers, pyargs=pyargs))


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


@app.command("cost-summary")
def cost_summary_cmd(
    obs_dir: str = typer.Argument(..., help="the observation artifact directory"),
    out: str = typer.Option(..., "--out", help="cost summary JSON output path"),
    md_out: str | None = typer.Option(
        None, "--md-out", help="optional markdown output path (the PR comment body)"
    ),
    line_out: str | None = typer.Option(
        None, "--line-out", help="optional one-line output path (the Slack text)"
    ),
    run_url: str | None = typer.Option(None, "--run-url", help="workflow run URL to link"),
    key_dir: str | None = typer.Option(
        None,
        "--key-dir",
        help="directory holding the key_*.json snapshots (default: <obs_dir>/cost); the "
        "workflow keeps them outside the observation dir so they never reach an artifact",
    ),
) -> None:
    """Attribute the run's spend: agent self-reports, wire aggregates, key-usage deltas."""
    raise typer.Exit(
        cost_summary.run(
            obs_dir, out=out, md_out=md_out, line_out=line_out, run_url=run_url, key_dir=key_dir
        )
    )


@app.command("key-snapshot")
def key_snapshot_cmd(
    out: str = typer.Option(..., "--out", help="where to write the key usage snapshot JSON"),
) -> None:
    """Snapshot the automation key's usage (OpenRouter GET /api/v1/key). Best-effort, exit 0."""
    raise typer.Exit(cost_summary.run_key_snapshot(out))


@app.command("agent-digest")
def agent_digest_cmd(
    path: str = typer.Argument(
        ..., help="a claude -p output file (json / json array / stream-json)"
    ),
    out: str | None = typer.Option(
        None,
        "--out",
        help="also write the NORMALIZED result event (cost, turns, usage, ending - no "
        "transcript) here; this is the shape the cost artifact uploads",
    ),
) -> None:
    """Print one line (subtype, turns, cost) for an agent run, for the job log."""
    raise typer.Exit(cost_summary.run_digest(path, out=out))


def _classify_paths_format(cls: bucket_classifier.Classification, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(
            {
                "bucket": cls.bucket.value,
                "reason": cls.reason,
                "engine_paths": list(cls.engine_paths),
            }
        )
    if fmt == "plain":
        return "\n".join(
            [
                f"bucket={cls.bucket.value}",
                f"reason={cls.reason}",
                f"engine_paths={','.join(cls.engine_paths)}",
            ]
        )
    raise typer.BadParameter(f"unknown --format {fmt!r}; expected json|plain")


def _classify_paths_source_paths(stdin: bool, diff: str | None, cached: bool) -> tuple[str, ...]:
    selected = sum(1 for flag in (stdin, diff is not None, cached) if flag)
    if selected != 1:
        raise typer.BadParameter(
            "exactly one of --paths-from-stdin | --paths-from-diff | --paths-from-cached required"
        )
    if stdin:
        return tuple(line for line in sys.stdin.read().splitlines() if line)
    argv = (
        ["git", "diff", "--cached", "--name-only"]
        if cached
        else ["git", "diff", "--name-only", diff or ""]
    )
    # git's stderr passes straight through so the workflow log surfaces any git failure.
    completed = subprocess.run(argv, check=True, stdout=subprocess.PIPE, text=True)
    return tuple(line for line in completed.stdout.splitlines() if line)


@app.command("classify-paths")
def classify_paths_cmd(
    paths_from_stdin: bool = typer.Option(
        False, "--paths-from-stdin", help="read newline-separated paths from stdin"
    ),
    paths_from_diff: str | None = typer.Option(
        None,
        "--paths-from-diff",
        help="run `git diff --name-only <ref>` and classify the touched paths",
    ),
    paths_from_cached: bool = typer.Option(
        False,
        "--paths-from-cached",
        help="run `git diff --cached --name-only` and classify the staged paths",
    ),
    output_format: str = typer.Option(
        "json", "--format", help="json (default) | plain (bucket=A/reason=.../engine_paths=...)"
    ),
) -> None:
    """Classify touched paths as Bucket A (models-wheel only) or Bucket B (engine change)."""
    touched = _classify_paths_source_paths(paths_from_stdin, paths_from_diff, paths_from_cached)
    typer.echo(_classify_paths_format(bucket_classifier.classify_paths(touched), output_format))


@app.command("resolve-models")
def resolve_models(
    request: str = typer.Argument(
        ..., help="free-text integrate request, e.g. 'integrate Grok 4.5 and GPT 5.6'"
    ),
) -> None:
    """Deterministically resolve the model phrases in a Slack request to model slugs.
    Prints a JSON list of {query, status, slug, candidates, source} for the poller to act on.

    Searches the same two catalogs as the poller, through the same accessor, so debugging a
    request by hand cannot disagree with what the poll actually did."""
    catalog = model_resolver.fetch_openrouter_catalog()
    gateway_entries = gateway_catalog.fetch_configured_catalog()
    resolutions = model_resolver.resolve_all(request, catalog, gateway_entries=gateway_entries)
    typer.echo(json.dumps([model_resolver.as_dict(r) for r in resolutions], indent=2))


@app.command("langfuse-upload")
def langfuse_upload_cmd(
    directory: str = typer.Argument(
        ..., help="a directory of claude -p output files (or one such file)"
    ),
    run_id: str = typer.Option(..., "--run-id", help="the run this session belongs to"),
    label: str = typer.Option(..., "--label", help="what the agents worked on (the trace name)"),
    session: str | None = typer.Option(None, "--session", help="default: the run id"),
    run_tag: str = typer.Option(
        langfuse_upload.DEFAULT_RUN_TAG, "--run-tag", help="the id contract's run tag"
    ),
    environment: str | None = typer.Option(
        None, "--environment", help="default: LANGFUSE_ENVIRONMENT"
    ),
    project: str | None = typer.Option(None, "--project", help="default: LANGFUSE_PROJECT"),
    tag: list[str] = typer.Option(
        [], "--tag", help="a caller tag, prefix:value (team and run_kind are required)"
    ),
    metadata: list[str] = typer.Option(
        [], "--metadata", help="free trace metadata, key=value (the stage, the iteration, ...)"
    ),
    tool_io: str = typer.Option(
        "drop",
        "--tool-io",
        help="what happens to tool arguments and results: drop (default) or scrub",
    ),
    receipt: str | None = typer.Option(None, "--receipt", help="write the report JSON here"),
    summary: str | None = typer.Option(
        None, "--summary", help="append the report here (default: GITHUB_STEP_SUMMARY)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="read, gate and project, but send nothing and need no key"
    ),
) -> None:
    """Send the agents' own transcripts to the tracing receiver (read, gate, project, send)."""
    try:
        report = langfuse_upload.upload(
            Path(directory),
            run_id=run_id,
            label=label,
            session=session,
            run_tag=run_tag,
            environment=environment or os.environ.get("LANGFUSE_ENVIRONMENT"),
            project=project or os.environ.get("LANGFUSE_PROJECT"),
            caller_tags=langfuse_upload.parse_pairs(tag, ":", "--tag"),
            metadata=langfuse_upload.parse_pairs(metadata, "=", "--metadata"),
            tool_io=tool_io,
            dry_run=dry_run,
        )
    except langfuse_upload.UploadError as exc:
        # tracing never fails the pipeline: say what is missing and leave the step to decide
        typer.echo(f"agent transcripts not sent: {exc}", err=True)
        raise typer.Exit(1) from None
    if receipt:
        Path(receipt).write_text(json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8")
    langfuse_upload.write_summary(report, summary)
    typer.echo(report.as_markdown())
    raise typer.Exit(0 if report.ok else 1)


if __name__ == "__main__":
    app()
