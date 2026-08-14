"""What a run's harness fingerprint says about the registry it ran on.

The claim under test: same shipped registry + same overlay + same plugin set
⇒ identical ``resolved_sha256``; drift in any layer ⇒ a different one, with
``overlay_file`` and ``plugin_bundles`` naming where the drift came from.

No digest is pinned here. A literal sha would force a regen on every
``harnesses.yaml`` edit while locking nothing the relational assertions do
not already lock.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from tolokaforge_adapter_terminal_bench.harness import (
    ENGINE_LOOP,
    HARNESSES,
    HarnessSpec,
    resolve_effective_registry,
)
from tolokaforge_adapter_terminal_bench.harness.fingerprint import (
    _digest,
    compute_harness_fingerprint,
)

from tests.utils.harness_plugins import build_plugin, bundle_yaml, install_plugins

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("env_backed_secrets")]


@pytest.fixture(autouse=True)
def _isolated_discovery():
    """Drop the per-group entry-point cache around every case, so an injected
    plugin set cannot leak into a case asserting there is none."""
    from tolokaforge.core import plugin_registry

    plugin_registry._clear_discovery_cache()
    yield
    plugin_registry._clear_discovery_cache()


def _overlay(tmp_path: Path, name: str, version: str) -> Path:
    overlay = tmp_path / "harness_presets.yaml"
    overlay.write_text(bundle_yaml(name, version))
    return overlay


def test_the_digest_recipe_is_the_models_fingerprint_recipe() -> None:
    """Both fingerprints on a run bundle canonicalise the same way.

    The probe carries a non-ASCII character and a nested mapping, so flipping
    ``ensure_ascii`` or ``separators`` in either module moves this digest
    instead of silently changing what one of the two shas means.
    """
    probe = {
        "probe-cli": HarnessSpec(
            install_source="@acme/probe-cli",
            version="1.0.0",
            argv_prefix=("probe-cli",),
            argv_suffix=("--go",),
            config_files={"/etc/probe.toml": "note = 'café'"},
        )
    }
    payload = {name: spec.model_dump(mode="json") for name, spec in probe.items()}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))

    assert _digest(probe) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_the_same_inputs_give_the_same_digests() -> None:
    """Two computes agree, and so does a second resolution of the same layers —
    otherwise the digest would measure the resolution rather than its inputs."""
    first = compute_harness_fingerprint(resolve_effective_registry(), "claude-code")
    second = compute_harness_fingerprint(resolve_effective_registry(), "claude-code")

    assert first == second


def test_an_unlayered_run_resolves_to_what_it_ships() -> None:
    """No plugin and no overlay: the two digests are the same value, which is
    the statement "no layer changed anything"."""
    fingerprint = compute_harness_fingerprint(resolve_effective_registry(), "claude-code")

    assert fingerprint.resolved_sha256 == fingerprint.shipped_sha256
    assert fingerprint.plugin_bundles == ()
    assert fingerprint.overlay_file is None
    assert fingerprint.agent_harness == "claude-code"


def test_an_overlay_moves_the_resolved_digest_only(tmp_path: Path) -> None:
    """The shipped digest is a fixed reference point: an overlay moves what
    ran, never what the adapter ships."""
    baseline = compute_harness_fingerprint(resolve_effective_registry(), "claude-code")
    overlay = _overlay(tmp_path, "acme-cli", "4.5.6")

    overlaid = compute_harness_fingerprint(resolve_effective_registry(str(overlay)), "claude-code")

    assert overlaid.resolved_sha256 != baseline.resolved_sha256
    assert overlaid.shipped_sha256 == baseline.shipped_sha256
    assert overlaid.overlay_file == str(overlay.resolve())
    assert Path(overlaid.overlay_file).is_absolute()


def test_the_recorded_version_is_the_one_that_installs(tmp_path: Path) -> None:
    """An overlay that bumps a pin: the run records the overlaid version, not
    the shipped one, because the overlaid spec is what the image installs."""
    overlay = _overlay(tmp_path, "claude-code", "9.9.9-overlay")

    fingerprint = compute_harness_fingerprint(
        resolve_effective_registry(str(overlay)), "claude-code"
    )

    assert fingerprint.agent_harness_version == "9.9.9-overlay"
    assert HARNESSES["claude-code"].version != "9.9.9-overlay"


def test_an_installed_plugin_is_named_and_moves_the_resolved_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bundle that contributes a harness is recorded by distribution, version
    and harness names — enough to reinstall the same registry elsewhere."""
    install_plugins(
        monkeypatch,
        build_plugin(
            tmp_path,
            monkeypatch,
            "acme_harnesses",
            "acme-tbench-harnesses",
            bundle_yaml("acme-cli", "4.5.6"),
            version="2.3.4",
        ),
    )

    fingerprint = compute_harness_fingerprint(resolve_effective_registry(), "acme-cli")

    (bundle,) = fingerprint.plugin_bundles
    assert bundle.distribution == "acme-tbench-harnesses"
    assert bundle.version == "2.3.4"
    assert bundle.harnesses == ("acme-cli",)
    assert fingerprint.agent_harness_version == "4.5.6"
    # An unlayered run has the two equal, so a difference is the plugin's.
    assert fingerprint.resolved_sha256 != fingerprint.shipped_sha256


def test_the_engine_loop_records_no_version() -> None:
    """The engine loop installs no CLI, so there is no version to name — and
    the digests still describe the registry the run resolved."""
    fingerprint = compute_harness_fingerprint(resolve_effective_registry(), ENGINE_LOOP)

    assert fingerprint.agent_harness == ENGINE_LOOP
    assert fingerprint.agent_harness_version is None
    assert len(fingerprint.resolved_sha256) == 64
    assert fingerprint.resolved_sha256 == fingerprint.shipped_sha256
