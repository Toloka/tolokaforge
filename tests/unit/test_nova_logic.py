"""Unit tests for Nova pricing calculation and model name normalization.

Pure-logic tests that don't require API keys or network access.
"""

import pytest

from tolokaforge.core.pricing import estimate_cost, normalize_model_name

pytestmark = pytest.mark.unit


def test_nova_pricing_calculation():
    """Test Nova pricing calculation works"""
    # Test with known Nova model
    model_name = "nova/Nova Pro v3"

    cost = estimate_cost(model=model_name, input_tokens=100, output_tokens=50)

    # Should calculate non-zero cost
    assert cost > 0
    # Should be reasonable (not too expensive for test tokens)
    assert cost < 1.0


def test_nova_model_name_normalization():
    """Test Nova model name normalization"""
    # Test various Nova model name formats
    test_cases = [
        ("Nova Pro v3", "nova/Nova Pro v3"),
        ("nova-orchestrator-v1", "nova/nova-orchestrator-v1"),
        ("nova/Nova Pro v3", "nova/Nova Pro v3"),  # Already normalized
    ]

    for input_name, expected in test_cases:
        normalized = normalize_model_name(input_name)
        assert normalized == expected, f"Expected {expected}, got {normalized}"
