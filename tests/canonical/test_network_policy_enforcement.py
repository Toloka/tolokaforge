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

PARTITIONED_SIBLING_COMPOSE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "network_policy_enforcement"
    / "partitioned_sibling_compose.yaml"
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


def test_no_internet_with_restricted_service_snapshot(canon_snapshot) -> None:
    """Under ``no_internet`` a ``bash`` sibling marked
    ``network_access: restricted`` joins only its task-declared
    ``tool_bridge`` network — no injected internal-net attachment — while
    the runner keeps both injected nets and every other application
    service keeps the injected internal net."""
    assert PARTITIONED_SIBLING_COMPOSE.is_file(), f"missing fixture: {PARTITIONED_SIBLING_COMPOSE}"
    doc = yaml.safe_load(PARTITIONED_SIBLING_COMPOSE.read_text())

    transformed = enforce_network_policy(
        doc,
        NetworkPolicy.NO_INTERNET,
        "runner",
        [],
        frozenset({"bash"}),
    )

    snapshot = canon_snapshot("network_policy_enforcement")
    snapshot.assert_match(transformed, "partitioned_sibling_no_internet.json")


def test_limited_internet_with_restricted_service_snapshot(canon_snapshot) -> None:
    """Under ``limited_internet`` a restricted ``bash`` sibling joins only
    ``tool_bridge``, carries no ``HTTP(S)_PROXY`` env, and the injected
    squid proxy sits on both harness nets; every non-restricted sibling
    still joins the injected internal net and still has the proxy env."""
    assert PARTITIONED_SIBLING_COMPOSE.is_file(), f"missing fixture: {PARTITIONED_SIBLING_COMPOSE}"
    doc = yaml.safe_load(PARTITIONED_SIBLING_COMPOSE.read_text())

    transformed = enforce_network_policy(
        doc,
        NetworkPolicy.LIMITED_INTERNET,
        "runner",
        ["api.openai.com", "*.example.com"],
        frozenset({"bash"}),
    )

    snapshot = canon_snapshot("network_policy_enforcement")
    snapshot.assert_match(transformed, "partitioned_sibling_limited_internet.json")
