"""Main CLI entry point"""

import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

import click
import yaml
from rich.console import Console

from tolokaforge.core import pricing
from tolokaforge.core.budgets import LimitHitMarker, make_budget
from tolokaforge.core.dry_run import load_tasks_for_dry_run, materialize_dry_run_sample
from tolokaforge.core.duration import parse_duration
from tolokaforge.core.engine_run_state import read_persisted_presets_file
from tolokaforge.core.grading.replay import (
    KnowledgeSearchMode,
    ReplayOutcomeStatus,
    ReplayReport,
    TrialReplayOutcome,
    emit_replay_report,
    load_grading_override,
    run_replay_batch,
)
from tolokaforge.core.llm.client import LLMClient
from tolokaforge.core.llm.fallback_client import FallbackLLMClient
from tolokaforge.core.llm.presets import (
    resolve_overlay_path,
    set_overlay_path,
    validate_overlay_file,
)
from tolokaforge.core.logging import (
    LogFormat,
    configure_root_logging,
    silence_root_logging,
)
from tolokaforge.core.models import ModelConfig, ProjectConfig, RunConfig
from tolokaforge.core.orchestrator import Orchestrator, OrchestratorDeps, resolve_run_directory
from tolokaforge.core.project_loader import construct_config, load_effective_run_config
from tolokaforge.core.resume import RunStateManager, resolve_resume_run_directory
from tolokaforge.core.run_queue import create_run_queue
from tolokaforge.dx._display import (
    DisplayMode,
    console,
    emit_artifact_path,
    select_display_mode,
    silence_console,
)
from tolokaforge.dx.banners import (
    print_run_end_banner,
    print_run_start_banner,
)
from tolokaforge.dx.dry_run_render import render_dry_run
from tolokaforge.dx.live_panel import LiveRunDisplay
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


