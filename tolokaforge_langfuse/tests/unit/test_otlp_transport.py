"""The OTLP exporters on the wire (ADR-0048).

Every test sends a real batch through the exporter's own request path to a local receiver and
counts what arrived, so it holds for whichever OpenTelemetry SDK is installed: that path moved from
the exporter into an OTLP client over a transport in 1.45. Engine-free, like the module.
"""

from __future__ import annotations

import gzip
import re
import zlib

import pytest

pytest.importorskip("opentelemetry.sdk")
import requests
import tolokaforge_langfuse.otlp_transport as otlp_transport
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from otlp_receiver import DROP, STALL, Receiver
from requests.adapters import HTTPAdapter
from tolokaforge_langfuse.otlp_transport import INGESTION_VERSION_HEADER, make_otlp_exporter
from urllib3.util.retry import Retry

pytestmark = pytest.mark.unit

AUTHORIZATION = "Basic x"


@pytest.fixture
def receiver():
    with Receiver() as running:
        yield running


@pytest.fixture
def spans():
    memory = InMemorySpanExporter()
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(memory))
    with provider.get_tracer("otlp-transport-test").start_as_current_span("generation"):
        pass
    return memory.get_finished_spans()


def _write_once(receiver: Receiver):
    return make_otlp_exporter(receiver.url(), {"Authorization": AUTHORIZATION}, retry=False)


def _span_names(body: bytes) -> list[str]:
    request = ExportTraceServiceRequest()
    request.ParseFromString(body)
    return [
        span.name
        for resource in request.resource_spans
        for scope in resource.scope_spans
        for span in scope.spans
    ]


class TestOnePostPerBatch:
    """The v4 producer policy: one POST attempt per batch, only a 2xx answer is a success."""

    def test_a_batch_is_one_post_of_its_encoded_spans(self, receiver, spans) -> None:
        exporter = _write_once(receiver)

        assert type(exporter).__name__ == "SingleAttemptSpanExporter"
        assert exporter.export(spans) is SpanExportResult.SUCCESS
        [post] = receiver.requests
        assert (post.method, post.path) == ("POST", "/v1/traces")
        assert _span_names(post.body) == ["generation"]
        assert post.headers["authorization"] == AUTHORIZATION
        assert post.headers[INGESTION_VERSION_HEADER] == "4"
        assert post.headers["content-type"] == "application/x-protobuf"

    def test_a_lost_answer_is_not_posted_again(self, receiver, spans) -> None:
        """The case the guarantee exists for: the receiver took the body and the answer never
        came back. The SDK's own request path posts the same bytes again here."""
        receiver.answer = DROP
        assert _write_once(receiver).export(spans) is SpanExportResult.FAILURE
        assert len(receiver.requests) == 1

    def test_an_answer_that_never_comes_is_not_awaited_twice(
        self, receiver, spans, monkeypatch
    ) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "1")
        receiver.answer = STALL
        assert _write_once(receiver).export(spans) is SpanExportResult.FAILURE
        assert len(receiver.requests) == 1

    @pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504])
    def test_a_refusal_the_sdk_would_retry_is_not_posted_again(
        self, receiver, spans, status_code: int
    ) -> None:
        receiver.answer = status_code
        assert _write_once(receiver).export(spans) is SpanExportResult.FAILURE
        assert len(receiver.requests) == 1

    @pytest.mark.parametrize("status_code", [301, 302, 303, 307, 308])
    def test_a_redirect_is_a_failure_and_is_not_followed(
        self, receiver, spans, status_code: int
    ) -> None:
        """Following a 307 or 308 re-sends the body: nothing may reach the redirect's target."""
        receiver.answer = status_code
        assert _write_once(receiver).export(spans) is SpanExportResult.FAILURE
        assert [request.path for request in receiver.requests] == ["/v1/traces"]

    @pytest.mark.parametrize(
        ("compression", "decompress"), [("gzip", gzip.decompress), ("deflate", zlib.decompress)]
    )
    def test_the_body_is_compressed_as_the_sdk_announces(
        self, receiver, spans, monkeypatch, compression: str, decompress
    ) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_COMPRESSION", compression)
        assert _write_once(receiver).export(spans) is SpanExportResult.SUCCESS
        [post] = receiver.posts
        assert post.headers["content-encoding"] == compression
        assert _span_names(decompress(post.body)) == ["generation"]

    def test_each_answer_reaches_a_response_hook_on_its_session(self, receiver, spans) -> None:
        """A caller reads each answer through ``_session``'s response hooks (the offline
        connector tells a rate limit apart this way), whichever SDK built the request path."""
        exporter = _write_once(receiver)
        seen: list[int] = []
        exporter._session.hooks["response"].append(
            lambda response, **_: seen.append(response.status_code)
        )
        receiver.answer = 429
        assert exporter.export(spans) is SpanExportResult.FAILURE
        assert seen == [429]

    def test_a_credential_providers_session_posts_once(self, receiver, spans, monkeypatch) -> None:
        """A session the SDK loads from a credential provider is the one the post goes through,
        and the retry policy its adapter brings makes no attempt of its own."""
        common = pytest.importorskip("opentelemetry.exporter.otlp.proto.http._common")
        if not hasattr(common, "_load_session_from_envvar"):
            pytest.skip("this OpenTelemetry SDK loads no credential provider")
        provided = requests.Session()
        provided.mount(
            "http://",
            HTTPAdapter(
                max_retries=Retry(
                    total=3,
                    status_forcelist=[503],
                    allowed_methods=None,
                    backoff_factor=0,
                    raise_on_status=False,
                )
            ),
        )
        receiver.answer = 503
        provided.post(receiver.url(), data=b"control")
        assert len(receiver.requests) == 4, "the provider's adapter alone retries a 503"
        receiver.requests.clear()

        class _EntryPoint:
            @staticmethod
            def load():
                return lambda: provided

        monkeypatch.setattr(common, "entry_points", lambda **_: [_EntryPoint()])
        monkeypatch.setenv("OTEL_PYTHON_EXPORTER_OTLP_HTTP_TRACES_CREDENTIAL_PROVIDER", "provider")
        exporter = _write_once(receiver)

        assert exporter._session is provided
        assert exporter.export(spans) is SpanExportResult.FAILURE
        assert len(receiver.requests) == 1

    def test_the_retrying_exporter_posts_a_lost_batch_again(
        self, receiver, spans, monkeypatch
    ) -> None:
        """The control for every count of one above: the stock exporter, which the v3 family
        keeps, sends a batch whose answer was lost again, and the receiver records the repeat."""
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "1")
        exporter = make_otlp_exporter(receiver.url(), {"Authorization": AUTHORIZATION})
        receiver.answer = DROP
        assert type(exporter).__name__ == "OTLPSpanExporter"
        try:
            result = exporter.export(spans)
        except requests.exceptions.ConnectionError:
            pytest.skip("this SDK's stock exporter raises on a lost connection, posts no repeat")
        assert result is SpanExportResult.FAILURE
        assert len(receiver.requests) >= 2


