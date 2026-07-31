"""Tests for the advisory gateway-availability lookup and the route directive.

Two behaviours carry real risk and are pinned hardest:

* **OpenRouter stays the default.** A request that does not name a route must not
  change serving path, because the leaderboard is calibrated on OpenRouter.
* **A wildcard is weaker evidence than an explicit entry.** Reporting a
  passthrough match as if someone had configured the model would invite a human
  to pick a route that then 404s at eval time.
"""

from __future__ import annotations

import json

import pytest
from automation import gateway_catalog, model_resolver, poller, slack

pytestmark = pytest.mark.unit

_CATALOG = [
    "anthropic/claude-sonnet-4-5",
    "azure_ai/cohere-command-a-plus-05-2026",
    "x-ai/*",
    "openrouter/anthropic/claude-opus-4.7",
]


class TestLookup:
    def test_prefixed_entry_is_not_evidence_for_this_run(self) -> None:
        """litellm strips `openrouter/`, so the run asks for the BARE slug (LLM_LAYER.md)."""
        found = gateway_catalog.lookup("anthropic/claude-opus-4.7", _CATALOG)
        assert found.status == gateway_catalog.STATUS_ABSENT
        assert not found.reachable

    def test_bare_slug_entry_is_the_exact_match(self) -> None:
        found = gateway_catalog.lookup("azure_ai/cohere-command-a-plus-05-2026", _CATALOG)
        assert found.status == gateway_catalog.STATUS_EXACT
        assert found.route == "azure_ai/cohere-command-a-plus-05-2026"

    def test_wildcard_is_reported_separately_from_exact(self) -> None:
        """A passthrough means 'probably', and the reply must not overstate it."""
        found = gateway_catalog.lookup("x-ai/grok-4.5", _CATALOG)
        assert found.status == gateway_catalog.STATUS_WILDCARD
        assert found.route == "x-ai/grok-4.5"
        assert found.reachable
        assert "passthrough" in gateway_catalog.describe(found)

    def test_absent_when_catalog_covers_nothing(self) -> None:
        found = gateway_catalog.lookup("x-ai/grok-4.5", ["anthropic/claude-sonnet-4-5"])
        assert found.status == gateway_catalog.STATUS_ABSENT
        assert not found.reachable
        assert found.route is None

    def test_unfetched_catalog_is_unknown_not_absent(self) -> None:
        """No gateway configured must not read as 'the model is missing'."""
        found = gateway_catalog.lookup("x-ai/grok-4.5", None)
        assert found.status == gateway_catalog.STATUS_UNKNOWN
        assert not found.reachable
        assert gateway_catalog.describe(found) == ""


class TestFetchDegradesQuietly:
    def test_no_base_url_returns_none(self) -> None:
        assert gateway_catalog.fetch_gateway_catalog(None, "sk-x") is None
        assert gateway_catalog.fetch_gateway_catalog("   ", "sk-x") is None

    def test_unreachable_gateway_returns_none_rather_than_raising(self, monkeypatch) -> None:
        """A notification path must never break the poll (no real socket: stub the transport)."""

        def fake_urlopen(request, timeout=None):
            raise gateway_catalog.urllib.error.URLError("boom")

        monkeypatch.setattr(gateway_catalog.urllib.request, "urlopen", fake_urlopen)
        assert gateway_catalog.fetch_gateway_catalog("http://gw.invalid/v1", "sk-x") is None

    def test_catalog_is_parsed_and_sorted_from_the_models_route(self, monkeypatch) -> None:
        seen = {}

        class _Resp:
            def read(self):
                return json.dumps(
                    {"data": [{"id": "x-ai/grok-4.5"}, {"id": "x-ai/*"}, {}, {"id": None}]}
                ).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(request, timeout=None):
            seen["url"] = request.full_url
            seen["auth"] = request.headers.get("Authorization")
            return _Resp()

        monkeypatch.setattr(gateway_catalog.urllib.request, "urlopen", fake_urlopen)
        assert gateway_catalog.fetch_gateway_catalog("https://gw.invalid/v1/", "sk-x") == [
            "x-ai/*",
            "x-ai/grok-4.5",
        ]
        assert seen["url"] == "https://gw.invalid/v1/models"
        assert seen["auth"] == "Bearer sk-x"

    def test_non_list_data_is_no_information_not_an_empty_catalog(self, monkeypatch) -> None:
        """An empty list would read as 'the gateway serves nothing'; None reads as 'unknown'."""

        class _Resp:
            def read(self):
                return json.dumps({"data": {"x-ai/grok-4.5": {}}}).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(
            gateway_catalog.urllib.request, "urlopen", lambda request, timeout=None: _Resp()
        )
        assert gateway_catalog.fetch_gateway_catalog("https://gw.invalid/v1", "sk-x") is None


