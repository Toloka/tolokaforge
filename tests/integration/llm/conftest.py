"""Shared pytest scaffolding for the Stage 8 capability-driven suite.

This conftest owns:

* **Auto-marking** — every test collected under ``tests/integration/llm/``
  is tagged with :func:`pytest.mark.integration`,
  :func:`pytest.mark.requires_api`, and :func:`pytest.mark.llm` so that
  marker-gated pytest runs (``-m integration``) pick them up without
  individual files having to declare ``pytestmark`` lists.
* **Live client factory** — :func:`live_client` builds an
  :class:`~tolokaforge.core.llm.client.LLMClient` for a certificate and
  skips the test when the certificate's ``env_key`` is absent from the
  environment. Default reasoning is ``mode="off"``; capability tests
  needing reasoning (``test_thinking_*``, ``test_prompt_caching``)
  construct their own :class:`ModelConfig` directly to keep the fixture
  contract narrow.
* **Capability honesty guard** —
  :func:`skip_unless_capability_declared` enforces Stage 8 rule #5:
  a test MUST either run cleanly or skip with an explanatory message;
  never silently treat a missing declaration as a pass.

See [`docs/ADD_NEW_MODEL.md`](../../../docs/ADD_NEW_MODEL.md) for the
contributor walkthrough.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import pytest

from tolokaforge.core.llm import LLMClient, ReasoningConfig
from tolokaforge.core.models import ModelConfig

from ._capability import Capability, ModelCertificate

# ---------------------------------------------------------------------------
# Auto-marking — every test under tests/integration/llm/ is marker-gated
# to the `integration`, `requires_api`, and `llm` markers. Declared once
# here so individual capability files stay terse.
# ---------------------------------------------------------------------------

_AUTO_MARKERS = ("integration", "requires_api", "llm")


def pytest_configure(config):  # type: ignore[no-untyped-def]
    """Install a preset overlay for the whole session when ``TF_PRESETS_FILE`` is set.

    The model auto-integration re-probe step points this at the policy overlay the
    resolve agent set or created, so the capability probes run under that policy's
    adapters (schema sanitizer / response / prompt) instead of the bundled default.
    The candidate certificate stays all-required (nothing skips); only the adapters
    change, which is exactly what a policy re-probe verifies. The overlay is
    validated up front so a malformed policy fails loudly at startup, not silently as
    skipped probes. A normal CI run leaves ``TF_PRESETS_FILE`` unset and this is a
    no-op (the bundled presets apply as usual).
    """
    del config  # unused - the hook signature is fixed by pytest.
    overlay = os.getenv("TF_PRESETS_FILE")
    if not overlay:
        return
    from tolokaforge.core.llm import presets

    presets.validate_overlay_file(overlay)  # raises ValueError on a malformed overlay
    presets.set_overlay_path(overlay)


def pytest_collection_modifyitems(config, items):  # type: ignore[no-untyped-def]
    """Tag every test in this directory with the marker-gated markers."""
    del config  # unused — the hook signature is fixed by pytest.
    for item in items:
        # ``item.nodeid`` starts with ``tests/integration/llm/`` (relative
        # to the rootdir) for tests owned by this conftest. That's the
        # cheapest reliable filter.
        if "tests/integration/llm/" not in item.nodeid.replace("\\", "/"):
            continue
        for marker in _AUTO_MARKERS:
            if not item.get_closest_marker(marker):
                item.add_marker(getattr(pytest.mark, marker))


# ---------------------------------------------------------------------------
# live_client — factory fixture; called as ``live_client(cert)``.
# ---------------------------------------------------------------------------


@pytest.fixture
def live_client() -> Callable[[ModelCertificate], LLMClient]:
    """Return a factory that constructs an :class:`LLMClient` for a
    given certificate, skipping the test if the provider key is missing.

    The factory returns an :class:`LLMClient` with
    ``ReasoningConfig(mode="off")`` — the default for plain-completion /
    tool-call capability tests. Tests exercising reasoning (thinking /
    adaptive / budget) build their own client directly against
    :class:`ModelConfig`; this fixture exists for the simple case.
    """

    def _build(cert: ModelCertificate) -> LLMClient:
        if not os.getenv(cert.env_key):
            pytest.skip(
                f"{cert.env_key} not set — skipping live capability test for "
                f"{cert.model_id}. Set the env var in .env to enable."
            )
        return LLMClient(
            ModelConfig(
                provider=cert.provider,
                name=cert.name,
                reasoning=ReasoningConfig(mode="off"),
            )
        )

    return _build


# ---------------------------------------------------------------------------
# skip_unless_capability_declared — honesty gate.
# ---------------------------------------------------------------------------


@pytest.fixture
def skip_unless_capability_declared() -> Callable[[ModelCertificate, Capability], None]:
    """Return a helper that enforces the Stage 8 capability contract.

    Behaviour matrix:

    * ``capability in cert.required`` → returns cleanly; test body runs.
    * ``capability in cert.known_unsupported`` → ``pytest.skip`` with
      ``"<model_id> known_unsupported: <capability>"``.
    * Neither → ``pytest.skip`` with ``"capability not declared on
      <model_id>: <capability>"``. This branch is a contributor bug —
      the Stage 8 canonical test rejects this state at the registry
      level — but the runtime skip is the belt-and-braces defence.
    """

    def _gate(cert: ModelCertificate, capability: Capability) -> None:
        if capability in cert.required:
            return
        if capability in cert.known_unsupported:
            pytest.skip(f"{cert.model_id} known_unsupported: {capability.value}")
        pytest.skip(f"capability not declared on {cert.model_id}: {capability.value}")

    return _gate
