"""The OTLP/HTTP span exporter, with an optional single-attempt transport.

The v4 producer layout deliberately avoids re-sending observations (ADR-0048). On the measured
Langfuse 4.38.0 ``events_only`` receiver, a re-sent observation id is an update, last write wins.
One attempt avoids unnecessary requests and unintended overwrites; it does not guarantee delivery.
The receipt reports failed exports so the offline sibling can recover missing observations.

Three repeats have to be off, and the stock exporter leaves two of them on:

- its ``export()`` runs a retry loop of its own (six attempts);
- a lost connection is posted again with the same bytes (``_export()``'s ``except
  ConnectionError`` branch; from OpenTelemetry 1.45 its OTLP client's ``_submit()``), which is
  exactly the case that matters: the receiver took the body and the answer was lost. Skipping
  only the outer loop therefore still double-posts, which is why :class:`SingleAttemptSpanExporter`
  owns the ``session.post`` call instead of delegating to the SDK;
- ``requests`` follows a 307 or 308 by re-sending the body, and a session's adapter can carry a
  retry policy of its own (a caller-supplied session through
  ``OTEL_PYTHON_EXPORTER_OTLP_HTTP_TRACES_CREDENTIAL_PROVIDER`` may), so redirects are refused and
  the endpoint's adapter is mounted with no retries.

That one post goes through a ``requests`` session on every supported SDK. Up to OpenTelemetry 1.44
the exporter holds one; from 1.45 it holds an OTLP client over a transport, urllib3 unless the
exporter is given a session, so the single-attempt exporter gives it one.

Engine-free by construction: both producers need this guarantee, and the offline connector imports
it next to any engine pin, or with none.
"""

from __future__ import annotations

import gzip
import logging
import os
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

# what :class:`SingleAttemptSpanExporter` reads off the SDK's exporter to post for itself, checked
# on the built object (they are instance attributes). Up to OpenTelemetry 1.44 the exporter keeps
# the request settings itself and the headers on its session:
SESSION_LAYOUT = (
    "_session",
    "_endpoint",
    "_timeout",
    "_compression",
    "_certificate_file",
    "_client_cert",
)
# from 1.45 an OTLP client keeps the resolved headers and timeout, and the session (taken from the
# client's transport) the certificates:
CLIENT_LAYOUT = (
    "_session",
    "_endpoint",
    "_compression",
    "_client._headers",
    "_client._timeout",
)

# a session named by one of these is the SDK's to load (its generic variable, then the traces one)
_CREDENTIAL_PROVIDER_VARIABLES = (
    "OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER",
    "OTEL_PYTHON_EXPORTER_OTLP_HTTP_TRACES_CREDENTIAL_PROVIDER",
)


class SingleAttemptUnavailable(RuntimeError):
    """This OpenTelemetry SDK cannot be asked to post a batch once."""


