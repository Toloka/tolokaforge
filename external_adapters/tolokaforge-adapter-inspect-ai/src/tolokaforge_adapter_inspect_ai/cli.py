"""Command-line entry for running Inspect AI tasks through the adapter.

``tolokaforge-inspect list`` discovers tasks; ``tolokaforge-inspect run`` executes a
pack via the local bridge and writes tolokaforge-typed results. Model credentials are
resolved through :mod:`tolokaforge.secrets` (``--set-secret NAME=SECRET_KEY``), never
read from the environment directly; ``--set-env NAME=VALUE`` passes non-secret vars
(e.g. an OpenAI-compatible base URL) to the ``inspect eval`` subprocess.
"""

from __future__ import annotations

import os
from pathlib import Path

import typer

from tolokaforge.secrets import get_default
from tolokaforge_adapter_inspect_ai.adapter import InspectAiAdapter
from tolokaforge_adapter_inspect_ai.runner import run_pack

app = typer.Typer(
    help="Run Inspect AI tasks through the tolokaforge adapter.", add_completion=False
)


def _split_pair(pair: str) -> tuple[str, str]:
    if "=" not in pair:
        raise typer.BadParameter(f"expected NAME=VALUE, got {pair!r}")
    name, value = pair.split("=", 1)
    return name, value


def _build_env(set_env: list[str], set_secret: list[str]) -> dict[str, str]:
    env = dict(os.environ)  # inherit PATH etc. for the subprocess
    for pair in set_env:
        name, value = _split_pair(pair)
        env[name] = value
    if set_secret:
        secrets = get_default()
        for pair in set_secret:
            name, secret_key = _split_pair(pair)
            env[name] = secrets.get_secret_or_raise(secret_key)
    return env


@app.command("list")
def list_tasks(
    pack_dir: Path = typer.Argument(..., help="Directory of Inspect *.py task files."),
    tasks_glob: str | None = typer.Option(None, help="Glob narrowing the scan (relative to pack)."),
) -> None:
    """List the Inspect tasks discovered in a pack."""
    adapter = InspectAiAdapter({"inspect_task_dir": str(pack_dir), "tasks_glob": tasks_glob})
    for task_id in adapter.get_task_ids():
        typer.echo(f"{task_id}\t{adapter.inspect_task(task_id).file}")


@app.command("run")
def run(
    pack_dir: Path = typer.Argument(..., help="Directory of Inspect *.py task files."),
    model: str = typer.Option(..., help="Inspect model, e.g. litellm-proxy/<m> or mockllm/model."),
    output_dir: Path = typer.Option(Path("runs/inspect"), help="Where results are written."),
    task_id: list[str] | None = typer.Option(None, "--task-id", help="Run only these tasks."),
    tasks_glob: str | None = typer.Option(None, help="Glob narrowing the scan (relative to pack)."),
    limit: int | None = typer.Option(None, help="Cap samples per task."),
    timeout: float | None = typer.Option(None, help="Per-task subprocess timeout (seconds)."),
    set_env: list[str] = typer.Option([], "--set-env", help="NAME=VALUE for the eval subprocess."),
    set_secret: list[str] = typer.Option(
        [], "--set-secret", help="NAME=SECRET_KEY resolved via tolokaforge.secrets."
    ),
) -> None:
    """Run a pack of Inspect tasks and write normalized results."""
    env = _build_env(set_env, set_secret)
    results = run_pack(
        pack_dir,
        model=model,
        output_dir=output_dir,
        env=env,
        task_ids=task_id or None,
        tasks_glob=tasks_glob,
        limit=limit,
        timeout=timeout,
    )
    for result in results:
        status = "PASS" if result.grade.binary_pass else "FAIL"
        typer.echo(f"{result.task_id}: {status} score={result.grade.score:.3f}")
    passed = sum(1 for r in results if r.grade.binary_pass)
    typer.echo(f"{passed}/{len(results)} passed -> {output_dir / 'summary.json'}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
