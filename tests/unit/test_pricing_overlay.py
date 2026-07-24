"""Unit tests for the ``overlay_path`` branch of
:func:`tolokaforge.core.pricing.reload_pricing`.
"""

from __future__ import annotations

import json

import pytest
import yaml

from tolokaforge.core.pricing import (
    MODEL_PRICING,
    estimate_cost,
    get_pricing_info,
    reload_pricing,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def restore_pricing_after_each_test():
    """Every test may mutate ``MODEL_PRICING``; restore the shipped table."""
    yield
    reload_pricing()


def test_no_overlay_preserves_shipped_defaults() -> None:
    reload_pricing()
    baseline = get_pricing_info("openai/gpt-4o")
    assert baseline is not None
    baseline_copy = dict(baseline)

    reload_pricing(overlay_path=None)

    reloaded = get_pricing_info("openai/gpt-4o")
    assert reloaded == baseline_copy


def test_yaml_overlay_overrides_shipped_rate(tmp_path) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        yaml.safe_dump({"models": {"openai/gpt-4o": {"input": 0.05, "output": 0.15}}})
    )

    reload_pricing(overlay_path=overlay)

    info = get_pricing_info("openai/gpt-4o")
    assert info is not None
    assert info["input"] == pytest.approx(0.05)
    assert info["output"] == pytest.approx(0.15)


def test_json_overlay_overrides_shipped_rate(tmp_path) -> None:
    overlay = tmp_path / "overlay.json"
    overlay.write_text(json.dumps({"models": {"openai/gpt-4o": {"input": 0.10, "output": 0.20}}}))

    reload_pricing(overlay_path=overlay)

    info = get_pricing_info("openai/gpt-4o")
    assert info is not None
    assert info["input"] == pytest.approx(0.10)
    assert info["output"] == pytest.approx(0.20)


def test_overlay_adds_new_model_id(tmp_path) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        yaml.safe_dump({"models": {"synthetic/test-model": {"input": 1.0, "output": 2.0}}})
    )

    reload_pricing(overlay_path=overlay)

    assert "synthetic/test-model" in MODEL_PRICING
    cost = estimate_cost(
        "synthetic/test-model",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert cost is not None
    assert cost == pytest.approx(3.0, abs=0.01)


def test_overlay_field_level_merge_preserves_shipped_field(tmp_path) -> None:
    """An overlay that omits a field leaves the baseline value intact."""
    reload_pricing()
    baseline = get_pricing_info("openai/gpt-4o")
    assert baseline is not None
    baseline_output = baseline["output"]

    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(yaml.safe_dump({"models": {"openai/gpt-4o": {"input": 0.05}}}))

    reload_pricing(overlay_path=overlay)

    info = get_pricing_info("openai/gpt-4o")
    assert info is not None
    assert info["input"] == pytest.approx(0.05)
    assert info["output"] == pytest.approx(baseline_output)


def test_overlay_leaves_untouched_models_unchanged(tmp_path) -> None:
    reload_pricing()
    other_model = "anthropic/claude-sonnet-4.6"
    baseline_other = get_pricing_info(other_model)
    assert baseline_other is not None
    baseline_copy = dict(baseline_other)

    overlay = tmp_path / "overlay.json"
    overlay.write_text(json.dumps({"models": {"openai/gpt-4o": {"input": 0.99, "output": 0.99}}}))

    reload_pricing(overlay_path=overlay)

    assert get_pricing_info(other_model) == baseline_copy


def test_missing_overlay_file_raises(tmp_path) -> None:
    missing = tmp_path / "does-not-exist.json"
    with pytest.raises(FileNotFoundError, match=str(missing)):
        reload_pricing(overlay_path=missing)


def test_malformed_yaml_overlay_raises(tmp_path) -> None:
    overlay = tmp_path / "bad.yaml"
    overlay.write_text("models:\n  openai/gpt-4o: {input: '\n")

    with pytest.raises(ValueError, match="invalid YAML"):
        reload_pricing(overlay_path=overlay)


def test_malformed_json_overlay_raises(tmp_path) -> None:
    overlay = tmp_path / "bad.json"
    overlay.write_text('{"models": {"openai/gpt-4o": ')

    with pytest.raises(ValueError, match="invalid JSON"):
        reload_pricing(overlay_path=overlay)


def test_unknown_suffix_raises(tmp_path) -> None:
    overlay = tmp_path / "overlay.txt"
    overlay.write_text('{"models": {}}')

    with pytest.raises(ValueError, match=r"\.txt"):
        reload_pricing(overlay_path=overlay)


def test_overlay_rejects_non_mapping_entry(tmp_path) -> None:
    overlay = tmp_path / "overlay.json"
    overlay.write_text(json.dumps({"models": {"openai/gpt-4o": "not-a-dict"}}))

    with pytest.raises(ValueError, match="must be a mapping"):
        reload_pricing(overlay_path=overlay)
