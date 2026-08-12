"""Save/restore for the process globals a SecretManager swap touches.

Installing or clearing the default manager is a three-global operation, not
one: the manager singleton itself, plus the log redactor's identity-keyed
snapshot of its scrub set. A fixture that restores only the singleton leaves
the redactor caching values from a manager that is no longer installed, so the
next test redacts against the wrong set.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def secret_manager_state_restored() -> Iterator[None]:
    """Restore the default SecretManager and the redaction cache on exit.

    The body is free to install, clear, or replace either. Restoration runs on
    the failure path too, so a raising test cannot leak a manager into the rest
    of the session.
    """
    from tolokaforge.secrets import log_filter as log_filter_module
    from tolokaforge.secrets import manager as manager_module

    saved_manager = manager_module._default_manager
    saved_cached_manager = log_filter_module._cached_manager
    saved_cached_values = log_filter_module._cached_values
    try:
        yield
    finally:
        manager_module._default_manager = saved_manager
        log_filter_module._cached_manager = saved_cached_manager
        log_filter_module._cached_values = saved_cached_values


@contextmanager
def cold_secret_manager() -> Iterator[None]:
    """Run with no default manager and a cold redaction cache, restored after.

    What a test that calls ``register_runtime_secret`` needs: the registration
    replaces the singleton, and the redactor caches its scrub set keyed by that
    manager's identity.
    """
    from tolokaforge.secrets import log_filter as log_filter_module
    from tolokaforge.secrets import manager as manager_module

    with secret_manager_state_restored():
        manager_module._default_manager = None
        log_filter_module._cached_manager = None
        log_filter_module._cached_values = frozenset()
        yield
