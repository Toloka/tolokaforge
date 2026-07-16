"""Unit tests for :meth:`Orchestrator._write_open_agent_loop_trace` — M1 sub-4b.

The write is a small helper called from the trial-completion handler; these
tests exercise it directly to avoid spinning up Docker / a real conductor.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from tolokaforge.core.models import OpenAgentLoopConfig, RunConfig
from tolokaforge.core.orchestrator import Orchestrator
from tolokaforge.session import AssistantMessage, TurnStarted
from tolokaforge.session._status import TrialStatus
from tolokaforge.session.events import TerminalReached

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)


def _minimal_run_config(open_agent_loop: OpenAgentLoopConfig | None = None) -> RunConfig:
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


class TestSealedModeIsNoOp:
    def test_sealed_write_creates_no_file(self, tmp_path: Path):
        """No sessions in sealed mode — the writer must be a silent no-op."""
        orch = Orchestrator(config=_minimal_run_config())
        orch._write_open_agent_loop_trace(tmp_path, "MAN-34", 0)
        assert not (tmp_path / "trials").exists()

    def test_open_mode_but_no_session_yet_is_no_op(self, tmp_path: Path):
        """Open-mode registry exists, but if the trial never entered the run
        body the session for that trial_id doesn't exist — nothing to write.
        """
        orch = Orchestrator(config=_minimal_run_config(OpenAgentLoopConfig(enabled=True)))
        orch._write_open_agent_loop_trace(tmp_path, "never-ran", 0)
        assert not (tmp_path / "trials").exists()


class TestOpenModeWritesTraceFile:
    def _seed_session(self, orch: Orchestrator, trial_id: str) -> None:
        provider = orch._build_observer_provider()
        assert provider is not None
        provider(trial_id)  # create the session via the same path the conductor uses

        session = orch.sessions.get(trial_id)
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

    def test_writes_yaml_at_expected_path(self, tmp_path: Path):
        orch = Orchestrator(config=_minimal_run_config(OpenAgentLoopConfig(enabled=True)))
        self._seed_session(orch, "MAN-34:0")

        orch._write_open_agent_loop_trace(tmp_path, "MAN-34", 0)

        trace_path = tmp_path / "trials" / "MAN-34" / "0" / "open_agent_loop.yaml"
        assert trace_path.exists()

        content = yaml.safe_load(trace_path.read_text())
        assert content["trial_id"] == "MAN-34:0"
        assert content["closed"] is True
        assert len(content["events"]) == 3
        assert [e["kind"] for e in content["events"]] == [
            "turn_started",
            "assistant_message",
            "terminal_reached",
        ]

    def test_write_overwrites_on_retry(self, tmp_path: Path):
        """A retried trial gets more events on the same session; the file is
        overwritten with the accumulated state.
        """
        orch = Orchestrator(config=_minimal_run_config(OpenAgentLoopConfig(enabled=True)))
        provider = orch._build_observer_provider()
        provider("t:0")
        session = orch.sessions.get("t:0")

        session.publish(
            TurnStarted(trial_id="t:0", seq=session.next_seq(), timestamp=_NOW, turn_index=0)
        )
        orch._write_open_agent_loop_trace(tmp_path, "t", 0)
        first = yaml.safe_load(
            (tmp_path / "trials" / "t" / "0" / "open_agent_loop.yaml").read_text()
        )
        assert len(first["events"]) == 1

        # Simulate more events on the same session (retry appends to history)
        session.publish(
            AssistantMessage(
                trial_id="t:0", seq=session.next_seq(), timestamp=_NOW, content_preview="retry"
            )
        )
        orch._write_open_agent_loop_trace(tmp_path, "t", 0)
        second = yaml.safe_load(
            (tmp_path / "trials" / "t" / "0" / "open_agent_loop.yaml").read_text()
        )
        assert len(second["events"]) == 2

    def test_write_failure_logs_and_returns_without_raising(self, tmp_path: Path):
        """Trace-write failures must not mask the trial result. Force a write
        error by pointing at a path where mkdir will fail (an existing FILE
        where a directory is expected).
        """
        orch = Orchestrator(config=_minimal_run_config(OpenAgentLoopConfig(enabled=True)))
        self._seed_session(orch, "t:0")

        # tmp_path/trials is a FILE, not a directory — parent.mkdir will raise
        blocker = tmp_path / "trials"
        blocker.write_text("blocking-file")

        # Must not raise
        orch._write_open_agent_loop_trace(tmp_path, "t", 0)
