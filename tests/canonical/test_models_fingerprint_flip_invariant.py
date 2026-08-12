"""Cutover-acceptance lock on the fingerprint sub-payload.

**This is a cutover-scope test.** It certifies that the wheel-split flip
of ``_DATA_ROOT`` from ``tolokaforge/core/data/`` to
``tolokaforge_models/data/`` preserves the fingerprint sub-payload
(``{presets, pricing, certificates}``) byte-for-byte. Once the first
``v0.17.0`` / ``models-v1.0.0`` tags ship, any Bucket-A model change is
*supposed* to shift the digest — new prices, new certificates, new preset
entries all legitimately move it — and the hardcoded hex here would then
red every legitimate integration.

**Scheduled for deletion** in the same PR that lands the first
post-cutover model integration (or, whichever comes first, the
``v0.17.0`` release commit). Tracked as a follow-up in ADR-0030
§ Follow-ups. The invariant this test locks is the milestone's
release-gate signal, not an ongoing ratchet.

Locks ``sha256({presets, pricing, certificates})`` — the models-fingerprint
payload minus its ``providers`` slice — as byte-invariant against a
hardcoded hex.
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

EXPECTED_SUB_PAYLOAD_SHA256: Final[str] = (
    "29b3ee7c769aeca01a50bfbcc656e2bc86183fef7edb8339851894a6483004e1"
)
"""sha256 over the canonicalised ``{presets, pricing, certificates}`` triple.
Reproduce by running :func:`_compute_sub_payload_sha`."""


def _compute_sub_payload_sha() -> str:
    """Compute ``sha256({presets, pricing, certificates})`` using the same
    canonicalisation
    :func:`~tolokaforge.core.model_data_fingerprint.compute_models_fingerprint`
    applies: ``json.dumps(..., sort_keys=True, ensure_ascii=True,
    separators=(",", ":"))`` over the UTF-8 bytes. Excludes the ``providers``
    slice so the hash locks the three inputs that were hashed together before
    ``providers.yaml`` joined the payload.
    """
    payload = {
        "presets": get_resolved_presets(),
        "pricing": dict(MODEL_PRICING),
        "certificates": [_certificate_to_dict(cert) for cert in ALL_MODELS],
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_sub_payload_sha_matches_locked_hex() -> None:
    actual = _compute_sub_payload_sha()

    assert actual == EXPECTED_SUB_PAYLOAD_SHA256, (
        f"sha256({{presets, pricing, certificates}}) drifted: expected "
        f"{EXPECTED_SUB_PAYLOAD_SHA256}, got {actual}. One of the three "
        f"inputs shifted — a byte tweak to a bundled data file, an "
        f"ALL_MODELS reordering, a dropped certificate, or a whitespace "
        f"/ sort-key change in the canonical JSON."
    )
