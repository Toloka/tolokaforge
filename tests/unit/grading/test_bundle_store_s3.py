"""Unit acceptance for ``S3BundleStore``.

Driven by ``botocore.stub.Stubber`` — no live S3, no ``moto`` dep.
Locks the upload-order invariant (parts first, manifest LAST), the
dedupe short-circuit on an existing manifest, the byte-identical
round-trip through ``put``/``get``, the non-empty dest refusal, and
proves that ``import tolokaforge.core.grading.bundle_store`` succeeds
when ``boto3`` is not installed.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

boto3 = pytest.importorskip("boto3")
from botocore.stub import Stubber  # noqa: E402

from tolokaforge.core.grading.bundle import (  # noqa: E402
    manifest_digest,
    serialize_grade_bundle,
)
from tolokaforge.core.grading.bundle_store import S3BundleStore  # noqa: E402

pytestmark = pytest.mark.unit


BUCKET = "test-bundles"
PREFIX = "grade_bundles"


def _make_bundle(out_dir: Path, filesystem_root: Path, *, trial_id: str = "trial-s3") -> Path:
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


def _stubbed_client() -> tuple[Any, Stubber]:
    client = boto3.client("s3", region_name="us-east-1")
    stubber = Stubber(client)
    return client, stubber


def _key(digest: str, rel: str) -> str:
    return f"{PREFIX}/{digest}/{rel}"


def test_lazy_boto3_import(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    original = sys.modules.get("boto3")
    monkeypatch.setitem(sys.modules, "boto3", None)
    try:
        module = importlib.reload(importlib.import_module("tolokaforge.core.grading.bundle_store"))
        bundle = _make_bundle(tmp_path / "bundle", tmp_path / "fs")
        store = module.S3BundleStore(bucket="x")
        with pytest.raises(RuntimeError, match="bundle-store-s3"):
            store.put(bundle)
    finally:
        if original is not None:
            sys.modules["boto3"] = original
        importlib.reload(importlib.import_module("tolokaforge.core.grading.bundle_store"))


def test_put_uploads_parts_then_manifest_last(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "bundle", tmp_path / "fs")
    manifest_bytes = (bundle / "manifest.json").read_bytes()
    digest = manifest_digest(manifest_bytes)
    manifest = json.loads(manifest_bytes)
    part_keys = sorted(rel for rel in manifest["parts"] if rel != "manifest.json")

    client, stubber = _stubbed_client()
    stubber.add_client_error(
        "head_object",
        service_error_code="404",
        service_message="Not Found",
        http_status_code=404,
        expected_params={"Bucket": BUCKET, "Key": _key(digest, "manifest.json")},
    )
    call_order: list[str] = []
    for rel in part_keys:
        stubber.add_response(
            "put_object",
            service_response={},
            expected_params={
                "Bucket": BUCKET,
                "Key": _key(digest, rel),
                "Body": (bundle / rel).read_bytes(),
            },
        )
        call_order.append(rel)
    stubber.add_response(
        "put_object",
        service_response={},
        expected_params={
            "Bucket": BUCKET,
            "Key": _key(digest, "manifest.json"),
            "Body": manifest_bytes,
        },
    )
    call_order.append("manifest.json")

    stubber.activate()
    try:
        store = S3BundleStore(bucket=BUCKET, prefix=PREFIX, client=client)
        uri = store.put(bundle)
    finally:
        stubber.deactivate()
    stubber.assert_no_pending_responses()

    assert uri == f"bundle://s3/{digest}"
    assert call_order[-1] == "manifest.json"
    assert call_order[:-1] == part_keys


def test_put_short_circuits_on_dedupe(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "bundle", tmp_path / "fs")
    digest = manifest_digest((bundle / "manifest.json").read_bytes())

    client, stubber = _stubbed_client()
    stubber.add_response(
        "head_object",
        service_response={"ContentLength": 1},
        expected_params={"Bucket": BUCKET, "Key": _key(digest, "manifest.json")},
    )
    stubber.activate()
    try:
        store = S3BundleStore(bucket=BUCKET, prefix=PREFIX, client=client)
        uri = store.put(bundle)
    finally:
        stubber.deactivate()
    stubber.assert_no_pending_responses()

    assert uri == f"bundle://s3/{digest}"


def test_get_downloads_and_writes_byte_identical_tree(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "bundle", tmp_path / "fs")
    manifest_bytes = (bundle / "manifest.json").read_bytes()
    digest = manifest_digest(manifest_bytes)
    manifest = json.loads(manifest_bytes)
    part_rels = sorted(rel for rel in manifest["parts"] if rel != "manifest.json")

    client, stubber = _stubbed_client()
    stubber.add_response(
        "head_object",
        service_response={"ContentLength": len(manifest_bytes)},
        expected_params={"Bucket": BUCKET, "Key": _key(digest, "manifest.json")},
    )
    stubber.add_response(
        "get_object",
        service_response={"Body": _StreamStub(manifest_bytes)},
        expected_params={"Bucket": BUCKET, "Key": _key(digest, "manifest.json")},
    )
    for rel in part_rels:
        data = (bundle / rel).read_bytes()
        stubber.add_response(
            "get_object",
            service_response={"Body": _StreamStub(data)},
            expected_params={"Bucket": BUCKET, "Key": _key(digest, rel)},
        )

    dest = tmp_path / "dest"
    stubber.activate()
    try:
        store = S3BundleStore(bucket=BUCKET, prefix=PREFIX, client=client)
        returned = store.get(f"bundle://s3/{digest}", dest)
    finally:
        stubber.deactivate()
    stubber.assert_no_pending_responses()

    assert returned == dest
    source_files = {
        p.relative_to(bundle).as_posix(): p.read_bytes() for p in bundle.rglob("*") if p.is_file()
    }
    dest_files = {
        p.relative_to(dest).as_posix(): p.read_bytes() for p in dest.rglob("*") if p.is_file()
    }
    assert source_files == dest_files


def test_get_refuses_non_empty_dest_dir(tmp_path: Path) -> None:
    client, _ = _stubbed_client()
    store = S3BundleStore(bucket=BUCKET, prefix=PREFIX, client=client)
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "stray.txt").write_bytes(b"pre-existing")
    with pytest.raises(FileExistsError):
        store.get("bundle://s3/" + "a" * 64, dest)


class _StreamStub:
    """Minimal file-like stand-in for a boto3 ``StreamingBody``."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, amt: int | None = None) -> bytes:
        if amt is None:
            data, self._data = self._data, b""
            return data
        chunk, self._data = self._data[:amt], self._data[amt:]
        return chunk
