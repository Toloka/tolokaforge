"""Who answers "what supplies a hash source beneath the authored block", and how.

The pre-run gates ask an adapter what lies beneath a task's ``state_checks.hash``
block. Three answers are possible and they decide opposite things, so the default
matters as much as the overrides: an adapter that has not implemented the hook must
say it cannot answer, because reading its silence as "nothing beneath" would refuse
every pack whose source lives in a fixture the block never names.

The registry seam is here for the same reason. ``tolokaforge validate`` holds no
adapter instance and runs against packs whose adapter package is not installed, so
the answer has to be reachable from the class alone and the lookup has to have a
``None`` for "not installed" rather than an exception.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import tolokaforge.adapters as adapters_package
from tolokaforge.adapters import NativeAdapter, adapter_class, register_adapter
from tolokaforge.adapters._task_loader import load_task_yaml
from tolokaforge.adapters.base import BaseAdapter
from tolokaforge.core.grading.config_validation import HashSourceLayer
from tolokaforge.core.models import TaskConfig

_A_REAL_TASK = (
    Path(__file__).resolve().parents[3]
    / "examples/native/multi_service_helpdesk_workflow/dataset/tasks/helpdesk_01/task.yaml"
)


class _AnAdapterThatImplementsNothing(BaseAdapter):
    """A plugin written against the interface before this hook existed.

    Deliberately abstract — the hook is a classmethod precisely so the answer needs
    no instance, and nothing here overrides anything.
    """


@pytest.fixture
def a_task() -> tuple[TaskConfig, Path]:
    """A real task and its directory, since the hook is asked about exactly those."""
    return load_task_yaml(_A_REAL_TASK)


@pytest.fixture
def a_pristine_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry as a fresh process has it: nothing registered, nothing discovered.

    Both halves matter. Replacing the dict keeps a test's registration out of the
    real one, and clearing the flag is what puts the test *before* first discovery,
    which is the only place the ordering bug this guards against can appear.
    """
    monkeypatch.setattr(adapters_package, "_ADAPTERS", {})
    monkeypatch.setattr(adapters_package, "_DISCOVERED", False)


def test_an_adapter_that_never_implemented_the_hook_cannot_say(
    a_task: tuple[TaskConfig, Path],
) -> None:
    """Silence is "I cannot say", never "nothing is beneath the block".

    Every adapter written before the hook existed inherits this answer, and it is the
    one that leaves such a pack exactly as checkable as it was: reported, never
    refused. The other default would turn every unported adapter's packs into
    refusals at both gates.
    """
    task, task_dir = a_task

    layer = _AnAdapterThatImplementsNothing.grading_hash_source_layer(task, task_dir)

    assert layer == HashSourceLayer.unresolvable()
    assert layer.known is False


def test_a_native_task_answers_that_the_authored_block_is_the_whole_layer(
    a_task: tuple[TaskConfig, Path],
) -> None:
    """Native resolves to "nothing beneath", which is an answer, not an inability.

    That is what keeps an enabled hash declaring no source a refusable authoring
    defect for native packs — under the inherited default it would become merely
    unchecked, and the defect would reach grade time.
    """
    task, task_dir = a_task

    layer = NativeAdapter.grading_hash_source_layer(task, task_dir)

    assert layer == HashSourceLayer()
    assert layer.known is True
    assert layer.supplied is None


def test_the_registry_answers_none_for_an_adapter_nothing_registered() -> None:
    """A name the registry cannot resolve is an answer the gates act on, not an error.

    ``validate`` runs over packs naming adapters this environment has never installed;
    raising here would make the gate refuse packs on the grounds that it cannot check
    them, which is the opposite of what the unresolved answer exists for.
    """
    assert adapter_class("native") is NativeAdapter
    assert adapter_class("no_such_adapter") is None


def test_a_registration_made_before_discovery_survives_it_and_does_not_suppress_it(
    a_pristine_registry: None,
) -> None:
    """Discovery merges into the registry, and an explicit registration outranks it.

    Both halves are one failure mode seen from two sides — a write that silently
    disappears. Reading the registry's own emptiness as "already discovered" lets one
    early registration suppress entry-point discovery for the life of the process, so
    ``native`` never resolves; replacing the registry with the discovered set instead
    of merging throws that early registration away.
    """
    register_adapter("an_adapter_registered_by_hand", _AnAdapterThatImplementsNothing)

    assert adapter_class("native") is NativeAdapter
    assert adapter_class("an_adapter_registered_by_hand") is _AnAdapterThatImplementsNothing


def test_a_hand_registered_name_wins_the_collision_against_a_discovered_one(
    a_pristine_registry: None,
) -> None:
    """An explicit registration outranks ambient discovery under the same name.

    ``native`` is the collision every environment can provoke, since discovery always
    supplies it. Losing it would make a deliberate substitution — a test double, an
    embedder's own subclass — vanish the moment anything triggered discovery.
    """
    register_adapter("native", _AnAdapterThatImplementsNothing)

    assert adapter_class("native") is _AnAdapterThatImplementsNothing
