"""Unit tests for the model-data fingerprint schema and compute path.

Locks the fingerprint contract behaviour:

* Determinism — same resolved state, byte-identical sha.
* Overlay sensitivity — preset overlay, pricing overlay, and certificate
  registry mutations each change the sha.
* Field shape — every field is populated, the api_version is fixed, the
  digest is 64-char lowercase hex, ``package_version`` matches
  :data:`tolokaforge_models.__version__`, and ``minimum_engine_version``
  matches :data:`tolokaforge_models.minimum_engine_version` and parses
  as a PEP 440 specifier.
* Decoder — absence returns ``None``; a well-formed dict round-trips;
  a malformed dict raises :class:`pydantic.ValidationError`.

Schema types live in :mod:`tolokaforge.core.model_data`; the compute
path lives in :mod:`tolokaforge.core.model_data_fingerprint`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from packaging.specifiers import SpecifierSet
from pydantic import ValidationError

import tolokaforge_models
from tolokaforge.core import model_data as md
from tolokaforge.core import model_data_fingerprint as mdf
from tolokaforge.core.llm.presets import set_overlay_path
from tolokaforge.core.pricing import MODEL_PRICING, reload_pricing
from tolokaforge.testing import certify as certify_pkg
from tolokaforge.testing.certify import Capability, ModelCertificate

pytestmark = pytest.mark.unit


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


@pytest.fixture(autouse=True)
def _restore_pricing() -> Any:
    """Restore :data:`MODEL_PRICING` after any test that mutates it."""
    snapshot = {k: dict(v) for k, v in MODEL_PRICING.items()}
    yield
    reload_pricing()
    MODEL_PRICING.clear()
    MODEL_PRICING.update(snapshot)


def test_field_shape_reflects_installed_models_wheel() -> None:
    fp = mdf.compute_models_fingerprint()

    assert fp.package_version == tolokaforge_models.__version__
    assert fp.api_version == 1
    assert _HEX64.match(fp.content_sha256), fp.content_sha256
    assert fp.minimum_engine_version == tolokaforge_models.minimum_engine_version


def test_minimum_engine_version_parses_as_pep440_specifier() -> None:
    spec = SpecifierSet(tolokaforge_models.minimum_engine_version)

    assert "0.17.0" in spec
    assert "0.17.5" in spec
    assert "0.18.0" not in spec
    assert "0.16.9" not in spec


def test_determinism_same_state_same_digest() -> None:
    first = mdf.compute_models_fingerprint()
    second = mdf.compute_models_fingerprint()

    assert first.content_sha256 == second.content_sha256
    assert first.model_dump() == second.model_dump()


def test_preset_overlay_changes_digest(write_overlay: Any) -> None:
    baseline = mdf.compute_models_fingerprint().content_sha256

    overlay_path = write_overlay(
        {
            "presets": {
                "fingerprint_test_preset": {
                    "match": ["fingerprint/test-*"],
                    "schema_sanitizer": "strict",
                }
            }
        }
    )
    set_overlay_path(overlay_path)

    after = mdf.compute_models_fingerprint().content_sha256

    assert after != baseline


def test_pricing_overlay_changes_digest(tmp_path: Path) -> None:
    baseline = mdf.compute_models_fingerprint().content_sha256

    overlay = tmp_path / "pricing_overlay.json"
    overlay.write_text(
        json.dumps({"models": {"fingerprint/test-only": {"input": 1.23, "output": 4.56}}})
    )
    reload_pricing(overlay_path=overlay)

    after = mdf.compute_models_fingerprint().content_sha256

    assert after != baseline


def test_certificate_registry_changes_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    """A change to ANY certificate field must move the sha.

    Rebuilds the first entry of :data:`ALL_MODELS` with one extra
    ``capability_extras`` key — the field has no cross-field invariants
    so the tweak is minimal and unambiguous — and asserts the digest
    shifts.
    """
    baseline = mdf.compute_models_fingerprint().content_sha256

    first = certify_pkg.ALL_MODELS[0]
    tweaked_first = ModelCertificate(
        model_id=first.model_id,
        provider=first.provider,
        name=first.name,
        env_key=first.env_key,
        required=first.required,
        known_unsupported=first.known_unsupported,
        excluded_capabilities=first.excluded_capabilities,
        known_unsupported_reasons=dict(first.known_unsupported_reasons),
        probe_params={k: dict(v) for k, v in first.probe_params.items()},
        capability_extras={**dict(first.capability_extras), "fingerprint_probe": "1"},
    )
    replacement = (tweaked_first, *certify_pkg.ALL_MODELS[1:])
    monkeypatch.setattr(mdf, "ALL_MODELS", replacement)

    after = mdf.compute_models_fingerprint().content_sha256

    assert after != baseline


def test_capability_serialisation_uses_value_not_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every :class:`Capability` in the hashed payload must serialise as
    ``.value`` (snake_case) — never ``.name`` (upper-snake).

    Injects a minimal single-certificate registry so the assertion is
    unambiguous, then rebuilds the canonical JSON payload with the same
    recipe :func:`compute_models_fingerprint` uses and inspects the
    serialised certificate.
    """
    cert = ModelCertificate(
        model_id="fingerprint__probe",
        provider="fingerprint",
        name="fingerprint/probe",
        env_key="FINGERPRINT_TEST_KEY",
        required=frozenset({Capability.BASIC_COMPLETION}),
        known_unsupported=frozenset({Capability.SIMPLE_TOOL_CALL}),
        known_unsupported_reasons={Capability.SIMPLE_TOOL_CALL: "probe fixture"},
        probe_params={Capability.BASIC_COMPLETION: {"prompt_tokens": 10}},
    )
    monkeypatch.setattr(mdf, "ALL_MODELS", (cert,))

    fp = mdf.compute_models_fingerprint()

    # Rebuild the payload the compute function hashed to inspect it.
    serialised = mdf._certificate_to_dict(cert)

    assert "basic_completion" in serialised["required"]
    assert "BASIC_COMPLETION" not in serialised["required"]
    assert "simple_tool_call" in serialised["known_unsupported"]
    assert "simple_tool_call" in serialised["known_unsupported_reasons"]
    assert "basic_completion" in serialised["probe_params"]
    assert serialised["probe_params"]["basic_completion"] == {"prompt_tokens": 10}
    assert _HEX64.match(fp.content_sha256)


