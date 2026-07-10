"""Unit tests for ``scripts/integration/ensure_pricing.py``.

Path-loaded (``scripts/integration`` is not an importable package), like
``test_slack_notify.py`` / ``test_cert_reconcile.py``.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "integration" / "ensure_pricing.py"
_spec = importlib.util.spec_from_file_location("ensure_pricing", _MODULE_PATH)
ensure_pricing = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ensure_pricing)


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
    assert ensure_pricing.entry_for(models, "vendor/model-x") == {
        "input": 1.5,
        "output": 2.0,
        "cache_read": 0.1,
    }


def test_entry_for_skips_zero_pricing_and_absent_cache_and_missing_model():
    # both prompt+completion zero -> not priced
    assert (
        ensure_pricing.entry_for([{"id": "z", "pricing": {"prompt": "0", "completion": "0"}}], "z")
        is None
    )
    # zero cache rate omitted (a literal cache_read:0 would tell the engine reads are free)
    e = ensure_pricing.entry_for(
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
        ensure_pricing.entry_for(
            [{"id": "a", "pricing": {"prompt": "0.000001", "completion": "0"}}], "nope"
        )
        is None
    )


def test_check_mode_exit_codes(tmp_path):
    pf = tmp_path / "pricing.json"
    pf.write_text(
        json.dumps({"_meta": {}, "models": {"vendor/known": {"input": 1.0, "output": 2.0}}})
    )
    assert (
        ensure_pricing.main(["--name", "vendor/known", "--pricing-file", str(pf), "--check"]) == 0
    )
    assert (
        ensure_pricing.main(["--name", "vendor/unknown", "--pricing-file", str(pf), "--check"]) == 1
    )
