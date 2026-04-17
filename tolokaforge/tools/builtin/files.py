"""File manipulation tools"""

import fnmatch
import re
import shutil
from pathlib import Path
from typing import Any

from tolokaforge.tools.registry import Tool, ToolCategory, ToolPolicy, ToolResult


class ReadFileTool(Tool):
    """Read file contents"""

    def __init__(self, base_path: str = "/env/fs/agent-visible"):
        policy = ToolPolicy(
            timeout_s=10.0,
            category=ToolCategory.READ,
            visibility=["agent"],
        )
        super().__init__(
            name="read_file",
            description="Read the contents of a file",
            policy=policy,
        )
        self.base_path = Path(base_path)

    @staticmethod
    def _normalize_requested_path(path: str) -> Path:
        normalized = path.strip()
        if normalized.startswith("/env/fs/agent-visible/"):
            normalized = normalized[len("/env/fs/agent-visible/") :]
        elif normalized == "/env/fs/agent-visible":
            normalized = ""
        elif normalized.startswith("/work/"):
            normalized = normalized[len("/work/") :]
        elif normalized == "/work":
            normalized = ""
        return Path(normalized)

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to the file to read",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "1-based line number to start reading from (default: 1)",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of lines to return (default: all)",
                        },
                        "with_line_numbers": {
                            "type": "boolean",
                            "description": "Prefix each line with its 1-based line number (default: false)",
                        },
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        }

    def execute(
        self,
        path: str,
        offset: int | None = None,
        limit: int | None = None,
        with_line_numbers: bool = False,
    ) -> ToolResult:
        """Read file with optional line-oriented slicing"""
        try:
            # Ensure path is within allowed directory
            requested = self._normalize_requested_path(path)
            full_path = (self.base_path / requested).resolve()
            if not str(full_path).startswith(str(self.base_path.resolve())):
                return ToolResult(
                    success=False,
                    output="",
                    error="Path outside allowed directory",
                )

            if not full_path.exists():
                return ToolResult(
                    success=False,
                    output="",
                    error=f"File not found: {path}",
                )

            with open(full_path, encoding="utf-8") as f:
                lines = f.readlines()

            total_lines = len(lines)

            # Apply offset (1-based)
            start = 0
            if offset is not None:
                start = max(0, offset - 1)

            # Apply limit
            if limit is not None:
                end = start + limit
            else:
                end = total_lines

            selected = lines[start:end]

            # Optionally add line numbers
            if with_line_numbers:
                numbered = []
                for i, line in enumerate(selected, start=start + 1):
                    numbered.append(f"{i:>6}\t{line}")
                content = "".join(numbered)
            else:
                content = "".join(selected)

            return ToolResult(
                success=True,
                output=content,
                metadata={
                    "size": len(content),
                    "total_lines": total_lines,
                    "start_line": start + 1,
                    "lines_returned": len(selected),
                },
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Failed to read file: {str(e)}",
            )


class WriteFileTool(Tool):
    """Write file contents"""

    def __init__(self, base_path: str = "/env/fs/agent-visible"):
        policy = ToolPolicy(
            timeout_s=10.0,
            category=ToolCategory.WRITE,
            visibility=["agent"],
        )
        super().__init__(
            name="write_file",
            description="Write content to a file",
            policy=policy,
        )
        self.base_path = Path(base_path)

    @staticmethod
    def _normalize_requested_path(path: str) -> Path:
        normalized = path.strip()
        if normalized.startswith("/env/fs/agent-visible/"):
            normalized = normalized[len("/env/fs/agent-visible/") :]
        elif normalized == "/env/fs/agent-visible":
            normalized = ""
        elif normalized.startswith("/work/"):
            normalized = normalized[len("/work/") :]
        elif normalized == "/work":
            normalized = ""
        return Path(normalized)

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to the file to write",
                        },
                        "content": {
                            "type": "string",
                            "description": "Content to write to the file",
                        },
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
            },
        }

    def execute(self, path: str, content: str) -> ToolResult:
        """Write file"""
        try:
            # Ensure path is within allowed directory
            requested = self._normalize_requested_path(path)
            full_path = (self.base_path / requested).resolve()
            if not str(full_path).startswith(str(self.base_path.resolve())):
                return ToolResult(
                    success=False,
                    output="",
                    error="Path outside allowed directory",
                )

            # Create parent directories if needed
            full_path.parent.mkdir(parents=True, exist_ok=True)

            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)

            return ToolResult(
                success=True,
                output=f"File written successfully: {path}",
                metadata={"size": len(content)},
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Failed to write file: {str(e)}",
            )


