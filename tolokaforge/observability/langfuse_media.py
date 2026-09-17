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
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tolokaforge.observability.attachments import (
    ATTACH_ALL,
    AttachCounts,
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
# the ingestion API rejects bodies over about 4.5 MB; batches stay well below (the connector's cap)
MAX_BATCH_BYTES = 3_500_000


def iter_batches(
    events: list[dict[str, Any]], *, batch_size: int = 40, max_bytes: int = MAX_BATCH_BYTES
) -> Iterator[list[dict[str, Any]]]:
    """Split events into ingestion batches of at most ``batch_size`` events whose serialised
    size stays under ``max_bytes`` (one oversized event still travels alone)."""
    batch: list[dict[str, Any]] = []
    size = 0
    for event in events:
        event_size = len(json.dumps(event).encode("utf-8")) + 1
        if batch and (size + event_size > max_bytes or len(batch) >= max(1, batch_size)):
            yield batch
            batch, size = [], 0
        batch.append(event)
        size += event_size
    if batch:
        yield batch


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
    ``/api/public/otel`` is kept, e.g. a reverse proxy mount). Userinfo in the URL is dropped:
    the base is logged, credentials never are."""
    parsed = urllib.parse.urlparse(endpoint)
    path = parsed.path or ""
    prefix = path[: path.index(OTEL_PATH_MARKER)] if OTEL_PATH_MARKER in path else ""
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return f"{parsed.scheme}://{host}{prefix}".rstrip("/")


def list_projects(
    api_base: str,
    headers: Mapping[str, str],
    *,
    timeout_s: float = 30.0,
    opener: Opener | None = None,
) -> list[str]:
    """The names of the projects the credentials in ``headers`` open (``GET
    /api/public/projects``; a project key lists exactly one). Raises ``LangfuseApiError`` on a
    non-2xx answer and lets connection errors propagate: the caller decides what an unreachable
    receiver means."""
    status, raw = (opener or urllib_opener)(
        "GET", f"{api_base.rstrip('/')}/api/public/projects", dict(headers), None, timeout_s
    )
    if not 200 <= status < 300:
        raise LangfuseApiError(f"GET /api/public/projects: HTTP {status}", status=status)
    try:
        answer = json.loads(raw or b"{}")
    except ValueError as exc:
        raise LangfuseApiError(
            "GET /api/public/projects: answer is not JSON", status=status
        ) from exc
    listed = answer.get("data") if isinstance(answer, dict) else None
    if not isinstance(listed, list):
        raise LangfuseApiError(
            "GET /api/public/projects: answer without a project list", status=status
        )
    return [str(p["name"]) for p in listed if isinstance(p, dict) and p.get("name")]


class LangfuseApiError(RuntimeError):
    """A Langfuse API call answered outside 2xx (the message names status and path, never a
    credential); ``status`` carries the HTTP status (None for a malformed answer)."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AttachBudgetExceeded(RuntimeError):
    """The trial's attachment budget is spent; the remaining files are counted as failed."""


