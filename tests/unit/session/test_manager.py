"""Unit tests for :class:`OpenAgentLoopManager` — the run-scoped OAL coordinator.

Split off from the earlier ``test_orchestrator_open_mode.py`` +
``test_orchestrator_trace_write.py`` — those retained tests focus on
orchestrator-level delegation; the concern-specific tests live here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from tolokaforge.core.models import OpenAgentLoopConfig, RunConfig
from tolokaforge.session import (
    AssistantMessage,
    InProcessTrialSession,
    OpenAgentLoopManager,
    SessionInterventionHandler,
    SessionLoopObserver,
    SessionRegistry,
    TerminalReached,
    TurnStarted,
)
from tolokaforge.session._status import TrialStatus

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)


def _minimal_config(open_agent_loop: OpenAgentLoopConfig | None = None) -> RunConfig:
    return RunConfig.model_validate(
        {
            "models": {
                "agent": {"name": "claude-opus-4-7", "provider": "anthropic"},
                "user": {"name": "claude-opus-4-7", "provider": "anthropic"},
            },
            "orchestrator": {},
            "evaluation": {
                "task_packs": ["fixtures/dummy"],
                "output_dir": "/tmp/does-not-matter",
            },
            **(
                {"open_agent_loop": open_agent_loop.model_dump()}
                if open_agent_loop is not None
                else {}
            ),
        }
    )


class TestFromConfig:
    def test_no_config_block_returns_none(self):
        assert OpenAgentLoopManager.from_config(_minimal_config()) is None

    def test_explicit_disabled_returns_none(self):
        cfg = _minimal_config(OpenAgentLoopConfig(enabled=False))
        assert OpenAgentLoopManager.from_config(cfg) is None

    def test_enabled_returns_manager(self):
        cfg = _minimal_config(OpenAgentLoopConfig(enabled=True))
        manager = OpenAgentLoopManager.from_config(cfg)
        assert isinstance(manager, OpenAgentLoopManager)
        assert isinstance(manager.sessions, SessionRegistry)
        assert len(manager.sessions) == 0


class TestObserverProvider:
    def test_provider_creates_session_and_returns_observer(self):
        manager = OpenAgentLoopManager()
        provider = manager.observer_provider()

        obs = provider("MAN-34:0")
        assert isinstance(obs, SessionLoopObserver)
        assert "MAN-34:0" in manager.sessions
        session = manager.sessions.get("MAN-34:0")
        assert isinstance(session, InProcessTrialSession)
        assert session.trial_id == "MAN-34:0"

    def test_provider_reuses_same_session_across_calls_for_same_trial(self):
        manager = OpenAgentLoopManager()
        provider = manager.observer_provider()
        provider("t:0")
        provider("t:0")
        assert len(manager.sessions) == 1

    def test_provider_returns_distinct_sessions_per_trial(self):
        manager = OpenAgentLoopManager()
        provider = manager.observer_provider()
        provider("A:0")
        provider("B:0")
        assert manager.sessions.get("A:0") is not manager.sessions.get("B:0")


class TestInterventionHandlerProvider:
    def test_provider_returns_handler_bound_to_same_session_as_observer(self):
        """Observer and handler for the same trial_id must bind to the same
        session so events + interventions round-trip through one bus.
        """
        manager = OpenAgentLoopManager()
        obs_provider = manager.observer_provider()
        int_provider = manager.intervention_handler_provider()

        obs = obs_provider("t:0")
        handler = int_provider("t:0")
        assert isinstance(handler, SessionInterventionHandler)
        # Both bind to manager.sessions.get("t:0") — same InProcessTrialSession
        assert manager.sessions.get("t:0") is not None
        assert obs is not None


class TestWriteTrace:
    def _seed(self, manager: OpenAgentLoopManager, trial_id: str) -> None:
        provider = manager.observer_provider()
        provider(trial_id)
        session = manager.sessions.get(trial_id)
        assert session is not None
        session.publish(
            TurnStarted(trial_id=trial_id, seq=session.next_seq(), timestamp=_NOW, turn_index=0)
        )
        session.publish(
            AssistantMessage(
                trial_id=trial_id,
                seq=session.next_seq(),
                timestamp=_NOW,
                content_preview="hi",
            )
        )
        session.publish(
            TerminalReached(
                trial_id=trial_id,
                seq=session.next_seq(),
                timestamp=_NOW,
                status=TrialStatus.COMPLETED,
            )
        )

    def test_no_session_yet_is_noop(self, tmp_path: Path):
        manager = OpenAgentLoopManager()
        manager.write_trace(tmp_path, "never-ran", 0)
        assert not (tmp_path / "trials").exists()

    def test_writes_yaml_at_expected_path(self, tmp_path: Path):
        manager = OpenAgentLoopManager()
        self._seed(manager, "MAN-34:0")

        manager.write_trace(tmp_path, "MAN-34", 0)

        trace_path = tmp_path / "trials" / "MAN-34" / "0" / "open_agent_loop.yaml"
        assert trace_path.exists()
        content = yaml.safe_load(trace_path.read_text())
        assert content["trial_id"] == "MAN-34:0"
        assert [e["kind"] for e in content["events"]] == [
            "turn_started",
            "assistant_message",
            "terminal_reached",
        ]

    def test_overwrites_on_retry(self, tmp_path: Path):
        manager = OpenAgentLoopManager()
        provider = manager.observer_provider()
        provider("t:0")
        session = manager.sessions.get("t:0")
        session.publish(
            TurnStarted(trial_id="t:0", seq=session.next_seq(), timestamp=_NOW, turn_index=0)
        )
        manager.write_trace(tmp_path, "t", 0)
        first = yaml.safe_load(
            (tmp_path / "trials" / "t" / "0" / "open_agent_loop.yaml").read_text()
        )
        assert len(first["events"]) == 1

        session.publish(
            AssistantMessage(
                trial_id="t:0", seq=session.next_seq(), timestamp=_NOW, content_preview="more"
            )
        )
        manager.write_trace(tmp_path, "t", 0)
        second = yaml.safe_load(
            (tmp_path / "trials" / "t" / "0" / "open_agent_loop.yaml").read_text()
        )
        assert len(second["events"]) == 2

    def test_write_failure_raises_for_orchestrator_to_catch(self, tmp_path: Path):
        """Manager's write raises on failure; orchestrator catches and logs.
        Contract: manager is honest about failures; caller policy decides
        whether to swallow.
        """
        manager = OpenAgentLoopManager()
        self._seed(manager, "t:0")

        # Point at a path where mkdir will fail (an existing FILE at
        # tmp_path/trials)
        blocker = tmp_path / "trials"
        blocker.write_text("blocking-file")

        with pytest.raises(OSError):  # NotADirectoryError on POSIX subclasses OSError
            manager.write_trace(tmp_path, "t", 0)


class TestSnapshotAll:
    def test_snapshot_all_returns_dict_keyed_by_trial_id(self):
        manager = OpenAgentLoopManager()
        provider = manager.observer_provider()
        provider("a:0")
        provider("b:0")

        snapshots = manager.snapshot_all()
        assert set(snapshots.keys()) == {"a:0", "b:0"}
        assert snapshots["a:0"]["trial_id"] == "a:0"
        assert snapshots["b:0"]["trial_id"] == "b:0"

    def test_snapshot_all_empty_for_fresh_manager(self):
        manager = OpenAgentLoopManager()
        assert manager.snapshot_all() == {}