class TestRouteDirective:
    @pytest.mark.parametrize(
        "text",
        [
            "<@U1> integrate Grok 4.5 via litellm",
            "<@U1> integrate Grok 4.5 via LiteLLM proxy",
            "<@U1> integrate Grok 4.5 through the gateway",
            "<@U1> integrate Grok 4.5 using the proxy",
        ],
    )
    def test_gateway_route_is_recognised(self, text: str) -> None:
        assert model_resolver.parse_route(text) == gateway_catalog.ROUTE_GATEWAY

    @pytest.mark.parametrize(
        "text",
        ["<@U1> integrate Grok 4.5 via openrouter", "<@U1> integrate Grok 4.5 via OR"],
    )
    def test_openrouter_route_is_recognised(self, text: str) -> None:
        assert model_resolver.parse_route(text) == gateway_catalog.ROUTE_OPENROUTER

    def test_no_directive_is_none_not_the_default(self) -> None:
        """The caller must distinguish 'chose OpenRouter' from 'did not ask'."""
        assert model_resolver.parse_route("<@U1> integrate Grok 4.5") is None

    def test_default_route_is_openrouter(self) -> None:
        assert gateway_catalog.DEFAULT_ROUTE == gateway_catalog.ROUTE_OPENROUTER

    @pytest.mark.parametrize(
        "text",
        [
            "<@U1> integrate Grok 4.5 via litellm",
            "<@U1> integrate Grok 4.5 via openrouter",
        ],
    )
    def test_directive_is_stripped_from_the_model_phrase(self, text: str) -> None:
        """Otherwise 'Grok 4.5 via litellm' becomes the phrase and resolves to nothing."""
        assert model_resolver.parse_command(text) == ["Grok 4.5"]

    def test_directive_stripped_with_several_models(self) -> None:
        phrases = model_resolver.parse_command(
            "<@U1> integrate Grok 4.5 and GPT 5.6 via the gateway"
        )
        assert phrases == ["Grok 4.5", "GPT 5.6"]

    def test_a_model_named_or_is_not_mistaken_for_a_route(self) -> None:
        """The directive needs a via/through/over/using lead-in, so plain names are safe."""
        assert model_resolver.parse_route("<@U1> integrate openrouter/x-ai/grok-4.5") is None
        assert model_resolver.parse_command("<@U1> integrate openrouter/x-ai/grok-4.5") == [
            "openrouter/x-ai/grok-4.5"
        ]

    def test_conflicting_directives_fall_back_to_the_default(self) -> None:
        """One message, two routes: guessing could put an OpenRouter ask on the gateway."""
        assert (
            model_resolver.parse_route("<@U1> integrate Grok 4.5 via litellm and GPT 5.6 via OR")
            is None
        )

    @pytest.mark.parametrize(
        "text",
        [
            "<@U1> integrate Grok 4.5 through the LiteLLM gateway",
            "<@U1> integrate Grok 4.5 via litellm-proxy",
        ],
    )
    def test_route_noun_is_fully_absorbed(self, text: str) -> None:
        """A leftover 'gateway'/'proxy' token makes the model resolve to nothing."""
        assert model_resolver.parse_route(text) == gateway_catalog.ROUTE_GATEWAY
        assert model_resolver.parse_command(text) == ["Grok 4.5"]


