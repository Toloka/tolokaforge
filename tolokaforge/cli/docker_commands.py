"""Docker CLI commands for TolokaForge.

Provides `tolokaforge docker build|up|down|status` subcommands as replacements
for docker-compose.yaml and scripts/release/build_docker_images.sh.

Uses lazy imports to avoid requiring docker dependency at CLI registration time.
"""

from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

console = Console()


@click.group()
def docker():
    """Manage Docker images and service stacks."""


@docker.command()
@click.option("--core", is_flag=True, help="Build only core images (db-service + runner)")
@click.option("--service", "service_name", default=None, help="Build a single service image")
@click.option("--force", is_flag=True, help="Force rebuild even if cached")
def build(core: bool, service_name: str | None, force: bool):
    """Build Docker images for TolokaForge services.

    Uses content-hash caching: only rebuilds when Dockerfile or context
    files change.
    """
    from tolokaforge.docker.builder import (
        build_all_images,
        build_image,
    )

    try:
        if service_name:
            console.print(f"[bold blue]Building image for service: {service_name}[/bold blue]")
            image = build_image(service_name, force=force)
            console.print(f"[green]✓ {service_name} → {image.full_tag}[/green]")
        else:
            label = "core" if core else "all"
            console.print(f"[bold blue]Building {label} Docker images...[/bold blue]")
            images = build_all_images(core_only=core, force=force)

            for name, image in images.items():
                console.print(f"  [green]✓ {name} → {image.full_tag}[/green]")

            console.print(f"\n[bold green]✓ Built {len(images)} images[/bold green]")

    except Exception as e:
        console.print(f"[red]✗ Build failed: {e}[/red]")
        raise SystemExit(1) from e


@docker.command()
@click.option(
    "--profile",
    "profile",
    type=click.Choice(["core", "full", "test"]),
    default="core",
    help="Stack profile to start",
)
@click.option("--no-wait", is_flag=True, help="Don't wait for health checks")
@click.option("--build/--no-build", "do_build", default=True, help="Build images before starting")
def up(profile: str, no_wait: bool, do_build: bool):
    """Start Docker service stack.

    Starts services in dependency order with health check waiting.
    """
    from tolokaforge.docker.stacks import core_stack, full_stack, test_stack

    console.print(f"[bold blue]Starting {profile} stack...[/bold blue]")

    try:
        if profile == "full":
            stack = full_stack()
        elif profile == "test":
            stack = test_stack()
        else:
            stack = core_stack()

        stack.start_all(wait=not no_wait, build=do_build)

        # Display status
        statuses = stack.get_status()
        table = Table(title="Service Stack Status")
        table.add_column("Service", style="cyan")
        table.add_column("Container ID", style="dim")
        table.add_column("Status", style="green")
        table.add_column("Ports")

        for name, status in statuses.items():
            container_id = status.container_id[:12] if status.container_id else "N/A"
            ports_str = ", ".join(f"{cp}→{hp}" for cp, hp in status.ports.items())
            table.add_row(name, container_id, status.status, ports_str or "none")

        console.print(table)
        console.print("[bold green]✓ Stack is running[/bold green]")

    except Exception as e:
        console.print(f"[red]✗ Failed to start stack: {e}[/red]")
        raise SystemExit(1) from e


@docker.command()
@click.option("--volumes", is_flag=True, help="Also remove volumes")
def down(volumes: bool):
    """Stop and destroy Docker service stack.

    Stops all containers in reverse dependency order and removes them.
    """
    from tolokaforge.docker.stacks import core_stack

    console.print("[bold blue]Stopping service stack...[/bold blue]")

    try:
        # Create a stack and try to stop/destroy known containers
        # Since we don't persist stack state, we create a default and destroy
        stack = core_stack()
        stack.destroy(remove_networks=True, remove_volumes=volumes)
        console.print("[bold green]✓ Stack stopped and cleaned up[/bold green]")

    except Exception as e:
        console.print(f"[yellow]Warning during cleanup: {e}[/yellow]")
        console.print("[green]✓ Cleanup attempted[/green]")


@docker.command()
def status():
    """Show status of Docker services.

    Displays a Rich table showing container status, health, and ports.
    """
    try:
        import docker as docker_sdk

        client = docker_sdk.from_env()
    except ImportError:
        console.print("[red]Docker SDK not available. Install with: pip install docker[/red]")
        raise SystemExit(1)
    except Exception as e:
        console.print(f"[red]Cannot connect to Docker: {e}[/red]")
        raise SystemExit(1) from e

    # Find tolokaforge containers
    try:
        containers = client.containers.list(all=True, filters={"name": "tolokaforge"})
    except Exception as e:
        console.print(f"[red]Failed to list containers: {e}[/red]")
        raise SystemExit(1) from e

    if not containers:
        console.print("[yellow]No tolokaforge containers found[/yellow]")
        return

    table = Table(title="TolokaForge Docker Services")
    table.add_column("Name", style="cyan")
    table.add_column("Image", style="dim")
    table.add_column("Status", style="green")
    table.add_column("Ports")
    table.add_column("Created", style="dim")

    for container in sorted(containers, key=lambda c: c.name):
        # Parse port mappings
        ports = container.ports or {}
        port_strs = []
        for container_port, host_bindings in ports.items():
            if host_bindings:
                for binding in host_bindings:
                    port_strs.append(f"{binding['HostPort']}→{container_port}")
            else:
                port_strs.append(f"-→{container_port}")

        status_style = "green" if container.status == "running" else "red"
        table.add_row(
            container.name,
            container.image.tags[0] if container.image.tags else "unknown",
            f"[{status_style}]{container.status}[/{status_style}]",
            ", ".join(port_strs) or "none",
            str(container.attrs.get("Created", ""))[:19],
        )

    console.print(table)
