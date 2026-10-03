"""Trial bundle files as receiver-side attachments (ADR-0047, amendment of 2026-09-16).

Once the conductor has persisted a trial's bundle it calls the observer's ``trial_persisted``
hook, and a receiver-specific exporter may attach the files of the trial directory to the trace.
This module is the receiver-neutral half, shared by convention with the offline
``langfuse-connector`` (tolokaforge-tools) so that a trace looks the same whichever producer
wrote it:

- the **attachment set**: the regular files at the top level of the trial directory
  (``env.yaml``, ``grade.yaml``, ``logs.yaml``, ``metrics.yaml``, ``prompts.yaml``, ``task.yaml``,
  ``tools_schemas.yaml``, ``trajectory.yaml``; ``tool_log.yaml``, ``judge_trajectory.yaml`` and
  ``judge_inputs.yaml`` when the trial has them); hidden files and subdirectories (video,
  ``services/``) are not attachments and nothing derived is stored;
- the **compression rule**: ``env.yaml`` and ``trajectory.yaml`` are stored gzipped with
  ``mtime 0`` (deterministic bytes), everything else as written;
- the **outbound data-safety scan**: a file whose bytes contain a value the ``SecretManager``
  knows, or match a key-shaped pattern (dotenv secrets, ``Authorization`` headers, PEM blocks,
  URL credentials, well-known key prefixes, JWTs, secret-named fields), is skipped and named in
  the manifest; bytes are never rewritten, an attachment stays byte-exact or is not sent;
- **manifest v2**, the trace-metadata document a download rebuilds the trial directory from::

      attachments_schema: 2
      attachments: {<file name on disk>: {media_id, media, sha256, stored_sha256, bytes,
                    stored_bytes, content_type, encoding: none | gzip}}
      attachments_complete: true | false
      attachments_skipped: [{name, rule}]
"""

from __future__ import annotations

import gzip
import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tolokaforge_langfuse import safety

ATTACHMENTS_SCHEMA = 2
ATTACH_ALL = "all"
ATTACH_CORE = "core"
ATTACH_NONE = "none"
ATTACH_MODES = (ATTACH_ALL, ATTACH_CORE, ATTACH_NONE)
ENCODING_NONE = "none"
ENCODING_GZIP = "gzip"
GZIP_CONTENT_TYPE = "application/gzip"
CORE_FILES = ("task.yaml", "prompts.yaml", "tools_schemas.yaml", "logs.yaml", "grade.yaml")
GZIP_FILES = frozenset({"env.yaml", "trajectory.yaml"})
PLAIN_TEXT_FILES = frozenset({"prompts.yaml"})
CONTENT_TYPES = {
    ".yaml": "application/x-yaml",
    ".yml": "application/x-yaml",
    ".json": "application/json",
    ".md": "text/markdown",
    ".log": "text/plain",
    ".txt": "text/plain",
}
ALLOWED_SUFFIXES = frozenset({".yaml", ".yml", ".json", ".md", ".log", ".txt"})
# the shapes and the minimum length have one home, the safety gate (the offline connector's copy
# is kept identical by hand)
MIN_SECRET_VALUE = safety.MIN_SECRET_VALUE
SECRET_SHAPES = safety.SHAPES


@dataclass
class AttachCounts:
    """What one trial's attachment step did (summed into the tracing receipt)."""

    registered: int = 0
    uploaded: int = 0
    deduplicated: int = 0
    skipped: int = 0
    failed: int = 0
    manifests_sent: int = 0
    manifests_failed: int = 0

    def add(self, other: AttachCounts) -> None:
        self.registered += other.registered
        self.uploaded += other.uploaded
        self.deduplicated += other.deduplicated
        self.skipped += other.skipped
        self.failed += other.failed
        self.manifests_sent += other.manifests_sent
        self.manifests_failed += other.manifests_failed


@dataclass(frozen=True)
class TrialFile:
    """One top-level file of the trial directory as it will be stored."""

    name: str  # the file name on disk (never a derived name)
    original: bytes
    payload: bytes  # gzipped for GZIP_FILES, the original bytes otherwise
    content_type: str
    encoding: str  # none | gzip

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.original).hexdigest()

    @property
    def stored_sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