def _single_attempt_exporter_class() -> type | None:
    """An ``OTLPSpanExporter`` that posts a batch **once**, or None when the SDK has moved on.

    It reads the SDK exporter's own configuration (the session, the endpoint, the headers, the
    timeout, the compression and the certificates) and makes the request itself, so none of the
    three repeats described in this module's docstring can happen. An SDK that no longer offers
    those internals fails :func:`make_otlp_exporter`'s check, which refuses the run rather than
    falling back to a retrying exporter."""
    try:
        import requests
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
            encode_spans,
        )
        from requests.adapters import HTTPAdapter
    except ImportError:
        return None

    class SingleAttemptSpanExporter(OTLPSpanExporter):  # type: ignore[misc, valid-type]
        """One POST attempt per batch, without automatic repeats or overwrites."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            if kwargs.get("session") is None and not _credential_provider_named():
                # up to 1.44 the SDK makes this session itself; from 1.45 a session selects its
                # requests transport, which the single post goes through
                kwargs["session"] = requests.Session()
            super().__init__(*args, **kwargs)
            client = getattr(self, "_client", None)
            if client is not None:
                session = getattr(getattr(client, "_transport", None), "_session", None)
                if isinstance(session, requests.Session):
                    self._session = session
            session, endpoint = getattr(self, "_session", None), getattr(self, "_endpoint", "")
            if session is not None and endpoint:
                # a caller-supplied session may carry an adapter that retries on its own
                session.mount(str(endpoint), HTTPAdapter(max_retries=0))

        def _settings(self) -> dict[str, Any]:
            """The request settings where this SDK resolved them (see :data:`CLIENT_LAYOUT`).

            The certificates are always passed with the request, as up to 1.44, so that a
            ``REQUESTS_CA_BUNDLE`` cannot replace the receiver's configured certificate."""
            client = getattr(self, "_client", None)
            if client is None:
                return {
                    "verify": self._certificate_file,
                    "cert": self._client_cert,
                    "timeout": self._timeout,
                }
            return {
                "headers": client._headers,
                "timeout": client._timeout,
                "verify": self._session.verify,
                "cert": self._session.cert,
            }

        def _body(self, spans: Sequence[ReadableSpan]) -> bytes:
            """The encoded batch under the exporter's own compression setting; 1.45 replaced the
            compression enum, its values stayed."""
            raw: bytes = encode_spans(spans).SerializePartialToString()
            compression = getattr(self._compression, "value", self._compression)
            if compression == "gzip":
                buffer = BytesIO()
                with gzip.GzipFile(fileobj=buffer, mode="w") as stream:
                    stream.write(raw)
                return buffer.getvalue()
            if compression == "deflate":
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
                    allow_redirects=False,
                    **self._settings(),
                )
            except Exception as exc:  # noqa: BLE001 - the queue counts and reports a failure
                # the receiver may still have written this batch: the caller treats a failure as
                # unconfirmed, never as "certainly not written" (ADR-0048)
                _log.warning("span export failed: %s", type(exc).__name__)
                return SpanExportResult.FAILURE
            if 200 <= answer.status_code < 300:
                return SpanExportResult.SUCCESS
            _log.warning("span export refused: HTTP %s", getattr(answer, "status_code", "unknown"))
            return SpanExportResult.FAILURE

    return SingleAttemptSpanExporter


def _credential_provider_named() -> bool:
    return any(os.environ.get(name) for name in _CREDENTIAL_PROVIDER_VARIABLES)


def _has(obj: object, dotted: str) -> bool:
    for name in dotted.split("."):
        if not hasattr(obj, name):
            return False
        obj = getattr(obj, name)
    return True


def make_otlp_exporter(
    endpoint: str,
    headers: Mapping[str, str] | None = None,
    *,
    ingestion_version: str | None = INGESTION_VERSION,
    retry: bool = True,
) -> SpanExporter:
    """The standard OTLP/HTTP span exporter; ``OTEL_EXPORTER_OTLP_HEADERS`` supplies the
    receiver's credentials when ``headers`` is not given, and is never logged here. From
    OpenTelemetry 1.45 the SDK also merges that variable under given ``headers``, whose keys win.

    ``ingestion_version`` adds Langfuse's ``x-langfuse-ingestion-version`` header, which selects
    the receiver's direct ingestion path; the v3 family is not sent it at all. It joins
    caller-supplied headers only: with none, the SDK's own environment variable owns the header
    set.

    ``retry=False`` makes one POST attempt per batch (:func:`_single_attempt_exporter_class`),
    accepting only 2xx responses as successful exports. When this SDK no longer offers what
    that requires (neither :data:`SESSION_LAYOUT` nor :data:`CLIENT_LAYOUT` is complete), it
    raises :class:`SingleAttemptUnavailable` rather than silently changing the requested
    delivery policy to a retrying exporter."""
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
    layout = CLIENT_LAYOUT if hasattr(exporter, "_client") else SESSION_LAYOUT
    missing = [name for name in layout if not _has(exporter, name)]
    if missing:
        # the subclass posts for itself off these; without them it would have to delegate to the
        # SDK's own retrying request path, which re-posts on a lost connection
        raise SingleAttemptUnavailable(f"{_REFUSAL} (missing: {', '.join(missing)})")
    return exporter


_REFUSAL = (
    "the OTLP exporter of this OpenTelemetry SDK cannot enforce the single-attempt delivery "
    "policy: pin an SDK this package supports"
)
