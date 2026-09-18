"""The run refuses before any container work when the CLI's provider is dead.

A coding-harness CLI owns its own connection: the engine issues no request on
its behalf, so none of its retry or model-fallback machinery is in the path.
Observed live — a gateway 403'd every call, the CLI wrote a transcript and
exited, and three Arena tasks scored 0.42-0.58 against untouched repositories.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tolokaforge.core.orchestrator import _harness_provider_probe, _unreachable_reason

pytestmark = pytest.mark.unit


@pytest.fixture
def endpoint() -> Iterator[object]:
    """A stub provider whose next status the test chooses."""

    class Stub:
        status = 200

    stub = Stub()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # noqa: A002
            return

        def do_HEAD(self):
            self.send_response(stub.status)
            self.send_header("Content-Length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stub.url = f"http://127.0.0.1:{server.server_address[1]}"  # type: ignore[attr-defined]
    try:
        yield stub
    finally:
        server.shutdown()
        server.server_close()


class TestWhatCountsAsUnreachable:
    @pytest.mark.parametrize("status", [200, 204, 404, 405])
    def test_an_endpoint_that_answers_is_reachable(self, endpoint, status) -> None:
        """Including 404: the probe asks whether an authorised caller reaches
        the host, not whether one route exists. Which paths it serves is the
        CLI's business."""
        endpoint.status = status

        assert _unreachable_reason(endpoint.url, {}) is None

    @pytest.mark.parametrize("status", [401, 403])
    def test_a_refused_credential_is_unreachable(self, endpoint, status) -> None:
        """The case that wasted three live trials."""
        endpoint.status = status

        detail = _unreachable_reason(endpoint.url, {})

        assert detail is not None
        assert str(status) in detail

    def test_a_failing_endpoint_is_unreachable(self, endpoint) -> None:
        endpoint.status = 503

        assert _unreachable_reason(endpoint.url, {}) is not None

    def test_nothing_listening_is_unreachable(self) -> None:
        assert _unreachable_reason("http://127.0.0.1:9", {}) is not None


class TestWhatTheProbeDeclinesToJudge:
    """Silence must mean "not checked", never "checked and fine"."""

    def test_an_envelope_with_no_base_url_is_not_probed(self) -> None:
        assert _harness_provider_probe({"X_API_KEY": "k"}) is None

    def test_a_scheme_the_probe_does_not_speak_is_not_probed(self) -> None:
        assert _harness_provider_probe({"X_BASE_URL": "unix:///sock"}) is None

    def test_an_empty_envelope_is_not_probed(self) -> None:
        assert _harness_provider_probe({}) is None


class TestTheCredentialIsSent:
    def test_both_auth_header_shapes_are_offered(self) -> None:
        """An endpoint answering only an authorised caller is the case worth
        catching, and Google's REST surface reads its own header rather than
        `Authorization`."""
        probe = _harness_provider_probe(
            {"GOOGLE_GEMINI_BASE_URL": "https://example.invalid", "GEMINI_API_KEY": "k"}
        )

        assert probe is not None
        _, headers = probe
        assert headers["Authorization"] == "Bearer k"
        assert headers["x-goog-api-key"] == "k"

    def test_the_api_base_spelling_is_recognised(self) -> None:
        """LiteLLM-backed CLIs read `OPENROUTER_API_BASE`, not `_BASE_URL`."""
        probe = _harness_provider_probe(
            {"OPENROUTER_API_BASE": "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY": "k"}
        )

        assert probe is not None
        assert probe[0] == "https://openrouter.ai/api/v1"
