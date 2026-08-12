"""Unit tests for ``automation.pricing``: the per-token -> per-1M conversion and
the ``--check`` exit codes (``run(..., check=True)``)."""

from __future__ import annotations

import json

import automation.pricing as pricing
import pytest

pytestmark = pytest.mark.unit


def test_entry_for_converts_per_token_to_per_million():
    models = [
        {
            "id": "vendor/model-x",
            "pricing": {
                "prompt": "0.0000015",
                "completion": "0.000002",
                "input_cache_read": "0.0000001",
            },
        },
        {"id": "vendor/other", "pricing": {"prompt": "0.000001", "completion": "0.000001"}},
    ]
    assert pricing.entry_for(models, "vendor/model-x") == {
        "input": 1.5,
        "output": 2.0,
        "cache_read": 0.1,
    }


def test_entry_for_skips_zero_pricing_and_absent_cache_and_missing_model():
    # both prompt+completion zero -> not priced
    assert (
        pricing.entry_for([{"id": "z", "pricing": {"prompt": "0", "completion": "0"}}], "z") is None
    )
    # zero cache rate omitted (a literal cache_read:0 would tell the engine reads are free)
    e = pricing.entry_for(
        [
            {
                "id": "a",
                "pricing": {
                    "prompt": "0.000001",
                    "completion": "0.000002",
                    "input_cache_read": "0",
                },
            }
        ],
        "a",
    )
    assert e == {"input": 1.0, "output": 2.0}
    # model not in the list
    assert (
        pricing.entry_for(
            [{"id": "a", "pricing": {"prompt": "0.000001", "completion": "0"}}], "nope"
        )
        is None
    )


def test_check_mode_exit_codes(tmp_path):
    pf = tmp_path / "pricing.json"
    pf.write_text(
        json.dumps({"_meta": {}, "models": {"vendor/known": {"input": 1.0, "output": 2.0}}})
    )
    assert pricing.run(name="vendor/known", pricing_file=str(pf), check=True) == 0
    assert pricing.run(name="vendor/unknown", pricing_file=str(pf), check=True) == 1


class TestADeclaredPrice:
    """The only way a gateway-only model can satisfy ``cost_usd_populated``.

    OpenRouter is the sole price source here, so a model it does not carry can never
    be priced by fetching, and that capability is CORE: it can never be declared
    unsupported, so the run cannot finish clean without this path.
    """

    def _file(self, tmp_path):
        pf = tmp_path / "pricing.json"
        pf.write_text(json.dumps({"_meta": {}, "models": {}}))
        return pf

    def test_it_is_written_without_any_fetch(self, tmp_path, monkeypatch):
        def explode():
            raise AssertionError("a declared price must not reach the network")

        monkeypatch.setattr(pricing, "_fetch_openrouter", explode)
        pf = self._file(tmp_path)
        assert pricing.run(name="azure_ai/m", pricing_file=str(pf), declared=(0.8, 3.2)) == 0
        assert json.loads(pf.read_text())["models"]["azure_ai/m"] == {"input": 0.8, "output": 3.2}

    def test_an_existing_entry_is_not_overwritten(self, tmp_path):
        pf = tmp_path / "pricing.json"
        pf.write_text(json.dumps({"_meta": {}, "models": {"azure_ai/m": {"input": 1.0}}}))
        assert pricing.run(name="azure_ai/m", pricing_file=str(pf), declared=(9.9, 9.9)) == 0
        assert json.loads(pf.read_text())["models"]["azure_ai/m"] == {"input": 1.0}

    def test_without_a_declaration_the_miss_is_reported_not_invented(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pricing, "_fetch_openrouter", lambda: [])
        pf = self._file(tmp_path)
        assert pricing.run(name="azure_ai/m", pricing_file=str(pf)) == 0
        assert json.loads(pf.read_text())["models"] == {}
