"""LLM-layer unit-test fixtures.

The overlay fixtures (``overlay_isolation``, ``write_overlay``) live in the
top-level ``tests/conftest.py`` so integration tests can use them too.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tolokaforge.core.llm import gateway_route


@pytest.fixture(autouse=True)
def _no_gateway_catalog_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep the gateway catalog off the wire for every LLM unit test.

    Building an ``LLMClient`` with ``LLM_PROXY_BASE_URL`` set fetches the catalog, so
    without this a unit test does live DNS and HTTP against whatever host it configured
    and can block for the fetch timeout. It also made those tests *network-dependent*:
    they passed only while the fake host failed to answer, and would have flipped behind
    a captive portal or a proxy that answers 200.

    The default stands in for an unreadable catalog, which is the branch that keeps the
    gateway and leaves the model string alone, so existing expectations are unchanged.
    A test that cares about a resolved route patches
    ``tolokaforge.core.llm.client.fetch_gateway_catalog`` itself.
    """
    gateway_route.clear_catalog_cache()
    monkeypatch.setattr("tolokaforge.core.llm.client.fetch_gateway_catalog", lambda *_a, **_k: None)
    yield
    gateway_route.clear_catalog_cache()