def _without(exporter_class: type, dotted: str) -> type:
    """``exporter_class`` with one attribute of the built exporter gone, as a moved SDK would."""
    holder_path, _, attribute = dotted.rpartition(".")

    class _Without(exporter_class):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            holder = self
            for part in filter(None, holder_path.split(".")):
                holder = getattr(holder, part)
            delattr(holder, attribute)

    return _Without


def test_an_exporter_missing_anything_the_post_reads_refuses_the_run(monkeypatch) -> None:
    """Whichever layout the installed SDK has, losing any one thing the post reads stops the run
    rather than letting the exporter fall back to the SDK's retrying request path."""
    real = otlp_transport._single_attempt_exporter_class()
    built = real(endpoint="http://127.0.0.1:9/v1/traces")
    has_client = hasattr(built, "_client")
    layout = otlp_transport.CLIENT_LAYOUT if has_client else otlp_transport.SESSION_LAYOUT
    for name in layout:
        refused = _without(real, name)
        monkeypatch.setattr(otlp_transport, "_single_attempt_exporter_class", lambda c=refused: c)
        with pytest.raises(otlp_transport.SingleAttemptUnavailable, match=re.escape(name)):
            make_otlp_exporter(
                "http://127.0.0.1:9/v1/traces", {"Authorization": AUTHORIZATION}, retry=False
            )


class _RecordingAdapter(requests.adapters.BaseAdapter):
    """Answers 200 without a network and keeps the settings requests resolved for each send."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict] = []

    def send(self, request, **kwargs):
        self.sent.append(kwargs)
        response = requests.Response()
        response.status_code = 200
        response.request = request
        return response

    def close(self) -> None:
        pass


def test_the_certificates_the_sdk_resolved_reach_the_request(monkeypatch, spans) -> None:
    """On every SDK the receiver's configured certificates win over requests' own
    ``REQUESTS_CA_BUNDLE``. They are read where requests hands them to its adapter, because
    honouring them on the wire would need a TLS receiver."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_CERTIFICATE", "/otel/ca.pem")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_CLIENT_CERTIFICATE", "/otel/client.pem")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_CLIENT_KEY", "/otel/client.key")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/elsewhere/bundle.pem")
    endpoint = "https://127.0.0.1:9/v1/traces"
    exporter = make_otlp_exporter(endpoint, {"Authorization": AUTHORIZATION}, retry=False)
    adapter = _RecordingAdapter()
    exporter._session.mount(endpoint, adapter)

    assert exporter.export(spans) is SpanExportResult.SUCCESS
    [sent] = adapter.sent
    assert sent["verify"] == "/otel/ca.pem"
    assert sent["cert"] == ("/otel/client.pem", "/otel/client.key")


class TestTheIngestionHeader:
    def test_the_exporter_asks_for_the_direct_ingestion_path(self, receiver, spans) -> None:
        exporter = make_otlp_exporter(receiver.url(), {"Authorization": AUTHORIZATION})
        assert exporter.export(spans) is SpanExportResult.SUCCESS
        assert receiver.posts[0].headers[INGESTION_VERSION_HEADER] == "4"

    def test_a_caller_may_turn_it_off_and_keeps_its_own_headers(self, receiver, spans) -> None:
        exporter = make_otlp_exporter(
            receiver.url(), {"Authorization": AUTHORIZATION}, ingestion_version=None
        )
        assert exporter.export(spans) is SpanExportResult.SUCCESS
        [post] = receiver.posts
        assert INGESTION_VERSION_HEADER not in post.headers
        assert post.headers["authorization"] == AUTHORIZATION
