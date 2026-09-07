"""Unit acceptance for ``LocalDiskBundleStore``.

Covers the URI shape returned by ``put``, byte-identical round-trip
through ``get``, and the three refusal contracts (non-empty destination,
non-bundle scheme, wrong store name).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tolokaforge.core.grading.bundle import serialize_grade_bundle
from tolokaforge.core.grading.bundle_store import (
    BundleNotFoundError,
    InvalidBundleURIError,
    LocalDiskBundleStore,
    build_bundle_uri,
    parse_bundle_uri,
)

pytestmark = pytest.mark.unit


_URI_RE = re.compile(r"^bundle://local_disk/[0-9a-f]{64}$")


def _make_bundle(out_dir: Path, filesystem_root: Path, *, trial_id: str = "trial-1") -> Path:
    filesystem_root.mkdir(parents=True, exist_ok=True)
    (filesystem_root / "hello.txt").write_bytes(b"hello\n")
    serialize_grade_bundle(
        out_dir,
        trial_id=trial_id,
        initial_state={"score": 0.0},
        final_state={"score": 1.0},
        final_state_stable={"score": 1.0},
        filesystem_root=filesystem_root,
        checks=None,
        kb=None,
        trajectory={"llm_messages": []},
        grading_config={"combine_method": "weighted"},
    )
    return out_dir


def test_put_returns_bundle_uri(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "bundle", tmp_path / "fs")
    store = LocalDiskBundleStore(root_dir=tmp_path / "store")

    uri = store.put(bundle)

    assert _URI_RE.match(uri), f"URI {uri!r} does not match bundle://local_disk/<64-hex>"


def test_get_reads_back_byte_identical_bundle(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "bundle", tmp_path / "fs")
    store = LocalDiskBundleStore(root_dir=tmp_path / "store")
    uri = store.put(bundle)

    dest = tmp_path / "dest"
    returned = store.get(uri, dest)

    assert returned == dest
    source_files = {
        p.relative_to(bundle).as_posix(): p.read_bytes() for p in bundle.rglob("*") if p.is_file()
    }
    dest_files = {
        p.relative_to(dest).as_posix(): p.read_bytes() for p in dest.rglob("*") if p.is_file()
    }
    assert source_files == dest_files


def test_get_refuses_non_empty_dest_dir(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "bundle", tmp_path / "fs")
    store = LocalDiskBundleStore(root_dir=tmp_path / "store")
    uri = store.put(bundle)

    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "stray.txt").write_bytes(b"pre-existing")

    with pytest.raises(FileExistsError):
        store.get(uri, dest)


def test_get_refuses_unknown_uri_scheme(tmp_path: Path) -> None:
    store = LocalDiskBundleStore(root_dir=tmp_path / "store")
    dest = tmp_path / "dest"

    with pytest.raises(InvalidBundleURIError, match="scheme"):
        store.get("file:///tmp/whatever", dest)


def test_get_refuses_unknown_store_name(tmp_path: Path) -> None:
    store = LocalDiskBundleStore(root_dir=tmp_path / "store")
    dest = tmp_path / "dest"
    other_store_uri = "bundle://s3/" + "a" * 64

    with pytest.raises(InvalidBundleURIError, match="s3"):
        store.get(other_store_uri, dest)


@pytest.mark.parametrize(
    "malformed,match",
    [
        ("file:///tmp/x", "scheme"),
        ("bundle:///" + "a" * 64, "netloc"),
        ("bundle://local_disk/" + "a" * 64 + "?q=1", "query"),
        ("bundle://local_disk/" + "a" * 64 + "#f", "fragment"),
        ("bundle://local_disk/notenoughhex", "digest"),
        ("bundle://local_disk/" + "Z" * 64, "digest"),
    ],
)
def test_parse_bundle_uri_refuses_malformed(malformed: str, match: str) -> None:
    with pytest.raises(InvalidBundleURIError, match=match):
        parse_bundle_uri(malformed)


@pytest.mark.parametrize(
    "bad_name,bad_digest,match",
    [
        ("", "a" * 64, "store"),
        ("has spaces", "a" * 64, "store"),
        ("local_disk", "shorthex", "digest"),
        ("local_disk", "Z" * 64, "digest"),
    ],
)
def test_build_bundle_uri_refuses_malformed(bad_name: str, bad_digest: str, match: str) -> None:
    with pytest.raises(InvalidBundleURIError, match=match):
        build_bundle_uri(bad_name, bad_digest)


def test_get_raises_bundle_not_found_for_unknown_digest(tmp_path: Path) -> None:
    store = LocalDiskBundleStore(root_dir=tmp_path / "store")
    dest = tmp_path / "dest"
    missing_uri = build_bundle_uri("local_disk", "b" * 64)

    with pytest.raises(BundleNotFoundError):
        store.get(missing_uri, dest)


def test_get_raises_bundle_not_found_for_partial_bundle(tmp_path: Path) -> None:
    store_root = tmp_path / "store"
    store = LocalDiskBundleStore(root_dir=store_root)
    partial_dir = store_root / "grade_bundles" / ("c" * 64)
    partial_dir.mkdir(parents=True)
    (partial_dir / "final_state.json").write_text("{}")  # part exists, no manifest
    dest = tmp_path / "dest"
    partial_uri = build_bundle_uri("local_disk", "c" * 64)

    with pytest.raises(BundleNotFoundError):
        store.get(partial_uri, dest)