class ListDirTool(Tool):
    """List directory contents"""

    def __init__(self, base_path: str = "/env/fs/agent-visible"):
        policy = ToolPolicy(
            timeout_s=10.0,
            category=ToolCategory.READ,
            visibility=["agent"],
        )
        super().__init__(
            name="list_dir",
            description="List contents of a directory",
            policy=policy,
        )
        self.base_path = Path(base_path)

    @staticmethod
    def _normalize_requested_path(path: str) -> Path:
        normalized = path.strip()
        if normalized.startswith("/env/fs/agent-visible/"):
            normalized = normalized[len("/env/fs/agent-visible/") :]
        elif normalized == "/env/fs/agent-visible":
            normalized = ""
        elif normalized.startswith("/work/"):
            normalized = normalized[len("/work/") :]
        elif normalized == "/work":
            normalized = ""
        return Path(normalized)

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to the directory to list",
                        }
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        }

    def execute(self, path: str) -> ToolResult:
        """List directory"""
        try:
            # Ensure path is within allowed directory
            requested = self._normalize_requested_path(path)
            full_path = (self.base_path / requested).resolve()
            if not str(full_path).startswith(str(self.base_path.resolve())):
                return ToolResult(
                    success=False,
                    output="",
                    error="Path outside allowed directory",
                )

            if not full_path.exists():
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Directory not found: {path}",
                )

            if not full_path.is_dir():
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Not a directory: {path}",
                )

            entries = []
            for item in full_path.iterdir():
                entry_type = "dir" if item.is_dir() else "file"
                entries.append(f"{entry_type}: {item.name}")

            return ToolResult(
                success=True,
                output="\n".join(entries) if entries else "(empty directory)",
                metadata={"count": len(entries)},
            )
        except Exception as e:
            return ToolResult(
                success=False,
                output="",
                error=f"Failed to list directory: {str(e)}",
            )


# ---------------------------------------------------------------------------
# Helper: shared path normalisation
# ---------------------------------------------------------------------------


def _normalize_path(path: str) -> Path:
    """Normalise container-style path aliases to a relative Path."""
    normalized = path.strip()
    for prefix in ("/env/fs/agent-visible/", "/env/fs/agent-visible", "/work/", "/work"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    return Path(normalized)


class _PathError(Exception):
    """Raised when a path fails safety checks."""


def _resolve_safe(base: Path, requested: str) -> Path:
    """Resolve *requested* under *base*. Raises _PathError on violation."""
    rel = _normalize_path(requested)
    full = (base / rel).resolve()
    if not str(full).startswith(str(base.resolve())):
        raise _PathError("Path outside allowed directory")
    return full


# ---------------------------------------------------------------------------
# replace_lines — core line-oriented editing primitive
# ---------------------------------------------------------------------------


class ReplaceLinesTool(Tool):
    """Replace an exact text span inside a file (atomic write)."""

    def __init__(self, base_path: str = "/env/fs/agent-visible"):
        policy = ToolPolicy(
            timeout_s=10.0,
            category=ToolCategory.WRITE,
            visibility=["agent"],
        )
        super().__init__(
            name="replace_lines",
            description=(
                "Replace an exact occurrence of old_text with new_text inside a file. "
                "The old_text must appear exactly once. The file is rewritten atomically."
            ),
            policy=policy,
        )
        self.base_path = Path(base_path)

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to the file to edit",
                        },
                        "old_text": {
                            "type": "string",
                            "description": "Exact text to find (must appear exactly once)",
                        },
                        "new_text": {
                            "type": "string",
                            "description": "Replacement text",
                        },
                    },
                    "required": ["path", "old_text", "new_text"],
                    "additionalProperties": False,
                },
            },
        }

    def execute(self, path: str, old_text: str, new_text: str) -> ToolResult:
        try:
            full_path = _resolve_safe(self.base_path, path)

            if not full_path.exists():
                return ToolResult(success=False, output="", error=f"File not found: {path}")

            content = full_path.read_text(encoding="utf-8")
            count = content.count(old_text)
            if count == 0:
                return ToolResult(success=False, output="", error="old_text not found in file")
            if count > 1:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"old_text appears {count} times — must be unique",
                )

            new_content = content.replace(old_text, new_text, 1)

            # Atomic write via temp file + rename
            tmp = full_path.with_suffix(full_path.suffix + ".tmp")
            tmp.write_text(new_content, encoding="utf-8")
            tmp.replace(full_path)

            return ToolResult(
                success=True,
                output=f"Replaced text in {path}",
                metadata={"size": len(new_content)},
            )
        except _PathError as e:
            return ToolResult(success=False, output="", error=str(e))
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Failed to replace text: {e}")


