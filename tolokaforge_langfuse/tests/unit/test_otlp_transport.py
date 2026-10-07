"""The OTLP exporters on the wire (ADR-0048).

Every test sends a real batch through the exporter's request path to a local receiver and counts
what arrived, so it holds for whichever OpenTelemetry SDK is installed. The single-attempt
exporter takes nothing from the SDK but its public OTLP encoder. Engine-free, like the module.
"""

from __future__ import annotations

import gzip
import subprocess
import sys
import zlib

import pytest

pytest.importorskip("opentelemetry.sdk")
import requests
import tolokaforge_langfuse.otlp_transport as otlp_transport
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from otlp_receiver import DROP, LANGFUSE_403, STALL, Receiver, Reply, gateway_refusal
from tolokaforge_langfuse.otlp_transport import INGESTION_VERSION_HEADER, make_otlp_exporter

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
        assert post.headers["user-agent"].startswith("tolokaforge-langfuse/")

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

    def test_the_traces_variable_wins_over_the_generic_one(
        self, receiver, spans, monkeypatch
    ) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_COMPRESSION", "deflate")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_COMPRESSION", "gzip")
        assert _write_once(receiver).export(spans) is SpanExportResult.SUCCESS
        [post] = receiver.posts
        assert post.headers["content-encoding"] == "gzip"

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


