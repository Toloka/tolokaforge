"""The OTLP/HTTP span exporter, with an optional single-attempt transport.

The v4 producer layout deliberately avoids re-sending observations (ADR-0048). On the measured
Langfuse 4.38.0 ``events_only`` receiver, a re-sent observation id is an update, last write wins.
One attempt avoids unnecessary requests and unintended overwrites; it does not guarantee delivery.
The receipt reports failed exports so the offline sibling can recover missing observations.

Three repeats have to be off, and the SDK's exporter leaves two of them on:

- its ``export()`` runs a retry loop of its own (six attempts);
- its request path posts a lost connection again with the same bytes, which is exactly the case
  that matters: the receiver took the body and the answer was lost;
- ``requests`` follows a 307 or 308 by re-sending the body, and a session's adapter can carry a
  retry policy of its own.

So the single-attempt exporter does not go through the SDK's exporter at all: it builds the OTLP
request itself. The body is the batch as the SDK's public OTLP encoder writes it
(``opentelemetry.exporter.otlp.proto.common.trace_encoder.encode_spans``, the protobuf message of
the OTLP/HTTP specification), sent in one POST through a ``requests`` session of its own, with
redirects refused and the endpoint's adapter mounted with no retries. Its settings are the
caller's endpoint and headers plus the standard exporter variables for the timeout, the
compression and the certificates (``OTEL_EXPORTER_OTLP_TRACES_*``, then ``OTEL_EXPORTER_OTLP_*``).
It reads nothing an SDK keeps private: the encoder's public module is all it takes from the SDK's
exporter packages, and an install without it refuses the run at start rather than retrying.

A caller may hand it a :class:`~tolokaforge_langfuse.retry.Retrier`: the same bytes are then
posted again after an answer that proves the receiver did not read them (the gateway's block
page, 429, 503 by default), and after nothing else (ADR-0048, amendment 2026-10-07). Without one
it stays at one POST per batch.

Engine-free by construction: both producers need this guarantee, and the offline connector imports
it next to any engine pin, or with none.
"""

from __future__ import annotations

import gzip
import logging
import math
import os
import zlib
from collections.abc import Mapping, Sequence

from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from tolokaforge_langfuse import __version__
from tolokaforge_langfuse.retry import Answer, Retrier

_log = logging.getLogger(__name__)

# Langfuse v4 takes OTLP on the documented direct path behind this header; a measurement on an
# idle deployment found no latency benefit, but the vendor documents it as the path
INGESTION_VERSION_HEADER = "x-langfuse-ingestion-version"
INGESTION_VERSION = "4"

# the OTLP/HTTP binary encoding
PROTOBUF_CONTENT_TYPE = "application/x-protobuf"
USER_AGENT = f"tolokaforge-langfuse/{__version__}"
# the SDK exporter's default; the timeout variables are read in seconds, as the Python SDK reads
# them (the specification says milliseconds), so a deployment's value keeps its meaning
DEFAULT_TIMEOUT_SECONDS = 10.0
COMPRESSIONS = ("none", "gzip", "deflate")

# a session named by one of these is loaded by the SDK's exporter, which the single-attempt
# exporter does not use, so naming one refuses the run rather than being ignored
_CREDENTIAL_PROVIDER_VARIABLES = (
    "OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER",
    "OTEL_PYTHON_EXPORTER_OTLP_HTTP_TRACES_CREDENTIAL_PROVIDER",
)


class SingleAttemptUnavailable(RuntimeError):
    """The single-attempt exporter cannot be built here, so a batch cannot be posted once."""


def _standard_variable(name: str) -> str | None:
    """One of the exporter's standard settings: the traces variable, then the generic one."""
    return (
        os.environ.get(f"OTEL_EXPORTER_OTLP_TRACES_{name}")
        or os.environ.get(f"OTEL_EXPORTER_OTLP_{name}")
        or None
    )


