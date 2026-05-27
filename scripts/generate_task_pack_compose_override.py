#!/usr/bin/env python3
"""Generate docker-compose override for task-pack mounts.

Usage:
  uv run python scripts/generate_task_pack_compose_override.py \
    --config examples/native/coding/run_config.yaml \
    --output docker-compose.taskpacks.override.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from tolokaforge.core.mounts import (
    build_task_pack_mount_plan,
    compose_override_from_mount_plan,
    normalize_task_pack_paths,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Path to run config YAML")
    parser.add_argument(
        "--output",
        default="docker-compose.taskpacks.override.yaml",
        help="Output override compose file",
    )
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Run config not found: {config_path}")

    with config_path.open() as f:
        config = yaml.safe_load(f) or {}

    evaluation = config.get("evaluation", {}) or {}
    task_pack_values = evaluation.get("task_packs", []) or []
    if not isinstance(task_pack_values, list):
        raise ValueError("evaluation.task_packs must be a list of paths")

    try:
        task_packs = normalize_task_pack_paths([str(p) for p in task_pack_values], config_path)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1

    mount_plan = build_task_pack_mount_plan(task_packs)
    override = compose_override_from_mount_plan(mount_plan)

    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as f:
        yaml.safe_dump(override, f, sort_keys=False)

    print(f"Wrote {output_path}")
    if task_packs:
        print("Task packs mounted:")
        for host, container in zip(mount_plan.host_roots, mount_plan.container_roots):
            print(f" - {host} -> {container}")
        print(
            "Run with: docker compose -f docker-compose.yaml "
            f"-f {output_path} --profile test up --build --abort-on-container-exit"
        )
    else:
        print("No evaluation.task_packs in config; override contains empty task-pack mounts.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
