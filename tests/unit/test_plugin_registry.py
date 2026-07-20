"""Fail-loud entry-point registry loader.

Pins the two shapes of the fail-loud policy the registries enforce over
:mod:`importlib.metadata` entry points, with the metadata layer replaced by
injected fake entry points so the loader logic is exercised without any
installed package:

* an unknown name raises :class:`UnknownImplementationError` listing every
  known name;
* a duplicate name within a group raises :class:`DuplicateRegistrationError`
  naming both providing distributions, for any lookup into that group;
* a target that raises on import propagates that exception out of ``load_*``;
* a broken sibling never breaks resolution of a healthy name — discovery does
  not eager-load, so one broken plug-in cannot take down ``--runtime shared``.
"""

from __future__ import annotations

import importlib.metadata

import pytest

from tolokaforge.core import plugin_registry
from tolokaforge.core.plugin_registry import (
    CONDUCTORS_GROUP,
    RUNTIME_BACKENDS_GROUP,
    TRIAL_GRADERS_GROUP,
    DuplicateRegistrationError,
    UnknownImplementationError,
    available_conductors,
    available_runtime_backends,
    available_trial_graders,
    load_conductor,
    load_runtime_backend,
    load_trial_grader,
)

pytestmark = pytest.mark.unit


class _FakeDist:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeEntryPoint:
    """Duck-typed stand-in for :class:`importlib.metadata.EntryPoint`.

    Manually-constructed real ``EntryPoint`` tuples carry ``dist=None``; the
    fail-loud duplicate message needs a named distribution, so the loader reads
    ``ep.name`` / ``ep.dist`` and calls ``ep.load()`` — all satisfied here.
    """

    def __init__(
        self,
        name: str,
        *,
        factory: object = None,
        load_error: Exception | None = None,
        dist: str = "pkg",
    ) -> None:
        self.name = name
        self.dist = _FakeDist(dist)
        self._factory = factory
        self._load_error = load_error

    def load(self) -> object:
        if self._load_error is not None:
            raise self._load_error
        return self._factory


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the per-group discovery cache around every case.

    The registry caches discovery on first scan; without a reset one case's
    injected entry points would leak into the next.
    """
    plugin_registry._clear_discovery_cache()
    yield
    plugin_registry._clear_discovery_cache()


def _install_entry_points(
    monkeypatch: pytest.MonkeyPatch, by_group: dict[str, list[_FakeEntryPoint]]
) -> None:
    def fake_entry_points(*, group: str) -> list[_FakeEntryPoint]:
        return list(by_group.get(group, []))

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)


# --- (a) unknown name -------------------------------------------------------


@pytest.mark.parametrize(
    ("loader", "group"),
    [
        (load_runtime_backend, RUNTIME_BACKENDS_GROUP),
        (load_trial_grader, TRIAL_GRADERS_GROUP),
        (load_conductor, CONDUCTORS_GROUP),
    ],
)
def test_unknown_name_lists_every_known_name(
    monkeypatch: pytest.MonkeyPatch, loader, group: str
) -> None:
    _install_entry_points(
        monkeypatch,
        {
            group: [
                _FakeEntryPoint("alpha", factory=object()),
                _FakeEntryPoint("beta", factory=object()),
            ]
        },
    )

    with pytest.raises(UnknownImplementationError) as excinfo:
        loader("missing")

    message = str(excinfo.value)
    assert group in message
    assert "alpha" in message
    assert "beta" in message
    assert excinfo.value.known == ["alpha", "beta"]


# --- (b) duplicate name -----------------------------------------------------


@pytest.mark.parametrize(
    ("loader", "available", "group"),
    [
        (load_runtime_backend, available_runtime_backends, RUNTIME_BACKENDS_GROUP),
        (load_trial_grader, available_trial_graders, TRIAL_GRADERS_GROUP),
        (load_conductor, available_conductors, CONDUCTORS_GROUP),
    ],
)
def test_duplicate_name_fails_naming_both_distributions(
    monkeypatch: pytest.MonkeyPatch, loader, available, group: str
) -> None:
    _install_entry_points(
        monkeypatch,
        {
            group: [
                _FakeEntryPoint("shared", factory=object(), dist="first-pkg"),
                _FakeEntryPoint("shared", factory=object(), dist="second-pkg"),
            ]
        },
    )

    with pytest.raises(DuplicateRegistrationError) as excinfo:
        loader("shared")
    assert "first-pkg" in str(excinfo.value)
    assert "second-pkg" in str(excinfo.value)
    assert excinfo.value.distributions == ("first-pkg", "second-pkg")

    # The ambiguity is group-level: any lookup into the group re-raises,
    # including the name listing.
    with pytest.raises(DuplicateRegistrationError):
        available()


# --- (c) broken import propagates -------------------------------------------


def test_broken_import_propagates_out_of_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    boom = RuntimeError("import blew up")
    _install_entry_points(
        monkeypatch,
        {RUNTIME_BACKENDS_GROUP: [_FakeEntryPoint("broken", load_error=boom)]},
    )

    with pytest.raises(RuntimeError) as excinfo:
        load_runtime_backend("broken")
    assert excinfo.value is boom


# --- (d) broken sibling does not break a healthy name -----------------------


def test_broken_sibling_does_not_break_healthy_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good_factory = object()
    _install_entry_points(
        monkeypatch,
        {
            RUNTIME_BACKENDS_GROUP: [
                _FakeEntryPoint("good", factory=good_factory),
                _FakeEntryPoint("bad", load_error=ImportError("missing dependency")),
            ]
        },
    )

    assert load_runtime_backend("good") is good_factory
    with pytest.raises(ImportError):
        load_runtime_backend("bad")


def test_available_lists_sorted_names_without_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_entry_points(
        monkeypatch,
        {
            RUNTIME_BACKENDS_GROUP: [
                _FakeEntryPoint("zebra", load_error=ImportError("must not load")),
                _FakeEntryPoint("alpha", load_error=ImportError("must not load")),
            ]
        },
    )

    assert available_runtime_backends() == ["alpha", "zebra"]