class TestReply:
    def _resolved(self, slug: str) -> model_resolver.Resolution:
        return model_resolver.Resolution(query=slug, status="resolved", slug=slug)

    def test_reply_names_the_effective_route_and_the_gateway_note(self) -> None:
        slug = "azure_ai/cohere-command-a-plus-05-2026"
        reply = model_resolver.format_resolution_reply(
            "U1",
            [self._resolved(slug)],
            {slug: gateway_catalog.lookup(slug, _CATALOG)},
            None,
        )
        assert "via *openrouter*" in reply
        assert "azure_ai/cohere-command-a-plus-05-2026" in reply
        # Unrequested-but-available => tell the requester how to ask for it.
        assert "via litellm" in reply

    def test_explicit_gateway_request_is_echoed(self) -> None:
        slug = "anthropic/claude-opus-4.7"
        reply = model_resolver.format_resolution_reply(
            "U1",
            [self._resolved(slug)],
            {slug: gateway_catalog.lookup(slug, _CATALOG)},
            gateway_catalog.ROUTE_GATEWAY,
        )
        assert "via *litellm*" in reply
        # No nudge when the requester already chose.
        assert "re-request with" not in reply.lower()

    def test_reply_without_availability_is_unchanged_shape(self) -> None:
        """Back-compat: the pre-existing two-argument call still works."""
        reply = model_resolver.format_resolution_reply("U1", [self._resolved("x-ai/grok-4.5")])
        assert "starting integration as `x-ai/grok-4.5`" in reply
        assert "gateway" not in reply


class TestPlanRows:
    """The plan is the contract with the workflow, so its new fields need pinning.

    These drive the REAL :func:`poller.run`, not a reproduction of its body: a
    reproduction keeps passing while the poller writes something else.
    """

    def _plan(
        self, monkeypatch, tmp_path, text: str, gateway: list[str] | None
    ) -> tuple[list[dict], list[str]]:
        fetches = {"gateway": 0}
        posted: list[str] = []

        def fake_gateway(base_url, api_key, timeout=15):
            fetches["gateway"] += 1
            return gateway

        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setenv("LLM_PROXY_BASE_URL", "https://gateway.invalid/v1")
        monkeypatch.setenv("LLM_PROXY_API_KEY", "sk-test")
        monkeypatch.setattr(poller, "_auth_test", lambda token: "B1")
        monkeypatch.setattr(poller, "_already_handled", lambda *a, **k: False)
        monkeypatch.setattr(
            slack,
            "_history",
            lambda channel, token, oldest=None: [
                {"ts": "1.0", "user": "U1", "text": text},
                {"ts": "2.0", "user": "U1", "text": text},
            ],
        )
        monkeypatch.setattr(
            slack,
            "_post_message",
            lambda channel, text, token, thread_ts=None: (posted.append(text), True)[1],
        )
        monkeypatch.setattr(model_resolver, "fetch_openrouter_catalog", lambda: ["x-ai/grok-4.5"])
        monkeypatch.setattr(gateway_catalog, "fetch_gateway_catalog", fake_gateway)

        out = tmp_path / "plan.json"
        assert poller.run("C1", None, str(out)) == 0
        # The gateway catalog is fetched once for the whole poll, not once per request:
        # an unreachable gateway must not re-time-out for every message in the window.
        assert fetches["gateway"] == 1
        return json.loads(out.read_text()), posted

    def test_unstated_route_defaults_to_openrouter(self, monkeypatch, tmp_path) -> None:
        """The leaderboard is calibrated on OpenRouter: silence must never mean the gateway."""
        plan, posted = self._plan(monkeypatch, tmp_path, "<@B1> integrate Grok 4.5", ["x-ai/*"])
        assert [row["route"] for row in plan] == ["openrouter", "openrouter"]
        assert "via *openrouter*" in posted[0]

    def test_explicit_gateway_route_reaches_the_plan(self, monkeypatch, tmp_path) -> None:
        plan, posted = self._plan(
            monkeypatch, tmp_path, "<@B1> integrate Grok 4.5 via litellm", ["x-ai/*"]
        )
        assert [row["route"] for row in plan] == ["litellm", "litellm"]
        # The reply must state the route it actually queued, not the default.
        assert "via *litellm*" in posted[0]

    def test_row_shape_is_the_workflow_contract(self, monkeypatch, tmp_path) -> None:
        plan, posted = self._plan(
            monkeypatch, tmp_path, "<@B1> integrate Grok 4.5", ["x-ai/grok-4.5"]
        )
        assert "on the gateway as `x-ai/grok-4.5`" in posted[0]
        assert plan[0] == {
            "slug": "x-ai/grok-4.5",
            "requester": "U1",
            "message_ts": "1.0",
            "route": "openrouter",
            "gateway": {"slug": "x-ai/grok-4.5", "status": "exact", "route": "x-ai/grok-4.5"},
        }

    def test_no_gateway_configured_still_produces_a_dispatchable_row(
        self, monkeypatch, tmp_path
    ) -> None:
        plan, _ = self._plan(monkeypatch, tmp_path, "<@B1> integrate Grok 4.5", None)
        assert plan[0]["route"] == "openrouter"
        assert plan[0]["gateway"]["status"] == "unknown"


