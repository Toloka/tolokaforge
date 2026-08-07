"""LLM gateway (proxy) transport, configured entirely from the environment.

Some deployments do not let the engine talk to model providers directly.
Instead every call goes through a gateway — a `LiteLLM proxy
<https://docs.litellm.ai/docs/simple_proxy>`_ is the reference target, and any
gateway presenting the same surface works — which owns the upstream
credentials, enforces per-team budgets, and attributes spend.

This module resolves that transport from the environment. It is deliberately
**deployment-neutral**: nothing here knows about any particular organisation or
gateway product. A deployment supplies its own base URL, key, and whatever
attribution headers its gateway requires.

What "the same surface" means, precisely
----------------------------------------

Not "the gateway is OpenAI-compatible" — the narrower and more accurate
contract is: *for each routed provider, the gateway serves the route that
litellm's transport for that provider targets.* This module only overrides the
base URL; **litellm decides the path, the auth header, and the body shape**, and
that decision is per-provider. See the routing table below and
:data:`DEFAULT_ROUTED_PROVIDERS`, whose membership is pinned against the
installed litellm by
``tests/canonical/test_llm_gateway_envelope_contract.py``.

Environment contract
--------------------

All values are read through :class:`~tolokaforge.secrets.SecretManager`, so a
``.env`` file, the process environment, and the runner container's
``TOLOKAFORGE_SECRETS_JSON`` all work identically.

``LLM_PROXY_BASE_URL``
    Gateway base URL, e.g. ``https://gateway.example.com``. **Setting this is
    what enables the proxy transport** — every other variable is optional.

``LLM_PROXY_API_KEY``
    Credential presented to the gateway, sent as the request's API key. Leave
    it unset only for gateways that authenticate by network position: litellm
    then falls through to its normal provider-env lookup and would forward the
    *provider's* key to the gateway host instead.

``LLM_PROXY_HEADERS``
    JSON object of static headers added to every request, e.g.
    ``{"X-Team-Id": "research", "X-Cost-Center": "42"}``. Gateways commonly
    require attribution headers of this shape; keeping them as opaque
    configuration is what stops that requirement from leaking into engine code.

``LLM_PROXY_REQUEST_ID_HEADER``
    Optional header **name**. When set, each request gets that header with a
    fresh UUID4 value. Needed by gateways that want a per-request correlation
    id; a static env var cannot express "new value per call".

``LLM_PROXY_PROVIDERS``
    Optional comma-separated override of which ``provider`` values to route
    (matched against the config's ``provider``, and against its first path
    segment so ``openrouter/google`` matches ``openrouter``). When unset,
    only :data:`DEFAULT_ROUTED_PROVIDERS` are routed. Read the warning below
    before widening it.

Which providers can actually be routed
--------------------------------------

**Setting ``api_base`` does not make litellm speak OpenAI to that URL — it
makes litellm speak that provider's native protocol to that URL.** Verified by
capturing the wire (litellm 1.87.0):

===================  ==========================================================
provider             request litellm sends to the gateway
===================  ==========================================================
``openrouter/…``     ``POST {base}/chat/completions``, bearer auth
``openai/…``         ``POST {base}/chat/completions``, bearer auth
``anthropic/…``      ``POST {base}/v1/messages``, ``x-api-key``
``gemini/…``         ``POST {base}/models/<m>:generateContent``, ``x-goog-api-key``
===================  ==========================================================

Only the first two produce an OpenAI-envelope request, which is why
:data:`DEFAULT_ROUTED_PROVIDERS` contains exactly those. Naming another
provider in ``LLM_PROXY_PROVIDERS`` is allowed but means "my gateway also
serves this provider's native route" — true for a LiteLLM proxy's
``/v1/messages`` passthrough, false for a plain OpenAI-compatible gateway.
:data:`UNROUTABLE_PROVIDERS` cannot be opted in at all and are rejected
loudly, because no gateway can serve them (see that constant).

Routed calls now DO force the OpenAI envelope, because that is the gateway's own
protocol; see ``docs/LLM_LAYER.md`` § "Speaking to the gateway". Widening
``LLM_PROXY_PROVIDERS`` to a provider whose gateway route is a native passthrough
rather than an OpenAI-compatible one is therefore still not supported.

The model name on the wire
--------------------------

Routed calls are addressed by the gateway's route name, resolved from its catalog
(:mod:`tolokaforge.core.llm.gateway_route`). ``ModelConfig.provider`` / ``.name`` are
untouched, so preset resolution and
:func:`tolokaforge.core.pricing.normalize_model_name` are unaffected: both key off
the config, not off the wire name. See ``docs/LLM_LAYER.md`` § "Speaking to the
gateway" for the three catalog outcomes.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tolokaforge.secrets.expand import UnresolvedReferenceError, expand_secret_refs

if TYPE_CHECKING:
    from tolokaforge.secrets import SecretManager

ENV_BASE_URL = "LLM_PROXY_BASE_URL"
ENV_API_KEY = "LLM_PROXY_API_KEY"
ENV_HEADERS = "LLM_PROXY_HEADERS"
ENV_REQUEST_ID_HEADER = "LLM_PROXY_REQUEST_ID_HEADER"
ENV_PROVIDERS = "LLM_PROXY_PROVIDERS"
ENV_PREFERRED_ROUTE = "LLM_PROXY_PREFERRED_ROUTE"

#: Providers routed when ``LLM_PROXY_PROVIDERS`` is unset.
#:
#: Deliberately an allow-list, not an exclusion list: these are the providers
#: whose litellm transport emits an OpenAI-envelope ``POST
#: {base}/chat/completions``, which is what a LiteLLM proxy and comparable
#: gateways serve. See the module docstring for the wire capture behind this.
#:
#: This set is a claim about the *installed litellm*, not about gateways in
#: general, so it is pinned by
#: ``tests/canonical/test_llm_gateway_envelope_contract.py``. A litellm upgrade
#: that changes a provider's transport fails that test rather than silently
#: posting to a route the gateway does not serve.
DEFAULT_ROUTED_PROVIDERS = frozenset({"openrouter", "openai"})

#: Providers that can never be routed, even explicitly.
#:
#: ``mock`` never reaches the wire, so routing it is meaningless. ``nova``
#: relies on :meth:`LLMClient._call_with_key_rotation` rewriting its bare model
#: name into ``openai/<name>`` alongside its own hardcoded ``api_base``; a
#: gateway replaces the base URL but not the rewrite, leaving litellm with a
#: provider-less model string that raises ``BadRequestError`` before any
#: request is sent. Rejected at config time rather than failing per trial.
UNROUTABLE_PROVIDERS = frozenset({"mock", "nova"})


class ProxyConfigError(RuntimeError):
    """Raised when the proxy environment contract is set but malformed.

    Fail-fast by design: a gateway deployment whose attribution headers fail
    to parse would otherwise run a whole evaluation with unattributed spend.
    """


@dataclass(frozen=True)
class ProxyConfig:
    """Resolved gateway transport settings.

    Attributes
    ----------
    base_url:
        Gateway base URL passed to litellm as ``api_base``.
    api_key:
        Credential passed to litellm as ``api_key``. ``None`` leaves key
        resolution to litellm's normal provider-env lookup, which is useful for
        gateways that authenticate by network position rather than by token.
    headers:
        Static headers added to every request. Treated as read-only.
    request_id_header:
        Header name that receives a fresh UUID4 per request, or ``None``.
    providers:
        Explicit provider allow-list, or ``None`` for
        :data:`DEFAULT_ROUTED_PROVIDERS`.
    """

    base_url: str
    api_key: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    request_id_header: str | None = None
    providers: frozenset[str] | None = None
    preferred_route: str | None = None

    def applies_to(self, provider: str) -> bool:
        """Return whether ``provider`` should be routed through the gateway.

        Compound providers match on their first path segment too, so
        ``openrouter/google`` is covered by an allow-list entry of
        ``openrouter``.
        """
        normalized = (provider or "").strip().lower()
        base_segment = normalized.split("/")[0]
        if not base_segment or base_segment in UNROUTABLE_PROVIDERS:
            return False
        allowed = self.providers if self.providers is not None else DEFAULT_ROUTED_PROVIDERS
        return normalized in allowed or base_segment in allowed

    def request_headers(self) -> dict[str, str]:
        """Build this request's gateway headers.

        Returns a fresh dict every call because ``request_id_header`` must get
        a new value per request.
        """
        headers = dict(self.headers)
        if self.request_id_header:
            headers[self.request_id_header] = str(uuid.uuid4())
        return headers


def _parse_headers(raw: str | None, secrets: SecretManager) -> dict[str, str]:
    """Parse ``LLM_PROXY_HEADERS`` (a JSON object of string values).

    A value may reference a secret as ``${secret:NAME}``, resolved by
    :func:`tolokaforge.secrets.expand_secret_refs`.
    """
    if raw is None or not raw.strip():
        return {}
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProxyConfigError(
            f"{ENV_HEADERS} must be a JSON object of header name to value, "
            f"but it is not valid JSON: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ProxyConfigError(
            f"{ENV_HEADERS} must be a JSON object of header name to value, "
            f"got {type(parsed).__name__}"
        )
    headers: dict[str, str] = {}
    for raw_name, value in parsed.items():
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ProxyConfigError(f"{ENV_HEADERS} contains a non-string or empty header name")
        name = raw_name.strip()
        if not isinstance(value, str | int | float | bool):
            raise ProxyConfigError(
                f"{ENV_HEADERS} value for {name!r} must be a scalar, got {type(value).__name__}"
            )
        if not isinstance(value, str):
            # A JSON number/bool cannot carry a reference; keep the stringify path.
            headers[name] = str(value)
            continue
        try:
            headers[name] = expand_secret_refs(
                value, secrets, where=f"{ENV_HEADERS} value for {name!r}"
            )
        except UnresolvedReferenceError as exc:
            # Re-raised as the gateway's own error type so callers keep catching one
            # exception for "the gateway is misconfigured".
            raise ProxyConfigError(str(exc)) from exc
    return headers


def _parse_providers(raw: str | None) -> frozenset[str] | None:
    """Parse ``LLM_PROXY_PROVIDERS`` (comma-separated) into an allow-list."""
    if raw is None or not raw.strip():
        return None
    entries = {item.strip().lower() for item in raw.split(",") if item.strip()}
    if not entries:
        raise ProxyConfigError(
            f"{ENV_PROVIDERS} is set but contains no provider names; "
            f"unset it to use the default ({', '.join(sorted(DEFAULT_ROUTED_PROVIDERS))}), "
            f"or list at least one"
        )
    unroutable = sorted(entries & UNROUTABLE_PROVIDERS)
    if unroutable:
        raise ProxyConfigError(
            f"{ENV_PROVIDERS} names {', '.join(unroutable)}, which cannot be routed "
            f"through a gateway. 'mock' never reaches the wire; 'nova' needs a model-name "
            f"rewrite that the gateway path does not perform, so litellm would reject the "
            f"request before sending it. Remove it from {ENV_PROVIDERS}."
        )
    return frozenset(entries)


def resolve_proxy_config() -> ProxyConfig | None:
    """Resolve the gateway transport from the environment.

    Returns
    -------
    ProxyConfig | None
        ``None`` when ``LLM_PROXY_BASE_URL`` is unset, i.e. the engine talks to
        providers directly (the default).

    Raises
    ------
    ProxyConfigError
        When the gateway is enabled but a companion variable is malformed, or
        when companion variables are set while the base URL is missing.
    """
    from tolokaforge.secrets import get_default

    secrets = get_default()

    base_url = (secrets.get_secret(ENV_BASE_URL) or "").strip()
    if not base_url:
        # A companion variable without a base URL means someone intended to
        # route through a gateway and it silently did not happen — exactly the
        # unattributed-spend outcome this module exists to prevent. A typo in
        # the base-URL name lands here, so refuse rather than fall back to
        # direct provider access.
        orphans = sorted(
            name
            for name in (
                ENV_API_KEY,
                ENV_HEADERS,
                ENV_REQUEST_ID_HEADER,
                ENV_PROVIDERS,
                ENV_PREFERRED_ROUTE,
            )
            if (secrets.get_secret(name) or "").strip()
        )
        if orphans:
            raise ProxyConfigError(
                f"{', '.join(orphans)} is set but {ENV_BASE_URL} is empty, so calls would go "
                f"straight to the providers and bypass the gateway. Set {ENV_BASE_URL}, or "
                f"unset the others to disable the gateway deliberately."
            )
        return None

    return ProxyConfig(
        base_url=base_url.rstrip("/"),
        api_key=(secrets.get_secret(ENV_API_KEY) or "").strip() or None,
        headers=_parse_headers(secrets.get_secret(ENV_HEADERS), secrets),
        request_id_header=(secrets.get_secret(ENV_REQUEST_ID_HEADER) or "").strip() or None,
        providers=_parse_providers(secrets.get_secret(ENV_PROVIDERS)),
        preferred_route=(secrets.get_secret(ENV_PREFERRED_ROUTE) or "").strip() or None,
    )
