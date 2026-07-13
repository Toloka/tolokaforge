# Copyright 2025 The Harbor Authors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0

"""Claude Code command construction adapted from Harbor's installed adapter."""

from __future__ import annotations

from typing import Any


def build_claude_command(
    instruction: str,
    system_prompt: str,
    flags: dict[str, Any],
    *,
    mcp_config_path: str,
) -> list[str]:
    """Build an argv-only headless command; credentials never enter argv."""

    permission_mode = str(flags.get("permission_mode", "bypassPermissions"))
    command = [
        "claude",
        "--print",
        "--bare",
        "--verbose",
        "--output-format",
        "stream-json",
        "--strict-mcp-config",
        "--mcp-config",
        mcp_config_path,
        "--tools",
        "",
        "--permission-mode",
        permission_mode,
    ]
    if permission_mode == "bypassPermissions":
        command.append("--dangerously-skip-permissions")
    for flag, cli_name in (
        ("model", "--model"),
        ("effort", "--effort"),
        ("max_budget_usd", "--max-budget-usd"),
    ):
        value = flags.get(flag)
        if value is not None:
            command.extend([cli_name, str(value)])
    append_prompt = flags.get("append_system_prompt")
    if append_prompt:
        system_prompt = f"{system_prompt}\n\n{append_prompt}"
    if system_prompt:
        command.extend(["--append-system-prompt", system_prompt])
    command.append(instruction)
    return command