def test_decode_returns_none_when_field_absent() -> None:
    assert md.decode_models_fingerprint({}) is None
    assert md.decode_models_fingerprint({"run_id": "r", "presets_file": None}) is None


def test_decode_round_trip_with_valid_payload() -> None:
    fp = mdf.compute_models_fingerprint()
    payload = {"models_fingerprint": fp.model_dump()}

    decoded = md.decode_models_fingerprint(payload)

    assert decoded is not None
    assert decoded.model_dump() == fp.model_dump()


def test_decode_raises_validation_error_on_malformed_payload() -> None:
    with pytest.raises(ValidationError):
        md.decode_models_fingerprint({"models_fingerprint": {"package_version": "x"}})


def test_minimum_engine_version_rejects_invalid_pep440_specifier() -> None:
    with pytest.raises(ValidationError):
        md.ModelsFingerprint(
            package_version="1.0.0",
            content_sha256="a" * 64,
            api_version=1,
            minimum_engine_version="not a specifier",
        )


def test_api_version_rejects_unknown_contract_version() -> None:
    with pytest.raises(ValidationError):
        md.ModelsFingerprint(
            package_version="1.0.0",
            content_sha256="a" * 64,
            api_version=99,
            minimum_engine_version=">=0.16,<0.17",
        )


def test_content_sha256_rejects_non_hex_or_truncated_digest() -> None:
    with pytest.raises(ValidationError):
        md.ModelsFingerprint(
            package_version="1.0.0",
            content_sha256="not_hex_valid_content",
            api_version=1,
            minimum_engine_version=">=0.16,<0.17",
        )
