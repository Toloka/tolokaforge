"""Locks ``InMemoryNodeRegistry`` — leasing binds a trial to a node, blocks when none is free."""

from __future__ import annotations

import threading
import time

import pytest

from tolokaforge.core.node_registry import InMemoryNodeRegistry, NodeRegistry, RunnerLease

pytestmark = pytest.mark.unit


def test_satisfies_the_node_registry_protocol() -> None:
    assert isinstance(InMemoryNodeRegistry(["a:1"]), NodeRegistry)


def test_lease_binds_trial_to_a_seeded_address() -> None:
    registry = InMemoryNodeRegistry(["10.0.0.1:50051"])

    lease = registry.lease("t:0")

    assert isinstance(lease, RunnerLease)
    assert lease.runner_address == "10.0.0.1:50051"


def test_distinct_trials_get_distinct_nodes() -> None:
    registry = InMemoryNodeRegistry(["a:1", "b:2"])

    first = registry.lease("t:0")
    second = registry.lease("t:1")

    assert {first.runner_address, second.runner_address} == {"a:1", "b:2"}
    assert first.node_id != second.node_id


def test_lease_is_idempotent_per_trial() -> None:
    registry = InMemoryNodeRegistry(["a:1", "b:2"])

    assert registry.lease("t:0") == registry.lease("t:0")
    assert registry.lease("t:1").runner_address != registry.lease("t:0").runner_address


def test_lease_blocks_until_a_node_frees() -> None:
    registry = InMemoryNodeRegistry(["only:1"], lease_timeout_s=5.0)
    registry.lease("t:0")
    acquired: list[RunnerLease] = []
    started = threading.Event()

    def contender() -> None:
        started.set()
        acquired.append(registry.lease("t:1"))

    thread = threading.Thread(target=contender, daemon=True)
    thread.start()
    assert started.wait(timeout=5)
    time.sleep(0.2)
    assert acquired == []

    registry.release("t:0")
    thread.join(timeout=5)

    assert [lease.runner_address for lease in acquired] == ["only:1"]


def test_lease_raises_on_timeout_when_no_node_is_free() -> None:
    registry = InMemoryNodeRegistry(["only:1"], lease_timeout_s=0.1)
    registry.lease("t:0")

    with pytest.raises(TimeoutError, match="no node became free within 0.1s"):
        registry.lease("t:1")


def test_lease_with_no_nodes_times_out() -> None:
    with pytest.raises(TimeoutError, match="no node became free"):
        InMemoryNodeRegistry([], lease_timeout_s=0.05).lease("t:0")


def test_release_is_idempotent_and_frees_the_node_once() -> None:
    registry = InMemoryNodeRegistry(["only:1"], lease_timeout_s=0.1)
    registry.lease("t:0")

    registry.release("t:0")
    registry.release("t:0")
    registry.release("never-leased")

    assert registry.lease("t:1").runner_address == "only:1"
    with pytest.raises(TimeoutError):
        registry.lease("t:2")


def test_double_release_does_not_free_a_node_another_trial_now_holds() -> None:
    registry = InMemoryNodeRegistry(["only:1"], lease_timeout_s=0.1)
    registry.lease("t:0")
    registry.release("t:0")
    registry.lease("t:1")

    registry.release("t:0")

    with pytest.raises(TimeoutError):
        registry.lease("t:2")


def test_register_node_adds_capacity() -> None:
    registry = InMemoryNodeRegistry(["a:1"], lease_timeout_s=0.1)
    registry.lease("t:0")

    registry.register_node("late", "z:9")

    assert registry.lease("t:1") == RunnerLease(node_id="late", runner_address="z:9")


def test_has_nodes() -> None:
    assert not InMemoryNodeRegistry([]).has_nodes()
    assert InMemoryNodeRegistry(["a:1"]).has_nodes()
