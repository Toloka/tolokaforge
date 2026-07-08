"""CLI for langfuse-uploader."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console

from .uploader import LangfuseClient, discover_trials, upload_trial

app = typer.Typer(help="Upload tolokaforge trial bundles into Langfuse as traces.")
console = Console()

RUN_DIR_ARG = typer.Argument(
    ..., help="Run output dir (results/<run-name>) or a single trial bundle dir"
)
LABEL_OPT = typer.Option(
    None, "--label", "-l", help="Grouping label used in trace names/tags (default: run dir name)"
)
SESSION_OPT = typer.Option(
    None, "--session", "-s", help="Langfuse session id for the run (default: run dir name)"
)
RUN_TAG_OPT = typer.Option(
    "v1", "--run-tag", help="Trace id namespace; bump to re-upload as fresh traces"
)
HOST_OPT = typer.Option(None, "--host", help="Langfuse base URL (default: env LANGFUSE_HOST)")
MEDIA_PUT_VIA_OPT = typer.Option(
    None,
    "--media-put-via",
    help=(
        "Advanced: 'host:port' to route presigned media PUTs (e.g. a kubectl port-forward) "
        "when the object store hostname is not reachable from this machine "
        "(default: env LANGFUSE_MEDIA_PUT_VIA)"
    ),
)


def _default_run_name(run_dir: Path) -> str:
    """Run name for default label/session; for a single trial dir, walk up to the run dir."""
    resolved = run_dir.resolve()
    if (resolved / "trajectory.yaml").exists() and resolved.parent.parent.name == "trials":
        return resolved.parent.parent.parent.name or "tolokaforge"
    return resolved.name or "tolokaforge"


def _client(host: str | None, media_put_via: str | None) -> LangfuseClient:
    host = host or os.environ.get("LANGFUSE_HOST")
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
    media_put_via = media_put_via or os.environ.get("LANGFUSE_MEDIA_PUT_VIA")
    missing = [
        name
        for name, value in [
            ("LANGFUSE_HOST (or --host)", host),
            ("LANGFUSE_PUBLIC_KEY", public_key),
            ("LANGFUSE_SECRET_KEY", secret_key),
        ]
        if not value
    ]
    if missing:
        console.print(f"[red]❌ Missing configuration: {', '.join(missing)}[/red]")
        raise typer.Exit(1)
    assert host and public_key and secret_key
    return LangfuseClient(
        host=host, public_key=public_key, secret_key=secret_key, media_put_via=media_put_via
    )


def _upload_trials(
    client: LangfuseClient, trials: list[Path], *, label: str, session: str, run_tag: str
) -> set[str]:
    """Upload each trial; returns dirs that completed (successfully or with ingest errors)."""
    done: set[str] = set()
    ok = 0
    for trial_dir in trials:
        try:
            result = upload_trial(client, trial_dir, label=label, session=session, run_tag=run_tag)
        except Exception as exc:
            console.print(f"  [red]FAIL[/red] {trial_dir}: {exc}")
            continue
        if result.status == "empty":
            console.print(f"  [yellow]SKIP[/yellow] {trial_dir}: no trajectory.yaml")
            continue
        done.add(str(trial_dir))
        ok += result.status == "ok"
        marker = "[green]OK [/green]" if result.status == "ok" else "[red]ERR[/red]"
        media = f" media={result.media_uploaded}" if result.media_uploaded else ""
        if result.media_failed:
            media += f" media_failed={result.media_failed}"
        console.print(
            f"  {marker} {trial_dir.parent.name}/{trial_dir.name}"
            f" events={result.events}{media} trace={result.trace_id}"
        )
        if result.error:
            console.print(f"      [red]{result.error[:300]}[/red]")
    console.print(f"[bold]{ok}/{len(trials)} ok[/bold]")
    return done


@app.command()
def upload(
    run_dir: Path = RUN_DIR_ARG,
    label: str | None = LABEL_OPT,
    session: str | None = SESSION_OPT,
    run_tag: str = RUN_TAG_OPT,
    host: str | None = HOST_OPT,
    media_put_via: str | None = MEDIA_PUT_VIA_OPT,
) -> None:
    """Upload all trial bundles under RUN_DIR."""
    client = _client(host, media_put_via)
    trials = discover_trials(run_dir)
    if not trials:
        console.print(f"[yellow]No trial bundles found under {run_dir}[/yellow]")
        raise typer.Exit(1)
    run_name = _default_run_name(run_dir)
    label = label or run_name
    session = session or run_name
    console.print(f"[bold]{len(trials)} trial(s)[/bold] → label={label} session={session}")
    _upload_trials(client, trials, label=label, session=session, run_tag=run_tag)


@app.command()
def watch(
    run_dir: Path = RUN_DIR_ARG,
    label: str | None = LABEL_OPT,
    session: str | None = SESSION_OPT,
    run_tag: str = RUN_TAG_OPT,
    host: str | None = HOST_OPT,
    media_put_via: str | None = MEDIA_PUT_VIA_OPT,
    interval: int = typer.Option(15, "--interval", help="Poll interval in seconds"),
) -> None:
    """Poll RUN_DIR and upload new trial bundles as they appear (Ctrl-C to stop)."""
    client = _client(host, media_put_via)
    run_name = _default_run_name(run_dir)
    label = label or run_name
    session = session or run_name
    state_path = run_dir / ".langfuse_uploaded.json"
    seen: set[str] = set(json.loads(state_path.read_text())) if state_path.exists() else set()
    console.print(
        f"Watching {run_dir} every {interval}s — already uploaded: {len(seen)} (Ctrl-C to stop)"
    )
    while True:
        pending = [t for t in discover_trials(run_dir) if str(t) not in seen]
        if pending:
            console.print(f"[dim]{datetime.now():%H:%M:%S}[/dim] {len(pending)} new trial(s)")
            seen |= _upload_trials(client, pending, label=label, session=session, run_tag=run_tag)
            state_path.write_text(json.dumps(sorted(seen)))
        time.sleep(interval)


def main() -> None:
    """Entrypoint."""
    app()
