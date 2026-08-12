"""Pytest fixtures for the certification suite — public engine seam.

Consumable inside :mod:`tolokaforge.testing.certify.suite` via the
suite's own conftest, and by out-of-tree callers who want just the two
fixtures without the shared bodies:

.. code-block:: python

    # conftest.py in the consumer's repo
    pytest_plugins = ["tolokaforge.testing.certify.fixtures"]

* :func:`live_client` — factory returning an
  :class:`~tolokaforge.core.llm.client.LLMClient` for a given
  :class:`~tolokaforge.testing.certify.ModelCertificate`, skipping the
  test if the certificate's ``env_key`` is absent.
* :func:`skip_unless_capability_declared` — helper enforcing the
  required / known_unsupported / undeclared skip matrix so a test never
  silently passes a capability the certificate did not claim.

Auto-marking and the ``TF_PRESETS_FILE`` overlay hook live on the
suite's own conftest, not here, so an out-of-tree caller pulling in
just these fixtures does not have their unrelated tests silently marked
``integration`` / ``requires_api`` / ``llm``.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tolokaforge.core.llm import LLMClient, ReasoningConfig
from tolokaforge.core.models import ModelConfig

from ._capability import Capability
from .certificate import ModelCertificate


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
        # Route credential presence through SecretManager rather than
        # os.getenv so .env is honoured and the static-grep guard at
        # tests/unit/secrets/test_no_raw_secret_access.py sees no raw
        # credential env read on the shipped import path.
        from tolokaforge.secrets import get_default

        if not get_default().get_secret(cert.env_key):
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


@pytest.fixture
def skip_unless_capability_declared() -> Callable[[ModelCertificate, Capability], None]:
    """Return a helper that enforces the capability contract.

    Behaviour matrix:

    * ``capability in cert.required`` → returns cleanly; test body runs.
    * ``capability in cert.known_unsupported`` → ``pytest.skip`` with
      ``"<model_id> known_unsupported: <capability>"``.
    * Neither → ``pytest.skip`` with ``"capability not declared on
      <model_id>: <capability>"``. This branch is a contributor bug —
      the canonical capability-registry test rejects this state at the
      registry level — but the runtime skip is the belt-and-braces
      defence.
    """

    def _gate(cert: ModelCertificate, capability: Capability) -> None:
        if capability in cert.required:
            return
        if capability in cert.known_unsupported:
            pytest.skip(f"{cert.model_id} known_unsupported: {capability.value}")
        pytest.skip(f"capability not declared on {cert.model_id}: {capability.value}")

    return _gate
