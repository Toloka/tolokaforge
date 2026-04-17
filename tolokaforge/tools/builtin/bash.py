"""Bash tool for executing shell commands"""

import re
import subprocess
from pathlib import Path
from typing import Any

from tolokaforge.tools.registry import Tool, ToolCategory, ToolPolicy, ToolResult


class BashTool(Tool):
    """Execute bash commands with restrictions"""

    def __init__(
        self,
        workdir: str | Path = "/work",
        allowed_commands: list[str] | None = None,
    ):
        # Backward compatibility with the old positional signature BashTool(allowed_commands)
        if isinstance(workdir, list) and allowed_commands is None:
            allowed_commands = workdir
            workdir = "/work"

        policy = ToolPolicy(
            timeout_s=30.0,
            category=ToolCategory.COMPUTE,
            visibility=["agent"],
        )
        super().__init__(
            name="bash",
            description="Execute a bash command. Limited to allowed commands and timeouts.",
            policy=policy,
        )
        self.workdir = Path(workdir)
        # Default allowlist - can be extended
        self.allowed_patterns = allowed_commands or [
            r"^ls$",
            r"^ls\s.*",
            r"^cat$",
            r"^cat\s.*",
            r"^grep\s.*",
            r"^find\s.*",
            r"^echo\s.*",
            r"^pwd$",
            r"^whoami$",
            r"^date$",
            r"^python\s.*",
            r"^python3\s.*",
            r"^pytest\s.*",
            r"^mkdir\s.*",
            r"^touch\s.*",
            r"^cp\s.*",
            r"^mv\s.*",
        ]

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The bash command to execute",
                        }
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
            },
        }

    def _is_allowed(self, command: str) -> bool:
        """Check if command matches allowlist"""
        command_head = command.strip().splitlines()[0] if command.strip() else ""
        return any(re.match(pattern, command_head) for pattern in self.allowed_patterns)

    def execute(self, command: str) -> ToolResult:
        """Execute bash command"""
        # Validate command
        if not self._is_allowed(command):
            return ToolResult(
                success=False,
                output="",
                error=f"Command not allowed: {command}",
            )

        try:
            workdir = self.workdir if self.workdir.exists() else Path.cwd()

            # Normalize container path aliases to workdir.
            rewritten = command
            rewritten = rewritten.replace("/env/fs/agent-visible", str(workdir))
            rewritten = rewritten.replace("/work", str(workdir))

            result = subprocess.run(
                rewritten,
                shell=True,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=self.policy.timeout_s,
            )

            return ToolResult(
                success=result.returncode == 0,
                output=result.stdout if result.returncode == 0 else result.stderr,
                error=result.stderr if result.returncode != 0 else None,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                success=False,
                output="",
                error=f"Command timed out after {self.policy.timeout_s}s",
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Execution failed: {str(e)}",
            )
