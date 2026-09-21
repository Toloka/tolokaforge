"""The OTLP/HTTP span exporter, and the one that posts a batch **exactly once**.

A receiver whose observations are append-only cannot delete a row, so a batch written twice is
two rows forever (ADR-0048). A batch that never arrived is recoverable: the run says so in its
receipt and the offline sibling completes the trace. So on that family the transport trades every
repeat away, and the guarantee has to hold for the **physical** request, not just for the
exporter's own loop.

Three repeats have to be off, and the stock exporter leaves two of them on:

- its ``export()`` runs a retry loop of its own (``_MAX_RETRYS``, six attempts);
- its ``_export()`` re-posts the same bytes in an ``except ConnectionError`` branch, which is
  exactly the case that matters: the receiver took the body and the answer was lost. Skipping
  only the outer loop therefore still double-posts, which is why :class:`SingleAttemptSpanExporter`
  owns the ``session.post`` call instead of delegating to ``_export``;
- ``requests`` follows a 307 or 308 by re-sending the body, and a session's adapter can carry a
  retry policy of its own (a caller-supplied session through
  ``OTEL_PYTHON_EXPORTER_OTLP_HTTP_TRACES_CREDENTIAL_PROVIDER`` may), so redirects are refused and
  the endpoint's adapter is mounted with no retries.

Engine-free by construction: both producers need this guarantee, and the offline connector imports
it next to any engine pin, or with none.
"""

from __future__ import annotations

import gzip
import logging
import zlib
from collections.abc import Mapping, Sequence
from io import BytesIO
from typing import Any

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

_log = logging.getLogger(__name__)

# Langfuse v4 takes OTLP on the documented direct path behind this header; a measurement on an
# idle deployment found no latency benefit, but the vendor documents it as the path
INGESTION_VERSION_HEADER = "x-langfuse-ingestion-version"
INGESTION_VERSION = "4"

# what :class:`SingleAttemptSpanExporter` reads off the SDK's exporter to post for itself; they
# are instance attributes, so they are checked on the built object, not on the class
REQUIRED_EXPORTER_ATTRIBUTES = (
    "_session",
    "_endpoint",
    "_timeout",
    "_compression",
    "_certificate_file",
    "_client_cert",
)


class SingleAttemptUnavailable(RuntimeError):
    """This OpenTelemetry SDK cannot be asked to post a batch once."""


def _single_attempt_exporter_class() -> type | None:
    """An ``OTLPSpanExporter`` that posts a batch **once**, or None when the SDK has moved on.

    It reads the SDK exporter's own configuration (the session, the endpoint, the timeout, the
    compression and the certificates) and makes the request itself, so none of the three repeats
    described in this module's docstring can happen. A version that no longer offers those
    internals returns None here, and :func:`make_otlp_exporter` refuses the run rather than
    falling back to a retrying exporter."""
    try:
        from opentelemetry.exporter.otlp.proto.http import Compression
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
            encode_spans,
        )
        from requests.adapters import HTTPAdapter
    except ImportError:
        return None

    class SingleAttemptSpanExporter(OTLPSpanExporter):  # type: ignore[misc, valid-type]
        """One POST per batch: a repeat could duplicate what the receiver already wrote."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            session, endpoint = getattr(self, "_session", None), getattr(self, "_endpoint", "")
            if session is not None and endpoint:
                # a caller-supplied session may carry an adapter that retries on its own
                session.mount(str(endpoint), HTTPAdapter(max_retries=0))

        def _body(self, spans: Sequence[ReadableSpan]) -> bytes:
            """The encoded batch under the exporter's own compression setting."""
            raw: bytes = encode_spans(spans).SerializePartialToString()
            if self._compression is Compression.Gzip:
                buffer = BytesIO()
                with gzip.GzipFile(fileobj=buffer, mode="w") as stream:
                    stream.write(raw)
                return buffer.getvalue()
            if self._compression is Compression.Deflate:
                return zlib.compress(raw)
            return raw

        def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:  # type: ignore[override]
            if getattr(self, "_shutdown", False):
                return SpanExportResult.FAILURE
            try:
                # the one request: no redirect is followed (that would re-send the body) and the
                # adapter above makes no attempt of its own, so this is the only POST
                answer = self._session.post(
                    url=self._endpoint,
                    data=self._body(spans),
                    verify=self._certificate_file,
                    timeout=self._timeout,
                    cert=self._client_cert,
                    allow_redirects=False,
                )
            except Exception as exc:  # noqa: BLE001 - the queue counts and reports a failure
                # the receiver may still have written this batch: the caller treats a failure as
                # unconfirmed, never as "certainly not written" (ADR-0048)
                _log.warning("span export failed: %s", type(exc).__name__)
                return SpanExportResult.FAILURE
            if getattr(answer, "ok", False):
                return SpanExportResult.SUCCESS
            _log.warning("span export refused: HTTP %s", getattr(answer, "status_code", "unknown"))
            return SpanExportResult.FAILURE

    return SingleAttemptSpanExporter


def make_otlp_exporter(
    endpoint: str,
    headers: Mapping[str, str] | None = None,
    *,
    ingestion_version: str | None = INGESTION_VERSION,
    retry: bool = True,
) -> SpanExporter:
    """The standard OTLP/HTTP span exporter; ``OTEL_EXPORTER_OTLP_HEADERS`` supplies the
    receiver's credentials when ``headers`` is not given, and is never logged here.

    ``ingestion_version`` adds Langfuse's ``x-langfuse-ingestion-version`` header, which selects
    the receiver's direct ingestion path; the v3 family is not sent it at all. It joins
    caller-supplied headers only: with none, the SDK's own environment variable owns the header
    set.

    ``retry=False`` posts each batch exactly once (:func:`_single_attempt_exporter_class`), which
    is what a write-once receiver needs. When this SDK no longer offers what that requires, it
    raises :class:`SingleAttemptUnavailable` instead of returning a retrying exporter: a
    duplicate on such a receiver cannot be deleted, so the run has to stop rather than risk
    one."""
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    merged = dict(headers) if headers else {}
    if merged and ingestion_version:
        merged.setdefault(INGESTION_VERSION_HEADER, ingestion_version)
    if retry:
        return OTLPSpanExporter(endpoint=endpoint, headers=merged or None)

    single = _single_attempt_exporter_class()
    if single is None:
        raise SingleAttemptUnavailable(_REFUSAL)
    exporter: SpanExporter = single(endpoint=endpoint, headers=merged or None)
    missing = [name for name in REQUIRED_EXPORTER_ATTRIBUTES if not hasattr(exporter, name)]
    if missing:
        # the subclass posts for itself off these; without them it would have to delegate to the
        # SDK's own ``_export``, which re-posts on a lost connection
        raise SingleAttemptUnavailable(f"{_REFUSAL} (missing: {', '.join(missing)})")
    return exporter


_REFUSAL = (
    "the OTLP exporter of this OpenTelemetry SDK cannot be asked to post a batch exactly once, "
    "and a repeated batch the receiver already wrote would be a duplicate it cannot delete: pin "
    "an SDK this package supports, or write for the v3 family"
)
