"""File-editor tool matching Anthropic's ``str_replace_based_edit_tool`` contract.

Three roles live here, mirroring :mod:`tolokaforge.tools.persistent_shell`:

- :class:`StrReplaceEditorTool` is the *schema provider*. The builtin registry
  advertises its ``get_schema()`` to the LLM; it is never executed (the runner
  drives execution through ``StrReplaceEditorToolWrapper``).
- :class:`EditorBackend` is the engine :class:`typing.Protocol` — the shape both
  the local and the compose engines implement.
- :class:`LocalFilesystemEditor` is the local engine: in-process file operations
  rooted at a working directory.

The four commands ``view`` / ``create`` / ``str_replace`` / ``insert`` match the
Claude-4 editor variants (``text_editor_20250429`` / ``text_editor_20250728``);
``undo_edit`` is absent, as in those variants. Every ambiguous or destructive
operation fails loud by raising :class:`EditorError`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from tolokaforge.tools.registry import Tool, ToolCategory, ToolPolicy, ToolResult

_EDITOR_COMMANDS = ("view", "create", "str_replace", "insert")

# Middle-truncation budget for a single view's rendered output.
_MAX_OUTPUT_CHARS = 16384
_HEAD_CHARS = 8192
_TAIL_CHARS = 8192


class EditorError(Exception):
    """An editor operation failed loud (ambiguous, destructive, or out of range).

    Raised by an :class:`EditorBackend`; the runner-side wrapper converts it to a
    ``ToolExecutionError`` so the failure reaches the LLM for self-correction.
    """


@runtime_checkable
class EditorBackend(Protocol):
    """A file editor rooted at a working directory.

    Implementations differ only in *where* the files live (the local filesystem
    vs. a compose service container); the command semantics, path containment,
    and fail-loud conditions are the shared contract.
    """

    def view(self, path: str, view_range: list[int] | None = None) -> str:
        """Return line-numbered file content or a directory listing."""
        ...

    def create(self, path: str, file_text: str) -> str:
        """Create a new file; fail loud if *path* already exists."""
        ...

    def str_replace(self, path: str, old_str: str, new_str: str) -> str:
        """Replace the unique occurrence of *old_str*; fail loud on 0 or >1."""
        ...

    def insert(self, path: str, insert_line: int, insert_text: str) -> str:
        """Insert *insert_text* after line *insert_line* (``0`` = before line 1)."""
        ...


def _truncate_middle(text: str) -> str:
    """Return *text* unchanged, or head+marker+tail when over budget."""
    if len(text) <= _MAX_OUTPUT_CHARS:
        return text
    elided = len(text) - _HEAD_CHARS - _TAIL_CHARS
    marker = (
        f"\n...[truncated: {elided} chars elided; view a narrower view_range "
        f"or grep for the pattern you need]...\n"
    )
    return text[:_HEAD_CHARS] + marker + text[-_TAIL_CHARS:]


class LocalFilesystemEditor:
    """In-process editor rooted at *base_path* on the local filesystem.

    Paths are resolved to their realpath and must stay contained in the resolved
    root; symlink-escapes and ``..``-traversal are rejected. Relative paths are
    interpreted under the root. ``view`` reads with ``errors="replace"`` (display
    only); the mutating commands use strict UTF-8 and fail loud on a non-UTF-8
    file, which cannot be safely round-tripped.
    """

    def __init__(self, base_path: str) -> None:
        self._base = Path(base_path).resolve()

    def view(self, path: str, view_range: list[int] | None = None) -> str:
        resolved = self._resolve(path)
        if resolved.is_dir():
            if view_range is not None:
                raise EditorError("view_range is not allowed when viewing a directory")
            return _truncate_middle(self._view_directory(resolved))
        if not resolved.is_file():
            raise EditorError(f"file not found: {path}")
        text = resolved.read_text(encoding="utf-8", errors="replace")
        return _truncate_middle(self._format_file(text, view_range, path))

    def create(self, path: str, file_text: str) -> str:
        resolved = self._resolve(path)
        if resolved.exists():
            raise EditorError(f"cannot create {path}: path already exists")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(file_text, encoding="utf-8")
        return f"File created successfully at {path}"

    def str_replace(self, path: str, old_str: str, new_str: str) -> str:
        resolved = self._resolve(path)
        content = self._read_strict(resolved, path)
        count = content.count(old_str)
        if count == 0:
            raise EditorError(f"no match for old_str in {path}")
        if count > 1:
            raise EditorError(f"old_str is not unique in {path}: found {count} matches")
        self._atomic_write(resolved, content.replace(old_str, new_str, 1))
        return f"Successfully replaced text in {path}"

    def insert(self, path: str, insert_line: int, insert_text: str) -> str:
        resolved = self._resolve(path)
        content = self._read_strict(resolved, path)
        lines = content.split("\n")
        if insert_line < 0 or insert_line > len(lines):
            raise EditorError(
                f"insert_line {insert_line} is out of range [0, {len(lines)}] for {path}"
            )
        new_lines = lines[:insert_line] + insert_text.split("\n") + lines[insert_line:]
        self._atomic_write(resolved, "\n".join(new_lines))
        return f"Successfully inserted text at line {insert_line} in {path}"

    # -- internals ------------------------------------------------------------

    def _resolve(self, path: str) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self._base / candidate
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self._base):
            raise EditorError(f"path {path} escapes the working root {self._base}")
        return resolved

    def _read_strict(self, resolved: Path, path: str) -> str:
        if not resolved.is_file():
            raise EditorError(f"file not found: {path}")
        try:
            return resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise EditorError(f"cannot edit non-UTF-8 file {path}: {exc}") from exc

    @staticmethod
    def _atomic_write(resolved: Path, content: str) -> None:
        tmp = resolved.with_suffix(resolved.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(resolved)

    @staticmethod
    def _format_file(text: str, view_range: list[int] | None, path: str) -> str:
        lines = text.splitlines(keepends=True)
        start = 1
        if view_range is not None:
            if len(view_range) != 2:
                raise EditorError("view_range must be [start, end]")
            start, end = view_range
            if start < 1 or start > len(lines):
                raise EditorError(
                    f"view_range start {start} is out of range [1, {len(lines)}] for {path}"
                )
            if end == -1:
                end = len(lines)
            elif end < start:
                raise EditorError(f"view_range end {end} is before start {start}")
            else:
                end = min(end, len(lines))
            lines = lines[start - 1 : end]
        return "".join(f"{i:>6}\t{line}" for i, line in enumerate(lines, start=start))

    @staticmethod
    def _view_directory(resolved: Path) -> str:
        entries = LocalFilesystemEditor._walk_two_levels(resolved)
        header = (
            f"Here are the files and directories up to 2 levels deep in {resolved}, "
            f"excluding hidden items and __pycache__:"
        )
        return "\n".join([header, *entries])

    @staticmethod
    def _walk_two_levels(root: Path) -> list[str]:
        entries: list[str] = []
        for level1 in sorted(root.iterdir()):
            if LocalFilesystemEditor._skip(level1):
                continue
            entries.append(str(level1))
            if level1.is_dir():
                for level2 in sorted(level1.iterdir()):
                    if LocalFilesystemEditor._skip(level2):
                        continue
                    entries.append(str(level2))
        return entries

    @staticmethod
    def _skip(entry: Path) -> bool:
        return entry.name.startswith(".") or entry.name == "__pycache__"


class StrReplaceEditorTool(Tool):
    """Schema provider for the ``str_replace_editor`` tool.

    Publishes the ``str_replace_based_edit_tool`` input schema so the native
    adapter can advertise it. Execution is handled by the runner-side wrapper,
    not here.

    ``__init__`` accepts and ignores the ``tool_config`` keys the wrapper
    consumes (``service``, ``compose_project_prefix``): the adapter builds the
    schema provider as ``cls(**tool_config)``, so an unexpected keyword would
    raise inside schema extraction and drop the tool from the LLM's view.
    """

    def __init__(
        self,
        service: str | None = None,
        compose_project_prefix: str | None = None,
        **_: object,
    ) -> None:
        super().__init__(
            name="str_replace_editor",
            description=(
                "View, create, and edit files. Commands: 'view' shows "
                "line-numbered file content (or a directory listing); 'create' "
                "writes a new file and fails if the path already exists; "
                "'str_replace' replaces a single unique occurrence of old_str "
                "with new_str; 'insert' inserts insert_text after insert_line "
                "(0 inserts before the first line)."
            ),
            policy=ToolPolicy(timeout_s=30.0, category=ToolCategory.WRITE),
        )

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
                            "enum": list(_EDITOR_COMMANDS),
                            "description": "The editing command to run.",
                        },
                        "path": {
                            "type": "string",
                            "description": "Absolute path to the file or directory.",
                        },
                        "view_range": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": (
                                "Optional [start, end] line range for 'view' "
                                "(1-indexed; -1 for end of file)."
                            ),
                        },
                        "file_text": {
                            "type": "string",
                            "description": "Content of the file to create (required for 'create').",
                        },
                        "old_str": {
                            "type": "string",
                            "description": (
                                "Exact text to replace (required for 'str_replace'); "
                                "must match a single unique occurrence."
                            ),
                        },
                        "new_str": {
                            "type": "string",
                            "description": "Replacement text (required for 'str_replace').",
                        },
                        "insert_line": {
                            "type": "integer",
                            "description": (
                                "Line number after which to insert; 0 inserts before "
                                "the first line (required for 'insert')."
                            ),
                        },
                        "insert_text": {
                            "type": "string",
                            "description": "Text to insert (required for 'insert').",
                        },
                    },
                    "required": ["command", "path"],
                    "additionalProperties": False,
                },
            },
        }

    def execute(self, **kwargs: Any) -> ToolResult:
        raise NotImplementedError(
            "StrReplaceEditorTool is a schema provider; execution is handled by "
            "StrReplaceEditorToolWrapper on the runner."
        )