def _timeout() -> float:
    raw = _standard_variable("TIMEOUT")
    if raw is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        seconds = float(raw)
    except ValueError:
        seconds = math.nan
    # requests would take a zero, negative or non-finite timeout and fail every post unsent
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(
            f"OTEL_EXPORTER_OTLP_(TRACES_)TIMEOUT={raw!r} is not a positive number of seconds"
        )
    return seconds


def _compression() -> str:
    value = (_standard_variable("COMPRESSION") or "none").strip().lower()
    if value not in COMPRESSIONS:
        raise ValueError(
            f"OTEL_EXPORTER_OTLP_(TRACES_)COMPRESSION={value!r} is not one of {', '.join(COMPRESSIONS)}"
        )
    return value


def _certificates() -> tuple[bool | str, str | tuple[str, str] | None]:
    """What the receiver is verified against and the client certificate presented to it. They go
    with each request, so ``REQUESTS_CA_BUNDLE`` cannot replace a configured certificate."""
    verify: bool | str = _standard_variable("CERTIFICATE") or True
    client_certificate = _standard_variable("CLIENT_CERTIFICATE")
    client_key = _standard_variable("CLIENT_KEY")
    if client_certificate and client_key:
        return verify, (client_certificate, client_key)
    return verify, client_certificate


def _single_attempt_exporter_class() -> type | None:
    """:class:`SingleAttemptSpanExporter`, or None when this install lacks what it posts with
    (``requests``, or the SDK's OTLP encoder)."""
    try:
        import requests
        from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
        from requests.adapters import HTTPAdapter
    except ImportError:
        return None

    def _the_callers_headers(request: requests.PreparedRequest) -> requests.PreparedRequest:
        # requests' auth hook as a no-op: with one set, requests does not look the endpoint up in
        # a netrc file, whose entry would replace the caller's Authorization header
        return request

    class SingleAttemptSpanExporter(SpanExporter):
        """One POST attempt per batch, without automatic repeats or overwrites; with a
        ``retrier``, the same bytes again after a refusal its policy names, and only then."""

        def __init__(
            self,
            endpoint: str,
            headers: Mapping[str, str] | None = None,
            *,
            retrier: Retrier | None = None,
        ) -> None:
            self._endpoint = endpoint
            self._retrier = retrier
            self._timeout = _timeout()
            self._compression = _compression()
            self._verify, self._cert = _certificates()
            self._headers = {
                "User-Agent": USER_AGENT,
                **(headers or {}),
                "Content-Type": PROTOBUF_CONTENT_TYPE,
            }
            if self._compression != "none":
                self._headers["Content-Encoding"] = self._compression
            # A caller reads each answer through this session's response hooks (the offline
            # connector tells a rate limit apart this way). requests' own adapters make no
            # retries; the endpoint's is mounted anyway, so the guarantee does not rest on that.
            self._session = requests.Session()
            self._session.mount(endpoint, HTTPAdapter(max_retries=0))
            self._session.auth = _the_callers_headers
            self._shutdown = False

        def _body(self, spans: Sequence[ReadableSpan]) -> bytes:
            raw: bytes = encode_spans(spans).SerializePartialToString()
            if self._compression == "gzip":
                return gzip.compress(raw)
            if self._compression == "deflate":
                return zlib.compress(raw)
            return raw

        def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
            if self._shutdown:
                return SpanExportResult.FAILURE
            retrier = self._retrier
            try:
                body = self._body(spans)
                if retrier is None:
                    answer = self._post(body)
                else:
                    answer = retrier.run(lambda: self._post(body), what="span export")
            except Exception as exc:  # noqa: BLE001 - the queue counts and reports a failure
                # the receiver may still have written this batch: the caller treats a failure as
                # unconfirmed, never as "certainly not written" (ADR-0048)
                _log.warning("span export failed: %s", type(exc).__name__)
                return SpanExportResult.FAILURE
            if answer.ok:
                return SpanExportResult.SUCCESS
            if retrier is None or not retrier.retries(answer):
                # a refusal the retrier gave up on has said so already
                _log.warning("span export refused: HTTP %s", answer.status)
            return SpanExportResult.FAILURE

        def _post(self, body: bytes) -> Answer:
            """One POST: no redirect is followed (that would re-send the body) and the adapter
            above makes no attempt of its own, so this is one request on the wire."""
            response = self._session.post(
                self._endpoint,
                data=body,
                headers=self._headers,
                timeout=self._timeout,
                verify=self._verify,
                cert=self._cert,
                allow_redirects=False,
            )
            return Answer(
                response.status_code,
                b"" if response.ok else response.content,
                response.headers.get("Retry-After"),
            )

        def shutdown(self) -> None:
            self._shutdown = True
            if self._retrier is not None:
                self._retrier.cancel()
            self._session.close()

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            # nothing is buffered here: an export returns once its one request has
            return True

    return SingleAttemptSpanExporter


