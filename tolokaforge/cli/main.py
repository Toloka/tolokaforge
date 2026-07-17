"""Main CLI entry point"""

import json
import os
from datetime import datetime
from pathlib import Path

import click
import yaml
from rich.console import Console

from tolokaforge.core.engine_run_state import read_persisted_presets_file
from tolokaforge.core.llm.presets import (
    resolve_overlay_path,
    set_overlay_path,
    validate_overlay_file,
)
from tolokaforge.core.models import RunConfig
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.core.project_loader import load_effective_run_config
from tolokaforge.core.resume import RunStateManager
from tolokaforge.core.run_queue import create_run_queue
from tolokaforge.secrets import init_default, install_global_redactor

# Initialize SecretManager singleton — reads .env via DotEnvProvider, then
# falls back to os.environ. Must run before any code that needs a credential.
# After this point, all secret access goes through `get_default()`.
_secrets = init_default()

# Scrub known secret values out of any log record reaching root's handlers.
install_global_redactor()

# Many third-party SDKs (litellm in particular) look up provider keys via
# os.environ directly. Mirror the resolved secrets into os.environ once at
# CLI startup so those SDKs find them. Use setdefault so explicit shell
# exports always win.
_secrets.export_to_environ(
    [
        "OPENROUTER_API_KEY",
        "OPENROUTER_API_KEYS",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "NOVA_API_KEY",
        "TYPESENSE_API_KEY",
    ]
)

console = Console()


def _print_runtime_banner(*, console: Console, runtime_choice: str, source: str) -> None:
    """Print a loud banner naming the selected runtime backend and the
    isolation posture callers should expect.

    Called after the run config is resolved and before the orchestrator
    starts. Users see the effective backend + how it was chosen — no
    silent defaults."""
    backend_class = {
        "shared": "SharedStackRuntimeBackend",
        "per_trial": "PerTrialRuntimeBackend",
    }.get(runtime_choice, runtime_choice)
    isolation_line = {
        "shared": (
            "one docker-compose stack shared across every trial (fastest, "
            "no cross-trial isolation)"
        ),
        "per_trial": (
            "one docker-compose stack per trial via Testcontainers "
            "(concurrent trials fully isolated)"
        ),
    }.get(runtime_choice, "unknown")
    console.print(
        f"[bold cyan]Runtime backend:[/bold cyan] {backend_class} "
        f"([dim]{runtime_choice}, from {source}[/dim])"
    )
    console.print(f"[cyan]  → {isolation_line}[/cyan]")


@click.group()
def cli():
    """Universal LLM Tool-Use Benchmarking Harness (ULB-H)"""
    pass


# Register docker subcommand group (lazy import to avoid docker dep at registration)
from tolokaforge.cli.docker_commands import docker  # noqa: E402

cli.add_command(docker)

# Register adapter subcommand group
from tolokaforge.cli.adapter_commands import adapter  # noqa: E402

cli.add_command(adapter)

# Register config subcommand group
from tolokaforge.cli.config_commands import config  # noqa: E402

cli.add_command(config)

# Register assets subcommand group (`tolokaforge assets stamp` verb).
from tolokaforge.cli.assets_commands import assets  # noqa: E402

cli.add_command(assets)


# Default user model configuration
DEFAULT_USER_MODEL = "anthropic/claude-sonnet-4.6"
DEFAULT_USER_MODEL_PROVIDER = "openrouter"
DEFAULT_USER_MODEL_TEMPERATURE = 0.2