# ---------------------------------------------------------------------------
# Workspace ops: append_file, move_file, copy_file, delete_file
# ---------------------------------------------------------------------------


class AppendFileTool(Tool):
    """Append content to a file (creates if missing)."""

    def __init__(self, base_path: str = "/env/fs/agent-visible"):
        policy = ToolPolicy(timeout_s=10.0, category=ToolCategory.WRITE, visibility=["agent"])
        super().__init__(name="append_file", description="Append text to a file", policy=policy)
        self.base_path = Path(base_path)

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to the file"},
                        "content": {"type": "string", "description": "Content to append"},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
            },
        }

    def execute(self, path: str, content: str) -> ToolResult:
        try:
            full_path = _resolve_safe(self.base_path, path)
            full_path.parent.mkdir(parents=True, exist_ok=True)
            with open(full_path, "a", encoding="utf-8") as f:
                f.write(content)
            return ToolResult(success=True, output=f"Appended to {path}")
        except _PathError as e:
            return ToolResult(success=False, output="", error=str(e))
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Failed to append: {e}")


class MoveFileTool(Tool):
    """Move / rename a file or directory."""

    def __init__(self, base_path: str = "/env/fs/agent-visible"):
        policy = ToolPolicy(timeout_s=10.0, category=ToolCategory.WRITE, visibility=["agent"])
        super().__init__(name="move_file", description="Move or rename a file", policy=policy)
        self.base_path = Path(base_path)

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "description": "Source path"},
                        "destination": {"type": "string", "description": "Destination path"},
                    },
                    "required": ["source", "destination"],
                    "additionalProperties": False,
                },
            },
        }

    def execute(self, source: str, destination: str) -> ToolResult:
        try:
            src = _resolve_safe(self.base_path, source)
            dst = _resolve_safe(self.base_path, destination)
            if not src.exists():
                return ToolResult(success=False, output="", error=f"Source not found: {source}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            return ToolResult(success=True, output=f"Moved {source} -> {destination}")
        except _PathError as e:
            return ToolResult(success=False, output="", error=str(e))
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Failed to move: {e}")


class CopyFileTool(Tool):
    """Copy a file or directory."""

    def __init__(self, base_path: str = "/env/fs/agent-visible"):
        policy = ToolPolicy(timeout_s=10.0, category=ToolCategory.WRITE, visibility=["agent"])
        super().__init__(name="copy_file", description="Copy a file or directory", policy=policy)
        self.base_path = Path(base_path)

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "description": "Source path"},
                        "destination": {"type": "string", "description": "Destination path"},
                    },
                    "required": ["source", "destination"],
                    "additionalProperties": False,
                },
            },
        }

    def execute(self, source: str, destination: str) -> ToolResult:
        try:
            src = _resolve_safe(self.base_path, source)
            dst = _resolve_safe(self.base_path, destination)
            if not src.exists():
                return ToolResult(success=False, output="", error=f"Source not found: {source}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(str(src), str(dst))
            else:
                shutil.copy2(str(src), str(dst))
            return ToolResult(success=True, output=f"Copied {source} -> {destination}")
        except _PathError as e:
            return ToolResult(success=False, output="", error=str(e))
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Failed to copy: {e}")


class DeleteFileTool(Tool):
    """Delete a file or empty directory."""

    def __init__(self, base_path: str = "/env/fs/agent-visible"):
        policy = ToolPolicy(timeout_s=10.0, category=ToolCategory.WRITE, visibility=["agent"])
        super().__init__(name="delete_file", description="Delete a file", policy=policy)
        self.base_path = Path(base_path)

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to delete"},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        }

    def execute(self, path: str) -> ToolResult:
        try:
            full_path = _resolve_safe(self.base_path, path)
            if not full_path.exists():
                return ToolResult(success=False, output="", error=f"Not found: {path}")
            if full_path.is_dir():
                full_path.rmdir()  # only empty dirs
            else:
                full_path.unlink()
            return ToolResult(success=True, output=f"Deleted {path}")
        except _PathError as e:
            return ToolResult(success=False, output="", error=str(e))
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Failed to delete: {e}")


# ---------------------------------------------------------------------------
# grep_workspace — recursive text search + file-name glob
# ---------------------------------------------------------------------------


class GrepWorkspaceTool(Tool):
    """Search workspace files by content pattern or filename glob."""

    MAX_MATCHES = 200

    def __init__(self, base_path: str = "/env/fs/agent-visible"):
        policy = ToolPolicy(timeout_s=15.0, category=ToolCategory.READ, visibility=["agent"])
        super().__init__(
            name="grep_workspace",
            description=(
                "Search files in the workspace. "
                "Provide 'pattern' for content search (regex) and/or "
                "'file_glob' for filename matching. Returns file:line:match."
            ),
            policy=policy,
        )
        self.base_path = Path(base_path)

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Regex pattern to search in file contents",
                        },
                        "file_glob": {
                            "type": "string",
                            "description": "Glob pattern to filter filenames (e.g. '*.py')",
                        },
                        "path": {
                            "type": "string",
                            "description": "Subdirectory to search in (default: workspace root)",
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        }

    def execute(
        self,
        pattern: str | None = None,
        file_glob: str | None = None,
        path: str | None = None,
    ) -> ToolResult:
        try:
            if not pattern and not file_glob:
                return ToolResult(
                    success=False,
                    output="",
                    error="Provide at least one of 'pattern' or 'file_glob'",
                )

            search_root = self.base_path
            if path:
                search_root = _resolve_safe(self.base_path, path)

            if not search_root.exists():
                return ToolResult(success=False, output="", error="Search path not found")

            regex = re.compile(pattern) if pattern else None

            matches: list[str] = []
            base_resolved = self.base_path.resolve()

            for file_path in search_root.rglob("*"):
                if not file_path.is_file():
                    continue
                rel = str(file_path.relative_to(base_resolved))

                # Apply filename filter
                if file_glob and not fnmatch.fnmatch(file_path.name, file_glob):
                    continue

                # If only file_glob and no content pattern, list matching files
                if not regex:
                    matches.append(rel)
                    if len(matches) >= self.MAX_MATCHES:
                        break
                    continue

                # Content search
                try:
                    text = file_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue

                for lineno, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        matches.append(f"{rel}:{lineno}:{line.rstrip()}")
                        if len(matches) >= self.MAX_MATCHES:
                            break
                if len(matches) >= self.MAX_MATCHES:
                    break

            truncated = len(matches) >= self.MAX_MATCHES
            output = "\n".join(matches) if matches else "(no matches)"
            if truncated:
                output += f"\n... (truncated at {self.MAX_MATCHES} matches)"

            return ToolResult(
                success=True,
                output=output,
                metadata={"match_count": len(matches), "truncated": truncated},
            )
        except _PathError as e:
            return ToolResult(success=False, output="", error=str(e))
        except re.error as e:
            return ToolResult(success=False, output="", error=f"Invalid regex: {e}")
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Search failed: {e}")
