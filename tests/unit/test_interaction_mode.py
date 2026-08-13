"""Schema-level tests for :attr:`TaskConfig.interaction_mode` and
:attr:`TaskDefaults.interaction_mode`.

The field selects the trial's turn-loop shape:

- ``conversational`` (default) — user simulator dispatched every turn;
  the historical / τ-bench shape every existing pack was written for.
- ``agent_only`` — no user turn after the first message; the agent runs to
  its first tool-call-free turn, ``max_turns`` or ``episode_timeout_s``.
  Backwards-compatible via the default.

These tests pin the schema contract at the model layer. Layer 3 tests
(``test_turn_policy_contract.py``) exercise the dispatch behavior; Layer
5 tests (``test_conductor_contract.py``) cover the runner wiring.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tolokaforge.core.models.task_config import TaskConfig, TaskDefaults

pytestmark = pytest.mark.unit


def _minimal_task_kwargs(**overrides: object) -> dict[str, object]:
    """Bare-minimum TaskConfig kwargs; every field under test overrides
    these defaults."""
    return {
        "task_id": "task-1",
        "description": "test task",
        **overrides,
    }


class TestInteractionModeDefaults:
    """The field is backward-compatible: unset means ``conversational``,
    which is byte-for-byte the shape every existing pack authored today
    already expects."""

    def test_task_config_default_is_conversational(self) -> None:
        task = TaskConfig(**_minimal_task_kwargs())
        assert task.interaction_mode == "conversational"

    def test_task_defaults_default_is_none(self) -> None:
        """Project-side default is ``None`` (unset) so a task's own
        ``interaction_mode`` wins on merge without needing to know the
        engine default."""
        defaults = TaskDefaults()
        assert defaults.interaction_mode is None


class TestInteractionModeExplicit:
    def test_task_config_accepts_conversational(self) -> None:
        task = TaskConfig(**_minimal_task_kwargs(interaction_mode="conversational"))
        assert task.interaction_mode == "conversational"

    def test_task_config_accepts_agent_only(self) -> None:
        task = TaskConfig(**_minimal_task_kwargs(interaction_mode="agent_only"))
        assert task.interaction_mode == "agent_only"

    def test_task_defaults_accepts_agent_only(self) -> None:
        defaults = TaskDefaults(interaction_mode="agent_only")
        assert defaults.interaction_mode == "agent_only"


class TestInteractionModeRejectsUnknown:
    """The literal must reject typos and future values that haven't
    landed yet (e.g. ``multi_actor`` before its policy ships), so a
    pack referencing an unknown mode fails at load time rather than at
    turn dispatch when there's no matching :class:`TurnPolicy`."""

    def test_task_config_rejects_typo(self) -> None:
        with pytest.raises(ValidationError, match="interaction_mode"):
            TaskConfig(**_minimal_task_kwargs(interaction_mode="conversation"))

    def test_task_config_rejects_future_value_not_yet_registered(self) -> None:
        """``multi_actor`` is reserved in the design (Layer 3 registry
        will unlock it) but not yet a valid literal — reject until a
        matching policy is registered."""
        with pytest.raises(ValidationError, match="interaction_mode"):
            TaskConfig(**_minimal_task_kwargs(interaction_mode="multi_actor"))

    def test_task_defaults_rejects_typo(self) -> None:
        with pytest.raises(ValidationError, match="interaction_mode"):
            TaskDefaults(interaction_mode="agent-only")


class TestModelDumpRoundTrip:
    """The field must survive ``model_dump`` → ``model_validate`` so
    engine paths that serialise TaskConfig (canonical fixtures, run
    reports) preserve the mode."""

    def test_task_config_round_trip_conversational_default(self) -> None:
        task = TaskConfig(**_minimal_task_kwargs())
        restored = TaskConfig.model_validate(task.model_dump())
        assert restored.interaction_mode == "conversational"

    def test_task_config_round_trip_agent_only(self) -> None:
        task = TaskConfig(**_minimal_task_kwargs(interaction_mode="agent_only"))
        restored = TaskConfig.model_validate(task.model_dump())
        assert restored.interaction_mode == "agent_only"
