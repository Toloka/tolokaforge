"""Public materialization of one native task's exact active-case bundle."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

from tolokaforge.adapters.native import NativeAdapter
from tolokaforge.runner.models import TaskDescription


@dataclass(frozen=True)
class MaterializedNativeCase:
    """The adapter-produced runner tree for exactly one active testcase."""

    root: Path
    server_path: Path
    task: TaskDescription


def materialize_active_native_case(
    task_file: Path,
    destination: Path,
) -> MaterializedNativeCase:
    """Materialize the same active-case artifacts sent to the Docker runner.

    The NativeAdapter remains the sole authority for shared-domain resolution,
    sibling exclusion, and copying case-local state beside the shared server.
    """
    task_file = task_file.resolve()
    destination = destination.resolve()
    adapter = NativeAdapter(
        {
            "base_dir": str(task_file.parent),
            "tasks_glob": task_file.name,
        }
    )
    task_ids = adapter.get_task_ids()
    if len(task_ids) != 1:
        raise RuntimeError(f"expected exactly one native task at {task_file}")
    task = adapter.to_task_description(task_ids[0])
    destination.mkdir(parents=True, exist_ok=True)
    for relative, encoded in sorted(task.tool_artifacts.items()):
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError(f"native artifact path escapes active bundle: {relative}")
        output = destination / path
        if not output.resolve().is_relative_to(destination):
            raise RuntimeError(f"native artifact path escapes active bundle: {relative}")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(base64.b64decode(encoded, validate=True))

    mcp_ref = task.metadata.get("mcp_server_ref")
    if not isinstance(mcp_ref, str) or not mcp_ref:
        raise RuntimeError("active native task has no MCP server reference")
    server_path = destination / mcp_ref
    if not server_path.is_file():
        raise RuntimeError(f"active native bundle omitted MCP server: {mcp_ref}")
    return MaterializedNativeCase(
        root=destination,
        server_path=server_path,
        task=task,
    )
