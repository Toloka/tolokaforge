"""Observe-stage shape-variant runner (separate from the canonical cert suite).

Runs each :class:`ShapeVariant` against the candidate model. It is gated the same
way as the canonical suite (``skip_unless_capability_declared``) AND behind the
``TF_RUN_VARIANTS`` env flag, so a normal CI integration run skips it entirely and
only the model auto-integration observe workflow (which sets the flag + injects the
all-required candidate cert) exercises it. Report-only: a miss is data for the next
step, not a gate.
"""

from __future__ import annotations

import json
import os

import pytest

from tolokaforge.core.models import Message, MessageRole

from .._capability import ModelCertificate
from ..registry import ALL_MODELS
from ._shape_variants import SHAPE_VARIANTS, ShapeVariant


@pytest.mark.parametrize("cert", ALL_MODELS, ids=lambda c: c.model_id)
@pytest.mark.parametrize(
    "variant", SHAPE_VARIANTS, ids=lambda v: f"{v.capability.value}__{v.variant_id}"
)
def test_shape_variant(
    cert: ModelCertificate,
    variant: ShapeVariant,
    live_client,
    skip_unless_capability_declared,
) -> None:
    """Stress a shape-sensitive capability with one structural variation.

    Same contract as the canonical shape probes: a non-empty tool call whose
    arguments are a native dict, then a variant-specific structural check.
    """
    if not os.getenv("TF_RUN_VARIANTS"):
        pytest.skip("shape-variant suite runs only in the observe stage (set TF_RUN_VARIANTS)")
    skip_unless_capability_declared(cert, variant.capability)

    client = live_client(cert)
    result = client.generate(
        system=variant.system,
        messages=[Message(role=MessageRole.USER, content=variant.user)],
        tools=list(variant.tools),
        tool_choice="auto",
    )

    assert (
        result.tool_calls
    ), f"{cert.model_id}/{variant.variant_id}: expected at least one tool call ({result!r})"
    args = result.tool_calls[0].arguments
    if isinstance(args, str):
        args = json.loads(args)
    assert isinstance(args, dict), (
        f"{cert.model_id}/{variant.variant_id}: arguments must parse as dict, "
        f"got {type(args).__name__}: {args!r}"
    )
    variant.check(args)