class TestRefusalsTheReceiverNeverRead:
    """With a retrier the same bytes go again after an answer that proves the receiver did not
    read them, and after nothing else (ADR-0048, amendment 2026-10-07). Real posts to a local
    receiver; the waits are faked."""

    @staticmethod
    def _exporter(receiver: Receiver, time, **policy):
        from tolokaforge_langfuse.retry import Retrier, RetryPolicy

        retrier = Retrier(
            RetryPolicy(**{"jitter": 0.0, **policy}), clock=time.clock, sleep=time.sleep
        )
        exporter = make_otlp_exporter(
            receiver.url(), {"Authorization": AUTHORIZATION}, retry=False, retrier=retrier
        )
        return exporter, retrier

    def test_the_gateway_page_is_posted_again_and_then_lands(self, receiver, spans) -> None:
        from fake_time import FakeTime

        time = FakeTime()
        exporter, retrier = self._exporter(receiver, time)
        receiver.script = [gateway_refusal()]
        assert exporter.export(spans) is SpanExportResult.SUCCESS
        first, second = receiver.posts
        assert first.body == second.body and _span_names(second.body) == ["generation"]
        assert time.sleeps == [65.0]
        assert retrier.stats.counts()["retries_recovered"] == 1

    def test_a_403_langfuse_answers_itself_is_posted_once(self, receiver, spans) -> None:
        from fake_time import FakeTime

        time = FakeTime()
        exporter, _ = self._exporter(receiver, time)
        receiver.answer = Reply(403, LANGFUSE_403, (("Content-Type", "application/json"),))
        assert exporter.export(spans) is SpanExportResult.FAILURE
        assert len(receiver.posts) == 1 and time.sleeps == []

    @pytest.mark.parametrize(("asked", "waited"), [("30", 30.0), ("600", 60.0)])
    def test_a_429_waits_as_long_as_its_retry_after_asks_up_to_the_cap(
        self, receiver, spans, asked: str, waited: float
    ) -> None:
        from fake_time import FakeTime

        time = FakeTime()
        exporter, _ = self._exporter(receiver, time)
        receiver.script = [Reply(429, b"", (("Retry-After", asked),))]
        assert exporter.export(spans) is SpanExportResult.SUCCESS
        assert len(receiver.posts) == 2 and time.sleeps == [waited]

    @pytest.mark.parametrize("status_code", [500, 502, 504])
    def test_a_status_not_in_the_list_is_posted_once(
        self, receiver, spans, status_code: int
    ) -> None:
        from fake_time import FakeTime

        time = FakeTime()
        exporter, _ = self._exporter(receiver, time)
        receiver.answer = status_code
        assert exporter.export(spans) is SpanExportResult.FAILURE
        assert len(receiver.posts) == 1 and time.sleeps == []

    def test_a_lost_answer_is_still_posted_once(self, receiver, spans) -> None:
        from fake_time import FakeTime

        time = FakeTime()
        exporter, _ = self._exporter(receiver, time)
        receiver.answer = DROP
        assert exporter.export(spans) is SpanExportResult.FAILURE
        assert len(receiver.requests) == 1 and time.sleeps == []

    def test_a_batch_still_refused_after_its_schedule_fails(self, receiver, spans) -> None:
        from fake_time import FakeTime

        time = FakeTime()
        exporter, retrier = self._exporter(receiver, time)
        receiver.answer = 503
        assert exporter.export(spans) is SpanExportResult.FAILURE
        assert len(receiver.posts) == 5 and time.sleeps == [1.0, 2.0, 4.0, 8.0]
        assert retrier.stats.counts()["retries_exhausted"] == 1

    def test_every_attempt_reaches_a_response_hook_on_its_session(self, receiver, spans) -> None:
        """The offline connector reads each answer through ``_session``'s hooks."""
        from fake_time import FakeTime

        exporter, _ = self._exporter(receiver, FakeTime())
        seen: list[int] = []
        exporter._session.hooks["response"].append(
            lambda response, **_: seen.append(response.status_code)
        )
        receiver.script = [gateway_refusal(), 503]
        assert exporter.export(spans) is SpanExportResult.SUCCESS
        assert seen == [403, 503, 200]

    def test_shutdown_ends_a_wait_in_progress(self, receiver, spans) -> None:
        """No injected sleep: the exporter's own wait is cut short when it shuts down."""
        import threading

        from tolokaforge_langfuse.retry import Retrier, RetryPolicy

        exporter = make_otlp_exporter(
            receiver.url(),
            {"Authorization": AUTHORIZATION},
            retry=False,
            retrier=Retrier(RetryPolicy()),
        )
        receiver.answer = gateway_refusal()
        results: list[SpanExportResult] = []
        worker = threading.Thread(target=lambda: results.append(exporter.export(spans)))
        worker.start()
        for _ in range(100):
            if receiver.posts:
                break
            worker.join(0.05)
        exporter.shutdown()
        worker.join(5)
        assert not worker.is_alive() and results == [SpanExportResult.FAILURE]
        assert len(receiver.posts) == 1

    def test_the_sdks_exporter_takes_no_retrier(self, receiver) -> None:
        from tolokaforge_langfuse.retry import Retrier, RetryPolicy

        with pytest.raises(ValueError, match="single-attempt exporter"):
            make_otlp_exporter(receiver.url(), retrier=Retrier(RetryPolicy()))


# The SDK's HTTP exporter packages made unimportable, in a process of its own (the exporter, and
# the client and transport 1.45 moved its request code into): the batch still leaves as one POST
# of its encoded spans, so whatever an OpenTelemetry release moves inside them cannot reach the
# single-attempt exporter.
WITHOUT_THE_SDK_EXPORTER = """
import sys
BLOCKED = (
    "opentelemetry.exporter.otlp.proto.http",
    "opentelemetry.exporter.otlp.common",
    "opentelemetry.exporter.http",
)
class _Block:
    def find_spec(self, name, path=None, target=None):
        if any(name == blocked or name.startswith(blocked + ".") for blocked in BLOCKED):
            raise ImportError(f"blocked: {name}")
        return None
sys.meta_path.insert(0, _Block())
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tolokaforge_langfuse.otlp_transport import make_otlp_exporter
memory = InMemorySpanExporter()
provider = TracerProvider(shutdown_on_exit=False)
provider.add_span_processor(SimpleSpanProcessor(memory))
with provider.get_tracer("probe").start_as_current_span("generation"):
    pass
exporter = make_otlp_exporter(sys.argv[1], {"Authorization": "Basic x"}, retry=False)
assert exporter.export(memory.get_finished_spans()) is SpanExportResult.SUCCESS
assert not [name for name in sys.modules if name.startswith(BLOCKED)], "the block did nothing"
print("ok")
"""


