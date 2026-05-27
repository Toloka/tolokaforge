"""Tests for pricing_updater.fetcher — pricing conversion and file I/O."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from pricing_updater.cli import app as cli_app
from pricing_updater.fetcher import (
    OPENROUTER_MODELS_URL,
    SIGNIFICANT_PRICING_CHANGE_FACTOR,
    convert_pricing,
    fetch_openrouter_models,
    find_significant_pricing_changes,
    write_pricing_json,
)
from typer.testing import CliRunner

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

    # ----- Cache rate extraction -------------------------------------------
    #
    # OpenRouter's pricing object carries `input_cache_read` and
    # `input_cache_write` per-token strings — see
    # https://openrouter.ai/docs/api/api-reference/models/get-models. The
    # harness consumes these as `cache_read` / `cache_write` (USD per 1M
    # tokens) in `pricing.json`; ``tolokaforge.core.pricing._compute_cost``
    # treats a missing key as "no cache discount → fall back to input_rate",
    # so emitting a literal zero would silently under-report cached cost.
    # Therefore zeros and missing keys must produce no entry, only positive
    # rates surface.

    def test_extracts_cache_read_and_write(self) -> None:
        """Non-zero cache rates from the API land as cache_read/cache_write."""
        models = [
            {
                "id": "anthropic/claude-opus-4.6",
                "pricing": {
                    "prompt": "0.000005",
                    "completion": "0.000025",
                    "input_cache_read": "0.0000005",
                    "input_cache_write": "0.000005",
                },
            }
        ]
        result = convert_pricing(models)
        entry = result["anthropic/claude-opus-4.6"]
        assert entry["input"] == pytest.approx(5.0)
        assert entry["output"] == pytest.approx(25.0)
        assert entry["cache_read"] == pytest.approx(0.5)
        assert entry["cache_write"] == pytest.approx(5.0)

    def test_omits_cache_fields_when_api_zero(self) -> None:
        """Cache fields equal to "0" must not be written.

        A literal ``cache_read: 0.0`` would tell the cost engine that cached
        reads are free — wrong for any caching-supporting model. Absence
        means "no explicit rate" and the engine falls back to ``input``.
        """
        models = [
            {
                "id": "openai/gpt-no-cache",
                "pricing": {
                    "prompt": "0.000003",
                    "completion": "0.000006",
                    "input_cache_read": "0",
                    "input_cache_write": "0",
                },
            }
        ]
        result = convert_pricing(models)
        entry = result["openai/gpt-no-cache"]
        assert "cache_read" not in entry
        assert "cache_write" not in entry

    def test_omits_cache_fields_when_api_missing(self) -> None:
        """Cache fields not present on the API object must not be written."""
        models = [
            {
                "id": "bare-model",
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            }
        ]
        result = convert_pricing(models)
        entry = result["bare-model"]
        assert "cache_read" not in entry
        assert "cache_write" not in entry

    def test_extracts_only_one_cache_side(self) -> None:
        """Some models price reads but not writes (or vice versa)."""
        models = [
            {
                "id": "read-only-cache",
                "pricing": {
                    "prompt": "0.000002",
                    "completion": "0.000004",
                    "input_cache_read": "0.0000002",
                    "input_cache_write": "0",
                },
            }
        ]
        result = convert_pricing(models)
        entry = result["read-only-cache"]
        assert entry["cache_read"] == pytest.approx(0.2)
        assert "cache_write" not in entry

    def test_skips_invalid_cache_values(self) -> None:
        """Non-numeric cache strings are dropped without skipping the model."""
        models = [
            {
                "id": "bad-cache-model",
                "pricing": {
                    "prompt": "0.000001",
                    "completion": "0.000002",
                    "input_cache_read": "not-a-number",
                    "input_cache_write": "also-bad",
                },
            }
        ]
        result = convert_pricing(models)
        entry = result["bad-cache-model"]
        # Core rates still extracted; cache fields silently dropped.
        assert entry["input"] == pytest.approx(1.0)
        assert "cache_read" not in entry
        assert "cache_write" not in entry


# ---------------------------------------------------------------------------
# find_significant_pricing_changes
# ---------------------------------------------------------------------------
#
# This is the regression catcher: a unit-conversion bug, a fetcher
# field-mapping error, or an upstream re-pricing event tends to produce
# order-of-magnitude swings rather than gentle drift. Fires symmetrically
# (a 3× drop and a 3× rise both trigger). Models present only on one side
# are deliberately ignored — additions/removals are not "changes".


class TestFindSignificantPricingChanges:
    """Test find_significant_pricing_changes() detection logic."""

    def test_flags_3x_drop(self) -> None:
        """Symmetric ratio: a 3× drop in input fires."""
        existing = {"m": {"input": 30.0, "output": 60.0}}
        pricing = {"m": {"input": 10.0, "output": 60.0}}

        changes = find_significant_pricing_changes(existing, pricing, threshold=3.0)

        assert len(changes) == 1
        model_id, _, _, ratios = changes[0]
        assert model_id == "m"
        assert ratios["input_ratio"] == pytest.approx(3.0)
        assert ratios["output_ratio"] == pytest.approx(1.0)

    def test_flags_3x_rise(self) -> None:
        """Symmetric ratio: a 3× rise in output also fires."""
        existing = {"m": {"input": 1.0, "output": 1.0}}
        pricing = {"m": {"input": 1.0, "output": 5.0}}

        changes = find_significant_pricing_changes(existing, pricing, threshold=3.0)

        assert len(changes) == 1
        _, _, _, ratios = changes[0]
        assert ratios["output_ratio"] == pytest.approx(5.0)

    def test_ignores_below_threshold(self) -> None:
        """A 2× swing does not fire at threshold=3."""
        existing = {"m": {"input": 1.0, "output": 1.0}}
        pricing = {"m": {"input": 2.0, "output": 1.5}}

        assert find_significant_pricing_changes(existing, pricing, threshold=3.0) == []

    def test_threshold_inclusive(self) -> None:
        """Exactly at threshold counts as significant (``>=``)."""
        existing = {"m": {"input": 1.0, "output": 1.0}}
        pricing = {"m": {"input": 3.0, "output": 1.0}}

        assert len(find_significant_pricing_changes(existing, pricing, threshold=3.0)) == 1

    def test_skips_models_only_in_one_side(self) -> None:
        """Additions and removals are not 'changes'."""
        existing = {"only-old": {"input": 1.0, "output": 1.0}}
        pricing = {"only-new": {"input": 100.0, "output": 100.0}}

        assert find_significant_pricing_changes(existing, pricing, threshold=3.0) == []

    def test_skips_zero_or_negative_baseline(self) -> None:
        """A non-positive old or new value short-circuits to ratio 0 (no fire).

        Without this guard, a free→paid transition (old=0) would report
        infinite ratio and dominate the warning list — pricing-fetcher
        deliberately drops zero-priced models, so a 0 baseline here means
        "we never had this", which is already covered by the
        only-on-one-side rule. The non-positive guard is belt-and-braces.
        """
        existing = {"m": {"input": 0.0, "output": 1.0}}
        pricing = {"m": {"input": 1000.0, "output": 1.0}}

        assert find_significant_pricing_changes(existing, pricing, threshold=3.0) == []

    def test_default_threshold_is_three(self) -> None:
        """Default threshold matches the published constant."""
        assert SIGNIFICANT_PRICING_CHANGE_FACTOR == 3.0

        existing = {"m": {"input": 1.0, "output": 1.0}}
        pricing = {"m": {"input": 3.0, "output": 1.0}}

        # Calling without explicit threshold uses the default.
        assert len(find_significant_pricing_changes(existing, pricing)) == 1


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

    # ----- Field-level merge preserves curated cache rates -----------------
    #
    # Older / direct-API rates may be hand-curated for providers whose
    # OpenRouter listing prices differently (markup, missing cache info).
    # A model-level wholesale replace silently destroys those entries on
    # every refresh — Issue #2 in the PR review. Field-level merge keeps
    # any field the API didn't refresh.

    def test_merge_preserves_existing_cache_rates(self, tmp_path: Path) -> None:
        """Existing cache_read/cache_write survive a refresh that lacks them."""
        output = tmp_path / "pricing.json"
        initial = {
            "_meta": {"updated_at": "2024-01-01"},
            "models": {
                "anthropic/claude-opus-4.6": {
                    "input": 5.0,
                    "output": 25.0,
                    "cache_read": 0.5,
                    "cache_write": 5.0,
                }
            },
        }
        output.write_text(json.dumps(initial))

        # API refresh returns only the core rates (e.g. cache fields zero
        # upstream, or older API response shape).
        new_pricing = {"anthropic/claude-opus-4.6": {"input": 5.0, "output": 25.0}}
        write_pricing_json(new_pricing, output, merge=True)

        data = json.loads(output.read_text())
        entry = data["models"]["anthropic/claude-opus-4.6"]
        assert entry["input"] == 5.0
        assert entry["output"] == 25.0
        # Hand-curated rates must NOT be wiped by a refresh that omits them.
        assert entry["cache_read"] == 0.5
        assert entry["cache_write"] == 5.0

    def test_merge_updates_core_rates_and_keeps_cache(self, tmp_path: Path) -> None:
        """Refresh updates input/output but leaves curated cache fields alone."""
        output = tmp_path / "pricing.json"
        initial = {
            "_meta": {},
            "models": {
                "model-x": {
                    "input": 5.0,
                    "output": 25.0,
                    "cache_read": 0.5,
                    "cache_write": 5.0,
                }
            },
        }
        output.write_text(json.dumps(initial))

        new_pricing = {"model-x": {"input": 6.0, "output": 30.0}}
        write_pricing_json(new_pricing, output, merge=True)

        entry = json.loads(output.read_text())["models"]["model-x"]
        assert entry["input"] == 6.0  # refreshed
        assert entry["output"] == 30.0  # refreshed
        assert entry["cache_read"] == 0.5  # preserved
        assert entry["cache_write"] == 5.0  # preserved

    def test_merge_overrides_cache_when_api_provides_it(self, tmp_path: Path) -> None:
        """When the API does provide cache rates, they win over old values."""
        output = tmp_path / "pricing.json"
        initial = {
            "_meta": {},
            "models": {
                "model-y": {
                    "input": 5.0,
                    "output": 25.0,
                    "cache_read": 0.5,  # stale value
                    "cache_write": 5.0,
                }
            },
        }
        output.write_text(json.dumps(initial))

        new_pricing = {
            "model-y": {
                "input": 5.0,
                "output": 25.0,
                "cache_read": 0.4,  # API-provided
                "cache_write": 4.5,
            }
        }
        write_pricing_json(new_pricing, output, merge=True)

        entry = json.loads(output.read_text())["models"]["model-y"]
        assert entry["cache_read"] == 0.4  # refreshed
        assert entry["cache_write"] == 4.5  # refreshed

    def test_no_merge_still_replaces_wholesale(self, tmp_path: Path) -> None:
        """``merge=False`` is a clean-slate operation — preservation does not apply."""
        output = tmp_path / "pricing.json"
        initial = {
            "_meta": {},
            "models": {"model-z": {"input": 5.0, "output": 25.0, "cache_read": 0.5}},
        }
        output.write_text(json.dumps(initial))

        new_pricing = {"model-z": {"input": 6.0, "output": 30.0}}
        write_pricing_json(new_pricing, output, merge=False)

        entry = json.loads(output.read_text())["models"]["model-z"]
        assert entry["input"] == 6.0
        # With merge disabled, prior cache_read is intentionally dropped.
        assert "cache_read" not in entry


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


# ---------------------------------------------------------------------------
# CLI integration — `pricing-updater update`
# ---------------------------------------------------------------------------
#
# The fetcher functions above are pure transformations; the CLI is where
# they're composed. The behaviour change in this PR is the new
# significant-change warning block in ``update``, so it gets a positive
# integration test against the new contract.


class TestUpdateCLI:
    """End-to-end test of ``pricing-updater update`` with a stubbed fetcher."""

    def test_warns_on_significant_change(self, tmp_path: Path) -> None:
        """The ≥3× warning fires when the fresh snapshot diverges from disk.

        Seeds an existing pricing.json with one model at a known rate, then
        stubs the fetcher to return that same model at >=3× input rate.
        The CLI must print the offending model id under a yellow warning
        banner and still write the file (warning, not failure).
        """
        seed = tmp_path / "pricing.json"
        seed.write_text(
            json.dumps(
                {
                    "_meta": {"updated_at": "2024-01-01"},
                    "models": {
                        "drift-model": {"input": 1.0, "output": 1.0},
                        "stable-model": {"input": 2.0, "output": 4.0},
                    },
                }
            )
        )

        fake_models = [
            {
                "id": "drift-model",
                "pricing": {"prompt": "0.000005", "completion": "0.000001"},
            },
            {
                "id": "stable-model",
                "pricing": {"prompt": "0.000002", "completion": "0.000004"},
            },
        ]

        runner = CliRunner()
        with patch("pricing_updater.cli.fetch_openrouter_models", return_value=fake_models):
            result = runner.invoke(cli_app, ["update", "--output", str(seed)])

        assert result.exit_code == 0, result.output
        assert "drift-model" in result.output
        # Warning banner uses the threshold value rendered as 3×.
        assert "3×" in result.output or "3.0×" in result.output or "≥3" in result.output
        # Stable model is not in the warning list.
        warning_section = result.output.split("Wrote")[0]
        assert "stable-model" not in warning_section
        # File still written (warning is non-fatal).
        written = json.loads(seed.read_text())
        assert "drift-model" in written["models"]
        assert written["models"]["drift-model"]["input"] == pytest.approx(5.0)