def _activate_presets_overlay(
    cli_presets_file: str | None,
    run_config: RunConfig,
    run_dir: Path | None = None,
) -> str | None:
    """Resolve preset-overlay precedence and install the overlay.

    Precedence (highest to lowest):

    1. ``cli_presets_file`` — ``--presets-file`` flag on the current command.
    2. ``run_dir / engine_run_state.json`` — the path persisted by
       ``tolokaforge prepare`` so worker subprocesses inherit the operator's
       overlay choice without threading the flag through manually. Only
       consulted when *run_dir* is given (the worker / queue-backed paths).
    3. ``engine.presets_file`` in the run config.

    Returns the resolved path (or ``None`` if no overlay is configured). The
    install side-effect is :func:`tolokaforge.core.llm.presets.set_overlay_path`,
    after which any later call to ``build_capabilities`` etc. reads the merged
    registry. Must run **before** the ``Orchestrator`` is constructed so that
    capability resolution at trial setup sees the overlay.

    When an overlay is resolved, this also **eagerly validates it** via
    :func:`validate_overlay_file`. ``set_overlay_path`` itself is lazy by
    contract (so tests can install paths cheaply), but at the CLI boundary
    we want a typo'd overlay to fail *here* — before the orchestrator is
    constructed, ``load_tasks()`` walks the task tree, or the Docker stack
    auto-starts.
    """
    config_value = run_config.engine.presets_file if run_config.engine else None
    queue_state_value = read_persisted_presets_file(run_dir) if run_dir is not None else None
    # Queue-state value (if any) sits above ``engine.presets_file`` because
    # ``prepare`` was the most recent operator decision. CLI still wins.
    effective_config_value = queue_state_value or config_value
    resolved = resolve_overlay_path(cli_value=cli_presets_file, config_value=effective_config_value)
    set_overlay_path(resolved)
    if resolved is not None:
        validate_overlay_file(resolved)
    return resolved


