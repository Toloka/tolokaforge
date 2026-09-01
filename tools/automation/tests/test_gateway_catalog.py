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
import pathlib
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from automation import gateway_catalog, model_resolver, poller, slack

from tolokaforge.core.llm import gateway_route
from tolokaforge.secrets import DictProvider, SecretManager
from tolokaforge.secrets import manager as secrets_manager

pytestmark = pytest.mark.unit

#: A deployment that has turned wildcard trust on. Off is the engine default, and the
#: poller inherits it, so every wildcard case has to say which one it is testing.
_TRUSTING = gateway_catalog.GatewayPolicy(trust_namespace_wildcards=True)

_CATALOG = [
    "anthropic/claude-sonnet-4-5",
    "azure_ai/cohere-command-a-plus-05-2026",
    "x-ai/*",
    "openrouter/anthropic/claude-opus-4.7",
]


class TestLookup:
    def test_a_prefixed_entry_is_reachable_and_reported_with_its_route(self) -> None:
        """The engine resolves the route name from this catalog and addresses the
        gateway by it, so a prefixed-only entry IS reachable. Reporting it absent was
        the bug that made the poller disagree with the engine."""
        found = gateway_catalog.lookup("anthropic/claude-opus-4.7", _CATALOG)
        assert found.status == gateway_catalog.STATUS_EXACT
        assert found.route == "openrouter/anthropic/claude-opus-4.7"
        assert found.reachable

    def test_bare_slug_entry_is_the_exact_match(self) -> None:
        found = gateway_catalog.lookup("azure_ai/cohere-command-a-plus-05-2026", _CATALOG)
        assert found.status == gateway_catalog.STATUS_EXACT
        assert found.route == "azure_ai/cohere-command-a-plus-05-2026"

    def test_wildcard_is_reported_separately_from_exact(self) -> None:
        """A passthrough means 'probably', and the reply must not overstate it."""
        found = gateway_catalog.lookup("x-ai/grok-4.5", ["openrouter/*"], _TRUSTING)
        assert found.status == gateway_catalog.STATUS_WILDCARD
        assert found.route == "openrouter/x-ai/grok-4.5"
        assert found.reachable
        assert "passthrough" in gateway_catalog.describe(found)

    def test_the_openrouter_passthrough_covers_the_addressed_name(self) -> None:
        """An ``openrouter/*``-only gateway serves every model the run runs over OpenRouter,
        addressed by its untranslated name - the case that made this lookup DOWNGRADE
        explicit `via litellm` requests before."""
        found = gateway_catalog.lookup("anthropic/claude-opus-4.9", ["openrouter/*"], _TRUSTING)
        assert found.status == gateway_catalog.STATUS_WILDCARD
        assert found.route == "openrouter/anthropic/claude-opus-4.9"

    def test_a_wildcard_is_not_trusted_unless_the_deployment_says_so(self) -> None:
        """The engine's default trusts no wildcard, so neither may the poller: promising
        a route the run then refuses is the misattribution this lookup exists to avoid."""
        found = gateway_catalog.lookup("x-ai/grok-4.5", ["openrouter/*"])
        assert found.status == gateway_catalog.STATUS_ABSENT
        assert not found.reachable

    def test_a_foreign_namespace_wildcard_does_not_cover_the_model(self) -> None:
        """`x-ai/*` forwards to xAI; the run addresses this model as `openrouter/...` and
        the engine refuses to swap upstream. Reported reachable, the reply would have said
        `via litellm` for a run that went to OpenRouter direct."""
        found = gateway_catalog.lookup("x-ai/grok-4.5", _CATALOG, _TRUSTING)
        assert found.status == gateway_catalog.STATUS_ABSENT
        assert not found.reachable

    def test_ambiguous_route_is_unknown_and_annotated(self, capsys) -> None:
        """Both names served and no preference: the run raises rather than guessing, so the
        poller must not promise the gateway either."""
        found = gateway_catalog.lookup("a/b", ["openrouter/a/b", "a/b"])
        assert found.status == gateway_catalog.STATUS_UNKNOWN
        assert not found.reachable
        assert "ambiguous" in capsys.readouterr().err

    def test_a_preference_resolves_the_ambiguity_like_the_engine(self) -> None:
        policy = gateway_catalog.GatewayPolicy(preferred_route="openrouter/")
        found = gateway_catalog.lookup("a/b", ["openrouter/a/b", "a/b"], policy)
        assert found.status == gateway_catalog.STATUS_EXACT
        assert found.route == "openrouter/a/b"

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


