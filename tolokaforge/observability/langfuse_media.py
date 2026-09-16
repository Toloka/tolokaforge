"""Langfuse-side attachment upload for the live exporter (ADR-0046, amendment of 2026-09-16).

Receiver-specific by nature, hence next to the OTLP observer behind the ``otel`` extra: the files
of a persisted trial reach the trace through Langfuse's media REST API (``POST /api/public/media``
registers bytes for a trace and answers with a presigned upload URL unless the project already
holds bytes of that sha256; the presigned ``PUT`` wants ``x-ms-blob-type: BlockBlob`` on Azure
Blob; the confirmation ``PATCH`` reports ``uploadHttpStatus: 200`` so the dedup works), and the
manifest reaches the trace-level metadata through the legacy ingestion API (a ``trace-create``
event under the same trace id the OTLP spans used; metadata keys merge, so the update adds the
manifest and touches nothing the spans wrote). The base URL derives from the OTLP endpoint, the
headers are the OTLP exporter's own (the Basic credential of ``OTEL_EXPORTER_OTLP_HEADERS``), the
transport is the standard library. Every request is bounded by one timeout, a failing file is
counted and leaves ``attachments_complete: false``; nothing here raises into the trial.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tolokaforge.observability.attachments import (
    ATTACH_ALL,
    AttachedFile,
    SecretScan,
    TrialFile,
    allowed_attachment,
    build_manifest,
    plan_attachments,
)

_log = logging.getLogger(__name__)

MEDIA_FIELD = "metadata"
OTEL_PATH_MARKER = "/api/public/otel"

# method, url, headers, body, timeout -> (status, response body)
Opener = Callable[[str, str, Mapping[str, str], "bytes | None", float], "tuple[int, bytes]"]


def urllib_opener(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
) -> tuple[int, bytes]:
    """The default transport: one request, one timeout, the status and body back (an HTTP error
    status is a result, not an exception; connection errors propagate)."""
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


def api_base_from_endpoint(endpoint: str) -> str:
    """``https://host/api/public/otel/v1/traces`` -> ``https://host`` (a path prefix before
    ``/api/public/otel`` is kept, e.g. a reverse proxy mount)."""
    parsed = urllib.parse.urlparse(endpoint)
    path = parsed.path or ""
    prefix = path[: path.index(OTEL_PATH_MARKER)] if OTEL_PATH_MARKER in path else ""
    return f"{parsed.scheme}://{parsed.netloc}{prefix}".rstrip("/")


class LangfuseApiError(RuntimeError):
    """A Langfuse API call answered outside 2xx (the message names status and path, never a
    credential)."""


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


class LangfuseAttachments:
    """Attach a persisted trial's files to its trace and write manifest v2 to the trace."""

    def __init__(
        self,
        *,
        api_base: str,
        headers: Mapping[str, str] | None = None,
        mode: str = ATTACH_ALL,
        scan: SecretScan | None = None,
        timeout_s: float = 60.0,
        put_timeout_s: float = 300.0,
        opener: Opener | None = None,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._headers = dict(headers or {})
        self._mode = mode
        self._scan = scan or SecretScan()
        self._timeout_s = timeout_s
        self._put_timeout_s = put_timeout_s
        self._open: Opener = opener or urllib_opener

    # -- the step -------------------------------------------------------------------------------

    def attach(
        self, trace_id: str, trial_dir: Path, *, trace_timestamp: datetime | None = None
    ) -> AttachCounts:
        """Register and upload every file of the trial directory (``mode``), then send the
        manifest; returns the counts. Never raises."""
        counts = AttachCounts()
        attached: list[AttachedFile] = []
        skipped: list[dict[str, str]] = []
        try:
            files = plan_attachments(trial_dir, self._mode)
        except OSError as exc:
            _log.warning("attachments: cannot read %s: %s", trial_dir, exc)
            files = []
        for file in files:
            if not allowed_attachment(file.name):
                skipped.append({"name": file.name, "rule": "file-type"})
                continue
            findings = self._scan.scan(file.original)
            if findings:
                skipped.append({"name": file.name, "rule": ", ".join(findings)})
                continue
            try:
                registered = self._register_and_upload(trace_id, file)
            except Exception as exc:  # noqa: BLE001 - the observability layer only warns
                _log.warning("attachments: %s of trace %s failed: %s", file.name, trace_id, exc)
                counts.failed += 1
                continue
            if registered is None:
                counts.failed += 1
                continue
            media_id, uploaded = registered
            counts.registered += 1
            if uploaded:
                counts.uploaded += 1
            else:
                counts.deduplicated += 1
            attached.append(
                AttachedFile(
                    file=file,
                    media_id=media_id,
                    token=f"@@@langfuseMedia:type={file.content_type}|id={media_id}|source=bytes@@@",
                )
            )
        counts.skipped = len(skipped)
        manifest = build_manifest(trial_dir, attached, skipped)
        if counts.failed:
            manifest["attachments_complete"] = False
        try:
            self._send_manifest(trace_id, manifest, trace_timestamp)
            counts.manifests_sent += 1
        except Exception as exc:  # noqa: BLE001
            _log.warning("attachments: manifest of trace %s not sent: %s", trace_id, exc)
            counts.manifests_failed += 1
        return counts

    # -- Langfuse calls -------------------------------------------------------------------------

    def _call(self, method: str, path: str, body: dict[str, Any] | None) -> tuple[int, bytes]:
        headers = dict(self._headers)
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        return self._open(method, f"{self._api_base}{path}", headers, data, self._timeout_s)

    def _register_and_upload(self, trace_id: str, file: TrialFile) -> tuple[str, bool] | None:
        """Register (link) the file's bytes on the trace; PUT them only when Langfuse asks.
        Returns (media id, uploaded by this call) or None when no media id was issued."""
        digest = hashlib.sha256(file.payload).digest()
        status, raw = self._call(
            "POST",
            "/api/public/media",
            {
                "traceId": trace_id,
                "field": MEDIA_FIELD,
                "contentType": file.content_type,
                "contentLength": len(file.payload),
                "sha256Hash": base64.b64encode(digest).decode(),
            },
        )
        if not 200 <= status < 300:
            raise LangfuseApiError(f"POST /api/public/media: HTTP {status}")
        answer = json.loads(raw or b"{}")
        media_id = answer.get("mediaId")
        if not media_id:
            return None
        upload_url = answer.get("uploadUrl")  # absent when the project already holds the bytes
        if not upload_url:
            return str(media_id), False
        put_status = self._put_presigned(upload_url, file)
        if not 200 <= put_status < 300:
            raise LangfuseApiError(f"presigned PUT for media {media_id}: HTTP {put_status}")
        status, _ = self._call(
            "PATCH",
            f"/api/public/media/{media_id}",
            {
                "uploadedAt": datetime.now(timezone.utc).isoformat(),
                # any 2xx is "stored": Azure answers 201, Langfuse dedups only on 200
                "uploadHttpStatus": 200,
                "uploadHttpError": None,
            },
        )
        if not 200 <= status < 300:
            raise LangfuseApiError(f"PATCH /api/public/media/{media_id}: HTTP {status}")
        return str(media_id), True

    def _put_presigned(self, url: str, file: TrialFile) -> int:
        host = urllib.parse.urlparse(url).hostname or ""
        headers = {"Content-Type": file.content_type}
        if host.endswith(".blob.core.windows.net"):
            headers["x-ms-blob-type"] = "BlockBlob"
        else:
            headers["x-amz-checksum-sha256"] = base64.b64encode(
                hashlib.sha256(file.payload).digest()
            ).decode()
        # no credential header: the URL carries its own signature
        status, _ = self._open("PUT", url, headers, file.payload, self._put_timeout_s)
        return status

    def _send_manifest(
        self, trace_id: str, manifest: dict[str, Any], trace_timestamp: datetime | None
    ) -> None:
        """A ``trace-create`` under the trace id the spans used: metadata keys merge on the
        Langfuse side, so this adds the manifest and leaves name, session, tags and timestamp as
        the spans set them (the trial start is re-sent as the timestamp when known)."""
        body: dict[str, Any] = {"id": trace_id, "metadata": manifest}
        if trace_timestamp is not None:
            body["timestamp"] = trace_timestamp.astimezone(timezone.utc).isoformat()
        event = {
            "id": uuid.uuid4().hex,
            "type": "trace-create",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "body": body,
        }
        status, raw = self._call("POST", "/api/public/ingestion", {"batch": [event]})
        if not 200 <= status < 300:
            raise LangfuseApiError(f"POST /api/public/ingestion: HTTP {status}")
        errors = json.loads(raw or b"{}").get("errors") or []
        if errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            raise LangfuseApiError(
                f"ingestion rejected the manifest event: {first.get('status')} {first.get('message')}"
            )
