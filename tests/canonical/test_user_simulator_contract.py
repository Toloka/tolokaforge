"""Pin the ``UserSimulator`` seam: the Protocol surface, and the kit's teeth.

Three things are locked here.

**The built-in simulator conforms.** ``builtin`` resolves through the
``tolokaforge.user_simulators`` registry and runs the whole
:class:`~tolokaforge.testing.user_simulators.UserSimulatorConformanceSuite`, so
the kit is proven by the implementation it describes rather than by its own
fixture.

**The reference fixture conforms.** :class:`InMemoryUserSimulator` is the worked
example an external implementer copies; a reference that does not pass the suite
teaches the wrong simulator.

**The suite has teeth.** Every obligation is read downstream, so a simulator that
breaks one produces a wrong artifact rather than raising. Each
:class:`SimulatorDefects` knob switches off exactly one obligation and the
assertion written for it must fail on that simulator.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from tolokaforge.core.actors.user_simulator import UserSimulator, UserSimulatorContext
from tolokaforge.core.llm.client import BuiltinUserSimulator
from tolokaforge.core.plugin_registry import available_user_simulators, load_user_simulator
from tolokaforge.testing.user_simulators import (
    InMemoryUserSimulator,
    SimulatorDefects,
    UserSimulatorConformanceSuite,
    in_memory_user_simulator_factory,
)

pytestmark = pytest.mark.canonical


class TestProtocolSurface:
    """``UserSimulator`` is ``@runtime_checkable`` and both shipped simulators satisfy it."""

    def test_the_builtin_simulator_satisfies_the_protocol(self) -> None:
        simulator = load_user_simulator("builtin")(_scripted_context())
        assert isinstance(simulator, BuiltinUserSimulator)
        assert isinstance(simulator, UserSimulator)

    def test_the_in_memory_fixture_satisfies_the_protocol(self) -> None:
        simulator = in_memory_user_simulator_factory(_scripted_context())
        assert isinstance(simulator, InMemoryUserSimulator)
        assert isinstance(simulator, UserSimulator)

    def test_an_object_without_reply_does_not_satisfy_the_protocol(self) -> None:
        class _NotASimulator:
            last_system_prompt: str | None = None

        assert not isinstance(_NotASimulator(), UserSimulator)

    def test_an_object_without_last_system_prompt_does_not_satisfy_the_protocol(self) -> None:
        class _ReplyOnly:
            def reply(self, context: Any, *, observation: Any = None) -> Any:
                return None

        assert not isinstance(_ReplyOnly(), UserSimulator)

    def test_builtin_is_registered(self) -> None:
        assert "builtin" in available_user_simulators()

    @pytest.mark.parametrize(
        "surface",
        [UserSimulator.reply, BuiltinUserSimulator.reply, InMemoryUserSimulator.reply],
        ids=["protocol", "builtin", "in_memory"],
    )
    def test_reply_takes_the_declared_parameters(self, surface: Any) -> None:
        """``context`` positional, ``observation`` keyword — the runner passes both."""
        parameters = [name for name in inspect.signature(surface).parameters if name != "self"]
        assert parameters == ["context", "observation"]


def _scripted_context() -> UserSimulatorContext:
    return UserSimulatorContext(
        mode="scripted",
        persona="cooperative",
        backstory=None,
        scripted_flow=[{"default": "Yes, please proceed."}],
        tool_schemas=None,
    )


class TestBuiltinSimulatorConformance(UserSimulatorConformanceSuite):
    """``builtin`` — the implementation the kit is written from."""

    @pytest.fixture
    def simulator_factory(self) -> Any:
        return load_user_simulator("builtin")


class TestInMemorySimulatorConformance(UserSimulatorConformanceSuite):
    """The reference fixture an external implementer copies."""

    @pytest.fixture
    def simulator_factory(self) -> Any:
        return in_memory_user_simulator_factory


def _defective(**defects: Any) -> Any:
    """A ``UserSimulatorFactory`` over a reference with the named obligations switched off."""

    def factory(context: UserSimulatorContext) -> InMemoryUserSimulator:
        return InMemoryUserSimulator(context=context, defects=SimulatorDefects(**defects))

    return factory


_VECTORS = [
    pytest.param(
        {"wrong_reply_type": True},
        "test_reply_returns_a_generation_result",
        id="reply-is-not-a-generation-result",
    ),
    pytest.param(
        {"last_system_prompt_wrong_type": True},
        "test_last_system_prompt_is_str_or_none",
        id="last-system-prompt-is-the-wrong-type",
    ),
    pytest.param(
        {"crash_on_simulator_config": True},
        "test_factory_accepts_a_simulator_config_passthrough",
        id="factory-rejects-the-simulator-config-passthrough",
    ),
]


class TestTheSuiteDetectsEachVector:
    """One defect, one failing assertion — the kit's own regression guard."""

    @pytest.mark.parametrize(("defects", "test_name"), _VECTORS)
    def test_the_named_assertion_fails_on_the_defective_simulator(
        self, defects: dict[str, Any], test_name: str
    ) -> None:
        suite = UserSimulatorConformanceSuite()
        with pytest.raises(AssertionError):
            getattr(suite, test_name)(_defective(**defects))

    @pytest.mark.parametrize(("defects", "test_name"), _VECTORS)
    def test_the_same_defect_leaves_the_conforming_simulator_green(
        self, defects: dict[str, Any], test_name: str
    ) -> None:
        """The control: the assertion passes on the reference with the defect off."""
        suite = UserSimulatorConformanceSuite()
        getattr(suite, test_name)(in_memory_user_simulator_factory)


def test_the_kit_is_importable_from_the_distributed_package() -> None:
    """An external implementer reaches the suite without a source checkout.

    ``tolokaforge.testing`` ships inside the wheel, so the import path a third
    party writes in their own test file is this one.
    """
    import tolokaforge.testing.user_simulators as kit

    for name in (
        "UserSimulatorConformanceSuite",
        "InMemoryUserSimulator",
        "in_memory_user_simulator_factory",
    ):
        assert name in kit.__all__
        assert getattr(kit, name) is not None
