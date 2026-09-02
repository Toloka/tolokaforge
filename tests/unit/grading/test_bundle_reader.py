"""Behaviour locks for ``tolokaforge.core.grading.bundle``'s reader half.

Three groups:

* Round-trip a hand-written manifest + parts fixture through
  :func:`load_grade_bundle` and confirm the returned view's manifest
  shape matches the on-disk JSON.
* :meth:`GradeBundleView.open_part` returns the part bytes on match and
  raises :class:`BundleIntegrityError` naming the part when the on-disk
  bytes were tampered with after the manifest was written.
* Parametrised rejection of every malformed shape ``schema_version``
  can take — the fail-loud contract must hold uniformly, never leak a
  raw ``KeyError``, ``TypeError``, ``AttributeError``, or ``IndexError``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from tolokaforge.core.grading.bundle import (
    BUNDLE_SCHEMA_VERSION,
    BundleIntegrityError,
    BundleSchemaVersionError,
    GradeBundleManifest,
    GradeBundleView,
    load_grade_bundle,
)

pytestmark = pytest.mark.unit


def _write_manifest(bundle_dir: Path, manifest: dict[str, Any]) -> None:
    (bundle_dir / "manifest.json").write_bytes(
        json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8")
    )


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_part(bundle_dir: Path, rel_path: str, data: bytes) -> dict[str, Any]:
    target = bundle_dir / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"sha256": _sha256_hex(data), "size": len(data)}


def _write_valid_bundle(bundle_dir: Path) -> dict[str, bytes]:
    """Materialise a two-part bundle; return {rel_path: bytes} of the parts."""
    bundle_dir.mkdir(parents=True, exist_ok=True)
    part_bytes = {
        "initial_state.json": b'{"tables":{"users":[]}}',
        "final_state.json": b'{"tables":{"users":[{"id":1}]}}',
    }
    parts_entries = {rel: _write_part(bundle_dir, rel, data) for rel, data in part_bytes.items()}
    _write_manifest(
        bundle_dir,
        {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "trial_id": "trial-abc-123",
            "parts": parts_entries,
        },
    )
    return part_bytes


def test_load_bundle_from_handwritten_fixture(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    expected_parts = _write_valid_bundle(bundle_dir)

    view = load_grade_bundle(bundle_dir)

    assert isinstance(view, GradeBundleView)
    assert isinstance(view.manifest, GradeBundleManifest)
    assert view.bundle_dir == bundle_dir
    assert view.manifest.schema_version == BUNDLE_SCHEMA_VERSION
    assert view.manifest.trial_id == "trial-abc-123"
    assert set(view.manifest.parts.keys()) == set(expected_parts.keys())
    for rel, data in expected_parts.items():
        entry = view.manifest.parts[rel]
        assert entry["sha256"] == _sha256_hex(data)
        assert entry["size"] == len(data)


def test_open_part_returns_bytes_when_digest_matches(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    expected_parts = _write_valid_bundle(bundle_dir)

    view = load_grade_bundle(bundle_dir)

    for rel, data in expected_parts.items():
        assert view.open_part(rel) == data


def test_open_part_raises_integrity_error_on_tamper(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    _write_valid_bundle(bundle_dir)

    tampered_rel = "initial_state.json"
    tampered_path = bundle_dir / tampered_rel
    original = tampered_path.read_bytes()
    tampered_path.write_bytes(original + b"\x00")

    view = load_grade_bundle(bundle_dir)

    with pytest.raises(BundleIntegrityError) as excinfo:
        view.open_part(tampered_rel)
    assert tampered_rel in str(excinfo.value)


_MISSING = object()


@pytest.mark.parametrize(
    "schema_version_value",
    [
        pytest.param("2.0", id="unknown-major"),
        pytest.param(_MISSING, id="missing-key"),
        pytest.param("", id="empty-string"),
        pytest.param(1, id="non-str-int"),
        pytest.param("1", id="no-dot"),
        pytest.param("1.0.0", id="three-components"),
        pytest.param("abc", id="non-digit"),
        pytest.param(None, id="none"),
    ],
)
def test_schema_version_rejection(tmp_path: Path, schema_version_value: object) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    manifest: dict[str, Any] = {"trial_id": "trial-x", "parts": {}}
    if schema_version_value is not _MISSING:
        manifest["schema_version"] = schema_version_value
    _write_manifest(bundle_dir, manifest)

    with pytest.raises(BundleSchemaVersionError):
        load_grade_bundle(bundle_dir)