@cli.command()
@click.option(
    "--config", required=True, type=click.Path(exists=True), help="Path to run config YAML"
)
@click.option("--resume", is_flag=True, help="Resume from interrupted run")
@click.option("--verbose", is_flag=True, help="Enable DEBUG level logging")
@click.option("--strict", is_flag=True, help="Raise error immediately on logging ERROR level")
@click.option(
    "--user-model",
    default=None,
    help="Override user simulator model (e.g., anthropic/claude-sonnet-4.6). Uses OpenRouter as provider.",
)
@click.option(
    "--judge-model",
    default=None,
    help="Set the read-only rubric judge model (e.g., anthropic/claude-sonnet-4.6). Uses OpenRouter as provider, temperature 0.",
)
@click.option(
    "--presets-file",
    "presets_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help=(
        "Path to a model-presets overlay YAML merged onto the bundled "
        "model_presets.yaml. Precedence: this flag > engine.presets_file in "
        "the run config. See docs/CONFIG.md."
    ),
)
@click.option(
    "--runtime",
    "runtime",
    type=click.Choice(["shared", "per_trial"]),
    default=None,
    help=(
        "Runtime backend. Overrides orchestrator.runtime in the run config. "
        "'shared' (default) uses one docker-compose stack across every trial; "
        "'per_trial' materialises an isolated stack per trial via Testcontainers "
        "(required by tasks whose environment_manifest declares "
        "isolation: per_trial). See docs/RUNTIME_BACKENDS.md."
    ),
)
@click.option(
    "--workers",
    "workers",
    type=click.IntRange(min=1),
    default=None,
    help=(
        "Number of concurrent trial workers. Overrides compute.workers "
        "in the run config. Must be a positive integer. See docs/CONFIG.md."
    ),
)
def run(
    config: str,
    resume: bool,
    verbose: bool,
    strict: bool,
    user_model: str | None,
    judge_model: str | None,
    presets_file: str | None,
    runtime: str | None,
    workers: int | None,
):
    """Run benchmark with specified configuration"""
    console.print(f"[bold blue]Loading configuration from {config}...[/bold blue]")

    # Load config with the enclosing project's run_defaults layered under
    # it. Every subcommand that constructs a RunConfig from disk goes
    # through the same helper so `run`, `prepare`, `worker`, `status`,
    # and `config validate` treat the same YAML identically.
    config_data, project = load_effective_run_config(Path(config))

    # Create output directory with timestamp (if not resuming)
    if "output_dir" not in config_data.get("evaluation", {}):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        config_data["evaluation"]["output_dir"] = f"results/run_{timestamp}"

    # Apply user model override: CLI flag > env var > YAML config
    # Priority: --user-model flag takes precedence over USER_MODEL env var
    user_model_override = user_model or os.environ.get("USER_MODEL")
    if user_model_override:
        config_data.setdefault("models", {})["user"] = {
            "provider": DEFAULT_USER_MODEL_PROVIDER,
            "name": user_model_override,
            "temperature": DEFAULT_USER_MODEL_TEMPERATURE,
        }
        console.print(f"[cyan]User model override: {user_model_override}[/cyan]")

    # Apply judge model: CLI flag > env var > YAML config (models.judge).
    # Temperature is pinned to 0 for grading determinism (the judge does not
    # honour a non-zero temperature yet). The YAML path is primary and parses
    # with no loader change; this flag is the ergonomic override mirroring
    # --user-model.
    judge_model_override = judge_model or os.environ.get("JUDGE_MODEL")
    if judge_model_override:
        config_data.setdefault("models", {})["judge"] = {
            "provider": DEFAULT_USER_MODEL_PROVIDER,
            "name": judge_model_override,
            "temperature": 0.0,
        }
        console.print(f"[cyan]Judge model: {judge_model_override}[/cyan]")

    # Apply --runtime CLI override before RunConfig construction so the
    # value survives every downstream config lookup.
    if runtime is not None:
        config_data.setdefault("orchestrator", {})["runtime"] = runtime

    # --workers writes to the canonical compute.workers home; the dual-
    # home alias-lift is a no-op here (no `orchestrator.workers` set by
    # the CLI), so this fires no DeprecationWarning.
    if workers is not None:
        config_data.setdefault("compute", {})["workers"] = workers

    run_config = RunConfig(**config_data)

    _print_runtime_banner(
        console=console,
        runtime_choice=run_config.orchestrator.runtime,
        source="cli-flag" if runtime is not None else "config",
    )

    console.print(f"[green]Output directory: {run_config.evaluation.output_dir}[/green]")

    if verbose:
        console.print("[yellow]Verbose mode enabled (DEBUG logging)[/yellow]")
    if strict:
        console.print("[yellow]Strict mode enabled (will raise on errors)[/yellow]")

    # Install preset overlay (if any) before constructing the orchestrator so
    # build_capabilities() sees the merged registry. Triggers loud-fail
    # validation on the overlay file at this point.
    overlay_path = _activate_presets_overlay(presets_file, run_config)
    if overlay_path:
        console.print(f"[cyan]Preset overlay: {overlay_path}[/cyan]")

    # Create orchestrator with flags. Pass the resolved project so the
    # adapter picks up project.task_defaults on task load.
    orchestrator = Orchestrator(
        run_config, resume=resume, verbose=verbose, strict=strict, project=project
    )

    # Load tasks
    console.print("[bold blue]Loading tasks...[/bold blue]")
    orchestrator.load_tasks()

    if not orchestrator.tasks:
        console.print("[red]No tasks found![/red]")
        return

    console.print(f"[green]Found {len(orchestrator.tasks)} tasks[/green]")

    # Run benchmarks
    if resume:
        console.print("[bold yellow]Resuming run (skipping completed trials)...[/bold yellow]")
    else:
        console.print(
            f"[bold blue]Running {run_config.orchestrator.repeats} trials per task...[/bold blue]"
        )

    orchestrator.run()

    console.print("[bold green]✓ Run complete![/bold green]")
    console.print(f"Results saved to: {run_config.evaluation.output_dir}")