@dataclass(frozen=True)
class AttachedFile:
    """A file the receiver holds: the manifest entry plus the receiver's media identity."""

    file: TrialFile
    media_id: str
    token: str

    def manifest_entry(self) -> dict[str, Any]:
        return {
            "media_id": self.media_id,
            "media": self.token,
            "sha256": self.file.sha256,
            "stored_sha256": self.file.stored_sha256,
            "bytes": len(self.file.original),
            "stored_bytes": len(self.file.payload),
            "content_type": self.file.content_type,
            "encoding": self.file.encoding,
        }


def content_type_of(name: str) -> str:
    if name in PLAIN_TEXT_FILES:
        return "text/plain"
    return CONTENT_TYPES.get(Path(name).suffix.lower(), "text/plain")


def encode(name: str, raw: bytes) -> TrialFile:
    """The compression rule applied to one file."""
    if name in GZIP_FILES:
        return TrialFile(
            name=name,
            original=raw,
            payload=gzip.compress(raw, compresslevel=6, mtime=0),
            content_type=GZIP_CONTENT_TYPE,
            encoding=ENCODING_GZIP,
        )
    return TrialFile(
        name=name,
        original=raw,
        payload=raw,
        content_type=content_type_of(name),
        encoding=ENCODING_NONE,
    )


def list_trial_files(trial_dir: Path) -> list[Path]:
    """The attachment set: regular files at the top level, hidden files excluded, by name."""
    if not trial_dir.is_dir():
        return []
    return sorted(
        (p for p in trial_dir.iterdir() if p.is_file() and not p.name.startswith(".")),
        key=lambda p: p.name,
    )


def attachment_names(trial_dir: Path, mode: str) -> list[str]:
    if mode not in ATTACH_MODES:
        raise ValueError(f"attach mode must be one of {ATTACH_MODES}, got {mode!r}")
    names = [p.name for p in list_trial_files(trial_dir)]
    if mode == ATTACH_ALL:
        return names
    if mode == ATTACH_CORE:
        return [n for n in names if n in CORE_FILES]
    return []


def plan_attachments(trial_dir: Path, mode: str) -> list[TrialFile]:
    """The files ``mode`` attaches from ``trial_dir``, read and encoded."""
    return [
        encode(name, (trial_dir / name).read_bytes()) for name in attachment_names(trial_dir, mode)
    ]


def allowed_attachment(name: str) -> bool:
    """The file-type allowlist: yaml, json, md, log, txt (the gzip happens after this check)."""
    return Path(name).suffix.lower() in ALLOWED_SUFFIXES


class SecretScan:
    """Finds known secret values and key-shaped strings in a payload; names the rule, never the
    value (a masked excerpt of at most four leading characters). A wrapper of the safety gate:
    one rule set and one scan for the files, the projection and the live spans."""

    def __init__(self, known_values: Iterable[str] = ()) -> None:
        self._gate = safety.SafetyGate.from_environment({}, extra=known_values)

    def scan(self, payload: bytes) -> list[str]:
        return [str(finding) for finding in self._gate.scan(payload)]

    def scan_structured(self, value: Any) -> list[str]:
        """:meth:`safety.SafetyGate.scan_structured`: the JSON-like ``value`` as the receiver is
        sent it, and its raw strings for the known values."""
        return [str(finding) for finding in self._gate.scan_structured(value)]


def build_manifest(
    trial_dir: Path,
    attached: Iterable[AttachedFile],
    skipped: Iterable[Mapping[str, str]],
) -> dict[str, Any]:
    """Manifest v2 for the trace metadata. ``attachments_complete`` is true when every top-level
    file of the trial directory is attached and nothing was kept back."""
    entries = {a.file.name: a.manifest_entry() for a in sorted(attached, key=lambda a: a.file.name)}
    skipped_list = [dict(s) for s in skipped]
    complete = not skipped_list and set(attachment_names(trial_dir, ATTACH_ALL)) <= set(entries)
    return {
        "attachments_schema": ATTACHMENTS_SCHEMA,
        "attachments": entries,
        "attachments_complete": complete,
        "attachments_skipped": skipped_list,
    }
