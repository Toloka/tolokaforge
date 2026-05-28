"""Contract tests for SecretManager.known_values() and get_default_or_none().

These two additions exist to support the RedactingFilter: the filter needs
to (a) peek at the current secret value-set without triggering lazy-init
and (b) be cheap to call from inside `logging.Filter.filter()`.
"""

from __future__ import annotations

import pytest

from tolokaforge.secrets import SecretManager
from tolokaforge.secrets import manager as manager_module
from tolokaforge.secrets.manager import get_default_or_none, init_default_from

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_singleton():
    original = manager_module._default_manager
    manager_module._default_manager = None
    yield
    manager_module._default_manager = original


# ---------------------------------------------------------------------------
# known_values()
# ---------------------------------------------------------------------------


def test_known_values_returns_long_enough_values() -> None:
    sm = SecretManager.from_dict({"API_KEY": "longenoughvalue123"})
    assert sm.known_values() == frozenset({"longenoughvalue123"})


def test_known_values_skips_short_values() -> None:
    """Values shorter than _REDACT_MIN_LEN must be excluded.

    Why: short tokens like "8080" or "abc" would over-match in prose and
    cause noisy false-positive redactions in unrelated log messages.
    """
    sm = SecretManager.from_dict({"PORT": "8080"})
    assert sm.known_values() == frozenset()


def test_known_values_skips_empty_values() -> None:
    sm = SecretManager.from_dict({"BLANK": ""})
    assert sm.known_values() == frozenset()


def test_known_values_mixed_lengths() -> None:
    sm = SecretManager.from_dict(
        {
            "SHORT": "abc",
            "LONG": "this-is-long-enough",
            "ALSO_LONG": "another-long-value",
            "EMPTY": "",
        }
    )
    assert sm.known_values() == frozenset({"this-is-long-enough", "another-long-value"})


# ---------------------------------------------------------------------------
# get_default_or_none()
# ---------------------------------------------------------------------------


def test_get_default_or_none_returns_none_before_init() -> None:
    assert manager_module._default_manager is None
    assert get_default_or_none() is None
    # Critically: the call must NOT have lazy-initialized the singleton.
    assert manager_module._default_manager is None


def test_get_default_or_none_returns_installed_manager() -> None:
    custom = SecretManager.from_dict({"X": "longenoughvalue"})
    init_default_from(custom)
    assert get_default_or_none() is custom


def test_get_default_or_none_does_not_lazy_init() -> None:
    """Repeated calls on a None singleton must remain None.

    Contrast with `get_default()`, which lazy-inits from SecretConfig.default()
    on first call. We don't want a logging.Filter to trigger that side effect
    on every record before bootstrap.
    """
    for _ in range(5):
        assert get_default_or_none() is None
        assert manager_module._default_manager is None