def make_otlp_exporter(
    endpoint: str,
    headers: Mapping[str, str] | None = None,
    *,
    ingestion_version: str | None = INGESTION_VERSION,
    retry: bool = True,
    retrier: Retrier | None = None,
) -> SpanExporter:
    """The OTLP/HTTP span exporter for one receiver.

    ``retry=True`` is the SDK's own exporter, as the v3 family has always used it:
    ``OTEL_EXPORTER_OTLP_HEADERS`` supplies the receiver's credentials when ``headers`` is not
    given, and is never logged here. From OpenTelemetry 1.45 the SDK also merges that variable
    under given ``headers``, whose keys win.

    ``retry=False`` makes one POST attempt per batch (:func:`_single_attempt_exporter_class`),
    accepting only 2xx responses as successful exports. The receiver's headers come from the
    caller alone (the plugin reads them through the secret manager): the SDK's header variables
    are not read, and a netrc entry cannot replace them. These raise
    :class:`SingleAttemptUnavailable` rather than silently changing the requested delivery policy
    or sending every batch to be refused: no caller headers, a credential provider
    (``OTEL_PYTHON_EXPORTER_OTLP_HTTP_*CREDENTIAL_PROVIDER``, the SDK exporter's), and an install
    without ``requests`` or the SDK's OTLP encoder. A timeout that is not a positive number of
    seconds, or an unknown compression, raises ``ValueError``.

    ``retrier`` (``retry=False`` only) posts a batch again after a refusal its policy names, the
    same bytes each time (:mod:`tolokaforge_langfuse.retry`); without one every batch is one POST.

    ``ingestion_version`` adds Langfuse's ``x-langfuse-ingestion-version`` header, which selects
    the receiver's direct ingestion path; the v3 family is not sent it at all. It joins
    caller-supplied headers only: with none, ``retry=True`` leaves the header set to the SDK's
    own environment variable."""
    merged = dict(headers) if headers else {}
    if merged and ingestion_version:
        merged.setdefault(INGESTION_VERSION_HEADER, ingestion_version)
    if retry:
        if retrier is not None:
            raise ValueError(
                "a retrier drives the single-attempt exporter (retry=False); the SDK's exporter "
                "repeats batches on its own terms"
            )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        return OTLPSpanExporter(endpoint=endpoint, headers=merged or None)

    providers = [name for name in _CREDENTIAL_PROVIDER_VARIABLES if os.environ.get(name)]
    if providers:
        raise SingleAttemptUnavailable(
            f"{', '.join(providers)} names a credential provider, which only the SDK's retrying "
            "exporter loads: give the single-attempt exporter the receiver's headers instead"
        )
    if not merged:
        raise SingleAttemptUnavailable(
            "the single-attempt exporter takes the receiver's headers from its caller, and none "
            "were given (it does not read OTEL_EXPORTER_OTLP_(TRACES_)HEADERS)"
        )
    single = _single_attempt_exporter_class()
    if single is None:
        raise SingleAttemptUnavailable(_REFUSAL)
    exporter: SpanExporter = single(endpoint, merged or None, retrier=retrier)
    return exporter


_REFUSAL = (
    "the single-attempt exporter builds its request with requests and the OpenTelemetry OTLP "
    "encoder (opentelemetry-exporter-otlp-proto-common), and this install lacks one of them: "
    "reinstall tolokaforge-langfuse with its dependencies"
)
