"""The shipped ``gateway_route`` recipes, and what a consuming runtime gets.

Two things are locked here. First, the gemini LiteLLM recipe ships in two
forms — the ``harness_presets_file`` overlay at
``examples/terminal_bench/gemini_litellm_overlay.yaml``, which is how a TF-side
run routes gemini through a gateway, and ``gemini-cli.gateway_route``, the same
facts as spec data for a runtime that provisions an already-running container.
Two representations of one recipe drift; the parity test is what makes shipping
both honest.

Second, the snapshot pins what a consumer actually receives once the ADR-0037
token table has been applied. A data edit that changes the endpoint, the
credential reference, or the file a trial lands with shows up as a snapshot
diff rather than as a wrong routing decision two repos away.

The renderer below lives in this test on purpose. ``tolokaforge_coding_harnesses``
stores those tokens opaque and expands none of them (ADR-0037); growing the
dialect in the package to make a test easier would delete the property the test
exists to describe.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tolokaforge_coding_harnesses import (
    ALTERNATIVE_GATEWAYS,
    DEFAULT_PATH_RESOLVER,
    HARNESSES,
    GatewayRoute,
    RuntimeGateway,
    load_harness_registry,
)

pytestmark = pytest.mark.canonical

REPO_ROOT = Path(__file__).resolve().parents[2]
GEMINI_OVERLAY = REPO_ROOT / "examples" / "terminal_bench" / "gemini_litellm_overlay.yaml"

STAND_IN_SECRETS = {
    "LITELLM_BASE_URL": "https://litellm.invalid",
    "LITELLM_API_KEY": "sk-litellm-not-a-real-key",
}
"""Stand-in resolution for the two operator secrets a gateway route names.

Obviously-fake values: the snapshot records the *shape* a consumer receives,
and a canonical golden file is the last place a real credential should be able
to reach.
"""


def _expand_secret_refs(value: str) -> str:
    """ADR-0037 token table, step 2."""
    for name, secret in STAND_IN_SECRETS.items():
        value = value.replace(f"${{secret:{name}}}", secret)
    return value


def _render_provider_env(route: GatewayRoute, gateway: RuntimeGateway) -> dict[str, str]:
    """ADR-0037 token table, steps 1 then 2 — in that order.

    ``${gateway.*}`` must expand before ``${secret:NAME}``: the gateway-derived
    URL is what a secret reference is concatenated onto, and swapping the two
    steps is exactly the mistake the table's numbering exists to prevent.
    """
    rendered = {}
    for key, value in route.provider_env.items():
        value = value.replace("${gateway.base_url}", STAND_IN_SECRETS[gateway.base_url_env])
        value = value.replace("${gateway.passthrough_path}", route.passthrough_path)
        rendered[key] = _expand_secret_refs(value)
    return rendered


def _render_route(name: str) -> dict[str, object]:
    """One harness's route as a consuming runtime would provision it."""
    route = HARNESSES[name].gateway_route
    assert route is not None, f"{name} declares no gateway_route"
    gateway = ALTERNATIVE_GATEWAYS[route.gateway]
    return {
        "gateway": route.gateway,
        "gateway_base_url_env": gateway.base_url_env,
        "gateway_credential_env": gateway.credential_env,
        "model_alias_pattern": route.model_alias_pattern,
        # Step 0: the injector expands nothing, so the caller resolves first.
        "config_files": {
            DEFAULT_PATH_RESOLVER.resolve(path): content
            for path, content in route.config_files.items()
        },
        "container_env": dict(route.container_env),
        "provider_env": _render_provider_env(route, gateway),
    }


@pytest.fixture(scope="module")
def overlay_spec():
    assert GEMINI_OVERLAY.is_file(), f"{GEMINI_OVERLAY} is missing"
    return load_harness_registry(GEMINI_OVERLAY)["gemini-cli"]


class TestTheTwoGeminiRepresentationsAgree:
    """``gemini_litellm_overlay.yaml`` and ``gemini-cli.gateway_route`` are one
    recipe written twice. Neither is a copy to be edited alone."""

    def test_the_settings_file_content_and_path_match(self, overlay_spec):
        route = HARNESSES["gemini-cli"].gateway_route
        assert overlay_spec.config_files == route.config_files

    def test_the_container_env_matches(self, overlay_spec):
        route = HARNESSES["gemini-cli"].gateway_route
        assert overlay_spec.container_env == route.container_env

    def test_both_resolve_to_the_same_endpoint_and_credential(self, overlay_spec):
        """The two spell the endpoint differently — the overlay concatenates
        ``${secret:LITELLM_BASE_URL}`` with a literal ``/gemini``, the route
        composes ``${gateway.base_url}`` with its own ``passthrough_path`` —
        so agreement is only observable after both are rendered."""
        route = HARNESSES["gemini-cli"].gateway_route
        gateway = ALTERNATIVE_GATEWAYS[route.gateway]

        overlay_rendered = {
            key: _expand_secret_refs(value) for key, value in overlay_spec.provider_env.items()
        }

        assert _render_provider_env(route, gateway) == overlay_rendered
        assert overlay_rendered["GOOGLE_GEMINI_BASE_URL"] == "https://litellm.invalid/gemini"


class TestTheRenderedRecipesAreWhatAConsumerReceives:
    def test_rendered_gateway_recipes(self, canon_snapshot):
        snap = canon_snapshot("gateway_route_recipes")
        snap.assert_match(
            {name: _render_route(name) for name in ("gemini-cli", "kimi-code")},
            "rendered_recipes.json",
        )

    def test_no_rendered_value_still_carries_an_unexpanded_token(self):
        """Steps 0–3 leave nothing behind. A leftover ``${gateway.…}`` or
        ``${secret:…}`` would reach a container verbatim and earn a 401 at the
        gateway — the failure class ADR-0037's ordering contract exists to
        remove."""
        for name in ("gemini-cli", "kimi-code"):
            rendered = _render_route(name)
            values = [
                *rendered["provider_env"].values(),
                *rendered["container_env"].values(),
                *rendered["config_files"],
                *rendered["config_files"].values(),
            ]
            leftover = [value for value in values if "${" in value]
            assert not leftover, f"{name}: {leftover!r} still carries an unexpanded construct"
