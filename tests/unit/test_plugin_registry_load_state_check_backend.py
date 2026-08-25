"""``plugin_registry.load_state_check_backend`` — fail-loud resolution.

Locks the loader for the ``tolokaforge.state_check_backends`` entry-point
group:

* the two shipped factories (``jsonpath`` and ``db_probes``) resolve to the
  callables ``pyproject.toml`` registers them against, so a future refactor
  renaming either symbol trips this test before it lands;
* both factories return instances of the corresponding reference impl —
  the two-step "loader -> factory() -> instance" chain end-to-end;
* an unknown name raises :class:`UnknownImplementationError` listing every
  known name in the group — the same fail-loud shape ``load_grading_substrate``
  and the other loaders use;
* hash grading is explicitly NOT reachable through this group — ``hash`` is
  not a registered backend and lookup fails loud like any other unknown
  name. (Hash grading stays runner-integrated on
  ``RunnerServiceImpl._execute_hash_grading``.)
"""

from __future__ import annotations

import pytest

from tolokaforge.core.grading.default_state_check_backends import (
    DbProbesStateCheckBackend,
    JsonpathStateCheckBackend,
    _db_probes_state_check_backend_factory,
    _jsonpath_state_check_backend_factory,
)
from tolokaforge.core.plugin_registry import (
    STATE_CHECK_BACKENDS_GROUP,
    UnknownImplementationError,
    available_state_check_backends,
    load_state_check_backend,
)

pytestmark = pytest.mark.unit


def test_jsonpath_resolves_to_the_shipped_factory() -> None:
    assert load_state_check_backend("jsonpath") is _jsonpath_state_check_backend_factory


def test_db_probes_resolves_to_the_shipped_factory() -> None:
    assert load_state_check_backend("db_probes") is _db_probes_state_check_backend_factory


def test_jsonpath_factory_returns_a_jsonpath_backend_instance() -> None:
    """Locks the two-step "loader -> factory() -> instance" chain end-to-end."""
    factory = load_state_check_backend("jsonpath")
    assert isinstance(factory(), JsonpathStateCheckBackend)


def test_db_probes_factory_returns_a_db_probes_backend_instance() -> None:
    factory = load_state_check_backend("db_probes")
    assert isinstance(factory(), DbProbesStateCheckBackend)


def test_available_lists_both_shipped_names() -> None:
    names = available_state_check_backends()
    assert "jsonpath" in names
    assert "db_probes" in names


def test_hash_is_not_a_registered_backend() -> None:
    """Hash grading has state-mutation semantics the read-only substrate
    cannot serve; it stays runner-integrated on
    ``RunnerServiceImpl._execute_hash_grading`` and is not reachable through
    this seam. Lookup fails loud like any other unknown name.
    """
    assert "hash" not in available_state_check_backends()
    with pytest.raises(UnknownImplementationError):
        load_state_check_backend("hash")


def test_unknown_name_raises_unknown_implementation_error() -> None:
    with pytest.raises(UnknownImplementationError) as excinfo:
        load_state_check_backend("nonexistent")
    message = str(excinfo.value)
    assert STATE_CHECK_BACKENDS_GROUP in message
    assert "jsonpath" in message
    assert "db_probes" in message
    assert excinfo.value.group == STATE_CHECK_BACKENDS_GROUP
    assert "jsonpath" in excinfo.value.known
    assert "db_probes" in excinfo.value.known
