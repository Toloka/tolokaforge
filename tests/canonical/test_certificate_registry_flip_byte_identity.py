"""Cutover-acceptance lock for the certificate registry.

**This is a cutover-scope test.** It certifies that the wheel-split move
of the certificate registry from ``tolokaforge/testing/certify/_registry.py``
to ``tolokaforge_models/src/tolokaforge_models/certificates/registry.py``
preserves the tuple byte-for-byte. Once the first ``v0.17.0`` engine tag
and the first ``models-v1.0.0`` models-wheel tag ship, any Bucket-A model
integration is *supposed* to change the registry — a new certificate is
exactly the shape a Bucket-A PR ships — and the hardcoded count / sha256
here would then red every legitimate integration.

**Scheduled for deletion** in the same PR that lands the first post-cutover
model integration (or, whichever comes first, the ``v0.17.0`` release
commit). Tracked as a follow-up in ADR-0030 § Follow-ups. The invariants
this test locks are the milestone's release-gate signal, not an ongoing
ratchet.

Locks four invariants of the tuple exposed at
:data:`tolokaforge.testing.certify.ALL_MODELS` and its authoritative
source :data:`tolokaforge_models.certificates.ALL_MODELS` at cutover time:

* Count exactly 39 — matches the pre-flip registry cardinality.
* The lowercase-hex sha256 over the newline-joined ``model_id`` list is
  the hardcoded :data:`EXPECTED_MODEL_ID_LIST_SHA256`. Any drift — an
  accidental reorder, a dropped certificate, a stray whitespace tweak
  in one entry's ``model_id`` — flips the digest and fails loud.
* A handful of well-known certificates (Muse Spark, one OpenAI, one
  Gemini) remain present at the merged registry.
* Every element is a :class:`ModelCertificate` — proves the tuple is
  the engine-side dataclass, not a stringly-typed stand-in.

Both access paths — the models-wheel entry point and the
:mod:`tolokaforge.testing.certify` re-export — hit the same tuple
identity.
"""

from __future__ import annotations

import hashlib
from typing import Final

import pytest
from tolokaforge_models.certificates import ALL_MODELS as MODELS_WHEEL_ALL_MODELS

from tolokaforge.testing.certify import ALL_MODELS, ModelCertificate

pytestmark = pytest.mark.canonical


EXPECTED_MODEL_ID_LIST_SHA256: Final[str] = (
    "f0316f5559f500c8484572a35ffb79b777e3b9881efee2339504f41a14331a3d"
)
"""Lowercase-hex sha256 over ``"\\n".join(cert.model_id for cert in ALL_MODELS)``.

Regenerate this constant only when the registry itself gains or loses a
certificate — never to make a green test go green after an accidental
reorder.
"""


EXPECTED_MODEL_COUNT: Final[int] = 39


def _model_id_list_sha256(certificates: tuple[ModelCertificate, ...]) -> str:
    payload = "\n".join(cert.model_id for cert in certificates)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class TestCertificateRegistryShape:
    def test_count_is_thirty_nine(self) -> None:
        assert len(ALL_MODELS) == EXPECTED_MODEL_COUNT

    def test_every_entry_is_a_model_certificate(self) -> None:
        for cert in ALL_MODELS:
            assert isinstance(cert, ModelCertificate)

    def test_model_id_list_sha256_matches_expected(self) -> None:
        assert _model_id_list_sha256(ALL_MODELS) == EXPECTED_MODEL_ID_LIST_SHA256


class TestSeamAgreement:
    def test_certify_seam_and_models_wheel_return_the_same_tuple(self) -> None:
        assert ALL_MODELS is MODELS_WHEEL_ALL_MODELS

    def test_models_wheel_count_matches_expected(self) -> None:
        assert len(MODELS_WHEEL_ALL_MODELS) == EXPECTED_MODEL_COUNT

    def test_models_wheel_hash_matches_expected(self) -> None:
        assert _model_id_list_sha256(MODELS_WHEEL_ALL_MODELS) == EXPECTED_MODEL_ID_LIST_SHA256


class TestWellKnownCertificatesPresent:
    """Family-coverage floor. The sha256 already guards individual model_ids,
    but a bare hash-drift error is opaque — these explicit membership checks
    surface the specific missing family in the failure message.
    """

    @pytest.mark.parametrize(
        "model_id",
        [
            "openrouter__meta_muse-spark-1.1",
            "openrouter__openai_gpt-5.4",
            "openrouter__google_gemini-3-flash-preview",
        ],
    )
    def test_well_known_model_id_present(self, model_id: str) -> None:
        model_ids = sorted(cert.model_id for cert in ALL_MODELS)
        assert model_id in model_ids, f"missing {model_id!r}; have {model_ids}"
