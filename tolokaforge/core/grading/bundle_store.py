"""Grade bundle transport — ``BundleStore`` Protocol + built-in stores.

A grade bundle is a manifest-first, part-addressable directory whose
canonical name is ``sha256(manifest.json.read_bytes())`` (see
:mod:`tolokaforge.core.grading.bundle`). This module ships the transport
seam: a small ``BundleStore`` Protocol plus concrete implementations that
move an already-serialised bundle between a producer directory and
addressable storage, keyed uniformly by the URI scheme
``bundle://<store-name>/<content-hash>``.

Two responsibilities are strictly separated: :func:`serialize_grade_bundle`
owns the *format* (canonical bytes, deterministic tar, manifest schema);
:class:`BundleStore` owns *where the bytes live* and *how a reader pulls
them back into a local directory*. A store never re-serialises — it copies
byte-identical parts + manifest. Content-addressable dedupe follows for
free: two puts of a byte-identical bundle land at the same URI, and the
store short-circuits the second copy.

``LocalDiskBundleStore`` writes to
``<root_dir>/grade_bundles/<digest>/`` — a directory, matching the shipped
bundle format verbatim. ``put`` stages into a per-worker
``.<digest>.<uuid4>.tmp/`` sibling and lands atomically via
:func:`os.replace`; the per-worker ``uuid4`` isolates concurrent puts of
the same digest so two workers never share a staging path. ``get`` refuses
a non-empty destination with :class:`FileExistsError` (a merge into a used
directory would silently mask stale parts from an earlier ``get``).

``S3BundleStore`` uploads each part as a separate object under
``<prefix>/<digest>/<rel>`` in an S3-compatible bucket. ``put`` uploads
every data part first and ``manifest.json`` LAST — a crashed put leaves
orphan parts but no manifest, so ``get`` fails cleanly with
:class:`BundleNotFoundError`. Idempotency is a ``head_object`` on the
manifest key; a second put of the same digest returns the URI without
re-uploading. ``boto3`` is a lazy import inside method bodies — the pure
module loads without ``boto3`` installed — and credentials come from the
default AWS credential chain (env, ``~/.aws/credentials``, EC2 / IRSA).
Install the optional extra with ``uv add 'tolokaforge[bundle-store-s3]'``
before instantiating.

Purity: stdlib + :mod:`tolokaforge.core.grading.bundle` for the manifest
digest primitive. No reach into ``tolokaforge.runner``,
``tolokaforge.grader``, or ``tolokaforge.core.grading.substrate_live`` —
locked by the ``bundle-store-purity`` contract in ``.importlinter``.
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from tolokaforge.core.grading.bundle import manifest_digest

__all__ = [
    "BUNDLE_URI_SCHEME",
    "BundleNotFoundError",
    "BundleStore",
    "BundleStoreError",
    "InvalidBundleURIError",
    "LocalDiskBundleStore",
    "S3BundleStore",
    "build_bundle_uri",
    "parse_bundle_uri",
]


BUNDLE_URI_SCHEME: str = "bundle"

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_STORE_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_BUNDLE_SUBDIR = "grade_bundles"
_MANIFEST_FILENAME = "manifest.json"


class BundleStoreError(Exception):
    """Base class for bundle store errors."""


class BundleNotFoundError(BundleStoreError):
    """The URI does not resolve to a stored bundle."""


class InvalidBundleURIError(BundleStoreError):
    """The URI is malformed, targets a different scheme, or names another store."""


def build_bundle_uri(store_name: str, digest: str) -> str:
    """Return ``bundle://<store_name>/<digest>``.

    Rejects any ``store_name`` outside ``[a-z0-9_]+`` or ``digest`` that is
    not 64 lowercase hex chars — both must match the entry-point naming
    contract and the ``sha256`` output of :func:`manifest_digest`.
    """
    if not _STORE_NAME_RE.match(store_name):
        raise InvalidBundleURIError(
            f"invalid store name {store_name!r}: must match {_STORE_NAME_RE.pattern!r}"
        )
    if not _DIGEST_RE.match(digest):
        raise InvalidBundleURIError(
            f"invalid digest {digest!r}: must be 64 lowercase hex characters"
        )
    return f"{BUNDLE_URI_SCHEME}://{store_name}/{digest}"


def parse_bundle_uri(uri: str) -> tuple[str, str]:
    """Parse ``bundle://<store_name>/<digest>`` into ``(store_name, digest)``.

    Refuses any scheme other than ``bundle``, missing netloc, path that is
    not exactly ``/<64-hex>``, or a query/fragment. Never raises
    :class:`ValueError` or :class:`KeyError` — always
    :class:`InvalidBundleURIError`.
    """
    parsed = urlparse(uri)
    if parsed.scheme != BUNDLE_URI_SCHEME:
        raise InvalidBundleURIError(
            f"URI {uri!r} scheme {parsed.scheme!r} is not {BUNDLE_URI_SCHEME!r}"
        )
    if not parsed.netloc:
        raise InvalidBundleURIError(f"URI {uri!r} has no store name (netloc)")
    if parsed.query or parsed.fragment:
        raise InvalidBundleURIError(f"URI {uri!r} carries query or fragment; not allowed")
    if not parsed.path.startswith("/"):
        raise InvalidBundleURIError(f"URI {uri!r} has no digest path")
    digest = parsed.path[1:]
    if not _STORE_NAME_RE.match(parsed.netloc):
        raise InvalidBundleURIError(
            f"URI {uri!r} store name {parsed.netloc!r} must match {_STORE_NAME_RE.pattern!r}"
        )
    if not _DIGEST_RE.match(digest):
        raise InvalidBundleURIError(
            f"URI {uri!r} digest {digest!r} must be 64 lowercase hex characters"
        )
    return parsed.netloc, digest


class BundleStore(Protocol):
    """Transport of an already-serialised grade bundle directory.

    A bundle is addressed as ``bundle://<store-name>/<content-hash>`` where
    ``<content-hash>`` is the hex ``sha256`` of the bundle's
    ``manifest.json`` bytes. Content-addressable dedupe is implicit: two
    puts of byte-identical bundles land at the same URI, and the store
    short-circuits the second copy.
    """

    name: str

    def put(self, bundle_dir: Path) -> str:
        """Upload ``bundle_dir`` to the store. Return its ``bundle://…`` URI.

        Reads ``bundle_dir/manifest.json`` to derive the digest.
        Idempotent: a second put of the same digest returns the existing
        URI without recopying.
        """
        ...

    def get(self, uri: str, dest_dir: Path) -> Path:
        """Materialise the bundle at ``uri`` into ``dest_dir``.

        ``dest_dir`` must be empty (raises :class:`FileExistsError` if
        anything is present). Raises :class:`BundleNotFoundError` if the
        URI does not resolve in this store,
        :class:`InvalidBundleURIError` if the URI is malformed or targets
        a different store. Returns ``dest_dir``.
        """
        ...

    def close(self) -> None:
        """Release store-held resources. Idempotent."""
        ...


@dataclass
class LocalDiskBundleStore:
    """Filesystem-backed bundle store rooted at ``root_dir/grade_bundles/``.

    ``put`` stages into ``root_dir/grade_bundles/.<digest>.<uuid4>.tmp/``
    (per-worker suffix isolates concurrent puts of the same digest — two
    workers never share a staging directory), then lands atomically via
    :func:`os.replace`. If ``root_dir/grade_bundles/<digest>/`` already
    exists, ``put`` short-circuits and returns the URI without copying.
    """

    root_dir: Path
    name: str = "local_disk"
    _bundles_root: Path = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.root_dir = Path(self.root_dir).expanduser().resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._bundles_root = self.root_dir / _BUNDLE_SUBDIR
        self._bundles_root.mkdir(parents=True, exist_ok=True)

    def put(self, bundle_dir: Path) -> str:
        manifest_path = bundle_dir / _MANIFEST_FILENAME
        digest = manifest_digest(manifest_path.read_bytes())
        final_dir = self._bundles_root / digest
        if final_dir.exists():
            return build_bundle_uri(self.name, digest)
        staging_dir = self._bundles_root / f".{digest}.{uuid.uuid4()}.tmp"
        shutil.copytree(bundle_dir, staging_dir)
        try:
            staging_dir.replace(final_dir)
        except OSError:
            if final_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)
            else:
                raise
        return build_bundle_uri(self.name, digest)

    def get(self, uri: str, dest_dir: Path) -> Path:
        store_name, digest = parse_bundle_uri(uri)
        if store_name != self.name:
            raise InvalidBundleURIError(
                f"URI {uri!r} targets store {store_name!r}; this store is {self.name!r}"
            )
        source = self._bundles_root / digest
        if not source.is_dir() or not (source / _MANIFEST_FILENAME).is_file():
            raise BundleNotFoundError(f"no bundle at {uri!r} under {self.root_dir}")
        dest_dir = Path(dest_dir)
        if dest_dir.exists() and any(dest_dir.iterdir()):
            raise FileExistsError(f"dest_dir {dest_dir} must be empty; found existing entries")
        shutil.copytree(source, dest_dir, dirs_exist_ok=True)
        return dest_dir

    def close(self) -> None:
        return None


_BOTO3_INSTALL_HINT = (
    "S3BundleStore requires boto3. Install the optional extra with "
    "'uv add tolokaforge[bundle-store-s3]' or 'pip install boto3'."
)


@dataclass
class S3BundleStore:
    """S3-compatible bundle store.

    Layout in the bucket::

        <prefix>/<digest>/manifest.json
        <prefix>/<digest>/initial_state.json
        <prefix>/<digest>/filesystem.tar
        ...

    ``put`` uploads every data part first, then ``manifest.json`` LAST —
    a crashed put leaves orphan parts but no manifest, so ``get`` fails
    cleanly with :class:`BundleNotFoundError`. Idempotency is a
    ``head_object`` on the manifest key: a second put of the same digest
    returns the URI without re-uploading any object.

    ``boto3`` is a lazy import: the pure module loads without it, and
    :meth:`put` / :meth:`get` build the client on first use if the caller
    did not inject one. Credentials come from the boto3 default chain
    (env vars, ``~/.aws/credentials``, EC2 / IRSA). To target
    S3-compatible endpoints (MinIO, Ceph) or a custom region, construct a
    boto3 client with ``endpoint_url=`` / ``region_name=`` and pass it as
    ``client=``.
    """

    bucket: str
    prefix: str = "grade_bundles"
    name: str = "s3"
    client: Any | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        self.prefix = self.prefix.strip("/")

    def _boto3_client(self) -> Any:
        if self.client is not None:
            return self.client
        try:
            import boto3
        except ImportError as e:
            raise RuntimeError(_BOTO3_INSTALL_HINT) from e
        self.client = boto3.client("s3")
        return self.client

    def _key(self, digest: str, rel: str) -> str:
        base = f"{self.prefix}/{digest}" if self.prefix else digest
        return f"{base}/{rel}"

    def _manifest_present(self, client: Any, digest: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            client.head_object(Bucket=self.bucket, Key=self._key(digest, _MANIFEST_FILENAME))
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"404", "NoSuchKey", "NotFound"} or status == 404:
                return False
            raise
        return True

    def put(self, bundle_dir: Path) -> str:
        bundle_dir = Path(bundle_dir)
        manifest_bytes = (bundle_dir / _MANIFEST_FILENAME).read_bytes()
        digest = manifest_digest(manifest_bytes)
        client = self._boto3_client()
        if self._manifest_present(client, digest):
            return build_bundle_uri(self.name, digest)
        manifest = json.loads(manifest_bytes)
        for rel in sorted(manifest.get("parts", {})):
            if rel == _MANIFEST_FILENAME:
                continue
            client.put_object(
                Bucket=self.bucket,
                Key=self._key(digest, rel),
                Body=(bundle_dir / rel).read_bytes(),
            )
        client.put_object(
            Bucket=self.bucket,
            Key=self._key(digest, _MANIFEST_FILENAME),
            Body=manifest_bytes,
        )
        return build_bundle_uri(self.name, digest)

    def get(self, uri: str, dest_dir: Path) -> Path:
        store_name, digest = parse_bundle_uri(uri)
        if store_name != self.name:
            raise InvalidBundleURIError(
                f"URI {uri!r} targets store {store_name!r}; this store is {self.name!r}"
            )
        dest_dir = Path(dest_dir)
        if dest_dir.exists() and any(dest_dir.iterdir()):
            raise FileExistsError(f"dest_dir {dest_dir} must be empty; found existing entries")
        client = self._boto3_client()
        if not self._manifest_present(client, digest):
            raise BundleNotFoundError(f"no bundle at {uri!r} in bucket {self.bucket!r}")
        dest_dir.mkdir(parents=True, exist_ok=True)
        manifest_bytes = client.get_object(
            Bucket=self.bucket, Key=self._key(digest, _MANIFEST_FILENAME)
        )["Body"].read()
        (dest_dir / _MANIFEST_FILENAME).write_bytes(manifest_bytes)
        manifest = json.loads(manifest_bytes)
        for rel in sorted(manifest.get("parts", {})):
            if rel == _MANIFEST_FILENAME:
                continue
            body = client.get_object(Bucket=self.bucket, Key=self._key(digest, rel))["Body"].read()
            target = dest_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        return dest_dir

    def close(self) -> None:
        if self.client is None:
            return
        close = getattr(self.client, "close", None)
        if callable(close):
            close()
        self.client = None
