"""Serialise a NativeTaskBundle to disk as a native task directory.

Usage::

    from tolokaforge.adapters.base import NativeTaskBundle
    from tolokaforge.adapters.bundle_writer import write_bundle

    bundle = adapter.convert_to_native(task_id)
    task_dir = write_bundle(bundle, output_dir=Path("converted"), task_id=task_id)

This module owns the *generic* native-format serialisation only. Adapters that
need to emit shared, cross-task resources (e.g. a ``_domain/`` bundle) do so by
overriding :meth:`tolokaforge.adapters.base.BaseAdapter.write_shared_resources`.
"""

from __future__ import annotations

import json
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


def _write_yaml(path: Path, data: Any) -> None:
    """Write *data* as YAML, preserving key order."""
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
