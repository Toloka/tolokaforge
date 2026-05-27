"""Forensic ratchet — :attr:`Capability.ENUM_SLASH_TOLERANCE` in ``known_unsupported``.

Companion to :mod:`test_enum_slash_tolerance` for the *opposite*
direction: certs that declare ``ENUM_SLASH_TOLERANCE`` as
``known_unsupported`` MUST continue to reject the slash-in-enum schema.
If the upstream provider (xAI grok-4.3) relaxes its tool-schema
validator and starts accepting ``/`` in enum values, this test fails
loudly and forces a registry correction — flipping the cert to
``required`` and removing the workaround story from the codebase.

Without this ratchet, an upstream-side fix could land silently: the
harness would keep skipping the capability test for grok-4.3 forever,
the OTS bank failure would mysteriously start working, and we'd never
realise we could re-enable the route for production use.

Mirrors the design of
:mod:`test_implicit_prompt_caching_unsupported_ratchet`: instead of
skipping when the capability is ``known_unsupported``, this test runs
ONLY in that case and asserts the negative — that the API call still
fails with the expected upstream rejection.

Cost is ~$0.01 per cert per run (single small request to OpenRouter).
"""

from __future__ import annotations

import pytest

from tolokaforge.core.models import Message, MessageRole

from ._capability import Capability, ModelCertificate
from .registry import ALL_MODELS
from .test_enum_slash_tolerance import _SYSTEM, _TOOL_WITH_SLASH_ENUM


def _ratchet_targets() -> list[ModelCertificate]:
    """Certs that claim to NOT accept ``/`` in enum values."""
    return [
        cert for cert in ALL_MODELS if Capability.ENUM_SLASH_TOLERANCE in cert.known_unsupported
    ]


_TARGETS = _ratchet_targets()


@pytest.mark.parametrize("cert", _TARGETS, ids=lambda c: c.model_id)
def test_known_unsupported_routes_still_reject_slashed_enum(
    cert: ModelCertificate,
    live_client,
) -> None:
    """If the cert says "rejects `/` in enum", the provider must agree.

    Fails when the call **succeeds** despite the cert declaring
    ``ENUM_SLASH_TOLERANCE`` in ``known_unsupported`` — ie upstream
    silently fixed the validator quirk. The fix path is to move the
    capability from ``known_unsupported`` to ``required`` in
    :mod:`tests.integration.llm.registry`, drop the comment explaining
    the rejection, and verify with the standard
    :mod:`test_enum_slash_tolerance` test.
    """
    client = live_client(cert)
    raised = None
    try:
        result = client.generate(
            system=_SYSTEM,
            messages=[
                Message(
                    role=MessageRole.USER,
                    content="Please create an income/salary verification letter for me.",
                )
            ],
            tools=[_TOOL_WITH_SLASH_ENUM],
            tool_choice="auto",
            max_tokens=64,
        )
    except Exception as exc:  # noqa: BLE001 — provider rejection is the success path here.
        raised = exc

    if raised is not None:
        # Expected: provider rejected the schema, just like in eval.
        # Sanity-check the failure shape so a *different* error (eg
        # auth, network) doesn't masquerade as the quirk being intact.
        msg = str(raised).lower()
        plausible = any(
            marker in msg
            for marker in (
                "invalid arguments passed to the model",
                "bad request",
                "400",
                "openrouterexception",
            )
        )
        assert plausible, (
            f"{cert.model_id}: call failed but the error doesn't look "
            f"like the documented schema rejection — it might be a "
            f"transient or unrelated error masking a real fix. "
            f"Raised: {raised!r}"
        )
        return  # ratchet still holds — provider rejected as expected.

    # Provider returned a result — the quirk is gone.
    pytest.fail(
        f"{cert.model_id}: provider accepted the slash-in-enum schema "
        f"that the cert claims it rejects (ENUM_SLASH_TOLERANCE in "
        f"known_unsupported). The upstream validator has been relaxed; "
        f"move the capability to ``required`` in "
        f"tests/integration/llm/registry.py and remove the explanatory "
        f"comment. Reference response: tool_calls={bool(result.tool_calls)}, "
        f"text={(result.text or '')[:120]!r}."
    )


def test_ratchet_has_targets() -> None:
    """Sanity: the ratchet must actually parametrize over some certs.

    If every cert moves to ``required`` (good outcome — the upstream
    quirk is fixed everywhere) this list goes empty, and parametrize
    would emit a single skipped item. Fail loudly so the ratchet is
    retired deliberately rather than silently rotting in the suite.
    """
    msg = (
        "ENUM_SLASH_TOLERANCE ratchet has no targets — every cert now "
        "declares the capability as required. Retire this diagnostic "
        "test module rather than leaving a parametrize-over-empty "
        "vestige."
    )
    assert _TARGETS, msg
