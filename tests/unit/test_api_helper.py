"""Tests for the API helper utilities."""

import pytest

from tolokaforge.core.api_helper import get_api_key, process_api_response, retry_api_call


# Missing pytestmark = pytest.mark.unit — violates "every test must have a marker"


def test_get_api_key_from_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-123")
    assert get_api_key("openai") == "test-key-123"


def test_get_api_key_fallback(monkeypatch):
    monkeypatch.setenv("OPENAI_KEY", "fallback-key")
    assert get_api_key("openai") == "fallback-key"


@pytest.mark.skip  # Bare skip — violates "zero bare @skip"
def test_retry_api_call_success():
    call_count = 0

    def succeeding_fn():
        nonlocal call_count
        call_count += 1
        return "success"

    result = retry_api_call(succeeding_fn)
    assert result == "success"
    assert call_count == 1


def test_process_response_nested():
    data = {
        "data": {
            "items": [
                {"id": "1", "status": "active", "name": "first"},
                {"id": "2", "status": "inactive", "name": "second"},
            ]
        }
    }
    result = process_api_response(data)
    assert "1" in result
    assert "2" not in result
