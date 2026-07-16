"""Wiring tests for Orchestrator's delegation to OAL manager's trace writer.

Focused on the orchestrator's guard/log-around-swallow contract; the
actual write semantics live in ``tests/unit/session/test_manager.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

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


class TestSealedIsNoOp:
    def test_sealed_write_creates_no_file(self, tmp_path: Path):
        orch = Orchestrator(config=_minimal_run_config())
        orch._write_open_agent_loop_trace(tmp_path, "MAN-34", 0)
        assert not (tmp_path / "trials").exists()


class TestDelegatesToManagerAndSwallowsErrors:
    def _seed(self, orch: Orchestrator, trial_id: str) -> None:
        assert orch._oal_manager is not None
        provider = orch._oal_manager.observer_provider()
        provider(trial_id)
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

    def test_writes_file_via_manager_delegation(self, tmp_path: Path):
        orch = Orchestrator(config=_minimal_run_config(OpenAgentLoopConfig(enabled=True)))
        self._seed(orch, "MAN-34:0")

        orch._write_open_agent_loop_trace(tmp_path, "MAN-34", 0)

        trace_path = tmp_path / "trials" / "MAN-34" / "0" / "open_agent_loop.yaml"
        assert trace_path.exists()

    def test_write_failure_swallowed_by_orchestrator(self, tmp_path: Path):
        """Orchestrator's contribution over the manager is the try/except.
        Manager raises; orchestrator catches, logs, does not propagate —
        trace writes must never mask the actual trial result.
        """
        orch = Orchestrator(config=_minimal_run_config(OpenAgentLoopConfig(enabled=True)))
        self._seed(orch, "t:0")

        blocker = tmp_path / "trials"
        blocker.write_text("blocking-file")

        # Must NOT raise
        orch._write_open_agent_loop_trace(tmp_path, "t", 0)
