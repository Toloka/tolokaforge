"""File-editor tool matching Anthropic's ``str_replace_based_edit_tool`` contract.

Four roles live here, mirroring :mod:`tolokaforge.tools.persistent_shell`:

- :class:`StrReplaceEditorTool` is the *schema provider*. The builtin registry
  advertises its ``get_schema()`` to the LLM; it is never executed (the runner
  drives execution through ``StrReplaceEditorToolWrapper``).
- :class:`EditorBackend` is the engine :class:`typing.Protocol` — the shape both
  the local and the compose engines implement.
- :class:`LocalFilesystemEditor` is the local engine: in-process file operations
  rooted at a working directory.
- :class:`DockerComposeEditor` is the compose engine: the same four commands
  routed through ``docker exec`` into an already-running service container.

The four commands ``view`` / ``create`` / ``str_replace`` / ``insert`` match the
Claude-4 editor variants (``text_editor_20250429`` / ``text_editor_20250728``);
``undo_edit`` is absent, as in those variants. Every ambiguous or destructive
operation fails loud by raising :class:`EditorError`. The command-level text
transforms and view rendering are shared free functions so both engines apply
identical semantics.
"""

from __future__ import annotations

import posixpath
import subprocess
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable

from tolokaforge.tools.registry import Tool, ToolCategory, ToolPolicy, ToolResult

_EDITOR_COMMANDS = ("view", "create", "str_replace", "insert")

# Middle-truncation budget for a single view's rendered output.
_MAX_OUTPUT_CHARS = 16384
_HEAD_CHARS = 8192
_TAIL_CHARS = 8192

_DEFAULT_EXEC_TIMEOUT_S = 30.0


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


def _render_view(text: str, view_range: list[int] | None, path: str) -> str:
    """Render *text* as 1-indexed ``cat -n``-style line-numbered output."""
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


def _replaced_once(content: str, old_str: str, new_str: str, path: str) -> str:
    """Return *content* with the unique *old_str* replaced; fail loud on 0 or >1."""
    count = content.count(old_str)
    if count == 0:
        raise EditorError(f"no match for old_str in {path}")
    if count > 1:
        raise EditorError(f"old_str is not unique in {path}: found {count} matches")
    return content.replace(old_str, new_str, 1)


def _inserted(content: str, insert_line: int, insert_text: str, path: str) -> str:
    """Return *content* with *insert_text* spliced after line *insert_line*."""
    lines = content.split("\n")
    if insert_line < 0 or insert_line > len(lines):
        raise EditorError(f"insert_line {insert_line} is out of range [0, {len(lines)}] for {path}")
    new_lines = lines[:insert_line] + insert_text.split("\n") + lines[insert_line:]
    return "\n".join(new_lines)


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
        return _truncate_middle(_render_view(text, view_range, path))

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
        self._atomic_write(resolved, _replaced_once(content, old_str, new_str, path))
        return f"Successfully replaced text in {path}"

    def insert(self, path: str, insert_line: int, insert_text: str) -> str:
        resolved = self._resolve(path)
        content = self._read_strict(resolved, path)
        self._atomic_write(resolved, _inserted(content, insert_line, insert_text, path))
        return f"Successfully inserted text at line {insert_line} in {path}"

    # -- internals ------------------------------------------------------------

    def _resolve(self, path: str) -> Path:
        if not self._base.is_dir():
            raise EditorError(f"working root {self._base} does not exist or is not a directory")
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


# ``realpath`` unavailable → exit 90; ``realpath`` present but resolution failed
# → exit 91. Anything else the caller treats as a generic exec failure. The
# missing-tail walk lets ``create`` resolve a not-yet-existing path the same way
# the local engine's ``Path.resolve()`` does, while still following symlinks on
# the existing prefix (so a symlink escape is caught inside the container).
# ``$p`` is always absolute (the caller roots relative paths under the working
# root), so no ``--`` end-of-options guard is needed — and busybox ``realpath``
# rejects it.
_REALPATH_SCRIPT = (
    "command -v realpath >/dev/null 2>&1 || exit 90\n"
    "p=$1\n"
    "suffix=\n"
    'while [ ! -e "$p" ] && [ "$p" != / ]; do\n'
    '  suffix=/$(basename "$p")$suffix\n'
    '  p=$(dirname "$p")\n'
    "done\n"
    'rp=$(realpath "$p") || exit 91\n'
    'printf %s "$rp$suffix"\n'
)