class LangfuseAttachments:
    """Attach a persisted trial's files to its trace and write manifest v2 to the trace.

    Two limits keep the step from holding a run hostage (ADR-0046: never slow a trial
    materially): every request gets the smaller of ``timeout_s`` and what is left of the
    trial's ``budget_s``, and after ``breaker_failures`` consecutive trials whose step failed
    entirely (a receiver that is down or blackholed) the step switches itself off for the rest
    of the run and only counts.
    """

    def __init__(
        self,
        *,
        api_base: str,
        headers: Mapping[str, str] | None = None,
        mode: str = ATTACH_ALL,
        scan: SecretScan | None = None,
        timeout_s: float = 60.0,
        budget_s: float = 120.0,
        breaker_failures: int = 3,
        opener: Opener | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._headers = dict(headers or {})
        self._mode = mode
        self._scan = scan or SecretScan()
        self._timeout_s = max(1.0, timeout_s)
        self._budget_s = max(1.0, budget_s)
        self._breaker_failures = max(1, breaker_failures)
        self._open: Opener = opener or urllib_opener
        self._clock = clock or time.monotonic
        self._deadline: float | None = None  # the current trial's budget end
        self._consecutive_failures = 0
        self._tripped = False

    @property
    def tripped(self) -> bool:
        """True once the breaker switched the step off for the rest of the run."""
        return self._tripped

    @property
    def mode(self) -> str:
        """The ``attach`` mode this step was built with (``all`` / ``core`` / ``none``)."""
        return self._mode

    def ingest(
        self,
        events: list[dict[str, Any]],
        *,
        batch_size: int = 40,
        max_bytes: int = MAX_BATCH_BYTES,
    ) -> None:
        """Send ingestion events (``POST /api/public/ingestion``) in batches of at most
        ``batch_size`` events and ``max_bytes`` serialised bytes (the legacy API's payload cap);
        raises ``LangfuseApiError`` on an HTTP failure or a rejected event (the first rejection
        is named). Runs under the trial's budget when one is open, else under the plain
        timeout."""
        for batch in iter_batches(events, batch_size=batch_size, max_bytes=max_bytes):
            status, raw = self._call("POST", "/api/public/ingestion", {"batch": batch})
            if not 200 <= status < 300:
                raise LangfuseApiError(f"POST /api/public/ingestion: HTTP {status}", status=status)
            try:
                errors = json.loads(raw or b"{}").get("errors") or []
            except (ValueError, AttributeError) as exc:
                raise LangfuseApiError("ingestion answered without JSON") from exc
            if errors:
                first = errors[0] if isinstance(errors[0], dict) else {}
                raise LangfuseApiError(
                    f"ingestion rejected an event: {first.get('status')} {first.get('message')}"
                )

    def register_media(
        self, trace_id: str, observation_id: str | None, field: str, content_type: str, raw: bytes
    ) -> str | None:
        """Register (and upload when the receiver asks) inline bytes, e.g. a base64 image block
        of a message, on a trace or one of its observations; returns the media token, or None
        when no media id was issued or the bytes failed the data-safety scan. Runs under the
        open budget; raises like the file path does."""
        if self._scan.scan(raw):
            return None
        file = TrialFile(
            name="inline", original=raw, payload=raw, content_type=content_type, encoding="none"
        )
        registered = self._register_and_upload(
            trace_id, file, observation_id=observation_id, field=field
        )
        if registered is None:
            return None
        media_id, _ = registered
        return f"@@@langfuseMedia:type={content_type}|id={media_id}|source=bytes@@@"

    def scan_events(self, events: list[dict[str, Any]]) -> list[str]:
        """The outbound data-safety gate over the serialised ingestion events: the rules hit
        (empty when the bytes are clean); the caller sends nothing on a hit."""
        return self._scan.scan(json.dumps(events, ensure_ascii=False).encode("utf-8"))

    @contextmanager
    def budget(self) -> Iterator[None]:
        """Open the trial's budget for a sequence of calls (the attachment step and the
        trial-end projection share one); closed on exit whatever happened."""
        self._deadline = self._clock() + self._budget_s
        try:
            yield
        finally:
            self._deadline = None

    def _remaining(self) -> float:
        if self._deadline is None:
            return self._timeout_s
        left = self._deadline - self._clock()
        if left <= 0:
            raise AttachBudgetExceeded("attachment budget spent")
        return min(self._timeout_s, left)

    # -- the step -------------------------------------------------------------------------------

    def attach(
        self,
        trace_id: str,
        trial_dir: Path,
        *,
        trace_timestamp: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AttachCounts:
        """Register and upload every file of the trial directory (``mode``), then send the
        manifest together with ``metadata`` (the trial's final status, so the trace ends with it
        whatever order the receiver merged the spans in); returns the counts. Never raises."""
        counts, _ = self.attach_with_manifest(
            trace_id, trial_dir, trace_timestamp=trace_timestamp, metadata=metadata
        )
        return counts

    def attach_with_manifest(
        self,
        trace_id: str,
        trial_dir: Path,
        *,
        trace_timestamp: datetime | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[AttachCounts, dict[str, Any]]:
        """:meth:`attach`, also returning manifest v2 as sent (for the trial-end projection,
        whose trace metadata carries the same complete manifest). Runs under the budget the
        caller opened, else under one of its own."""
        counts = AttachCounts()
        manifest = build_manifest(trial_dir, [], [])
        attached: list[AttachedFile] = []
        skipped: list[dict[str, str]] = []
        try:
            files = plan_attachments(trial_dir, self._mode)
        except OSError as exc:
            _log.warning("attachments: cannot read %s: %s", trial_dir, exc)
            files = []
        if self._tripped:
            # the receiver gave up on earlier trials: count, do not wait again
            counts.failed = len(files)
            counts.manifests_failed = 1
            return counts, manifest
        own_budget = self._deadline is None
        if own_budget:
            self._deadline = self._clock() + self._budget_s
        budget_spent = False
        for file in files:
            if not allowed_attachment(file.name):
                skipped.append({"name": file.name, "rule": "file-type"})
                continue
            findings = self._scan.scan(file.original)
            if findings:
                skipped.append({"name": file.name, "rule": ", ".join(findings)})
                continue
            if budget_spent:
                counts.failed += 1
                skipped.append({"name": file.name, "rule": "upload-failed: budget spent"})
                continue
            try:
                registered = self._register_and_upload(trace_id, file)
            except AttachBudgetExceeded:
                budget_spent = True
                counts.failed += 1
                skipped.append({"name": file.name, "rule": "upload-failed: budget spent"})
                _log.warning(
                    "attachments: budget of %.0fs spent on trace %s", self._budget_s, trace_id
                )
                continue
            except Exception as exc:  # noqa: BLE001 - the observability layer only warns
                # only the class of a foreign exception is logged: a malformed presigned URL
                # would otherwise print its signature
                reason = str(exc) if isinstance(exc, LangfuseApiError) else type(exc).__name__
                _log.warning("attachments: %s of trace %s failed: %s", file.name, trace_id, reason)
                counts.failed += 1
                skipped.append({"name": file.name, "rule": f"upload-failed: {reason[:80]}"})
                continue
            if registered is None:
                counts.failed += 1
                skipped.append({"name": file.name, "rule": "no-media-id"})
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
        counts.skipped = len(skipped) - counts.failed  # failures are listed by name, not skips
        manifest = build_manifest(trial_dir, attached, skipped)
        if counts.failed:
            manifest["attachments_complete"] = False
        try:
            self._send_manifest(trace_id, {**dict(metadata or {}), **manifest}, trace_timestamp)
            counts.manifests_sent += 1
        except Exception as exc:  # noqa: BLE001
            reason = str(exc) if isinstance(exc, LangfuseApiError) else type(exc).__name__
            _log.warning("attachments: manifest of trace %s not sent: %s", trace_id, reason)
            counts.manifests_failed += 1
        if own_budget:
            self._deadline = None
        # the breaker: a trial whose every attempt failed (nothing registered, nothing sent)
        # counts against the receiver; a trial that got anything through resets the count
        if files and counts.registered == 0 and counts.manifests_sent == 0:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._breaker_failures and not self._tripped:
                self._tripped = True
                _log.warning(
                    "attachments: %d trial(s) in a row reached nothing at %s; the step is off for"
                    " the rest of the run",
                    self._consecutive_failures,
                    self._api_base,
                )
        elif counts.registered or counts.manifests_sent:
            self._consecutive_failures = 0
        return counts, manifest

    def note_trial_outcome(self, *, reached: bool) -> None:
        """Feed the breaker from the trial-end projection when no file step ran (``attach:
        none``): a trial whose pass reached nothing counts against the receiver."""
        if reached:
            self._consecutive_failures = 0
            return
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._breaker_failures and not self._tripped:
            self._tripped = True
            _log.warning(
                "attachments: %d trial(s) in a row reached nothing at %s; the step is off for"
                " the rest of the run",
                self._consecutive_failures,
                self._api_base,
            )

    # -- Langfuse calls -------------------------------------------------------------------------

    def _call(self, method: str, path: str, body: dict[str, Any] | None) -> tuple[int, bytes]:
        headers = dict(self._headers)
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        return self._open(method, f"{self._api_base}{path}", headers, data, self._remaining())

    def _register_and_upload(
        self,
        trace_id: str,
        file: TrialFile,
        *,
        observation_id: str | None = None,
        field: str = MEDIA_FIELD,
    ) -> tuple[str, bool] | None:
        """Register (link) the file's bytes on the trace (or on ``observation_id`` under
        ``field``); PUT them only when Langfuse asks. Returns (media id, uploaded by this call)
        or None when no media id was issued."""
        digest = hashlib.sha256(file.payload).digest()
        body: dict[str, Any] = {
            "traceId": trace_id,
            "field": field,
            "contentType": file.content_type,
            "contentLength": len(file.payload),
            "sha256Hash": base64.b64encode(digest).decode(),
        }
        if observation_id:
            body["observationId"] = observation_id
        status, raw = self._call("POST", "/api/public/media", body)
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
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
        if parsed.scheme not in ("http", "https") or not host:
            # never let a malformed URL (and the signature in it) reach a log line
            raise LangfuseApiError("presigned upload URL is not an http(s) URL")
        headers = {"Content-Type": file.content_type}
        if host.endswith(".blob.core.windows.net"):
            headers["x-ms-blob-type"] = "BlockBlob"
        else:
            headers["x-amz-checksum-sha256"] = base64.b64encode(
                hashlib.sha256(file.payload).digest()
            ).decode()
        # no credential header: the URL carries its own signature; the PUT gets the remaining
        # budget (a large trajectory needs more than one API timeout, never more than the trial)
        try:
            status, _ = self._open("PUT", url, headers, file.payload, self._remaining_put())
        except AttachBudgetExceeded:
            raise
        except (OSError, ValueError) as exc:
            raise LangfuseApiError(f"presigned PUT to {host} failed: {type(exc).__name__}") from exc
        return status

    def _remaining_put(self) -> float:
        if self._deadline is None:
            return self._timeout_s * 5
        left = self._deadline - self._clock()
        if left <= 0:
            raise AttachBudgetExceeded("attachment budget spent")
        return left

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
        self.ingest([event])