ADMISSION_HEADER = "x-github-runner-key"
ADMISSION_VALUE = "runner-secret"


#: Insertion order is deliberately not sorted order: the fetch must sort.
SERVED = ["x-ai/grok-4.5", "x-ai/*", "azure_ai/cohere-command-a-plus-05-2026", "anthropic/*"]


class _Handler(BaseHTTPRequestHandler):
    """A gateway that admits callers by an attribution header, like the real deployment."""

    def do_GET(self) -> None:  # noqa: N802
        self.server.seen.append({k.lower(): v for k, v in self.headers.items()})  # type: ignore[attr-defined]
        if self.server.hang_s:  # type: ignore[attr-defined]
            time.sleep(self.server.hang_s)  # type: ignore[attr-defined]
        if self.server.seen[-1].get(ADMISSION_HEADER) != ADMISSION_VALUE:  # type: ignore[attr-defined]
            body, status = b'{"error":"forbidden"}', 403
        else:
            entries = [{"id": e} for e in self.server.serves]  # type: ignore[attr-defined]
            body, status = json.dumps({"data": entries}).encode(), 200
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def _server():
    """State lives on the server, not in module globals, so no test can inherit another's."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def gateway(_server, monkeypatch):
    """A configured gateway, reset, with the engine's per-process catalog cache cleared."""
    _server.seen = []
    _server.serves = list(SERVED)
    _server.hang_s = 0
    gateway_route.clear_catalog_cache()
    monkeypatch.setenv("LLM_PROXY_BASE_URL", f"http://127.0.0.1:{_server.server_address[1]}/v1")
    monkeypatch.setenv("LLM_PROXY_API_KEY", "sk-gw")
    yield _server
    gateway_route.clear_catalog_cache()


