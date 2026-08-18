"""How an installed bundle reaches the registry, and how ambiguity is refused.

Discovery reads ``importlib.metadata.entry_points`` off the module on every
call and caches the result per group. Both halves are load-bearing: the
attribute read is the seam a fabricated installed set replaces, and the cache is
why ``install_plugins`` drops it — a warm cache answers before the patched
attribute is ever read.
"""

import pytest
from tolokaforge_coding_harnesses._registry import _clear_discovery_cache, _discover_entry_points
from tolokaforge_coding_harnesses.testing import (
    FakeDistribution,
    FakeEntryPoint,
    build_plugin,
    bundle_yaml,
    install_plugins,
)

from tolokaforge_coding_harnesses import (
    HARNESS_REGISTRY_ENTRY_POINT_GROUP,
    DuplicateRegistrationError,
    discover_plugin_harness_registries,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_discovery():
    _clear_discovery_cache()
    yield
    _clear_discovery_cache()


def test_a_fabricated_installed_set_is_the_one_discovery_reads(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proof that ``entry_points`` is read at call time.

    A module-scope ``from importlib.metadata import entry_points`` would bind
    the real function once, and this case would silently discover whatever
    bundles the developer's environment happens to have installed — passing
    while measuring nothing.
    """
    install_plugins(
        monkeypatch,
        build_plugin(
            tmp_path,
            monkeypatch,
            "acme_harnesses",
            "acme-harnesses",
            bundle_yaml("acme-cli", "1.2.3"),
        ),
    )

    discovered = _discover_entry_points(HARNESS_REGISTRY_ENTRY_POINT_GROUP)

    assert list(discovered) == ["acme_harnesses"]


def test_two_entry_points_claiming_one_name_are_refused_naming_both_distributions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No safe pick: the two entry points resolve to different packages under
    one name, so install order must not decide which bundle a run reads."""
    shared_name = "acme_harnesses"
    install_plugins(
        monkeypatch,
        FakeEntryPoint(shared_name, FakeDistribution("acme-harnesses", "1.0.0"), object()),
        FakeEntryPoint(shared_name, FakeDistribution("globex-harnesses", "2.0.0"), object()),
    )

    with pytest.raises(DuplicateRegistrationError) as excinfo:
        _discover_entry_points(HARNESS_REGISTRY_ENTRY_POINT_GROUP)

    message = str(excinfo.value)
    assert "acme-harnesses" in message
    assert "globex-harnesses" in message
    assert HARNESS_REGISTRY_ENTRY_POINT_GROUP in message


def test_a_duplicate_is_refused_on_every_lookup_not_only_the_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The raise lands before the cache is written, so the second lookup cannot
    be served a map missing one of the two colliding entry points."""
    install_plugins(
        monkeypatch,
        FakeEntryPoint("acme_harnesses", FakeDistribution("acme-harnesses", "1.0.0"), object()),
        FakeEntryPoint("acme_harnesses", FakeDistribution("globex-harnesses", "2.0.0"), object()),
    )

    for _ in range(2):
        with pytest.raises(DuplicateRegistrationError):
            _discover_entry_points(HARNESS_REGISTRY_ENTRY_POINT_GROUP)


def test_the_refusal_is_a_value_error() -> None:
    """A caller already refusing malformed registry input with ``except
    ValueError`` keeps catching the ambiguity too."""
    assert issubclass(DuplicateRegistrationError, ValueError)


def test_an_entry_point_without_a_distribution_is_named_in_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A programmatically registered entry point has no distribution to name, so
    the message says so rather than fabricating one."""
    install_plugins(
        monkeypatch,
        FakeEntryPoint("acme_harnesses", None, object()),
        FakeEntryPoint("acme_harnesses", None, object()),
    )

    with pytest.raises(DuplicateRegistrationError, match="<unknown distribution>"):
        _discover_entry_points(HARNESS_REGISTRY_ENTRY_POINT_GROUP)


def test_installing_a_second_set_needs_no_cache_clearing_by_the_caller(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two different installed sets in one process resolve to two different
    registries with no manual cache drop in between.

    Discovery answers from the cache before it reads the patched attribute, so
    ``install_plugins`` clears it — otherwise the second call here would be a
    silent no-op and this case would read ``acme-cli`` twice while asserting
    nothing about the injection.
    """
    install_plugins(
        monkeypatch,
        build_plugin(
            tmp_path,
            monkeypatch,
            "acme_harnesses",
            "acme-harnesses",
            bundle_yaml("acme-cli", "1.0.0"),
        ),
    )
    first = discover_plugin_harness_registries()

    install_plugins(
        monkeypatch,
        build_plugin(
            tmp_path,
            monkeypatch,
            "globex_harnesses",
            "globex-harnesses",
            bundle_yaml("globex-cli", "2.0.0"),
        ),
    )
    second = discover_plugin_harness_registries()

    assert list(first.harnesses) == ["acme-cli"]
    assert list(second.harnesses) == ["globex-cli"]
