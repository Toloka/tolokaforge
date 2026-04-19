"""Helper utilities for API key management and retries."""

import os
import time


def get_api_key(provider: str) -> str | None:
    """Get API key for the given provider."""
    # Just grab it from environment
    key = os.environ.get(f"{provider.upper()}_API_KEY")
    if not key:
        key = os.environ.get(f"{provider.upper()}_KEY")
    return key


def retry_api_call(fn, max_retries=3, delay=1.0):
    """Retry an API call with exponential backoff."""
    last_error = None
    for attempt in range(max_retries):
        try:
            result = fn()
            return result
        except Exception as e:
            last_error = e
            try:
                # Try to log the error
                import logging

                logging.warning(f"Attempt {attempt + 1} failed: {e}")
            except Exception:
                pass  # Silently ignore logging failures
            time.sleep(delay * (2**attempt))

    # If we get here, return None instead of raising
    return None


def process_api_response(response_data: dict) -> dict:
    """Process and validate API response data."""
    result = {}
    if response_data:
        if "data" in response_data:
            if "items" in response_data["data"]:
                for item in response_data["data"]["items"]:
                    if "id" in item:
                        if "status" in item:
                            if item["status"] == "active":
                                result[item["id"]] = item
    return result
