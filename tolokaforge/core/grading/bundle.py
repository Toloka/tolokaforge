"""Grade bundle format v1.0 — reader, producer, and manifest schema.

A grade bundle is a manifest-first, part-addressable, content-addressable
directory carrying everything a grader needs to score one trial without a
live runner. The bundle's canonical name is
``sha256(manifest.json.read_bytes())``: mutate any part, and the manifest
entry for that part changes, which changes the manifest bytes, which
changes the name. External tools parse against ``manifest.json`` alone;
this module ships the Python reader + producer both sides use.

Layout::

    <bundle_dir>/
      manifest.json           # schema v1.0 — written LAST, canonical bytes
      initial_state.json      # required
      final_state.json        # required
      final_state_stable.json # required
      trajectory.json         # required
      grading_config.json     # required
      filesystem.tar          # required — USTAR, no PAX extensions
      checks/                 # optional subtree
        manifest.json         # per-file digests inside checks/
        <rel-path>            # each file digested by checks/manifest.json
      kb/                     # optional subtree — same shape as checks/

Manifest schema v1.0::

    {
      "schema_version": "1.0",
      "trial_id": "<opaque str>",
      "parts": {
        "<rel path>": {"sha256": "<hex>", "size": <int>},
        ...
      }
    }

**Deterministic serialisation.** JSON parts:
``json.dumps(payload, sort_keys=True, separators=(",", ":"))`` with floats
routed through :func:`normalise_floats` (six-significant-digit `%.6g`
normalisation shared with the byte-parity gate). Tar parts: USTAR
(POSIX.1-1988) format with per-entry ``TarInfo.pax_headers={}``,
``mtime=0``, ``uid=0``, ``gid=0``, ``uname=""``, ``gname=""``,
``mode=0o644``, entries added in sorted POSIX-path order. USTAR is not the
Python default (which is PAX and auto-injects extension headers for long
paths or non-ASCII names — bytes diverge silently across implementations);
the format pin is load-bearing for the cross-language recipe consumers
follow.

**Schema-version rule.** Semver-like ``MAJOR.MINOR``; parsers refuse
unknown MAJOR (``1`` is the sole supported MAJOR) and accept unknown
MINOR. Any malformed shape (missing, non-string, wrong split, non-digit)
surfaces as :class:`BundleSchemaVersionError` — never as a bare
``KeyError`` or ``TypeError`` escaping the reader.

**Integrity.** :meth:`GradeBundleView.open_part` recomputes SHA-256 on
demand and raises :class:`BundleIntegrityError` on mismatch. Reader is
O(1) at load time; verification cost is deferred to each part read.

Purity: stdlib plus :mod:`tolokaforge.core.grading.filesystem_view`. No
reach into ``tolokaforge.runner``, ``tolokaforge.grader``, or
``tolokaforge.core.grading.substrate_live`` — locked by the
``bundle-library-purity`` contract in ``.importlinter``.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from tolokaforge.core.grading.filesystem_view import AGENT_VISIBLE_EXCLUDES

__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "BundleError",
    "BundleIntegrityError",
    "BundleSchemaVersionError",
    "GradeBundleManifest",
    "GradeBundleView",
    "load_grade_bundle",
    "manifest_digest",
    "normalise_floats",
    "serialize_grade_bundle",
]


BUNDLE_SCHEMA_VERSION: str = "1.0"
_SUPPORTED_MAJOR: str = BUNDLE_SCHEMA_VERSION.split(".", 1)[0]


class BundleError(Exception):
    """Base class for grade bundle errors."""


class BundleSchemaVersionError(BundleError):
    """Manifest ``schema_version`` is missing, malformed, or an unsupported MAJOR."""


class BundleIntegrityError(BundleError):
    """A part's on-disk bytes do not match the digest declared in the manifest."""


