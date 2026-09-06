"""Canonical acceptance for ``LocalDiskBundleStore`` content-addressability.

Locks the invariants that make bundle URIs safe to reuse across producers
and workers: identical bundles dedupe to a single directory, distinct
bundles map to distinct URIs, concurrent puts of the same digest never
collide on staging, and a mid-put crash leaves no half-populated visible
bundle.
"""

from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from tests.canonical._bundle_fixtures import synthetic_inputs
from tolokaforge.core.grading.bundle import manifest_digest, serialize_grade_bundle
from tolokaforge.core.grading.bundle_store import LocalDiskBundleStore, parse_bundle_uri

pytestmark = pytest.mark.canonical


def _bundle_at(tmp_path: Path, sub: str, inputs: dict[str, Any]) -> Path:
    out_dir = tmp_path / sub
    serialize_grade_bundle(out_dir, **inputs)
    return out_dir


def _bundle_dirs(store_root: Path) -> list[Path]:
    grade_bundles = store_root / "grade_bundles"
    if not grade_bundles.exists():
        return []
    return sorted(p for p in grade_bundles.iterdir() if p.is_dir())


def test_put_same_bundle_twice_dedupes(tmp_path: Path) -> None:
    inputs = synthetic_inputs(tmp_path / "fixture")
    bundle_a = _bundle_at(tmp_path, "bundle_a", inputs)
    bundle_b = _bundle_at(tmp_path, "bundle_b", inputs)
    store = LocalDiskBundleStore(root_dir=tmp_path / "store")

    uri_a = store.put(bundle_a)
    uri_b = store.put(bundle_b)

    assert uri_a == uri_b
    dirs = _bundle_dirs(store.root_dir)
    assert len(dirs) == 1, f"expected exactly one bundle directory, got {dirs}"
    _, digest = parse_bundle_uri(uri_a)
    assert dirs[0].name == digest


def test_put_different_bundles_produce_different_uris(tmp_path: Path) -> None:
    inputs_a = synthetic_inputs(tmp_path / "fx_a")
    inputs_b = synthetic_inputs(tmp_path / "fx_b")
    inputs_b["trial_id"] = "trial-round-trip-2"
    bundle_a = _bundle_at(tmp_path, "bundle_a", inputs_a)
    bundle_b = _bundle_at(tmp_path, "bundle_b", inputs_b)
    store = LocalDiskBundleStore(root_dir=tmp_path / "store")

    uri_a = store.put(bundle_a)
    uri_b = store.put(bundle_b)

    assert uri_a != uri_b
    assert len(_bundle_dirs(store.root_dir)) == 2


def test_concurrent_put_same_bundle(tmp_path: Path) -> None:
    inputs = synthetic_inputs(tmp_path / "fixture")
    bundle_a = _bundle_at(tmp_path, "bundle_a", inputs)
    bundle_b = _bundle_at(tmp_path, "bundle_b", inputs)
    store = LocalDiskBundleStore(root_dir=tmp_path / "store")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(store.put, bundle_a), pool.submit(store.put, bundle_b)]
        uris = [f.result() for f in futures]

    assert uris[0] == uris[1]

    grade_bundles = store.root_dir / "grade_bundles"
    dirs = sorted(p for p in grade_bundles.iterdir() if p.is_dir())
    assert len(dirs) == 1, f"expected exactly one bundle directory, got {dirs}"

    stragglers = sorted(p.name for p in grade_bundles.iterdir() if p.name.endswith(".tmp"))
    assert stragglers == [], f"leftover staging directories: {stragglers}"

    source_manifest = (bundle_a / "manifest.json").read_bytes()
    dest_manifest = (dirs[0] / "manifest.json").read_bytes()
    assert dest_manifest == source_manifest
    assert dirs[0].name == manifest_digest(source_manifest)


def test_atomic_staging_leaves_no_partial_state_on_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs = synthetic_inputs(tmp_path / "fixture")
    bundle = _bundle_at(tmp_path, "bundle", inputs)
    store = LocalDiskBundleStore(root_dir=tmp_path / "store")

    real_copytree = shutil.copytree

    def _boom(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        real_copytree(src, dst, *args, **kwargs)
        raise RuntimeError("staged tree copy failed mid-put")

    monkeypatch.setattr("tolokaforge.core.grading.bundle_store.shutil.copytree", _boom)

    with pytest.raises(RuntimeError, match="staged tree copy failed mid-put"):
        store.put(bundle)

    grade_bundles = store.root_dir / "grade_bundles"
    digest = manifest_digest((bundle / "manifest.json").read_bytes())
    assert not (
        grade_bundles / digest
    ).exists(), "final <digest>/ directory must not exist after mid-put crash"
    staging_dirs = sorted(
        p.name
        for p in grade_bundles.iterdir()
        if p.name.startswith(f".{digest}.") and p.name.endswith(".tmp")
    )
    assert (
        len(staging_dirs) == 1
    ), f"expected exactly one orphan per-worker .<digest>.<uuid4>.tmp/, got {staging_dirs}"