def _print_runtime_banner(*, console: Console, runtime_choice: str | None, source: str) -> None:
    """Print a loud banner naming the selected runtime backend and the
    isolation posture callers should expect.

    Called after the run config is resolved and before the orchestrator
    starts. Users see the effective backend + how it was chosen — no
    silent defaults.

    When ``runtime_choice`` is ``None``, ``orchestrator.runtime`` was not
    set on the config (the recommended shape — the field is deprecated).
    In that case the actual backend is selected at orchestrator boot
    based on task requirements; the banner reads ``auto (task-driven)``
    and names both candidates so the operator knows which one will fire.
    """
    if runtime_choice is None:
        console.print(
            "[bold cyan]Runtime backend:[/bold cyan] auto "
            "([dim]task-driven, resolved at orchestrator boot[/dim])"
        )
        console.print(
            "[cyan]  → PerTrialRuntimeBackend if any task requires per-trial "
            "isolation, else SharedStackRuntimeBackend[/cyan]"
        )
        return
    backend_class = {
        "shared": "SharedStackRuntimeBackend",
        "per_trial": "PerTrialRuntimeBackend",
    }.get(runtime_choice, runtime_choice)
    isolation_line = {
        "shared": (
            "one docker-compose stack shared across every trial (fastest, no cross-trial isolation)"
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


def _resolve_display_mode(
    *,
    explicit: str | None,
    env: Mapping[str, str],
) -> DisplayMode:
    """Wrap ``select_display_mode`` and re-raise its :class:`ValueError`
    as :class:`click.UsageError` so bad flags / env-var values fail with
    Click's usual exit-2 surface.
    """
    try:
        return select_display_mode(explicit=explicit, env=env)
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc


class _GroupedCommandsGroup(click.Group):
    """Click Group that renders ``Commands:`` as fixed-order group sections.

    Every visible command must appear in ``COMMAND_GROUPS``. A registered
    command missing from the map raises ``RuntimeError`` at ``--help`` time
    so a new top-level verb never silently disappears from the help output.
    """

    GROUP_ORDER: tuple[str, ...] = (
        "Interactive",
        "Runs",
        "Tasks",
        "Docker",
        "Config",
        "Assets",
        "Adapters",
    )

    COMMAND_GROUPS: dict[str, str] = {
        "repl": "Interactive",
        "run": "Runs",
        "run-trial": "Runs",
        "prepare": "Runs",
        "worker": "Runs",
        "status": "Runs",
        "analyze": "Runs",
        "browse": "Runs",
        "rejudge": "Runs",
        "validate": "Tasks",
        "docker": "Docker",
        "config": "Config",
        "assets": "Assets",
        "adapter": "Adapters",
    }

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        visible_commands: dict[str, click.Command] = {}
        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is None or cmd.hidden:
                continue
            visible_commands[name] = cmd

        sections: dict[str, list[tuple[str, click.Command]]] = {
            heading: [] for heading in self.GROUP_ORDER
        }
        for name, cmd in visible_commands.items():
            heading = self.COMMAND_GROUPS.get(name)
            if heading is None:
                raise RuntimeError(
                    f"_GroupedCommandsGroup: no group heading for command '{name}'; "
                    "add it to COMMAND_GROUPS"
                )
            sections[heading].append((name, cmd))

        limit = formatter.width - 6 - max((len(n) for n in visible_commands), default=0)
        for heading in self.GROUP_ORDER:
            rows_source = sorted(sections[heading], key=lambda item: item[0])
            if not rows_source:
                continue
            rows = [(name, cmd.get_short_help_str(limit)) for name, cmd in rows_source]
            with formatter.section(heading):
                formatter.write_dl(rows)


@click.group(cls=_GroupedCommandsGroup, invoke_without_command=True)
@click.version_option(package_name="tolokaforge")
@click.option(
    "--verbose",
    "-v",
    "verbose",
    is_flag=True,
    help="Console log level DEBUG (mutually exclusive with --quiet).",
)
@click.option(
    "--quiet",
    "-q",
    "quiet",
    is_flag=True,
    help="Console log level WARNING (mutually exclusive with --verbose).",
)
@click.option(
    "--log-format",
    "log_format",
    type=click.Choice([m.value for m in LogFormat], case_sensitive=False),
    default=None,
    help=(
        "Line shape for log records on the console. Defaults to "
        "'pretty' on a TTY, 'plain' otherwise. 'json' emits one JSON "
        "object per line."
    ),
)
@click.option(
    "--display",
    "display",
    type=click.Choice([m.value for m in DisplayMode], case_sensitive=False),
    default=None,
    help=(
        "Overall stderr UI. 'rich' = Rich Live panel (default on TTY); "
        "'plain' = log-line stream (default on non-TTY / CI); 'log' = "
        "pure log stream, no banners; 'none' = silent on stderr, only "
        "the artifact path on stdout. Env override: TOLOKAFORGE_DISPLAY. "
        "Orthogonal to --log-format."
    ),
)
@click.pass_context
def cli(
    ctx: click.Context,
    verbose: bool,
    quiet: bool,
    log_format: str | None,
    display: str | None,
) -> None:
    """Universal LLM Tool-Use Benchmarking Harness (ULB-H)"""
    if verbose and quiet:
        raise click.UsageError("--verbose and --quiet are mutually exclusive")

    if quiet:
        level = logging.WARNING
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO

    resolved_format = LogFormat(log_format.lower()) if log_format is not None else None
    configure_root_logging(level=level, log_format=resolved_format)

    display_mode = _resolve_display_mode(explicit=display, env=os.environ)

    ctx.ensure_object(dict)
    ctx.obj["root_verbose"] = verbose
    ctx.obj["root_quiet"] = quiet
    ctx.obj["log_format"] = resolved_format
    ctx.obj["display_mode"] = display_mode

    if display_mode is DisplayMode.NONE:
        silence_console()
        silence_root_logging()

    if ctx.invoked_subcommand is None:
        from tolokaforge.dx.repl import enter_repl

        enter_repl(ctx)


@cli.command()
@click.pass_context
def repl(ctx: click.Context) -> None:
    """Enter the interactive tolokaforge shell."""
    from tolokaforge.dx.repl import enter_repl

    enter_repl(ctx)


def _bump_console_debug_if_allowed(ctx: click.Context) -> None:
    """Raise the root handler to DEBUG for subcommand `--verbose`, unless
    the root callback saw `-q`.

    Root `-q` is an explicit request to silence the console; the subcommand
    flag still drives the per-trial `logs.yaml` level via
    `Orchestrator(verbose=True)`, but must not override the operator's
    console-quiet decision.
    """
    root_obj = ctx.find_root().obj or {}
    if root_obj.get("root_quiet"):
        return
    configure_root_logging(level=logging.DEBUG, log_format=root_obj.get("log_format"))


# Register docker subcommand group (lazy import to avoid docker dep at registration)
from tolokaforge.dx.cli.docker import docker  # noqa: E402

cli.add_command(docker)

# Register adapter subcommand group
from tolokaforge.dx.cli.adapter import adapter  # noqa: E402

cli.add_command(adapter)

# Register config subcommand group
from tolokaforge.dx.cli.config import config  # noqa: E402

cli.add_command(config)

# Register assets subcommand group (`tolokaforge assets stamp` verb).
from tolokaforge.dx.cli.assets import assets  # noqa: E402

cli.add_command(assets)

# Register the `run-trial` subprocess-contract command (JSON-Lines wire over stdio).
from tolokaforge.dx.cli.run_trial_command import run_trial  # noqa: E402

cli.add_command(run_trial)


# Default user model configuration
DEFAULT_USER_MODEL = "anthropic/claude-sonnet-4.6"
DEFAULT_USER_MODEL_PROVIDER = "openrouter"
DEFAULT_USER_MODEL_TEMPERATURE = 0.2

_DEFAULT_DRY_RUN_SAMPLES = 3
"""Number of tasks rendered under ``tolokaforge run --dry-run``.
Deliberately not a CLI knob — three samples is enough to spot a
misconfigured system prompt or tool spec, and rare tweaks are covered
by inspecting the run's on-disk output."""


def _read_limit_hit_reason(run_dir: Path) -> str | None:
    """Return ``"<which> limit"`` when ``run_dir/LIMIT_HIT.json`` exists.

    Orchestrator writes the marker on the first budget hit; this helper
    lets the CLI shape the end banner without leaking orchestrator
    internals. Missing file → ``None`` (natural completion). Malformed
    marker → :class:`ValueError` propagates — a corrupt marker is a
    fail-loud condition, not something the banner should silently hide.
    """
    marker_path = run_dir / "LIMIT_HIT.json"
    if not marker_path.exists():
        return None
    marker = LimitHitMarker.model_validate_json(marker_path.read_text())
    return f"{marker.which} limit"


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


def _run_dry_run(
    *,
    run_config: RunConfig,
    project: ProjectConfig | None,
    dry_run_samples: int,
) -> None:
    """Render the first-turn samples for *run_config* on the shared console.

    Bypasses run-directory creation, orchestrator setup, live display,
    and start / end banners — dry-run's contract is "no run dir, no
    HTTP, no artifact emit". Every stderr write flows through the
    shared :data:`console` so ``--display=none`` silences the panels
    via ``console.quiet``. Exits 1 (via :class:`SystemExit`) when the
    adapter resolves zero tasks — matches the "No tasks found" surface
    the real run has at the same point in its flow.
    """
    adapter, tasks = load_tasks_for_dry_run(run_config=run_config, project=project)
    if not tasks:
        console.print("[red]No tasks found![/red]")
        raise SystemExit(1)

    agent_config = run_config.models.get("agent")
    if agent_config is None:
        raise click.UsageError("Agent model configuration required (models.agent)")
    judge_config = run_config.models.get("judge")
    runtime_choice = run_config.orchestrator.runtime

    n_rendered = min(dry_run_samples, len(tasks))
    samples = [
        materialize_dry_run_sample(
            task=task,
            adapter=adapter,
            agent_config=agent_config,
            judge_config=judge_config,
            runtime_choice=runtime_choice,
        )
        for task in tasks[:n_rendered]
    ]
    render_dry_run(samples, console=console, n_available=len(tasks))


@cli.command()
@click.option(
    "--config", required=True, type=click.Path(exists=True), help="Path to run config YAML"
)
@click.option("--resume", is_flag=True, help="Resume from interrupted run")
@click.option(
    "--run-dir",
    "run_dir_option",
    type=click.Path(exists=True, file_okay=False),
    default=None,
    help=(
        "Existing run directory to reuse. Required with --resume; forbidden "
        "without it. The directory must contain engine_run_state.json or "
        "run_state.json from a prior invocation. See docs/CLI.md § Resume."
    ),
)
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
    type=str,
    default=None,
    help=(
        "Runtime backend name. Overrides orchestrator.runtime in the run config. "
        "Any name registered in the tolokaforge.runtime_backends entry-point group: "
        "built-in 'shared' (one docker-compose stack across every trial), 'per_trial' "
        "(an isolated stack per trial via Testcontainers, required by tasks whose "
        "environment_manifest declares isolation: per_trial), 'in_memory', or a "
        "plug-in's registered name. Validated at run start with an actionable error "
        "listing the known names. See docs/RUNTIME_BACKENDS.md."
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
@click.option(
    "--cost-limit",
    "cost_limit",
    type=float,
    default=None,
    help=(
        "Hard cap on cumulative agent cost in USD. Stops enqueuing new "
        "trials on hit; in-flight trials finish. Writes LIMIT_HIT.json "
        "under the run directory. Overrides compute.max_budget_usd."
    ),
)
@click.option(
    "--time-limit",
    "time_limit",
    type=str,
    default=None,
    help=(
        "Hard cap on wall-clock execution time. Accepts compound units "
        "(e.g. '30m', '2h', '1h30m', '90s', '1d12h'). Clock starts on "
        "the first trial event, not at task-loading time."
    ),
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help=(
        "Resolve the run config and tasks, render the first-turn wire "
        "request (system prompt, user prompt, sanitized tool spec) for "
        "the first three tasks, and exit 0 without any HTTP call to a "
        "provider. See docs/CLI.md § Dry run."
    ),
)
@click.pass_context
def run(
    ctx: click.Context,
    config: str,
    resume: bool,
    run_dir_option: str | None,
    verbose: bool,
    strict: bool,
    user_model: str | None,
    judge_model: str | None,
    presets_file: str | None,
    runtime: str | None,
    workers: int | None,
    cost_limit: float | None,
    time_limit: str | None,
    dry_run: bool,
):
    """Run benchmark with specified configuration"""
    if verbose:
        _bump_console_debug_if_allowed(ctx)

    if dry_run and resume:
        raise click.UsageError(
            "--dry-run and --resume are mutually exclusive; --dry-run does not consult run state"
        )
    if resume and run_dir_option is None:
        raise click.UsageError(
            "--resume requires --run-dir <path> pointing at an existing run directory"
        )
    if run_dir_option is not None and not resume:
        raise click.UsageError(
            "--run-dir requires --resume; use it only to point --resume at an existing run directory"
        )

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

    # --cost-limit mutates compute.max_budget_usd (its existing home);
    # the CLI value beats any value in the run config, mirroring --workers.
    if cost_limit is not None:
        config_data.setdefault("compute", {})["max_budget_usd"] = cost_limit

    # --time-limit accepts compound-unit spec strings; ValueError from the
    # parser surfaces as click.BadParameter with a diagnostic message.
    time_limit_seconds: float | None = None
    if time_limit is not None:
        try:
            time_limit_seconds = parse_duration(time_limit)
        except ValueError as exc:
            raise click.BadParameter(f"--time-limit: {exc}") from exc

    run_config = construct_config(RunConfig, config_data, source=Path(config))

    # ``observability.pricing_overlay_path`` overlays the shipped pricing
    # table BEFORE the orchestrator is constructed so every downstream
    # cost computation (including litellm's fallback path) sees the
    # merged rates. Config-file driven; no CLI flag.
    if run_config.observability and run_config.observability.pricing_overlay_path:
        pricing.reload_pricing(overlay_path=Path(run_config.observability.pricing_overlay_path))

    _print_runtime_banner(
        console=console,
        runtime_choice=run_config.orchestrator.runtime,
        source="cli-flag" if runtime is not None else "config",
    )

    console.print(f"[green]Output base: {run_config.evaluation.output_dir}[/green]")

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

    # Fallback-model chain lives on ``models.agent.fallbacks`` in the run
    # config (list of ModelConfig entries, in order). Empty list → no
    # fallback client, orchestrator constructs a bare LLMClient.
    agent_cfg = run_config.models.get("agent")
    fallback_chain: list[ModelConfig] = list(agent_cfg.fallbacks) if agent_cfg is not None else []

    if dry_run:
        _run_dry_run(
            run_config=run_config,
            project=project,
            dry_run_samples=_DEFAULT_DRY_RUN_SAMPLES,
        )
        return

    display_mode = ctx.find_root().obj.get("display_mode", DisplayMode.PLAIN)

    if resume:
        assert run_dir_option is not None
        run_id, run_dir = resolve_resume_run_directory(Path(run_dir_option))
        state_manager = RunStateManager(run_dir)
        plan = state_manager.describe_resume_plan()
        if plan is None:
            raise click.ClickException(
                f"--resume: {run_dir}/run_state.json is missing; --run-dir must "
                "point at a completed prepare or a prior run"
            )
        if plan.is_complete:
            console.print(
                f"[muted]→[/muted] Nothing to do; run already complete "
                f"({plan.completed}/{plan.total} completed)"
            )
            emit_artifact_path(run_dir)
            return
        console.print(
            f"[bold]Resuming:[/bold] {plan.already_done}/{plan.total} completed, "
            f"{plan.to_retry} to retry"
        )
    else:
        run_id, run_dir = resolve_run_directory(run_config.evaluation.output_dir)
    print_run_start_banner(run_id=run_id, run_dir=run_dir, console=console, resumed=resume)

    # Resolve the cost budget once — feeds both the composite budget the
    # orchestrator drives and the live panel's amber/red threshold logic.
    # ``effective_max_budget_usd`` folds --cost-limit (via
    # ``compute.max_budget_usd``) and the pre-existing config field.
    cost_budget_usd = run_config.effective_max_budget_usd
    budget = make_budget(
        cost_limit_usd=cost_budget_usd,
        time_limit_seconds=time_limit_seconds,
        sample_limit=None,
    )

    # ``models.agent.fallbacks`` is realised as a per-invocation
    # ``agent_client_factory`` that wraps the primary agent model in a
    # :class:`FallbackLLMClient`. When the list is empty the seam stays
    # ``None`` and the orchestrator constructs a bare :class:`LLMClient`.
    agent_client_factory: Callable[[ModelConfig], LLMClient] | None = None
    if fallback_chain:

        def _fallback_factory(primary: ModelConfig) -> LLMClient:
            return FallbackLLMClient(primary=primary, fallbacks=fallback_chain)  # type: ignore[return-value]

        agent_client_factory = _fallback_factory
        console.print(
            f"[cyan]Fallback chain: {', '.join(f'{m.provider}/{m.name}' for m in fallback_chain)}[/cyan]"
        )

    start_ts = time.monotonic()
    success = False
    output_dir: Path | None = None
    try:
        with LiveRunDisplay.for_mode(display_mode, cost_budget_usd=cost_budget_usd) as display:
            # Create orchestrator with flags. Pass the resolved project so
            # the adapter picks up project.task_defaults on task load. The
            # display's event sink threads through OrchestratorDeps down to
            # the runner.
            orchestrator = Orchestrator(
                run_config,
                resume=resume,
                verbose=verbose,
                strict=strict,
                project=project,
                deps=OrchestratorDeps(
                    events=display.events,
                    budget=budget,
                    agent_client_factory=agent_client_factory,
                ),
            )

            # NB: no `console.print` calls between LiveRunDisplay.__enter__ and
            # __exit__. Every out-of-band write to the shared console while
            # Rich Live is active destabilises its cursor math and causes
            # panel copies to stack (visible bug). The panel is authoritative
            # during a run — status flows through phase_changed / run_started
            # already. Error paths use `logger.error` so `_LogSink` forwards
            # the WARNING+ to the wrapped stream above the panel.
            orchestrator.load_tasks()

            if not orchestrator.tasks:
                logging.getLogger("tolokaforge.dx").error(
                    "No tasks found — check tasks_glob in the run config"
                )
                raise SystemExit(1)

            output_dir = orchestrator.run(run_id=run_id, output_dir=run_dir)
        success = True
    finally:
        stopped_reason = _read_limit_hit_reason(output_dir if output_dir is not None else run_dir)
        print_run_end_banner(
            run_id=run_id,
            run_dir=run_dir,
            duration_seconds=time.monotonic() - start_ts,
            success=success,
            console=console,
            stopped_reason=stopped_reason,
        )
    emit_artifact_path(output_dir)


def _print_rejudge_summary(
    outcomes: list[TrialReplayOutcome], *, replay_id: str, source: Path, dry_run: bool
) -> None:
    """Print the batch disposition per trial + the aggregate counts."""
    label = "Would re-judge" if dry_run else "Re-judged"
    for outcome in outcomes:
        rel = outcome.bundle
        if outcome.status is ReplayOutcomeStatus.SKIPPED_NOT_APPLICABLE:
            console.print(f"[yellow]skip (not applicable)[/yellow] {rel}")
        elif outcome.status is ReplayOutcomeStatus.FAILED:
            console.print(f"[red]failed[/red] {rel} — {outcome.reason}")
        else:
            prov = outcome.provenance
            model = prov.judge_model if prov else "?"
            console.print(
                f"[green]{label.lower()}[/green] {rel} "
                f"[dim](judge={model}, rubric={prov.rubric_source.value if prov else '?'}, "
                f"kb={prov.knowledge_search_mode.value if prov else '?'}, "
                f"fidelity={prov.fidelity_mode.value if prov else '?'})[/dim]"
            )

    eligible = sum(
        o.status in (ReplayOutcomeStatus.REPLAYED, ReplayOutcomeStatus.WOULD_REPLAY)
        for o in outcomes
    )
    skipped = sum(o.status is ReplayOutcomeStatus.SKIPPED_NOT_APPLICABLE for o in outcomes)
    failed = sum(o.status is ReplayOutcomeStatus.FAILED for o in outcomes)
    console.print(
        f"\n[bold]{label}:[/bold] {eligible} eligible, "
        f"{skipped} skipped-not-applicable, {failed} failed-with-reason"
    )
    if not dry_run and eligible:
        console.print(f"Replay artifacts: {source / 'replays' / replay_id}")


def _print_replay_report(report: ReplayReport) -> None:
    """Print the per-run comparison summary (agreement + deltas + judge spend)."""
    rate = "n/a" if report.agreement_rate is None else f"{report.agreement_rate:.1%}"
    delta = (
        "n/a"
        if report.aggregate_llm_judge_delta is None
        else f"{report.aggregate_llm_judge_delta:+.3f}"
    )
    console.print(
        f"[bold]Agreement:[/bold] {rate} "
        f"({report.criteria_agreed}/{report.criteria_compared} criteria); "
        f"[bold]aggregate llm_judge delta:[/bold] {delta}"
    )
    usage = report.replay_usage
    console.print(
        f"[bold]Judge spend:[/bold] ${usage.cost_usd:.4f} "
        f"({usage.calls} calls, {usage.prompt_tokens}+{usage.completion_tokens} tokens)"
    )
    console.print(f"[dim]{report.carried_components}[/dim]")


@cli.command(name="rejudge")
@click.option(
    "--source",
    required=True,
    type=click.Path(exists=True, file_okay=False),
    help=(
        "Recorded run dir (trials/<task>/<idx> subtree), a flat collection of trial "
        "bundle dirs, or a single bundle dir. A directory is a bundle iff it contains "
        "grade.yaml + task.yaml."
    ),
)
@click.option(
    "--trial",
    default=None,
    type=click.Path(exists=True, file_okay=False),
    help="Re-judge a single bundle dir instead of the whole --source (default: whole source).",
)
@click.option(
    "--judge-model",
    default=None,
    help=(
        "Override the judge model as <provider>/<model> "
        "(e.g. openrouter/openai/gpt-4.1-mini), temperature 0. "
        "Default: the recorded model_config.judge."
    ),
)
@click.option(
    "--grading",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help=(
        "Override the rubric with a supplied grading.yaml (or a bare rubric mapping). "
        "Required for old bundles that did not record a rubric. Default: the recorded rubric."
    ),
)
@click.option(
    "--knowledge-search",
    "knowledge_search",
    type=click.Choice([m.value for m in KnowledgeSearchMode]),
    default=KnowledgeSearchMode.RECORDED.value,
    help=(
        "Judge knowledge-search gating: 'recorded' honours the bundle's recorded "
        "gating, 'on'/'off' force it. Default: recorded."
    ),
)
@click.option(
    "--replay-id",
    default=None,
    help="Name for this replay's artifact subdirectory (default: timestamped id).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Discover + classify + resolve inputs and print what would replay, spending nothing.",
)
def rejudge(
    source: str,
    trial: str | None,
    judge_model: str | None,
    grading: str | None,
    knowledge_search: str,
    replay_id: str | None,
    dry_run: bool,
):
    """Re-judge the rubric stage of recorded trials offline (judge-only spend).

    Re-executes only the rubric judge over recorded trajectories — no agent re-run,
    no live services — so judge changes (schema, prompt, wording, model) can be
    A/B-tested against a recorded run. Execution is sequential with no concurrency
    cap; inspect --dry-run first. Exits non-zero when any trial fails to classify
    or reconstruct (the report for the replayed subset is still written). See
    docs/JUDGE_REPLAY.md.
    """
    source_path = Path(source)
    replay_id = replay_id or f"replay_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    grading_override = load_grading_override(Path(grading)) if grading else None

    console.print(f"[bold blue]Re-judging trials under {source_path}...[/bold blue]")
    outcomes = run_replay_batch(
        source_path,
        replay_id=replay_id,
        trial=Path(trial) if trial else None,
        grading_override=grading_override,
        judge_model_override=judge_model,
        knowledge_search=KnowledgeSearchMode(knowledge_search),
        dry_run=dry_run,
    )
    _print_rejudge_summary(outcomes, replay_id=replay_id, source=source_path, dry_run=dry_run)

    if not dry_run:
        report = emit_replay_report(outcomes, source=source_path, replay_id=replay_id)
        if report is not None:
            _print_replay_report(report)

    if any(o.status is ReplayOutcomeStatus.FAILED for o in outcomes):
        raise SystemExit(1)


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
@click.pass_context
def prepare(
    ctx: click.Context,
    config: str,
    run_dir: str,
    reset_queue: bool,
    verbose: bool,
    strict: bool,
    presets_file: str | None,
):
    """Prepare a queue-backed run directory for distributed workers."""
    if verbose:
        _bump_console_debug_if_allowed(ctx)

    console.print(f"[bold blue]Preparing run from config {config}...[/bold blue]")
    config_data, project = load_effective_run_config(Path(config))
    run_config = construct_config(RunConfig, config_data, source=Path(config))

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
    emit_artifact_path(run_dir)


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
@click.pass_context
def worker(
    ctx: click.Context,
    config: str,
    run_dir: str,
    max_attempts: int | None,
    verbose: bool,
    strict: bool,
    presets_file: str | None,
):
    """Run a queue worker process (distributed execution mode)."""
    if verbose:
        _bump_console_debug_if_allowed(ctx)

    console.print(f"[bold blue]Loading worker config from {config}...[/bold blue]")
    config_data, project = load_effective_run_config(Path(config))
    run_config = construct_config(RunConfig, config_data, source=Path(config))

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

    Reads the ``metrics.yaml`` usage shape: ``usage.prompt_tokens`` /
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
        run_config = construct_config(RunConfig, config_data, source=Path(config))
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


def _complete_run_ids(ctx: click.Context, param: click.Parameter, incomplete: str) -> list[str]:
    """Tab-completion for ``browse``'s ``RUN_ID_OR_PATH`` argument.

    Enumerates directory names under the resolved results root (the
    ``--results-root`` flag if already typed, else ``TOLOKAFORGE_RESULTS_ROOT``
    env var, else ``./results``) and returns those that share the
    ``incomplete`` prefix. Best-effort — missing directory, unreadable
    entries, and OS errors return an empty list so tab-completion never
    raises inside the shell.

    Works in the ``tolokaforge`` REPL out of the box via ``click-repl``'s
    ``prompt-toolkit`` integration. For Bash / Zsh outside the REPL, users
    run the standard Click completion setup once:
    ``eval "$(_TOLOKAFORGE_COMPLETE=bash_source tolokaforge)"``.
    """
    try:
        root_str = (
            ctx.params.get("results_root")
            or os.environ.get("TOLOKAFORGE_RESULTS_ROOT")
            or "results"
        )
        root = Path(root_str)
        if not root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in root.iterdir()
            if entry.is_dir() and entry.name.startswith(incomplete)
        )
    except OSError:
        return []


@cli.command()
@click.argument("run_id_or_path", metavar="RUN_ID_OR_PATH", shell_complete=_complete_run_ids)
@click.option(
    "--results-root",
    "results_root",
    type=click.Path(exists=False, file_okay=False),
    default=None,
    help=(
        "Root directory to search when RUN_ID_OR_PATH is a bare run-id. "
        "Defaults to ./results (or the TOLOKAFORGE_RESULTS_ROOT env var)."
    ),
)
def browse(run_id_or_path: str, results_root: str | None) -> None:
    """Open a run's output directory in the OS default handler.

    Accepts either an absolute path to a run directory OR a bare run-id.
    A bare run-id is resolved against ``./results/`` (override with
    ``--results-root`` or the ``TOLOKAFORGE_RESULTS_ROOT`` env var). Uses
    ``webbrowser.open`` under a ``file://`` URL so the OS picks the right
    handler (Finder on macOS, xdg-open on Linux, Explorer on Windows).

    This is the command the ``→ Browse: tolokaforge browse <run-id>``
    line in the end banner refers to. Stdout stays empty; the resolved
    path is emitted on stderr as a status line.
    """
    import webbrowser

    candidate = Path(run_id_or_path)
    resolved: Path | None = None
    if candidate.is_absolute() and candidate.is_dir():
        resolved = candidate
    else:
        root_str = results_root or os.environ.get("TOLOKAFORGE_RESULTS_ROOT", "results")
        root = Path(root_str)
        candidate_under_root = root / run_id_or_path
        if candidate_under_root.is_dir():
            resolved = candidate_under_root.resolve()
        elif candidate.is_dir():
            resolved = candidate.resolve()

    if resolved is None:
        raise click.ClickException(
            f"No run directory found for {run_id_or_path!r}. "
            f"Pass an absolute path, or use --results-root to point at "
            f"the parent directory holding the run-id."
        )

    url = resolved.as_uri() + ("/" if not resolved.as_uri().endswith("/") else "")
    console.print(f"[muted]Opening[/muted] {url}")
    webbrowser.open(url)


if __name__ == "__main__":
    cli()