@dataclass(frozen=True)
class GradeBundleManifest:
    schema_version: str
    trial_id: str
    parts: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class GradeBundleView:
    bundle_dir: Path
    manifest: GradeBundleManifest

    def has_part(self, rel_path: str) -> bool:
        return rel_path in self.manifest.parts

    def open_part(self, rel_path: str) -> bytes:
        entry = self.manifest.parts[rel_path]
        expected = entry["sha256"]
        data = (self.bundle_dir / rel_path).read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            raise BundleIntegrityError(
                f"part {rel_path!r} digest mismatch: "
                f"manifest declares {expected}, on-disk bytes hash to {actual}"
            )
        return data


def normalise_floats(value: Any) -> Any:
    """Route every float through ``%.6g`` (six-significant-digit) formatting.

    Recurses through dicts and lists; leaves ``bool`` values alone (they
    are ``int`` subclasses but must not be reformatted). Ensures the byte
    output of :func:`json.dumps` on any float-bearing structure is
    independent of the platform's default float ``repr`` — a
    numerically-equivalent double rendered as ``0.30000000000000004`` on
    one build and ``0.3`` on another lands as the same string here.
    """
    if isinstance(value, dict):
        return {key: normalise_floats(child) for key, child in value.items()}
    if isinstance(value, list):
        return [normalise_floats(child) for child in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return float(f"{value:.6g}")
    return value


def manifest_digest(manifest_json_bytes: bytes) -> str:
    """Hex SHA-256 of the canonical manifest bytes — the bundle's name."""
    return hashlib.sha256(manifest_json_bytes).hexdigest()


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        normalise_floats(payload),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _part_from_bytes(data: bytes) -> dict[str, Any]:
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def _iter_agent_visible_files(
    root: Path, exclude_dirs: frozenset[str]
) -> Iterator[tuple[str, Path]]:
    """Yield ``(posix_rel_path, absolute_path)`` for every non-symlink regular
    file below ``root`` whose parent path does not pass through
    ``exclude_dirs``. Emission order matches the underlying rglob walk;
    callers that need a stable order sort the result.
    """
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in exclude_dirs for part in rel.parts[:-1]):
            continue
        yield rel.as_posix(), path