class TestUnconfirmedGatewayIsDowngraded:
    """An unconfirmable ``via litellm`` must not be dispatched as if it were fine.

    Dispatching it costs an hour of runner time and then blames the candidate for
    an infra failure the poller could already see coming; the Slack thread — the
    audit trail for the serving path — would also record a route the run did not
    use. Driven through the real :func:`poller.run` for the same reason as
    :class:`TestPlanRows`.
    """

    def _plan(self, monkeypatch, tmp_path, text: str, gateway: list[str] | None):
        return TestPlanRows()._plan(monkeypatch, tmp_path, text, gateway)

    def test_absent_model_downgrades_to_the_default_and_says_so(
        self, monkeypatch, tmp_path
    ) -> None:
        # Gateway readable, but nothing covers x-ai/grok-4.5.
        plan, posted = self._plan(
            monkeypatch, tmp_path, "<@B1> integrate Grok 4.5 via litellm", ["anthropic/*"]
        )
        assert [row["route"] for row in plan] == ["openrouter", "openrouter"]
        assert "via *openrouter*" in posted[0]
        assert "could not confirm the gateway serves every model" in posted[0]

    def test_unreadable_catalog_downgrades_to_the_default(self, monkeypatch, tmp_path) -> None:
        """ "unknown" is not "reachable" — an unconfigured gateway must not be promised."""
        plan, posted = self._plan(
            monkeypatch, tmp_path, "<@B1> integrate Grok 4.5 via litellm", None
        )
        assert plan[0]["route"] == "openrouter"
        assert "could not confirm the gateway serves every model" in posted[0]

    def test_confirmed_model_keeps_the_gateway_and_stays_quiet(self, monkeypatch, tmp_path) -> None:
        plan, posted = self._plan(
            monkeypatch, tmp_path, "<@B1> integrate Grok 4.5 via litellm", ["x-ai/grok-4.5"]
        )
        assert plan[0]["route"] == "litellm"
        assert "could not confirm" not in posted[0]

    def test_an_openrouter_request_is_never_downgraded_or_warned(
        self, monkeypatch, tmp_path
    ) -> None:
        """The guard is gateway-only; it must not editorialise the default route."""
        plan, posted = self._plan(
            monkeypatch, tmp_path, "<@B1> integrate Grok 4.5 via openrouter", ["anthropic/*"]
        )
        assert plan[0]["route"] == "openrouter"
        assert "could not confirm" not in posted[0]
