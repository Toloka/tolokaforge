"""Grade bundle reader and manifest schema v1.0.

A grade bundle is a manifest-first, part-addressable directory carrying
everything a grader needs to score one trial without a live runner. This
module ships the *reader half* of the format: a v1.0 manifest dataclass,
an open-part view that verifies each part's SHA-256 on demand, and a
top-level :func:`load_grade_bundle` entry point that refuses unknown
MAJOR schema versions or any ``schema_version`` field that is not the
strict ``"MAJOR.MINOR"`` shape (where both components are digit-only).

The bundle name IS the content: a future producer writes
``manifest.json`` last (its ``parts`` map already carries every part's
SHA-256), and the bundle's canonical name is
``sha256(manifest.json.read_bytes())`` — tampering with any part changes
the manifest entry, which changes the manifest bytes, which changes the
name. The reader honours the other half of that contract: it recomputes
each part's SHA-256 on :meth:`GradeBundleView.open_part` and raises
:class:`BundleIntegrityError` on mismatch.

Schema shape (v1.0)::

    {
      "schema_version": "1.0",
      "trial_id": "<opaque str>",
      "parts": {
        "<rel path>": {"sha256": "<hex>", "size": <int>},
        ...
      }
    }

Schema-version rule: parsers reject unknown MAJOR (``1`` is the sole
supported MAJOR); MINOR bumps are forward-compatible. Any malformed
``schema_version`` value (missing, non-string, wrong shape, non-digit)
surfaces as :class:`BundleSchemaVersionError` — never as a bare
``KeyError`` or ``TypeError`` escaping the reader.

Purity: stdlib only. No reach into ``tolokaforge.runner``,
``tolokaforge.grader``, or ``tolokaforge.core.grading.substrate_live``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "BundleError",
    "BundleIntegrityError",
    "BundleSchemaVersionError",
    "GradeBundleManifest",
    "GradeBundleView",
    "load_grade_bundle",
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
    parts_raw = raw["parts"]
    parts: Mapping[str, Mapping[str, Any]] = MappingProxyType(
        {rel: MappingProxyType(dict(entry)) for rel, entry in parts_raw.items()}
    )
    manifest = GradeBundleManifest(
        schema_version=raw["schema_version"],
        trial_id=raw["trial_id"],
        parts=parts,
    )
    return GradeBundleView(bundle_dir=bundle_dir, manifest=manifest)