def _write_filesystem_tar(
    filesystem_root: Path, exclude_dirs: frozenset[str], target: Path
) -> None:
    entries = sorted(
        _iter_agent_visible_files(filesystem_root, exclude_dirs),
        key=lambda kv: kv[0],
    )
    with tarfile.open(target, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, path in entries:
            info = tarfile.TarInfo(name=name)
            info.size = path.stat().st_size
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mode = 0o644
            info.type = tarfile.REGTYPE
            info.pax_headers = {}
            with path.open("rb") as fp:
                tar.addfile(info, fp)


def _sha256_and_size_of_file(path: Path) -> dict[str, Any]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fp:
        while chunk := fp.read(64 * 1024):
            h.update(chunk)
            size += len(chunk)
    return {"sha256": h.hexdigest(), "size": size}


def _write_named_subtree(
    subtree_root: Path,
    payload: Mapping[str, bytes],
    parts: dict[str, dict[str, Any]],
    parts_prefix: str,
) -> None:
    """Write ``{rel: bytes}`` into ``subtree_root`` + a nested manifest.json
    listing per-file digests. Register every file (and the nested manifest)
    under ``parts_prefix`` in the top-level ``parts`` dict.
    """
    subtree_root.mkdir(parents=True, exist_ok=True)
    nested_entries: dict[str, dict[str, Any]] = {}
    for rel in sorted(payload):
        data = payload[rel]
        target = subtree_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        entry = _part_from_bytes(data)
        nested_entries[rel] = entry
        parts[f"{parts_prefix}{rel}"] = entry
    nested_manifest = _canonical_json_bytes({"parts": nested_entries})
    (subtree_root / "manifest.json").write_bytes(nested_manifest)
    parts[f"{parts_prefix}manifest.json"] = _part_from_bytes(nested_manifest)


def _view_from_parts(
    bundle_dir: Path, trial_id: str, parts: Mapping[str, Mapping[str, Any]]
) -> GradeBundleView:
    proxied = MappingProxyType({rel: MappingProxyType(dict(entry)) for rel, entry in parts.items()})
    return GradeBundleView(
        bundle_dir=bundle_dir,
        manifest=GradeBundleManifest(
            schema_version=BUNDLE_SCHEMA_VERSION,
            trial_id=trial_id,
            parts=proxied,
        ),
    )


def serialize_grade_bundle(
    out_dir: Path,
    *,
    trial_id: str,
    initial_state: dict[str, Any],
    final_state: dict[str, Any],
    final_state_stable: dict[str, Any],
    filesystem_root: Path,
    checks: Mapping[str, bytes] | None,
    kb: Mapping[str, bytes] | None,
    trajectory: dict[str, Any],
    grading_config: dict[str, Any],
    exclude_dirs: frozenset[str] = AGENT_VISIBLE_EXCLUDES,
) -> GradeBundleManifest:
    """Materialise a v1.0 grade bundle into ``out_dir``.

    ``out_dir`` must not already contain any file (raises
    :class:`FileExistsError` on entry). The caller owns lifecycle. Returns
    the manifest the producer just wrote; the bundle's canonical name is
    ``manifest_digest((out_dir / "manifest.json").read_bytes())``.

    Every JSON part is canonicalised with ``sort_keys=True`` + compact
    separators + shared ``%.6g`` float normalisation; the filesystem tree
    is captured as a USTAR tar with sorted entries and zeroed metadata;
    the top-level ``manifest.json`` is written LAST so its bytes see every
    part's digest.
    """
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"out_dir {out_dir} must be empty; found existing entries")
    out_dir.mkdir(parents=True, exist_ok=True)

    parts: dict[str, dict[str, Any]] = {}

    json_parts: dict[str, dict[str, Any]] = {
        "initial_state.json": initial_state,
        "final_state.json": final_state,
        "final_state_stable.json": final_state_stable,
        "trajectory.json": trajectory,
        "grading_config.json": grading_config,
    }
    for rel, payload in json_parts.items():
        data = _canonical_json_bytes(payload)
        (out_dir / rel).write_bytes(data)
        parts[rel] = _part_from_bytes(data)

    tar_target = out_dir / "filesystem.tar"
    _write_filesystem_tar(filesystem_root, exclude_dirs, tar_target)
    parts["filesystem.tar"] = _sha256_and_size_of_file(tar_target)

    if checks is not None:
        _write_named_subtree(out_dir / "checks", checks, parts, "checks/")
    if kb is not None:
        _write_named_subtree(out_dir / "kb", kb, parts, "kb/")

    manifest_dict = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "trial_id": trial_id,
        "parts": parts,
    }
    manifest_bytes = _canonical_json_bytes(manifest_dict)
    (out_dir / "manifest.json").write_bytes(manifest_bytes)

    return _view_from_parts(out_dir, trial_id, parts).manifest


def _validate_schema_version(raw: object) -> None:
    if not isinstance(raw, str) or not raw:
        raise BundleSchemaVersionError(
            f"schema_version must be a non-empty 'MAJOR.MINOR' string; "
            f"got {raw!r}. This reader supports MAJOR {_SUPPORTED_MAJOR!r}."
        )
    parts = raw.split(".")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise BundleSchemaVersionError(
            f"schema_version must be a 'MAJOR.MINOR' string of digit components; "
            f"got {raw!r}. This reader supports MAJOR {_SUPPORTED_MAJOR!r}."
        )
    if parts[0] != _SUPPORTED_MAJOR:
        raise BundleSchemaVersionError(
            f"unsupported schema_version {raw!r}: MAJOR {parts[0]!r} not recognised. "
            f"This reader supports MAJOR {_SUPPORTED_MAJOR!r}."
        )


def load_grade_bundle(bundle_dir: Path) -> GradeBundleView:
    manifest_path = bundle_dir / "manifest.json"
    raw = json.loads(manifest_path.read_bytes())
    try:
        _validate_schema_version(raw["schema_version"])
    except BundleSchemaVersionError:
        raise
    except (KeyError, AttributeError, IndexError, TypeError) as exc:
        raise BundleSchemaVersionError(
            f"manifest at {manifest_path} has no parseable 'schema_version' field: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return _view_from_parts(bundle_dir, raw["trial_id"], raw["parts"])