_KIND_SCRIPT = (
    'if [ -d "$1" ]; then printf dir; '
    'elif [ -f "$1" ]; then printf file; '
    'elif [ -e "$1" ]; then printf other; '
    "else printf missing; fi\n"
)

_LIST_SCRIPT = (
    'find "$1" -mindepth 1 -maxdepth 2 '
    "'(' -name '.*' -o -name '__pycache__' ')' -prune -o -print\n"
)

_WRITE_SCRIPT = 'cat > "$2" && mv -- "$2" "$1"\n'

_NO_REALPATH_EXIT = 90


class DockerComposeEditor:
    """Editor rooted at *base_path* inside an already-running compose service.

    Implements the same :class:`EditorBackend` contract as
    :class:`LocalFilesystemEditor`, but every command runs through
    ``docker exec`` into *container_name* — an already-running service container
    (brought up by the environment / another lifecycle consumer); this engine
    never brings the stack up or down. File content
    (``file_text``/``new_str``/``insert_text``) is piped on **stdin** to
    ``docker exec -i``, never interpolated into the shell command string, and
    paths are passed as positional ``sh`` arguments — so agent-controlled bytes
    cannot inject shell commands.

    Path resolution and containment run **inside the container** via
    ``docker exec … realpath`` (a host realpath is meaningless for a container
    path); a resolved path must stay under ``realpath(base_path)`` or the
    operation fails loud. If ``realpath`` is absent from the target image the
    engine fails loud rather than silently skipping validation.

    Atomicity is weaker than the local engine: writes go to a temp file in the
    target's directory and are ``mv``-swapped into place, so a completed write
    is atomic, but an exec interrupted mid-write can leave the temp file behind
    (the local engine's single-process temp+rename has no such window).
    """

    def __init__(
        self,
        container_name: str,
        base_path: str,
        timeout_s: float | None = None,
        user: str | None = None,
    ) -> None:
        """Bind an editor to *base_path* inside a running compose service.

        ``user`` optionally sets ``docker exec --user <user>`` so every
        editor operation runs as a specific user (name or ``uid:gid``)
        rather than the container's default. Task-side callers use this
        to drop privileges: an image whose ENTRYPOINT runs as root can
        still hand the agent an unprivileged editor, so root-owned
        files (e.g. a hidden test oracle staged by the container's own
        grader) stay unreachable. Default ``None`` inherits the
        container's default user and preserves prior behaviour.
        """
        self._container_name = container_name
        self._base = base_path
        self._timeout_s = timeout_s if timeout_s is not None else _DEFAULT_EXEC_TIMEOUT_S
        self._base_real: str | None = None
        self._user = user

    @property
    def container_name(self) -> str:
        return self._container_name

    @property
    def base_path(self) -> str:
        return self._base

    @property
    def user(self) -> str | None:
        return self._user

    def view(self, path: str, view_range: list[int] | None = None) -> str:
        resolved = self._resolve(path)
        kind = self._path_kind(resolved)
        if kind == "dir":
            if view_range is not None:
                raise EditorError("view_range is not allowed when viewing a directory")
            return _truncate_middle(self._list_directory(resolved))
        if kind != "file":
            raise EditorError(f"file not found: {path}")
        return _truncate_middle(_render_view(self._read(resolved), view_range, path))

    def create(self, path: str, file_text: str) -> str:
        resolved = self._resolve(path)
        if self._path_kind(resolved) != "missing":
            raise EditorError(f"cannot create {path}: path already exists")
        parent = posixpath.dirname(resolved)
        self._exec('mkdir -p "$1"', parent)
        self._write_atomic(resolved, file_text)
        return f"File created successfully at {path}"

    def str_replace(self, path: str, old_str: str, new_str: str) -> str:
        resolved = self._resolve(path)
        if self._path_kind(resolved) != "file":
            raise EditorError(f"file not found: {path}")
        content = self._read_strict(resolved, path)
        self._write_atomic(resolved, _replaced_once(content, old_str, new_str, path))
        return f"Successfully replaced text in {path}"

    def insert(self, path: str, insert_line: int, insert_text: str) -> str:
        resolved = self._resolve(path)
        if self._path_kind(resolved) != "file":
            raise EditorError(f"file not found: {path}")
        content = self._read_strict(resolved, path)
        self._write_atomic(resolved, _inserted(content, insert_line, insert_text, path))
        return f"Successfully inserted text at line {insert_line} in {path}"

    # -- internals ------------------------------------------------------------

    def _resolve(self, path: str) -> str:
        abs_path = path if path.startswith("/") else posixpath.join(self._base, path)
        real = self._container_realpath(abs_path, path)
        base_real = self._resolve_base()
        if not PurePosixPath(real).is_relative_to(base_real):
            raise EditorError(f"path {path} escapes the working root {self._base}")
        return real

    def _resolve_base(self) -> str:
        if self._base_real is None:
            base_real = self._container_realpath(self._base, self._base)
            if self._path_kind(base_real) != "dir":
                raise EditorError(f"working root {self._base} does not exist or is not a directory")
            self._base_real = base_real
        return self._base_real

    def _container_realpath(self, abs_path: str, path: str) -> str:
        result = self._exec(_REALPATH_SCRIPT, abs_path, allow_failure=True)
        if result.returncode == _NO_REALPATH_EXIT:
            raise EditorError(
                f"'realpath' is unavailable in container {self._container_name}; "
                f"cannot validate that {path} stays within {self._base}"
            )
        if result.returncode != 0:
            raise EditorError(f"could not resolve path {path}: {self._stderr(result)}")
        return result.stdout.decode("utf-8", errors="replace").strip()

    def _path_kind(self, resolved: str) -> str:
        return self._exec(_KIND_SCRIPT, resolved).stdout.decode("utf-8", "replace").strip()

    def _list_directory(self, resolved: str) -> str:
        raw = self._exec(_LIST_SCRIPT, resolved).stdout.decode("utf-8", "replace")
        entries = sorted(line for line in raw.splitlines() if line)
        header = (
            f"Here are the files and directories up to 2 levels deep in {resolved}, "
            f"excluding hidden items and __pycache__:"
        )
        return "\n".join([header, *entries])

    def _read(self, resolved: str) -> str:
        return self._read_bytes(resolved).decode("utf-8", errors="replace")

    def _read_strict(self, resolved: str, path: str) -> str:
        try:
            return self._read_bytes(resolved).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EditorError(f"cannot edit non-UTF-8 file {path}: {exc}") from exc

    def _read_bytes(self, resolved: str) -> bytes:
        return self._exec('cat -- "$1"', resolved).stdout

    def _write_atomic(self, resolved: str, content: str) -> None:
        tmp = posixpath.join(posixpath.dirname(resolved), f".tf-editor-{uuid.uuid4().hex}.tmp")
        self._exec(_WRITE_SCRIPT, resolved, tmp, stdin=content.encode("utf-8"))

    def _exec(
        self,
        script: str,
        *args: str,
        stdin: bytes | None = None,
        allow_failure: bool = False,
    ) -> subprocess.CompletedProcess[bytes]:
        cmd = ["docker", "exec"]
        if self._user is not None:
            cmd += ["--user", self._user]
        if stdin is not None:
            cmd.append("-i")
        cmd += [self._container_name, "sh", "-c", script, "_", *args]
        try:
            result = subprocess.run(
                cmd, input=stdin, capture_output=True, timeout=self._timeout_s, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise EditorError(
                f"docker exec into {self._container_name} timed out after {self._timeout_s:g}s"
            ) from exc
        if not allow_failure and result.returncode != 0:
            raise EditorError(
                f"docker exec into {self._container_name} failed "
                f"(exit {result.returncode}): {self._stderr(result)}"
            )
        return result

    @staticmethod
    def _stderr(result: subprocess.CompletedProcess[bytes]) -> str:
        return result.stderr.decode("utf-8", errors="replace").strip()


class StrReplaceEditorTool(Tool):
    """Schema provider for the ``str_replace_editor`` tool.

    Publishes the ``str_replace_based_edit_tool`` input schema so the native
    adapter can advertise it. Execution is handled by the runner-side wrapper,
    not here.

    ``__init__`` accepts and ignores the ``tool_config`` keys the wrapper
    consumes (``service``, ``compose_project_prefix``, ``working_root``): the
    adapter builds the schema provider as ``cls(**tool_config)``, so an
    unexpected keyword would raise inside schema extraction and drop the tool
    from the LLM's view. The schema itself is root-independent, so
    ``working_root`` is accepted only to keep that surface self-documenting.
    """

    def __init__(
        self,
        service: str | None = None,
        compose_project_prefix: str | None = None,
        working_root: str | None = None,
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
            policy=ToolPolicy(timeout_s=_DEFAULT_EXEC_TIMEOUT_S, category=ToolCategory.WRITE),
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
