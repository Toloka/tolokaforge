"""Pin the exact injected topology of the ``no_internet`` and
``limited_internet`` network-policy transforms against the
``multi_service_example_01`` compose file.

The snapshots fix the wire shape of :func:`enforce_network_policy` so a
future refactor cannot silently change which networks are injected, which
services join them, the ``internal: true`` markings that block egress, or —
under ``limited_internet`` — the injected proxy sidecar and the per-service
``HTTP(S)_PROXY`` wiring.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tolokaforge.core.compose_materialisation import enforce_network_policy
from tolokaforge.core.trial import NetworkPolicy

pytestmark = pytest.mark.canonical


EXAMPLE_COMPOSE = (
    Path(__file__).resolve().parents[2]
    / "examples"
    / "native"
    / "multi_service"
    / "dataset"
    / "tasks"
    / "multi_service"
    / "multi_service_example_01"
    / "environment.compose.yaml"
)


def test_no_internet_topology_snapshot(canon_snapshot) -> None:
    assert EXAMPLE_COMPOSE.is_file(), f"missing example compose: {EXAMPLE_COMPOSE}"
    doc = yaml.safe_load(EXAMPLE_COMPOSE.read_text())

    transformed = enforce_network_policy(doc, NetworkPolicy.NO_INTERNET, "runner", [])

    snapshot = canon_snapshot("network_policy_enforcement")
    snapshot.assert_match(transformed, "multi_service_example_01_no_internet.json")


def test_limited_internet_topology_snapshot(canon_snapshot) -> None:
    assert EXAMPLE_COMPOSE.is_file(), f"missing example compose: {EXAMPLE_COMPOSE}"
    doc = yaml.safe_load(EXAMPLE_COMPOSE.read_text())

    transformed = enforce_network_policy(
        doc, NetworkPolicy.LIMITED_INTERNET, "runner", ["api.openai.com", "*.example.com"]
    )

    snapshot = canon_snapshot("network_policy_enforcement")
    snapshot.assert_match(transformed, "multi_service_example_01_limited_internet.json")
