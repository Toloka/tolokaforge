"""Tests for pricing_updater.fetcher — pricing conversion and file I/O."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from pricing_updater.fetcher import (
    OPENROUTER_MODELS_URL,
    convert_pricing,
    fetch_openrouter_models,
    write_pricing_json,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# convert_pricing
# ---------------------------------------------------------------------------


class TestConvertPricing:
    """Test convert_pricing() pure transformation logic."""

    def test_basic_conversion(self) -> None:
        """Per-token prices are multiplied by 1_000_000 to get per-1M-token prices."""
        models = [
            {
                "id": "openai/gpt-4",
                "pricing": {"prompt": "0.00003", "completion": "0.00006"},
            }
        ]
        result = convert_pricing(models)
        assert "openai/gpt-4" in result
        assert result["openai/gpt-4"]["input"] == pytest.approx(30.0)
        assert result["openai/gpt-4"]["output"] == pytest.approx(60.0)

    def test_multiple_models(self) -> None:
        """Multiple models are all converted."""
        models = [
            {
                "id": "model-a",
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            },
            {
                "id": "model-b",
                "pricing": {"prompt": "0.00001", "completion": "0.00005"},
            },
        ]
        result = convert_pricing(models)
        assert len(result) == 2
        assert result["model-a"]["input"] == pytest.approx(1.0)
        assert result["model-a"]["output"] == pytest.approx(2.0)
        assert result["model-b"]["input"] == pytest.approx(10.0)
        assert result["model-b"]["output"] == pytest.approx(50.0)

    def test_skips_zero_pricing(self) -> None:
        """Models with all-zero pricing are excluded."""
        models = [
            {
                "id": "free-model",
                "pricing": {"prompt": "0", "completion": "0"},
            }
        ]
        result = convert_pricing(models)
        assert result == {}

    def test_skips_missing_id(self) -> None:
        """Models without an id field are skipped."""
        models = [{"pricing": {"prompt": "0.001", "completion": "0.002"}}]
        result = convert_pricing(models)
        assert result == {}

    def test_skips_missing_pricing(self) -> None:
        """Models without a pricing field are skipped."""
        models = [{"id": "no-price-model"}]
        result = convert_pricing(models)
        assert result == {}

    def test_skips_invalid_pricing_values(self) -> None:
        """Non-numeric pricing strings cause the model to be skipped."""
        models = [
            {
                "id": "bad-model",
                "pricing": {"prompt": "not-a-number", "completion": "0.001"},
            }
        ]
        result = convert_pricing(models)
        assert result == {}

    def test_handles_none_pricing_values(self) -> None:
        """None values in pricing fields are treated as zero via the 'or 0' fallback."""
        models = [
            {
                "id": "partial-model",
                "pricing": {"prompt": None, "completion": "0.00001"},
            }
        ]
        result = convert_pricing(models)
        # prompt is 0 but completion is non-zero, so not both zero → included
        assert "partial-model" in result
        assert result["partial-model"]["input"] == 0.0
        assert result["partial-model"]["output"] == pytest.approx(10.0)

    def test_empty_models_list(self) -> None:
        """An empty model list returns an empty dict."""
        assert convert_pricing([]) == {}

    def test_rounding_precision(self) -> None:
        """Result is rounded to 6 decimal places."""
        models = [
            {
                "id": "precise-model",
                "pricing": {"prompt": "0.00000123456789", "completion": "0.00000987654321"},
            }
        ]
        result = convert_pricing(models)
        # 0.00000123456789 * 1_000_000 = 1.23456789 → rounded to 1.234568
        assert result["precise-model"]["input"] == pytest.approx(1.234568)
        assert result["precise-model"]["output"] == pytest.approx(9.876543)


# ---------------------------------------------------------------------------
# write_pricing_json
# ---------------------------------------------------------------------------


class TestWritePricingJson:
    """Test write_pricing_json() file writing and merge logic."""

    def test_writes_new_file(self, tmp_path: Path) -> None:
        """Creates a valid pricing.json from scratch."""
        output = tmp_path / "pricing.json"
        pricing = {"model-a": {"input": 1.0, "output": 2.0}}

        count = write_pricing_json(pricing, output)

        assert count == 1
        data = json.loads(output.read_text())
        assert "models" in data
        assert data["models"]["model-a"]["input"] == 1.0
        assert "_meta" in data
        assert "updated_at" in data["_meta"]

    def test_merge_with_existing(self, tmp_path: Path) -> None:
        """New entries are added, existing entries are updated."""
        output = tmp_path / "pricing.json"
        # Write initial data
        initial = {
            "_meta": {"updated_at": "2024-01-01"},
            "models": {
                "old-model": {"input": 5.0, "output": 10.0},
                "shared-model": {"input": 1.0, "output": 2.0},
            },
        }
        output.write_text(json.dumps(initial))

        new_pricing = {
            "shared-model": {"input": 3.0, "output": 4.0},
            "new-model": {"input": 7.0, "output": 8.0},
        }
        count = write_pricing_json(new_pricing, output, merge=True)

        assert count == 3  # old-model + shared-model (updated) + new-model
        data = json.loads(output.read_text())
        assert data["models"]["old-model"]["input"] == 5.0  # kept
        assert data["models"]["shared-model"]["input"] == 3.0  # updated
        assert data["models"]["new-model"]["input"] == 7.0  # added

    def test_no_merge_replaces(self, tmp_path: Path) -> None:
        """With merge=False, existing data is ignored."""
        output = tmp_path / "pricing.json"
        initial = {
            "_meta": {},
            "models": {"old-model": {"input": 5.0, "output": 10.0}},
        }
        output.write_text(json.dumps(initial))

        new_pricing = {"new-model": {"input": 1.0, "output": 2.0}}
        count = write_pricing_json(new_pricing, output, merge=False)

        assert count == 1
        data = json.loads(output.read_text())
        assert "old-model" not in data["models"]
        assert "new-model" in data["models"]

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        """Nested directory structure is created automatically."""
        output = tmp_path / "deep" / "nested" / "pricing.json"
        pricing = {"model-x": {"input": 1.0, "output": 2.0}}

        count = write_pricing_json(pricing, output)
        assert count == 1
        assert output.exists()

    def test_models_sorted_alphabetically(self, tmp_path: Path) -> None:
        """Output models are sorted by key."""
        output = tmp_path / "pricing.json"
        pricing = {
            "zulu-model": {"input": 1.0, "output": 2.0},
            "alpha-model": {"input": 3.0, "output": 4.0},
        }
        write_pricing_json(pricing, output)
        data = json.loads(output.read_text())
        keys = list(data["models"].keys())
        assert keys == ["alpha-model", "zulu-model"]

    def test_handles_corrupt_existing_file(self, tmp_path: Path) -> None:
        """If the existing file is corrupt JSON, merge treats it as empty."""
        output = tmp_path / "pricing.json"
        output.write_text("not valid json!!!")

        pricing = {"model-a": {"input": 1.0, "output": 2.0}}
        count = write_pricing_json(pricing, output, merge=True)

        assert count == 1
        data = json.loads(output.read_text())
        assert "model-a" in data["models"]

    def test_meta_contains_source_url(self, tmp_path: Path) -> None:
        """The _meta block includes the OpenRouter source URL."""
        output = tmp_path / "pricing.json"
        write_pricing_json({"m": {"input": 1.0, "output": 2.0}}, output)
        data = json.loads(output.read_text())
        assert data["_meta"]["source_url"] == OPENROUTER_MODELS_URL


# ---------------------------------------------------------------------------
# fetch_openrouter_models (mocked HTTP)
# ---------------------------------------------------------------------------


class TestFetchOpenrouterModels:
    """Test fetch_openrouter_models() with mocked HTTP responses."""

    def test_returns_data_list(self) -> None:
        """Successful response returns the 'data' list from JSON body."""
        mock_models = [{"id": "model-1"}, {"id": "model-2"}]
        mock_response = httpx.Response(
            200,
            json={"data": mock_models},
            request=httpx.Request("GET", OPENROUTER_MODELS_URL),
        )
        with patch("pricing_updater.fetcher.httpx.get", return_value=mock_response):
            result = fetch_openrouter_models()
        assert result == mock_models

    def test_returns_empty_when_no_data_key(self) -> None:
        """If API returns no 'data' key, returns empty list."""
        mock_response = httpx.Response(
            200,
            json={"something_else": []},
            request=httpx.Request("GET", OPENROUTER_MODELS_URL),
        )
        with patch("pricing_updater.fetcher.httpx.get", return_value=mock_response):
            result = fetch_openrouter_models()
        assert result == []

    def test_raises_on_http_error(self) -> None:
        """HTTP errors propagate as exceptions."""
        mock_response = httpx.Response(
            500,
            request=httpx.Request("GET", OPENROUTER_MODELS_URL),
        )
        with patch("pricing_updater.fetcher.httpx.get", return_value=mock_response):
            with pytest.raises(httpx.HTTPStatusError):
                fetch_openrouter_models()
