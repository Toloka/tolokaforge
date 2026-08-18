"""Three-layer registry precedence: shipped data, plug-in bundles, operator overlay.

The layers compose whole entries, not fields, and they compose in one order —
the overlay wins over a bundle, a bundle wins over shipped data, and two bundles
claiming one name are refused rather than ordered. Locked here, beside the code
that resolves them, because any runtime consuming this package inherits the same
precedence.
"""

from pathlib import Path

import pytest
from tolokaforge_coding_harnesses._registry import _clear_discovery_cache
from tolokaforge_coding_harnesses.testing import (
    FakeEntryPoint,
    build_plugin,
    bundle_yaml,
    install_plugins,
)

from tolokaforge_coding_harnesses import (
    HARNESSES,
    PluginBundle,
    discover_plugin_harness_registries,
    resolve_effective_registry,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_discovery():
    """Drop the per-group entry-point cache around every case.

    Discovery caches its scan, so an injected plugin set would otherwise leak
    into the next case — including into the no-plugin-installed case, which
    asserts the opposite.
    """
    _clear_discovery_cache()
    yield
    _clear_discovery_cache()


@pytest.fixture
def plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build an importable plugin package shipping a registry YAML."""

    def _build(
        package: str,
        distribution: str | None,
        harnesses: str,
        version: str = "1.0.0",
    ) -> FakeEntryPoint:
        return build_plugin(tmp_path, monkeypatch, package, distribution, harnesses, version)

    return _build


def test_nothing_installed_contributes_nothing() -> None:
    """The common case: no plugin, no change to what the package ships."""
    discovered = discover_plugin_harness_registries()
    assert discovered.harnesses == {}
    assert discovered.bundles == ()


def test_baseline_resolution_names_no_plugin_and_no_overlay() -> None:
    """A run with nothing installed and no overlay says so, rather than leaving
    a reader to infer it from a registry that happens to match."""
    resolved = resolve_effective_registry()
    assert resolved.harnesses == HARNESSES
    assert resolved.plugin_bundles == ()
    assert resolved.overlay_file is None


def test_installed_bundle_is_loaded_from_its_packaged_yaml(
    monkeypatch: pytest.MonkeyPatch, plugin
) -> None:
    install_plugins(
        monkeypatch,
        plugin(
            "acme_harnesses",
            "acme-tbench-harnesses",
            bundle_yaml("acme-cli", "4.5.6"),
            version="2.3.4",
        ),
    )
    discovered = discover_plugin_harness_registries()
    assert list(discovered.harnesses) == ["acme-cli"]
    assert discovered.harnesses["acme-cli"].version == "4.5.6"
    assert discovered.harnesses["acme-cli"].argv_prefix == ("acme-cli",)
    assert discovered.bundles == (
        PluginBundle(
            distribution="acme-tbench-harnesses",
            version="2.3.4",
            harnesses=("acme-cli",),
        ),
    )


def test_bundles_are_ordered_by_distribution(monkeypatch: pytest.MonkeyPatch, plugin) -> None:
    """Distribution order, not entry-point order: the two disagree here, and the
    recorded order must not depend on how a plugin names its package."""
    install_plugins(
        monkeypatch,
        plugin("acme_harnesses", "zeta-harnesses", bundle_yaml("acme-cli", "4.5.6")),
        plugin("globex_harnesses", "alpha-harnesses", bundle_yaml("globex-cli", "7.8.9")),
    )
    discovered = discover_plugin_harness_registries()
    assert [bundle.distribution for bundle in discovered.bundles] == [
        "alpha-harnesses",
        "zeta-harnesses",
    ]
    assert [bundle.harnesses for bundle in discovered.bundles] == [
        ("globex-cli",),
        ("acme-cli",),
    ]


def test_entry_point_without_a_distribution_reports_no_version(
    monkeypatch: pytest.MonkeyPatch, plugin
) -> None:
    """A programmatically registered entry point has no distribution to read a
    version off, and a fabricated one would be a lie."""
    install_plugins(monkeypatch, plugin("acme_harnesses", None, bundle_yaml("acme-cli", "4.5.6")))
    (bundle,) = discover_plugin_harness_registries().bundles
    assert bundle.distribution == "acme_harnesses"
    assert bundle.version is None


def test_two_plugins_claiming_one_harness_name_are_refused(
    monkeypatch: pytest.MonkeyPatch, plugin
) -> None:
    """No safe pick: the two bundles disagree about what the name installs and
    how it is invoked, so install order must not decide it."""
    install_plugins(
        monkeypatch,
        plugin("acme_harnesses", "acme-tbench-harnesses", bundle_yaml("shared-cli", "1.0.0")),
        plugin("globex_harnesses", "globex-harnesses", bundle_yaml("shared-cli", "2.0.0")),
    )
    with pytest.raises(ValueError) as excinfo:
        discover_plugin_harness_registries()
    message = str(excinfo.value)
    assert "shared-cli" in message
    assert "acme-tbench-harnesses" in message
    assert "globex-harnesses" in message


def test_plugin_replaces_a_shipped_entry_whole(monkeypatch: pytest.MonkeyPatch, plugin) -> None:
    """Whole-entry replacement, as at every other layer: the shipped ``codex``
    fields the bundle omits are gone, not merged underneath."""
    install_plugins(
        monkeypatch,
        plugin("acme_harnesses", "acme-tbench-harnesses", bundle_yaml("codex", "9.9.9")),
    )
    effective = resolve_effective_registry().harnesses
    assert effective["codex"].version == "9.9.9"
    assert effective["codex"].config_files == {}
    assert HARNESSES["codex"].config_files != {}
    assert effective["claude-code"] == HARNESSES["claude-code"]


def test_operator_overlay_wins_over_a_plugin(
    monkeypatch: pytest.MonkeyPatch, plugin, tmp_path: Path
) -> None:
    install_plugins(
        monkeypatch,
        plugin("acme_harnesses", "acme-tbench-harnesses", bundle_yaml("acme-cli", "4.5.6")),
    )
    overlay = tmp_path / "harness_presets.yaml"
    overlay.write_text(bundle_yaml("acme-cli", "0.0.0-overlay"))
    resolved = resolve_effective_registry(str(overlay))
    assert resolved.harnesses["acme-cli"].version == "0.0.0-overlay"
    assert resolved.overlay_file == overlay.resolve()


def test_relative_overlay_is_recorded_as_its_resolved_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The recorded overlay must name one file whatever directory the run started
    in, so a relative argument is recorded resolved."""
    overlay = tmp_path / "harness_presets.yaml"
    overlay.write_text(bundle_yaml("acme-cli", "4.5.6"))
    monkeypatch.chdir(tmp_path)
    recorded = resolve_effective_registry("harness_presets.yaml").overlay_file
    assert recorded is not None
    assert recorded.is_absolute()
    assert recorded == overlay.resolve()