@cli.command(name="prepare")
@click.option(
    "--config", required=True, type=click.Path(exists=True), help="Path to run config YAML"
)
@click.option(
    "--run-dir",
    required=True,
    type=click.Path(file_okay=False),
    help="Run directory used by queue workers",
)
@click.option("--reset-queue", is_flag=True, help="Clear existing queue entries before enqueueing")
@click.option("--verbose", is_flag=True, help="Enable DEBUG level logging")
@click.option("--strict", is_flag=True, help="Raise error immediately on logging ERROR level")
@click.option(
    "--presets-file",
    "presets_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help=(
        "Path to a model-presets overlay YAML; persisted into the queue "
        "run-state so worker subprocesses inherit it. Precedence: this flag > "
        "engine.presets_file."
    ),
)
def prepare(
    config: str,
    run_dir: str,
    reset_queue: bool,
    verbose: bool,
    strict: bool,
    presets_file: str | None,
):
    """Prepare a queue-backed run directory for distributed workers."""
    console.print(f"[bold blue]Preparing run from config {config}...[/bold blue]")
    config_data, project = load_effective_run_config(Path(config))
    run_config = RunConfig(**config_data)

    overlay_path = _activate_presets_overlay(presets_file, run_config)
    if overlay_path:
        console.print(f"[cyan]Preset overlay: {overlay_path}[/cyan]")

    orchestrator = Orchestrator(
        run_config, resume=False, verbose=verbose, strict=strict, project=project
    )
    orchestrator.load_tasks()
    summary = orchestrator.prepare_run(Path(run_dir), reset_queue=reset_queue)

    queue_counts = summary["queue_counts"]
    console.print("[bold green]✓ Run queue prepared[/bold green]")
    console.print(
        f"queued={summary['queued_attempts']} "
        f"pending={queue_counts.get('pending', 0)} "
        f"total={queue_counts.get('total', 0)} "
        f"backend={summary['queue_backend']}"
    )


@cli.command()
@click.option(
    "--config", required=True, type=click.Path(exists=True), help="Path to run config YAML"
)
@click.option(
    "--run-dir",
    required=True,
    type=click.Path(file_okay=False),
    help="Shared run directory containing queue/artifacts",
)
@click.option("--max-attempts", type=int, default=None, help="Optional max attempts to process")
@click.option("--verbose", is_flag=True, help="Enable DEBUG level logging")
@click.option("--strict", is_flag=True, help="Raise error immediately on logging ERROR level")
@click.option(
    "--presets-file",
    "presets_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help=(
        "Path to a model-presets overlay YAML. If unset, the worker reads the "
        "overlay path persisted by ``prepare`` from the queue run-state, then "
        "falls back to engine.presets_file."
    ),
)
def worker(
    config: str,
    run_dir: str,
    max_attempts: int | None,
    verbose: bool,
    strict: bool,
    presets_file: str | None,
):
    """Run a queue worker process (distributed execution mode)."""
    console.print(f"[bold blue]Loading worker config from {config}...[/bold blue]")
    config_data, project = load_effective_run_config(Path(config))
    run_config = RunConfig(**config_data)

    # Worker overlay precedence: --presets-file > ``prepare``-persisted queue
    # state > engine.presets_file.
    overlay_path = _activate_presets_overlay(presets_file, run_config, run_dir=Path(run_dir))
    if overlay_path:
        console.print(f"[cyan]Preset overlay: {overlay_path}[/cyan]")

    orchestrator = Orchestrator(
        run_config, resume=False, verbose=verbose, strict=strict, project=project
    )
    orchestrator.load_tasks()
    summary = orchestrator.run_worker(Path(run_dir), max_attempts=max_attempts)

    console.print("[bold green]✓ Worker complete[/bold green]")
    console.print(
        "processed={processed_attempts} completed={completed_attempts} "
        "failed={failed_attempts} requeued={requeued_attempts} cost=${total_cost_usd}".format(
            **summary
        )
    )


