"""Automated pre/post byte-identity lock on the fingerprint sub-payload.

The Milestone 29 cutover flips :data:`tolokaforge.core.model_data._DATA_ROOT`
from the engine-side ``tolokaforge/core/data/`` tree to the
``tolokaforge_models/data/`` tree shipped by the models wheel, and widens
the hashed payload of
:func:`tolokaforge.core.model_data_fingerprint.compute_models_fingerprint`
to include ``providers.yaml`` (previously not hashed).

The full-payload ``content_sha256`` therefore changes across the flip.
This test locks the *sub-payload* ``sha256({presets, pricing, certificates})``
— the payload minus the newly-joined ``providers`` slice — as byte-invariant
across the flip. Any drift — accidental ``ALL_MODELS`` reordering, dropped
certificate, whitespace tweak in the JSON canonicaliser, byte-copy mismatch
of the moved data files — fails the assertion naming the sub-payload.

The expected hex is the ``sha256({presets, pricing, certificates})`` computed
on the Stage 3 tip (post-move, pre-flip) using the exact same
canonicalisation :func:`compute_models_fingerprint` uses.
"""

from __future__ import annotations

import hashlib
import json
from typing import Final

import pytest

from tolokaforge.core.llm.presets import get_resolved_presets
from tolokaforge.core.model_data_fingerprint import _certificate_to_dict
from tolokaforge.core.pricing import MODEL_PRICING
from tolokaforge.testing.certify import ALL_MODELS

pytestmark = pytest.mark.canonical

EXPECTED_PRE_WIDENING_SHA256: Final[str] = (
    "29b3ee7c769aeca01a50bfbcc656e2bc86183fef7edb8339851894a6483004e1"
)
"""sha256 over the canonicalised ``{presets, pricing, certificates}`` triple
computed on Stage 3's tip (``3babf85a``) — before Stage 4 widened the payload
to include ``providers.yaml``. Reproduce by running
:func:`_compute_pre_widening_subset_sha` on that commit."""


def _compute_pre_widening_subset_sha() -> str:
    """Rebuild the pre-widening sub-payload from the current accessors.

    Uses the same canonicalisation
    :func:`~tolokaforge.core.model_data_fingerprint.compute_models_fingerprint`
    applies: ``json.dumps(..., sort_keys=True, ensure_ascii=True,
    separators=(",", ":"))`` over the UTF-8 bytes. Excludes the ``providers``
    key that Stage 4 added — the whole point of this test is to lock the
    unchanged sub-payload across the widening.
    """
    payload = {
        "presets": get_resolved_presets(),
        "pricing": dict(MODEL_PRICING),
        "certificates": [_certificate_to_dict(cert) for cert in ALL_MODELS],
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_sub_payload_is_byte_invariant_across_data_root_flip() -> None:
    actual = _compute_pre_widening_subset_sha()

    assert actual == EXPECTED_PRE_WIDENING_SHA256, (
        f"sha256({{presets, pricing, certificates}}) drifted across the "
        f"data-root flip: expected {EXPECTED_PRE_WIDENING_SHA256}, got "
        f"{actual}. This means one of the three inputs shifted — a byte "
        f"tweak to a moved data file, an ALL_MODELS reordering, a dropped "
        f"certificate, or a whitespace / sort-key change in the canonical "
        f"JSON — and the flip is not byte-identical anymore."
    )