def test_the_single_attempt_exporter_needs_nothing_from_the_sdk_exporter(receiver) -> None:
    result = subprocess.run(
        [sys.executable, "-c", WITHOUT_THE_SDK_EXPORTER, receiver.url()],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0 and result.stdout.strip() == "ok", result.stderr
    [post] = receiver.posts
    assert _span_names(post.body) == ["generation"]


@pytest.mark.parametrize(
    "variable",
    [
        "OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER",
        "OTEL_PYTHON_EXPORTER_OTLP_HTTP_TRACES_CREDENTIAL_PROVIDER",
    ],
)
def test_a_credential_provider_refuses_the_run(monkeypatch, variable: str) -> None:
    """A provider's session is loaded by the SDK's exporter only; going on without it would post
    without whatever it supplies, so the run stops and names the variable."""
    monkeypatch.setenv(variable, "provider")
    with pytest.raises(otlp_transport.SingleAttemptUnavailable, match=variable):
        make_otlp_exporter(
            "http://127.0.0.1:9/v1/traces", {"Authorization": AUTHORIZATION}, retry=False
        )


@pytest.mark.parametrize("headers", [None, {}])
def test_no_caller_headers_refuses_the_run(monkeypatch, headers) -> None:
    """The SDK's header variables are not read here, so without the caller's headers every batch
    would be refused for want of credentials: the run stops at start instead."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_HEADERS", "authorization=Basic%20x")
    with pytest.raises(otlp_transport.SingleAttemptUnavailable, match="headers"):
        make_otlp_exporter("http://127.0.0.1:9/v1/traces", headers, retry=False)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "soon"),
        ("OTEL_EXPORTER_OTLP_TIMEOUT", "soon"),
        # requests would take these and fail every post without sending it
        ("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "0"),
        ("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "-1"),
        ("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "nan"),
        ("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", "inf"),
        ("OTEL_EXPORTER_OTLP_TRACES_COMPRESSION", "brotli"),
        ("OTEL_EXPORTER_OTLP_COMPRESSION", "brotli"),
    ],
)
def test_a_setting_the_exporter_cannot_honour_refuses_the_run(
    monkeypatch, variable: str, value: str
) -> None:
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match=value):
        make_otlp_exporter(
            "http://127.0.0.1:9/v1/traces", {"Authorization": AUTHORIZATION}, retry=False
        )


def test_a_netrc_entry_does_not_replace_the_callers_credentials(
    receiver, spans, monkeypatch, tmp_path
) -> None:
    """requests would otherwise take a netrc entry for the endpoint over the Authorization header
    the caller gave, and every batch would carry someone else's credentials."""
    netrc = tmp_path / "netrc"
    netrc.write_text("default login someone password elsewhere\n")
    netrc.chmod(0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    assert _write_once(receiver).export(spans) is SpanExportResult.SUCCESS
    [post] = receiver.posts
    assert post.headers["authorization"] == AUTHORIZATION


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


def test_the_configured_certificates_reach_the_request(monkeypatch, spans) -> None:
    """The receiver's configured certificates win over requests' own ``REQUESTS_CA_BUNDLE``.
    They are read where requests hands them to its adapter, because honouring them on the wire
    would need a TLS receiver."""
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


def test_the_timeout_variable_is_read_in_seconds(monkeypatch, spans) -> None:
    """The generic variable when the traces one is unset, in seconds as the Python SDK reads it
    (the specification says milliseconds), so a deployment's value keeps its meaning."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "2.5")
    endpoint = "http://127.0.0.1:9/v1/traces"
    exporter = make_otlp_exporter(endpoint, {"Authorization": AUTHORIZATION}, retry=False)
    adapter = _RecordingAdapter()
    exporter._session.mount(endpoint, adapter)

    assert exporter.export(spans) is SpanExportResult.SUCCESS
    [sent] = adapter.sent
    assert sent["timeout"] == 2.5


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