@cli.command()
@click.option(
    "--trajectory",
    required=True,
    type=click.Path(exists=True),
    help="Path to trajectory file (JSON or YAML)",
)
def analyze(trajectory: str):
    """Analyze a single trial trajectory.

    Displays trial summary including task info, metrics, grade, and any
    tool failures or log errors found in the trajectory.
    """
    console.print(f"[bold blue]Analyzing trajectory: {trajectory}[/bold blue]")

    traj_path = Path(trajectory)

    # Load trajectory data (supports both JSON and YAML)
    with open(traj_path) as f:
        if traj_path.suffix in (".yaml", ".yml"):
            traj_data = yaml.safe_load(f)
        else:
            traj_data = json.load(f)

    # For split-file format (YAML), load metrics and grade from separate files
    metrics = traj_data.get("metrics")
    grade = traj_data.get("grade")
    logs = []

    if metrics is None:
        metrics_path = traj_path.parent / "metrics.yaml"
        if metrics_path.exists():
            with open(metrics_path) as f:
                metrics = yaml.safe_load(f)

    if grade is None:
        grade_path = traj_path.parent / "grade.yaml"
        if grade_path.exists():
            with open(grade_path) as f:
                grade = yaml.safe_load(f)

    logs_path = traj_path.parent / "logs.yaml"
    if logs_path.exists():
        with open(logs_path) as f:
            logs_data = yaml.safe_load(f)
            logs = logs_data.get("logs", []) if logs_data else []

    # Display summary
    task_id = traj_data.get("task_id", "N/A")
    console.print(f"\n[bold]Task:[/bold] {task_id}")
    console.print(f"[bold]Trial:[/bold] {traj_data.get('trial_index', 'N/A')}")
    console.print(f"[bold]Status:[/bold] {traj_data.get('status', 'N/A')}")

    if metrics:
        console.print(f"[bold]Duration:[/bold] {metrics.get('latency_total_s', 0):.2f}s")
        console.print(f"[bold]Turns:[/bold] {metrics.get('turns', 'N/A')}")
        console.print(f"[bold]Tool Calls:[/bold] {metrics.get('tool_calls', 'N/A')}")

    if grade:
        console.print("\n[bold]Grade:[/bold]")
        console.print(f"  Pass: {'✓' if grade.get('binary_pass') else '✗'}")
        console.print(f"  Score: {grade.get('score', 0):.2f}")
        if grade.get("reasons"):
            console.print(f"  Reasons: {grade['reasons']}")

    # Extract and display tool failures from trajectory
    tool_failures = _extract_tool_failures(traj_data)
    if tool_failures:
        console.print(f"\n[bold red]Tool Failures ({len(tool_failures)}):[/bold red]")
        for failure in tool_failures[:5]:
            console.print(f"  • {failure[:150]}")
        if len(tool_failures) > 5:
            console.print(f"  ... and {len(tool_failures) - 5} more")

    # Extract and display log errors
    log_errors = _extract_log_errors(logs)
    if log_errors:
        console.print(f"\n[bold red]Log Errors ({len(log_errors)}):[/bold red]")
        for error in log_errors[:5]:
            console.print(f"  • {error[:150]}")
        if len(log_errors) > 5:
            console.print(f"  ... and {len(log_errors) - 5} more")

    if not tool_failures and not log_errors:
        console.print("\n[green]No tool failures or log errors detected.[/green]")


def _extract_tool_failures(trajectory: dict) -> list[str]:
    """Extract failed tool calls from trajectory messages."""
    failures = []

    if "messages" in trajectory:
        for msg in trajectory["messages"]:
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                if isinstance(content, str) and (
                    "error" in content.lower() or "failed" in content.lower()
                ):
                    failures.append(content[:200])

    return failures


def _extract_log_errors(logs: list[dict]) -> list[str]:
    """Extract ERROR level logs."""
    errors = []

    for log_entry in logs:
        if log_entry.get("level") == "ERROR":
            msg = log_entry.get("message", "")
            errors.append(msg[:200])

    return errors


@cli.command()
@click.option("--tasks", required=True, help="Glob pattern for task files")
def validate(tasks: str):
    """Validate task configurations"""
    console.print(f"[bold blue]Validating tasks matching: {tasks}[/bold blue]")

    import glob

    from tolokaforge.adapters._task_loader import load_task_yaml, validate_grading_yaml

    task_files = glob.glob(tasks, recursive=True)

    valid = 0
    invalid = 0

    for task_file in task_files:
        try:
            # load_task_yaml applies the shared-domain merge (if the task.yaml
            # carries a ``domain:`` ref) before TaskConfig validation, so this
            # CLI accepts both flat and shared-domain layouts.
            task_config, task_dir = load_task_yaml(Path(task_file))
            # Also validate the referenced grading.yaml so that schema breaks —
            # e.g. the removed free-text ``rubric: str`` / ``output_schema`` —
            # fail loud here with a clear migration message rather than only at
            # run time.
            validate_grading_yaml(task_dir / task_config.grading)
            console.print(f"[green]✓ {task_file}[/green]")
            valid += 1
        except Exception as e:
            console.print(f"[red]✗ {task_file}: {str(e)}[/red]")
            invalid += 1

    console.print(f"\n[bold]Summary:[/bold] {valid} valid, {invalid} invalid")


