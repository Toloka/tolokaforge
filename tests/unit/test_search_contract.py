"""Contract test for ``tolokaforge.core.search`` public surface.

The symbols re-exported by ``tolokaforge.core.search.__init__`` are the
adapter-facing API. Renaming or removing one is a breaking change for any
adapter that builds a TypeSense-backed provider on top. This test pins
``__all__`` to its expected shape so an accidental cleanup fails CI here
rather than silently downstream.

If you add or remove a name, update ``EXPECTED`` and ``docs/TYPESENSE_INTEGRATION.md``
in the same change.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from tolokaforge.core import search as search_pkg

pytestmark = pytest.mark.unit


EXPECTED = {
    "DomainState",
    "DomainStateManager",
    "DomainStatus",
    "SearchResponse",
    "SearchResult",
}


def test_all_matches_expected_contract():
    assert set(search_pkg.__all__) == EXPECTED


def test_every_exported_name_resolves():
    for name in search_pkg.__all__:
        assert hasattr(search_pkg, name), f"{name} listed in __all__ but missing from module"


def test_data_classes_are_dataclasses():
    assert dataclasses.is_dataclass(search_pkg.SearchResponse)
    assert dataclasses.is_dataclass(search_pkg.SearchResult)


def test_domain_status_is_enum_like():
    assert inspect.isclass(search_pkg.DomainStatus)
    expected_members = {"PENDING", "INITIALIZING", "READY", "FAILED"}
    actual = {m for m in dir(search_pkg.DomainStatus) if not m.startswith("_")}
    assert expected_members.issubset(
        actual
    ), f"DomainStatus is missing expected members; have {actual}"


def test_no_typesense_client_abstract_resurfaces():
    for stale in ("TypeSenseClient", "TypeSenseStub", "create_typesense_client"):
        assert stale not in search_pkg.__all__, (
            f"{stale} was intentionally removed; do not re-export it. "
            "If you need a real client, use the typesense package directly."
        )
