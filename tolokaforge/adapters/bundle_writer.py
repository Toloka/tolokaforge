"""Serialise a NativeTaskBundle to disk as a native task directory.

Usage::

    from tolokaforge.adapters.base import NativeTaskBundle
    from tolokaforge.adapters.bundle_writer import write_bundle

    bundle = adapter.convert_to_native(task_id)
    task_dir = write_bundle(bundle, output_dir=Path("converted"), task_id=task_id)
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import yaml

from tolokaforge.adapters.base import NativeTaskBundle
from tolokaforge.core.logging import get_logger

logger = get_logger(__name__)


def write_bundle(
    bundle: NativeTaskBundle,
    output_dir: Path,
    task_id: str,
) -> Path:
    """Write a :class:`NativeTaskBundle` to disk as a native task directory.

    Creates the following structure under *output_dir*::

        {output_dir}/{task_id}/
        ├── task.yaml
        ├── grading.yaml
        ├── initial_state.json
        ├── system_prompt.md
        └── fixtures/
            ├── tools.json
            ├── golden_actions.json
            ├── unstable_fields.json
            └── metadata.json

    Args:
        bundle: The conversion result to serialise.
        output_dir: Parent directory that will contain the task folder.
        task_id: Used as the directory name; also injected into
                 ``task_config["task_id"]`` if missing.

    Returns:
        Absolute :class:`Path` to the created ``{task_id}/`` directory.
    """
    task_dir = Path(output_dir) / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    # Ensure task_id is present in task_config
    task_config = dict(bundle.task_config)
    task_config.setdefault("task_id", task_id)

    # Write task.yaml
    _write_yaml(task_dir / "task.yaml", task_config)

    # Write grading.yaml
    _write_yaml(task_dir / "grading.yaml", bundle.grading_config)

    # Write initial_state.json
    _write_json(task_dir / "initial_state.json", bundle.initial_state)

    # Write system_prompt.md
    (task_dir / "system_prompt.md").write_text(bundle.system_prompt or "", encoding="utf-8")

    # Write fixtures directory
    fixtures_dir = task_dir / "fixtures"
    fixtures_dir.mkdir(parents=True, exist_ok=True)

    # Separate well-known fixture keys from the rest
    tools = bundle.fixtures.get("tools", [])
    golden_actions = bundle.fixtures.get("golden_actions", [])
    unstable_fields = bundle.fixtures.get("unstable_fields", [])

    _write_json(fixtures_dir / "tools.json", tools)
    _write_json(fixtures_dir / "golden_actions.json", golden_actions)
    _write_json(fixtures_dir / "unstable_fields.json", unstable_fields)
    _write_json(fixtures_dir / "metadata.json", bundle.metadata)

    # Write any additional fixture keys (not tools/golden_actions/unstable_fields)
    extra_keys = set(bundle.fixtures.keys()) - {"tools", "golden_actions", "unstable_fields"}
    for key in sorted(extra_keys):
        _write_json(fixtures_dir / f"{key}.json", bundle.fixtures[key])

    logger.info(
        "Wrote native task bundle",
        task_id=task_id,
        path=str(task_dir),
    )
    return task_dir.resolve()


def write_domain_bundle(
    mcp_core_src: Path,
    tools_library_src: Path,
    tool_registry: dict[str, dict],
    system_prompt: str,
    domain_manifest: dict,
    output_dir: Path,
    allowed_toolsets: set[str] | None = None,
    docindex_src: Path | None = None,
) -> Path:
    """Write shared domain resources to ``_domain/`` directory.

    Copies:

    - Full mcp_core library (``mcp_core_src/mcp_core/`` → ``output_dir/mcp_core/``)
    - Tool library source filtered by *allowed_toolsets*
      (``tools_library_src/mcp_tools_library/`` → ``output_dir/tools/mcp_tools_library/``)
    - ``tool_registry.json``
    - ``system_prompt.md``
    - ``domain_manifest.yaml``
    - Docindex directory (``docindex_src/`` → ``output_dir/docindex/``) when
      *docindex_src* is provided and exists.

    The function is **idempotent** — if *output_dir* already exists, it is
    returned immediately without overwriting anything.

    Args:
        mcp_core_src: Path to ``mcp_core/src`` (contains ``mcp_core/`` package).
        tools_library_src: Path to ``mcp-tools-library/src`` (contains
            ``mcp_tools_library/`` package).
        tool_registry: Mapping of *tool_name* →
            ``{toolset, module_path, class_name, invocation_style}``.
        system_prompt: Content for ``system_prompt.md``.
        domain_manifest: Metadata dict written as ``domain_manifest.yaml``.
        output_dir: The ``_domain/`` directory to write to.
        allowed_toolsets: If set, only copy toolset subdirectories whose
            dotted module path (e.g. ``mcp_tools_library.retail.zendesk``)
            is in this set.
        docindex_src: Optional path to the domain's ``docindex/`` directory
            containing ``.md`` files for TypeSense knowledge base.

    Returns:
        Resolved path to the ``_domain/`` directory.
    """
    output_dir = Path(output_dir)

    # Idempotent: skip if _domain/ already exists
    if output_dir.exists():
        logger.info("Domain bundle already exists, skipping", path=str(output_dir))
        return output_dir.resolve()

    output_dir.mkdir(parents=True, exist_ok=True)

    # --- Copy mcp_core library ---
    mcp_core_pkg = mcp_core_src / "mcp_core"
    if mcp_core_pkg.is_dir():
        shutil.copytree(mcp_core_pkg, output_dir / "mcp_core")
        logger.debug("Copied mcp_core", src=str(mcp_core_pkg))
    else:
        logger.warning("mcp_core package not found", expected=str(mcp_core_pkg))

    # --- Copy tool library (optionally filtered) ---
    tools_pkg = tools_library_src / "mcp_tools_library"
    dest_tools = output_dir / "tools" / "mcp_tools_library"
    if tools_pkg.is_dir():
        if allowed_toolsets:
            # Extract relative directory paths from dotted module paths
            # e.g. "mcp_tools_library.external_retail_toolset.zendesk"
            #   → "external_retail_toolset/zendesk"
            allowed_dirs: set[str] = set()
            for ts in allowed_toolsets:
                parts = ts.split(".")
                if parts and parts[0] == "mcp_tools_library":
                    parts = parts[1:]
                if parts:
                    allowed_dirs.add(str(Path(*parts)))

            # Copy only matching subdirectories + top-level files (__init__.py, etc.)
            dest_tools.mkdir(parents=True, exist_ok=True)
            for item in tools_pkg.iterdir():
                if item.is_file():
                    shutil.copy2(item, dest_tools / item.name)
                elif item.is_dir():
                    # Check if this directory (or any prefix) is in allowed set
                    rel = item.name
                    if any(
                        ad == rel or ad.startswith(rel + "/") or rel.startswith(ad.split("/")[0])
                        for ad in allowed_dirs
                    ):
                        shutil.copytree(item, dest_tools / item.name)
                        logger.debug("Copied toolset dir", name=item.name)
        else:
            shutil.copytree(tools_pkg, dest_tools)
            logger.debug("Copied full mcp_tools_library", src=str(tools_pkg))
    else:
        logger.warning("mcp_tools_library package not found", expected=str(tools_pkg))

    # --- Write tool_registry.json ---
    _write_json(output_dir / "tool_registry.json", tool_registry)

    # --- Write system_prompt.md ---
    (output_dir / "system_prompt.md").write_text(system_prompt or "", encoding="utf-8")

    # --- Write domain_manifest.yaml ---
    _write_yaml(output_dir / "domain_manifest.yaml", domain_manifest)

    # --- Copy docindex (knowledge base markdown files) ---
    if docindex_src:
        docindex_src = Path(docindex_src)
        if docindex_src.is_dir():
            dest_docindex = output_dir / "docindex"
            shutil.copytree(docindex_src, dest_docindex)
            md_count = len(list(dest_docindex.glob("*.md")))
            logger.debug("Copied docindex", src=str(docindex_src), md_files=md_count)
        else:
            logger.debug("docindex_src does not exist, skipping", path=str(docindex_src))

    logger.info(
        "Wrote domain bundle",
        path=str(output_dir),
        tools_count=len(tool_registry),
    )
    return output_dir.resolve()


def _write_yaml(path: Path, data: Any) -> None:
    """Write *data* as YAML with sorted keys."""
    with open(path, "w", encoding="utf-8") as fh:
        yaml.dump(
            data,
            fh,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )


def _write_json(path: Path, data: Any) -> None:
    """Write *data* as pretty-printed JSON."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False, default=str)
        fh.write("\n")
