"""CLI commands for adapter management.

Provides the ``tolokaforge adapter`` command group, including
``tolokaforge adapter convert`` for converting external tasks to native
TolokaForge format.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console

console = Console()


@click.group()
def adapter():
    """Adapter management commands."""


@adapter.command()
@click.option("--name", required=True, help="Adapter name (tau, tlk_mcp_core)")
@click.option("--tasks-glob", required=True, help="Glob pattern for source tasks")
@click.option("--output", required=True, type=click.Path(), help="Output directory")
@click.option("--adapter-params", default="{}", help="JSON string of extra adapter params")
@click.option("--validate", "do_validate", is_flag=True, help="Validate converted output")
@click.option("--verbose", is_flag=True, help="Enable debug output")
def convert(
    name: str,
    tasks_glob: str,
    output: str,
    adapter_params: str,
    do_validate: bool,
    verbose: bool,
) -> None:
    """Convert external tasks to native TolokaForge format."""
    if verbose:
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)

    from tolokaforge.adapters import get_adapter
    from tolokaforge.adapters.bundle_writer import write_bundle, write_domain_bundle

    # Parse extra adapter params
    try:
        params: dict = json.loads(adapter_params)
    except json.JSONDecodeError as exc:
        console.print(f"[red]Invalid --adapter-params JSON: {exc}[/red]")
        raise SystemExit(1) from exc

    # Inject tasks_glob (or env_path for tau)
    if name == "tau":
        # Tau adapter expects env_path, not tasks_glob
        params.setdefault("env_path", tasks_glob)
    else:
        params["tasks_glob"] = tasks_glob

    # Instantiate adapter
    try:
        adapter_inst = get_adapter(name, params)
    except Exception as exc:
        console.print(f"[red]Failed to create adapter '{name}': {exc}[/red]")
        raise SystemExit(1) from exc

    # Discover task IDs
    try:
        task_ids = adapter_inst.get_task_ids()
    except Exception as exc:
        console.print(f"[red]Failed to discover tasks: {exc}[/red]")
        raise SystemExit(1) from exc

    if not task_ids:
        console.print("[yellow]No tasks found matching the pattern.[/yellow]")
        raise SystemExit(0)

    console.print(f"[bold blue]Converting {len(task_ids)} tasks → {output}[/bold blue]")

    output_path = Path(output)
    converted = 0
    errors = 0
    domain_written = False

    for task_id in task_ids:
        try:
            bundle = adapter_inst.convert_to_native(task_id)

            # Write shared domain bundle once (from first task's metadata)
            if not domain_written and bundle.metadata.get("source_adapter") == "tlk_mcp_core":
                try:
                    docindex_path = bundle.metadata.get("docindex_path", "")
                    write_domain_bundle(
                        mcp_core_src=Path(bundle.metadata["mcp_core_src"]),
                        tools_library_src=Path(bundle.metadata["tools_library_src"]),
                        tool_registry=bundle.metadata["tool_registry"],
                        system_prompt=bundle.metadata.get("system_prompt", ""),
                        domain_manifest={
                            "domain": bundle.metadata.get("domain"),
                            "adapter": "tlk_mcp_core",
                            "converted_at": datetime.now().isoformat(),
                        },
                        output_dir=output_path / "_domain",
                        allowed_toolsets=set(bundle.metadata.get("allowed_toolsets", [])) or None,
                        docindex_src=Path(docindex_path) if docindex_path else None,
                    )
                    console.print("  ✓ _domain/ (shared resources)", style="blue")
                except Exception as exc:
                    console.print(f"  ⚠ _domain/ write failed: {exc}", style="yellow")
                    if verbose:
                        import traceback

                        traceback.print_exc()
                domain_written = True

            write_bundle(bundle, output_path, task_id)
            converted += 1
            console.print(f"  ✓ {task_id}", style="green")
        except Exception as exc:
            errors += 1
            console.print(f"  ✗ {task_id}: {exc}", style="red")
            if verbose:
                import traceback

                traceback.print_exc()

    # Optional validation pass
    if do_validate and converted > 0:
        _validate_converted(output_path, task_ids, verbose)

    console.print(f"\nConverted {converted} tasks ({errors} errors)")
    if errors > 0:
        raise SystemExit(1)


def _validate_converted(output_path: Path, task_ids: list[str], verbose: bool) -> None:
    """Validate converted output by loading via NativeAdapter."""
    import yaml

    from tolokaforge.core.models import TaskConfig

    console.print("\n[bold blue]Validating converted output…[/bold blue]")
    valid = 0
    invalid = 0

    for task_id in task_ids:
        task_yaml = output_path / task_id / "task.yaml"
        if not task_yaml.exists():
            continue
        try:
            with open(task_yaml) as fh:
                data = yaml.safe_load(fh)
            TaskConfig(**data)
            valid += 1
            if verbose:
                console.print(f"  ✓ valid: {task_id}", style="green")
        except Exception as exc:
            invalid += 1
            console.print(f"  ✗ invalid: {task_id}: {exc}", style="red")

    console.print(f"  Validation: {valid} valid, {invalid} invalid")
