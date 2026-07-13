"""Unit tests for ``automation.pricing`` (migrated from
``tests/unit/test_ensure_pricing.py``). Covers the per-token -> per-1M conversion and
the ``--check`` exit codes (now ``run(..., check=True)``)."""

from __future__ import annotations

import json

import automation.pricing as pricing


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
