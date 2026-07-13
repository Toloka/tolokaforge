"""Asset-management CLI commands for TolokaForge.

Provides ``tolokaforge assets stamp [PATH]`` which walks
``assets.seeds.<name>`` entries in a project's ``project.yaml``,
computes ``sha256:<hex>`` over each referenced file, and writes the
``digest`` field back in place.

Uses lazy imports for heavy dependencies (yaml, project_loader) so
CLI registration stays cheap.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console

console = Console()

# Extension-based inference mirrors _SEED_KIND_BY_EXTENSION in the
# core models module. Kept as a private map here so the CLI can
# emit the same "kind" the loader would infer when coercing a
# bare-string entry to dict form before stamping.
_KIND_BY_EXTENSION: dict[str, str] = {
    ".sql": "sql_dump",
    ".rdb": "redis_dump",
}


@click.group()
def assets():
    """Manage project-level assets (seeds today)."""


@assets.command()
@click.argument(
    "project_path",
    type=click.Path(exists=True, file_okay=True, dir_okay=True, path_type=Path),
    default=Path.cwd(),
    required=False,
)
@click.option(
    "--check",
    "check_only",
    is_flag=True,
    help=(
        "Dry-run: print the diff of proposed digest updates and exit "
        "non-zero when anything is stale. Never writes. CI-friendly."
    ),
)
def stamp(project_path: Path, check_only: bool) -> None:
    """Compute SHA-256 digests for every ``assets.seeds.<name>`` entry
    in a project's ``project.yaml`` and write them back in place.

    PROJECT_PATH may be a directory (walks up looking for the enclosing
    ``project.yaml``) or an explicit ``project.yaml`` file. Defaults to
    the current working directory.

    Idempotent — re-running against a fully-stamped file is a no-op
    (verifies existing digests match; writes only when they don't).

    Note: YAML round-trip via PyYAML does not preserve comments. If the
    input file contains comments a warning is emitted before the
    write; inspect ``git diff`` before committing.
    """
    import yaml

    from tolokaforge.core.assets import compute_seed_digest

    project_yaml = _resolve_project_yaml(project_path)
    if project_yaml is None:
        console.print(
            f"[red]No project.yaml found at or above {project_path!s}[/red]",
            style="red",
        )
        sys.exit(1)

    project_dir = project_yaml.parent
    raw = yaml.safe_load(project_yaml.read_text()) or {}
    if not isinstance(raw, dict):
        console.print(f"[red]{project_yaml} is not a YAML mapping[/red]")
        sys.exit(1)

    seeds_container = _extract_seeds(raw)
    if not seeds_container:
        console.print(f"[yellow]{project_yaml}: no assets.seeds entries; nothing to stamp[/yellow]")
        return

    updates: list[tuple[str, str | None, str]] = []
    missing_files: list[tuple[str, Path]] = []
    changed = False

    for name, entry in list(seeds_container.items()):
        seed_path_str, existing_digest = _read_seed_entry(entry)
        if seed_path_str is None:
            console.print(
                f"[red]{project_yaml}: assets.seeds.{name} has no `path` field[/red]",
            )
            sys.exit(1)
        abs_path = _resolve_seed_path(seed_path_str, project_dir)
        if not abs_path.is_file():
            missing_files.append((name, abs_path))
            continue
        new_digest = compute_seed_digest(abs_path)
        updates.append((name, existing_digest, new_digest))
        if existing_digest != new_digest:
            changed = True
            if not check_only:
                seeds_container[name] = _write_seed_entry(entry, seed_path_str, new_digest)

    if missing_files:
        for name, path in missing_files:
            console.print(
                f"[red]{project_yaml}: assets.seeds.{name}.path does not exist: {path}[/red]",
            )
        sys.exit(1)

    if check_only:
        if changed:
            console.print(f"[yellow]{project_yaml}: digest(s) stale — would rewrite[/yellow]")
            for name, existing, new in updates:
                if existing != new:
                    console.print(f"  - {name}: {existing or '(unset)'} → {new}")
            sys.exit(1)
        console.print(f"[green]{project_yaml}: all seed digests match[/green]")
        return

    if not changed:
        console.print(f"[green]{project_yaml}: all seed digests already current[/green]")
        return

    if _has_comments(project_yaml):
        console.print(
            f"[yellow]Warning: {project_yaml} contains comments that PyYAML will "
            "strip on write. Inspect `git diff` before committing.[/yellow]",
        )

    project_yaml.write_text(
        yaml.safe_dump(raw, sort_keys=False, default_flow_style=False),
    )
    console.print(
        f"[green]{project_yaml}: wrote {sum(1 for _, e, n in updates if e != n)} digest(s)[/green]"
    )


def _resolve_project_yaml(project_path: Path) -> Path | None:
    """Return the ``project.yaml`` file for *project_path*.

    If *project_path* is already a file, return it verbatim. Otherwise
    walk up from the directory looking for ``project.yaml``.
    """
    from tolokaforge.core.project_loader import find_project_yaml

    if project_path.is_file():
        return project_path
    return find_project_yaml(project_path)


def _extract_seeds(raw: dict) -> dict | None:
    """Return the ``assets.seeds`` sub-mapping, or ``None`` when
    absent / malformed. Malformed shapes still return ``None`` (the
    caller treats it as "nothing to stamp") — schema validation at
    load time is a separate concern."""
    assets_section = raw.get("assets")
    if not isinstance(assets_section, dict):
        return None
    seeds = assets_section.get("seeds")
    if not isinstance(seeds, dict):
        return None
    return seeds


def _read_seed_entry(entry: object) -> tuple[str | None, str | None]:
    """Return ``(path, digest)`` from a seed entry in either shape.

    Bare-string shorthand ``"path/to/seed.sql"`` → ``("path/to/seed.sql", None)``.
    Dict form ``{"path": ..., "digest": ...}`` → ``(path, digest)``.
    Unknown shapes return ``(None, None)``.
    """
    if isinstance(entry, str):
        return entry, None
    if isinstance(entry, dict):
        path = entry.get("path")
        digest = entry.get("digest")
        return (
            path if isinstance(path, str) else None,
            digest if isinstance(digest, str) else None,
        )
    return None, None


def _write_seed_entry(entry: object, seed_path: str, new_digest: str) -> dict:
    """Return the dict form of *entry* with *new_digest* set.

    Coerces bare-string shorthand into dict form (inferring ``kind``
    from the file extension) — a string can't carry a digest, so
    stamping always emits dict form for touched entries. Untouched
    fields on an existing dict entry are preserved verbatim.
    """
    if isinstance(entry, dict):
        out = dict(entry)
        out["path"] = seed_path
        out["digest"] = new_digest
        # Ensure ``kind`` is present — SeedRef requires it in dict form.
        if "kind" not in out:
            inferred = _KIND_BY_EXTENSION.get(Path(seed_path).suffix.lower())
            if inferred is not None:
                out["kind"] = inferred
        return out
    # Bare-string entry — coerce to dict form.
    kind = _KIND_BY_EXTENSION.get(Path(seed_path).suffix.lower())
    if kind is None:
        # SeedRef's own load-time normaliser would fail loud here too;
        # match its shape.
        raise click.ClickException(
            f"cannot infer kind from bare-string seed path {seed_path!r} "
            f"(extension not in {sorted(_KIND_BY_EXTENSION)!r}). Rewrite "
            "as {path: ..., kind: ...} before stamping."
        )
    return {"path": seed_path, "kind": kind, "digest": new_digest}


def _resolve_seed_path(seed_path: str, project_dir: Path) -> Path:
    """Return an absolute path for a seed reference. Absolute paths
    pass through unchanged; relative paths anchor to *project_dir*."""
    p = Path(seed_path)
    if p.is_absolute():
        return p
    return (project_dir / p).resolve()


def _has_comments(path: Path) -> bool:
    """Best-effort check for ``#``-prefixed comment lines in *path*.
    Full YAML lexing isn't needed — the warning is advisory."""
    try:
        for line in path.read_text().splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                return True
    except OSError:
        pass
    return False
