"""Forensic ratchet — :attr:`Capability.RE2_PATTERN_TOLERANCE` in ``known_unsupported``.

Companion to :mod:`test_re2_pattern_tolerance` for the *opposite*
direction: certs that declare ``RE2_PATTERN_TOLERANCE`` as
``known_unsupported`` MUST continue to reject the lookaround-bearing
``pattern`` schema. If the upstream provider (xAI grok-4.3) relaxes
its tool-schema validator and starts accepting RE2-incompatible
patterns, this test fails loudly and forces a registry correction —
flipping the cert to ``required`` and (eventually) retiring the
``StrictSchema.strip_re2_incompatible_patterns`` workaround.

Without this ratchet, an upstream-side fix could land silently: the
harness would keep skipping the capability test for grok-4.3 forever,
and the strip-trigger would stay in place even though it had become
unnecessary.

Mirrors the design of
:mod:`test_enum_slash_tolerance_unsupported_ratchet`: instead of
skipping when the capability is ``known_unsupported``, this test runs
ONLY in that case and asserts the negative — that the API call still
fails with the expected upstream rejection.

Cost is ~$0.01 per cert per run.
"""

from __future__ import annotations

import pytest

from tolokaforge.core.models import Message, MessageRole
from tolokaforge.testing.certify import ALL_MODELS, Capability, ModelCertificate

from .test_re2_pattern_tolerance import (
    _SYSTEM,
    _TOOL_WITH_RE2_INCOMPAT_PATTERN,
    _USER_TURN,
    _with_passthrough_sanitiser,
)


def _ratchet_targets() -> list[ModelCertificate]:
    """Certs that claim to NOT accept RE2-incompatible ``pattern`` values."""
    return [
        cert for cert in ALL_MODELS if Capability.RE2_PATTERN_TOLERANCE in cert.known_unsupported
    ]


_TARGETS = _ratchet_targets()


@pytest.mark.parametrize("cert", _TARGETS, ids=lambda c: c.model_id)
def test_known_unsupported_routes_still_reject_re2_incompatible_pattern(
    cert: ModelCertificate,
    live_client,
) -> None:
    """If the cert says "rejects lookaround patterns", the provider must agree.

    Fails when the call **succeeds** despite the cert declaring
    ``RE2_PATTERN_TOLERANCE`` in ``known_unsupported`` — ie upstream
    silently fixed the validator quirk. The fix path is to move the
    capability from ``known_unsupported`` to ``required`` in
    :mod:`tolokaforge_models.certificates.registry`, then consider retiring the
    ``StrictSchema.strip_re2_incompatible_patterns`` flag entirely
    (it would no longer be load-bearing anywhere).
    """
    client = live_client(cert)
    _with_passthrough_sanitiser(client)

    raised = None
    try:
        result = client.generate(
            system=_SYSTEM,
            messages=[Message(role=MessageRole.USER, content=_USER_TURN)],
            tools=[_TOOL_WITH_RE2_INCOMPAT_PATTERN],
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
        f"{cert.model_id}: provider accepted the RE2-incompatible-pattern "
        f"schema that the cert claims it rejects (RE2_PATTERN_TOLERANCE "
        f"in known_unsupported). The upstream validator has been relaxed; "
        f"move the capability to ``required`` in "
        f"tolokaforge_models/src/tolokaforge_models/certificates/registry.py and consider retiring the "
        f"``strip_re2_incompatible_patterns`` flag. Reference response: "
        f"tool_calls={bool(result.tool_calls)}, "
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
        "RE2_PATTERN_TOLERANCE ratchet has no targets — every cert now "
        "declares the capability as required. Retire this diagnostic "
        "test module rather than leaving a parametrize-over-empty "
        "vestige. The ``strip_re2_incompatible_patterns`` flag on "
        "StrictSchema can probably also be retired at this point — "
        "verify with a follow-up direct REST probe per the "
        "AGENTS.md gotcha #21 schema-dialect debugging recipe."
    )
    assert _TARGETS, msg