def _admit(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROXY_HEADERS", json.dumps({"X-GitHub-Runner-Key": ADMISSION_VALUE}))


class TestTheConfiguredCatalogIsReadLikeARunReadsIt:
    """The poll must reach the same gateway an integration run reaches.

    It used to send only the key, so a deployment that admits callers by an attribution
    header answered 403. The catalog then read as unreadable, every model reported as
    unknown for a transport reason rather than a catalogue one, and a gateway-only model
    could not be resolved at all.
    """

    def test_no_gateway_configured_is_no_information(self, monkeypatch) -> None:
        monkeypatch.delenv("LLM_PROXY_BASE_URL", raising=False)
        monkeypatch.delenv("LLM_PROXY_API_KEY", raising=False)
        assert gateway_catalog.fetch_configured_catalog() is None

    def test_the_admission_header_rides_along(self, gateway, monkeypatch) -> None:
        _admit(monkeypatch)
        assert gateway_catalog.fetch_configured_catalog() == sorted(SERVED)
        assert gateway.seen[0]["authorization"] == "Bearer sk-gw"

    def test_a_secret_reference_in_a_header_is_resolved(self, gateway, monkeypatch) -> None:
        """The workflow passes the value as ``${secret:NAME}``, so an unexpanded
        reference would reach the gateway verbatim and be rejected."""
        monkeypatch.setenv("RUNNER_KEY", ADMISSION_VALUE)
        monkeypatch.setenv(
            "LLM_PROXY_HEADERS", json.dumps({"X-GitHub-Runner-Key": "${secret:RUNNER_KEY}"})
        )
        assert gateway_catalog.fetch_configured_catalog() is not None

    def test_without_the_header_the_catalog_is_unknown_not_empty(
        self, gateway, monkeypatch
    ) -> None:
        """403 is no information. Reporting it as an empty catalog would say
        "the gateway serves nothing", which downgrades every route with confidence."""
        monkeypatch.delenv("LLM_PROXY_HEADERS", raising=False)
        assert gateway_catalog.fetch_configured_catalog() is None

    def test_an_empty_catalog_is_unknown_too(self, gateway, monkeypatch) -> None:
        """The engine calls an empty answer unreadable; the poller now agrees, so one
        gateway state cannot produce two different routing decisions."""
        _admit(monkeypatch)
        gateway.serves = []
        assert gateway_catalog.fetch_configured_catalog() is None

    def test_a_hanging_gateway_gives_up_on_the_timeout(self, gateway, monkeypatch) -> None:
        """The poll runs on a schedule against a gateway nobody watches, so the timeout
        has to reach the socket rather than sit unused in the signature."""
        _admit(monkeypatch)
        gateway.hang_s = 2
        assert gateway_catalog.fetch_configured_catalog(timeout=1) is None

    def test_an_unreachable_gateway_returns_none_rather_than_raising(self, monkeypatch) -> None:
        """A notification path must never break the poll."""
        gateway_route.clear_catalog_cache()
        monkeypatch.setenv("LLM_PROXY_BASE_URL", "http://127.0.0.1:1/v1")
        monkeypatch.delenv("LLM_PROXY_API_KEY", raising=False)
        assert gateway_catalog.fetch_configured_catalog(timeout=2) is None

    def test_a_misconfigured_gateway_is_reported_but_does_not_break_the_poll(
        self, monkeypatch, capsys
    ) -> None:
        """A companion variable without a base URL is an operator error the engine
        refuses on. Here it must surface without taking the poll down with it."""
        monkeypatch.delenv("LLM_PROXY_BASE_URL", raising=False)
        monkeypatch.setenv("LLM_PROXY_API_KEY", "sk-orphan")
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        assert gateway_catalog.fetch_configured_catalog() is None
        captured = capsys.readouterr()
        assert "gateway unusable" in captured.err
        # The reply blames the model ("could not confirm the gateway serves..."), so
        # without an annotation the real cause is one stderr line in a green job.
        assert "::warning title=Gateway configuration unusable::" in captured.out


class TestTheConfiguredPolicyIsTheRunsPolicy:
    """The lookup's verdict is only worth reporting if a run would honour it.

    The engine reads the trust flag and the route preference from the environment; the
    poller reads the same two, so a promise of `via litellm` matches what the run does.
    """

    def test_no_gateway_configured_is_the_engine_default(self, monkeypatch) -> None:
        monkeypatch.delenv("LLM_PROXY_BASE_URL", raising=False)
        monkeypatch.delenv("LLM_PROXY_TRUST_NAMESPACE_WILDCARDS", raising=False)
        monkeypatch.delenv("LLM_PROXY_PREFERRED_ROUTE", raising=False)
        policy = gateway_catalog.configured_policy()
        assert policy.trust_namespace_wildcards is False
        assert policy.preferred_route is None

    def test_the_deployments_policy_is_picked_up(self, gateway, monkeypatch) -> None:
        monkeypatch.setenv("LLM_PROXY_TRUST_NAMESPACE_WILDCARDS", "true")
        monkeypatch.setenv("LLM_PROXY_PREFERRED_ROUTE", "openrouter/,nebius/")
        policy = gateway_catalog.configured_policy()
        assert policy.trust_namespace_wildcards is True
        assert policy.preferred_route == "openrouter/,nebius/"

    def test_a_malformed_policy_is_the_strict_default_not_a_crash(
        self, gateway, monkeypatch
    ) -> None:
        """A poll must never fail on an operator's typo, and the fallback has to be the
        strict reading: trusting a wildcard the engine will not is the misattribution."""
        monkeypatch.setenv("LLM_PROXY_TRUST_NAMESPACE_WILDCARDS", "yes-please")
        assert gateway_catalog.configured_policy().trust_namespace_wildcards is False


class TestALocalEnvCannotAnswerARealPoll:
    def test_the_default_manager_is_not_consulted(self, _server, monkeypatch) -> None:
        """Dotenv precedence would let a developer's gateway answer a production poll,
        and the reply would record availability nobody else can reproduce.

        The default manager is pointed at the REAL test server, so consulting it would
        succeed loudly. Pointing it at an unreachable host would prove nothing: that
        returns None as well, which is the answer this test expects.
        """
        _server.seen = []
        _server.serves = list(SERVED)
        _server.hang_s = 0
        gateway_route.clear_catalog_cache()
        monkeypatch.delenv("LLM_PROXY_BASE_URL", raising=False)
        monkeypatch.setattr(
            secrets_manager,
            "_default_manager",
            SecretManager(
                [
                    DictProvider(
                        {
                            "LLM_PROXY_BASE_URL": f"http://127.0.0.1:{_server.server_address[1]}/v1",
                            "LLM_PROXY_HEADERS": json.dumps(
                                {"X-GitHub-Runner-Key": ADMISSION_VALUE}
                            ),
                        }
                    )
                ]
            ),
        )
        assert gateway_catalog.fetch_configured_catalog() is None
        assert _server.seen == [], "the gateway was not even asked"


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

        def fake_gateway(timeout=15):
            fetches["gateway"] += 1
            return gateway

        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
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
        monkeypatch.setattr(gateway_catalog, "fetch_configured_catalog", fake_gateway)

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
        # Explicit entries, not a wildcard: a poll with no gateway policy in its environment
        # trusts none (the engine's default), so a `x-ai/*` catalog would be DOWNGRADED here
        # - the run would have gone to OpenRouter direct anyway.
        # The catalog covers the user simulator too: the integration run's gateway `.env` is
        # job-wide, so a gateway serving only the candidate could not serve this run at all.
        plan, posted = self._plan(
            monkeypatch,
            tmp_path,
            "<@B1> integrate Grok 4.5 via litellm",
            ["openrouter/x-ai/grok-4.5", gateway_catalog.USER_SIMULATOR_SLUG],
        )
        assert [row["route"] for row in plan] == ["litellm", "litellm"]
        # The reply must state the route it actually queued, not the default.
        assert "via *litellm*" in posted[0]

    def test_untrusted_wildcard_downgrades_an_explicit_gateway_request(
        self, monkeypatch, tmp_path
    ) -> None:
        """A gateway whose only coverage is a passthrough the deployment does not trust: the
        request is refused in the reply rather than queued as a run the engine sends direct."""
        plan, posted = self._plan(
            monkeypatch,
            tmp_path,
            "<@B1> integrate Grok 4.5 via litellm",
            ["openrouter/*", gateway_catalog.USER_SIMULATOR_SLUG],
        )
        assert [row["route"] for row in plan] == ["openrouter", "openrouter"]
        assert "could not confirm the gateway serves every model" in posted[0]

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
            monkeypatch,
            tmp_path,
            "<@B1> integrate Grok 4.5 via litellm",
            ["x-ai/grok-4.5", gateway_catalog.USER_SIMULATOR_SLUG],
        )
        assert plan[0]["route"] == "litellm"
        assert "could not confirm" not in posted[0]

    def test_an_unserved_user_simulator_also_downgrades(self, monkeypatch, tmp_path) -> None:
        """The candidate is covered, the simulator is not - and the run proxies both.

        The integration run writes the gateway credentials JOB-WIDE, so `via litellm` with a
        gateway that lacks `anthropic/claude-sonnet-4.6` sends observe infra-dirty in the
        SIMULATOR. Nothing in the reply would point at the gateway, so the poller has to.
        """
        plan, posted = self._plan(
            monkeypatch, tmp_path, "<@B1> integrate Grok 4.5 via litellm", ["x-ai/grok-4.5"]
        )
        assert [row["route"] for row in plan] == ["openrouter", "openrouter"]
        assert "user simulator" in posted[0]
        assert gateway_catalog.USER_SIMULATOR_SLUG in posted[0]

    def test_a_gateway_only_model_cannot_be_downgraded_so_it_warns_instead(
        self, monkeypatch, tmp_path
    ) -> None:
        """No OpenRouter route exists for it, so the honest outcome is a warning, not a move."""
        plan, posted = self._plan(
            monkeypatch,
            tmp_path,
            "<@B1> integrate azure_ai/cohere-command-a-plus-05-2026",
            ["azure_ai/cohere-command-a-plus-05-2026"],
        )
        assert [row["route"] for row in plan] == ["litellm", "litellm"]
        assert "user simulator" in posted[0]
        assert "infra-dirty in the simulator" in posted[0]

    def test_an_openrouter_request_is_never_downgraded_or_warned(
        self, monkeypatch, tmp_path
    ) -> None:
        """The guard is gateway-only; it must not editorialise the default route."""
        plan, posted = self._plan(
            monkeypatch, tmp_path, "<@B1> integrate Grok 4.5 via openrouter", ["anthropic/*"]
        )
        assert plan[0]["route"] == "openrouter"
        assert "could not confirm" not in posted[0]


class TestTheSimulatorConstantTracksTheWorkflow:
    """`USER_SIMULATOR_SLUG` is a copy of a value that lives in the workflow.

    The gateway evidence check is only as good as that name, and a rename in the workflow would
    leave the check silently looking up a model nobody runs - green, and useless. So the copy is
    pinned to its source here rather than trusted.
    """

    def test_the_constant_is_the_model_the_workflow_actually_runs(self) -> None:
        workflow = (
            pathlib.Path(__file__).resolve().parents[3]
            / ".github"
            / "workflows"
            / "integrate-model.yml"
        )
        text = workflow.read_text()
        # The user-simulator block in the generated model config: `user:` then its `name:`.
        match = re.search(r"^\s*user:\s*$.*?^\s*name:\s*\"(?P<slug>[^\"]+)\"", text, re.M | re.S)
        assert match, "could not find the user simulator's name in integrate-model.yml"
        assert match.group("slug") == gateway_catalog.USER_SIMULATOR_SLUG
