"""The ``tolokaforge.search_backends`` registry and its ``typesense`` reservation (ADR-0052).

The metadata layer is replaced by injected entry points, so the loader's rules are
exercised without any installed plug-in:

* a registered name resolves to its factory, and the listing names it;
* an unknown name is refused naming the registered ones;
* ``typesense`` is reserved: looking it up is refused, and while any distribution
  registers it every lookup into the group is refused naming that distribution —
  the same failure a duplicate registration produces, because in both cases no
  lookup can tell which implementation is meant;
* ``plugin_registry`` re-exports the seam's types from the module that declares them.
"""

from __future__ import annotations

import importlib.metadata
import logging
from typing import Any

import pytest

from tolokaforge.core import plugin_registry
from tolokaforge.core.plugin_registry import (
    RESERVED_SEARCH_BACKEND_NAMES,
    SEARCH_BACKENDS_GROUP,
    DuplicateRegistrationError,
    RegistryError,
    ReservedNameError,
    UnknownImplementationError,
    available_search_backends,
    load_search_backend,
)
from tolokaforge.core.search import backend as backend_module
from tolokaforge.core.search.backend import SearchBackendContext
from tolokaforge.runner.models import SearchPlane

pytestmark = pytest.mark.unit


class _FakeDist:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeEntryPoint:
    def __init__(self, name: str, factory: Any, *, dist: str = "third-party-search") -> None:
        self.name = name
        self.dist = _FakeDist(dist)
        self._factory = factory

    def load(self) -> Any:
        return self._factory


def _factory_a(context: Any) -> Any:  # pragma: no cover - identity only
    raise AssertionError("never built in these tests")


def _factory_b(context: Any) -> Any:  # pragma: no cover - identity only
    raise AssertionError("never built in these tests")


@pytest.fixture
def registered(monkeypatch: pytest.MonkeyPatch):
    """Replace the group's entry points with the ones a case names."""

    def install(*entry_points: _FakeEntryPoint) -> None:
        real = importlib.metadata.entry_points

        def fake(*, group: str) -> Any:
            if group == SEARCH_BACKENDS_GROUP:
                return list(entry_points)
            return real(group=group)

        monkeypatch.setattr(importlib.metadata, "entry_points", fake)
        plugin_registry._clear_discovery_cache()

    yield install
    plugin_registry._clear_discovery_cache()


def test_a_registered_backend_resolves_to_its_factory(registered) -> None:
    registered(_FakeEntryPoint("alpha", _factory_a), _FakeEntryPoint("beta", _factory_b))

    assert available_search_backends() == ["alpha", "beta"]
    assert load_search_backend("alpha") is _factory_a
    assert load_search_backend("beta") is _factory_b


def test_an_unknown_backend_is_refused_naming_the_registered_ones(registered) -> None:
    registered(_FakeEntryPoint("alpha", _factory_a))

    with pytest.raises(UnknownImplementationError) as excinfo:
        load_search_backend("ghost")

    assert excinfo.value.group == SEARCH_BACKENDS_GROUP
    assert excinfo.value.known == ["alpha"]
    assert "'ghost'" in str(excinfo.value)


def test_a_duplicate_registration_fails_every_lookup(registered) -> None:
    registered(
        _FakeEntryPoint("alpha", _factory_a, dist="first"),
        _FakeEntryPoint("alpha", _factory_b, dist="second"),
    )

    with pytest.raises(DuplicateRegistrationError):
        available_search_backends()


def test_typesense_is_the_reserved_plane_name() -> None:
    """The reservation is the plane the runner's TypeSense branch serves."""
    assert frozenset({SearchPlane.TYPESENSE.value}) == RESERVED_SEARCH_BACKEND_NAMES


def test_looking_up_typesense_is_refused_even_with_nothing_registered(registered) -> None:
    registered(_FakeEntryPoint("alpha", _factory_a))

    with pytest.raises(ReservedNameError) as excinfo:
        load_search_backend(SearchPlane.TYPESENSE.value)

    assert excinfo.value.distribution is None
    assert "search.plane: typesense" in str(excinfo.value)
    assert isinstance(excinfo.value, RegistryError)


def test_a_registration_claiming_typesense_fails_every_lookup_naming_its_distribution(
    registered,
) -> None:
    """A third-party ``typesense`` is refused, not preferred over the runner's own plane."""
    registered(
        _FakeEntryPoint("alpha", _factory_a),
        _FakeEntryPoint("typesense", _factory_b, dist="squatter-pkg"),
    )

    for lookup in (available_search_backends, lambda: load_search_backend("alpha")):
        with pytest.raises(ReservedNameError) as excinfo:
            lookup()
        assert excinfo.value.distribution == "squatter-pkg"
        assert "Uninstall or rename" in str(excinfo.value)


def test_plugin_registry_re_exports_the_seam_types() -> None:
    for name in (
        "SearchBackend",
        "SearchBackendContext",
        "SearchBackendFactory",
        "SearchIndex",
        "SearchIndexBuildError",
        "SearchOutcome",
    ):
        assert getattr(plugin_registry, name) is getattr(backend_module, name), name
        assert name in plugin_registry.__all__


def test_a_backend_reads_a_read_only_copy_of_its_config() -> None:
    """What the task declared is graded and bundled; a backend cannot change it."""
    declared = {"ranking": {"top_k": 3}}
    client = object()
    context = SearchBackendContext(
        backend_config=declared,
        tool_name="search_kb",
        tool_description=None,
        logger=logging.getLogger("test"),
        stack_service_clients={"rag_service": client},
    )

    with pytest.raises(TypeError):
        context.backend_config["ranking"] = {}  # type: ignore[index]
    context.backend_config["ranking"]["top_k"] = 99
    assert declared == {"ranking": {"top_k": 3}}
    assert context.stack_service_clients["rag_service"] is client, "clients are never copied"