def _collect_run_spend_and_tokens(run_dir: Path) -> tuple[float, int, int]:
    """Aggregate spend / prompt-tokens / completion-tokens from per-trial metrics.

    Reads the Stage-5 ``metrics.yaml`` shape: ``usage.prompt_tokens`` /
    ``usage.completion_tokens`` (plus cache and reasoning counters, ignored
    here — they are surfaced by the aggregate reporter under ``tools/``).
    """
    total_cost = 0.0
    total_prompt_tokens = 0
    total_completion_tokens = 0

    trials_root = run_dir / "trials"
    if not trials_root.exists():
        return total_cost, total_prompt_tokens, total_completion_tokens

    for metrics_path in trials_root.glob("*/*/metrics.yaml"):
        try:
            with open(metrics_path) as f:
                metrics = yaml.safe_load(f) or {}
            total_cost += float(metrics.get("cost_usd", 0.0) or 0.0)
            usage = metrics.get("usage", {}) or {}
            total_prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
            total_completion_tokens += int(usage.get("completion_tokens", 0) or 0)
        except Exception:
            continue

    return total_cost, total_prompt_tokens, total_completion_tokens


def _format_eta(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    sec = max(0, int(seconds))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


@cli.command()
@click.option(
    "--run-dir",
    required=True,
    type=click.Path(exists=True, file_okay=False),
    help="Path to a run output directory containing run_state.json",
)
@click.option(
    "--config",
    required=False,
    type=click.Path(exists=True),
    help="Optional run config YAML (used to resolve postgres queue backend/dsn for distributed status)",
)
def status(run_dir: str, config: str | None):
    """Show live/status snapshot for a run directory."""
    run_path = Path(run_dir)
    manager = RunStateManager(run_path)
    info = manager.get_resume_info()
    queue = None
    queue_db = run_path / "run_queue.sqlite"
    if queue_db.exists():
        queue = create_run_queue("sqlite", sqlite_path=queue_db, max_retries=0)
    elif config:
        config_data, _project = load_effective_run_config(Path(config))
        run_config = RunConfig(**config_data)
        if run_config.effective_queue_backend == "postgres":
            queue = create_run_queue(
                "postgres",
                sqlite_path=run_path / "run_queue.sqlite",
                max_retries=run_config.effective_max_attempt_retries,
                postgres_dsn=run_config.effective_queue_postgres_dsn,
            )

    if not info and queue is None:
        console.print(f"[red]No run_state.json or queue backend found in {run_path}[/red]")
        return

    total_cost, total_input_tokens, total_output_tokens = _collect_run_spend_and_tokens(run_path)

    if info:
        console.print(f"[bold]Run:[/bold] {info['run_id']}")
        console.print(f"[bold]Status:[/bold] {info['status']}")
        console.print(
            f"[bold]Progress:[/bold] {info['completed_trials']}/{info['total_trials']} "
            f"({info['progress_pct']:.1f}%)"
        )
        console.print(f"[bold]Pending:[/bold] {info['pending_trials']}")
        console.print(f"[bold]Failed:[/bold] {info['failed_trials']}")
    else:
        console.print("[bold]Run:[/bold] (no run_state.json)")

    if queue is not None:
        counts = queue.get_counts()
        eta_s = queue.estimate_eta_seconds()
        console.print(
            "[bold]Queue:[/bold] "
            f"pending={counts.get('pending', 0)} "
            f"leased={counts.get('leased', 0)} "
            f"running={counts.get('running', 0)} "
            f"completed={counts.get('completed', 0)} "
            f"failed={counts.get('failed', 0)} "
            f"total={counts.get('total', 0)}"
        )
        console.print(f"[bold]Queue ETA:[/bold] {_format_eta(eta_s)}")

    console.print(f"[bold]Estimated cost:[/bold] ${total_cost:.4f}")
    console.print(f"[bold]Input tokens:[/bold] {total_input_tokens}")
    console.print(f"[bold]Output tokens:[/bold] {total_output_tokens}")


if __name__ == "__main__":
    cli()
