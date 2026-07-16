"""End-to-end tests for the reference participants.

The proof that the contract is shared: both participants attach to the same
:class:`RecordedTrialSession`, drain the same events, and emit identical
:class:`SessionLogEntry` shape. The heuristic drafter is deterministic when
no API key is present, so these tests do not need a live LLM provider.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from intervener.participants import HumanIntervener, LLMIntervener
from intervener.schema import InterventionSuggestion

from tolokaforge.session import (
    AssistantMessage,
    RecordedTrialSession,
    TerminalReached,
    ToolCallEmitted,
    ToolResultObserved,
    TrialEvent,
    TurnStarted,
)
from tolokaforge.session._status import TrialStatus

_NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=UTC)


def _make_looping_session() -> RecordedTrialSession:
    """Three identical tool calls to `auth_login` — the heuristic drafter's
    canonical stuck-pattern trigger.
    """
    trial_id = "TEST-01:0"
    events: list[TrialEvent] = [
        TurnStarted(trial_id=trial_id, seq=0, timestamp=_NOW, turn_index=0),
        AssistantMessage(trial_id=trial_id, seq=1, timestamp=_NOW, content_preview="Let me try."),
        ToolCallEmitted(
            trial_id=trial_id,
            seq=2,
            timestamp=_NOW,
            call_id="c1",
            tool_name="auth_login",
            arguments_preview="{}",
        ),
        ToolResultObserved(
            trial_id=trial_id,
            seq=3,
            timestamp=_NOW,
            call_id="c1",
            tool_name="auth_login",
            duration_ms=120,
            truncated_preview="401 Unauthorized",
        ),
        AssistantMessage(trial_id=trial_id, seq=4, timestamp=_NOW, content_preview="Retrying."),
        ToolCallEmitted(
            trial_id=trial_id,
            seq=5,
            timestamp=_NOW,
            call_id="c2",
            tool_name="auth_login",
            arguments_preview="{}",
        ),
        ToolResultObserved(
            trial_id=trial_id,
            seq=6,
            timestamp=_NOW,
            call_id="c2",
            tool_name="auth_login",
            duration_ms=118,
            truncated_preview="401 Unauthorized",
        ),
        AssistantMessage(trial_id=trial_id, seq=7, timestamp=_NOW, content_preview="Once more."),
        ToolCallEmitted(
            trial_id=trial_id,
            seq=8,
            timestamp=_NOW,
            call_id="c3",
            tool_name="auth_login",
            arguments_preview="{}",
        ),
        TerminalReached(trial_id=trial_id, seq=9, timestamp=_NOW, status=TrialStatus.FAILED),
    ]
    return RecordedTrialSession.from_events(trial_id=trial_id, events=events)


class TestLLMIntervenerHeuristic:
    def test_stuck_pattern_is_detected_by_heuristic(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        session = _make_looping_session()
        intervener = LLMIntervener(auto_inject=False)
        log = intervener.run(session)

        # Every entry has identical shape (via the shared base class)
        for entry in log.entries:
            assert entry.trial_id == "TEST-01:0"
            assert entry.participant_id == "llm_intervener"

        # At least one suggestion should carry a payload with the InterventionSuggestion shape
        payloads = [entry.payload for entry in log.entries if entry.payload is not None]
        assert payloads, "expected at least one suggestion payload in the log"
        parsed = [InterventionSuggestion.model_validate(p) for p in payloads]

        # The heuristic should classify the loop as medium urgency somewhere in the window
        stuck_matches = [s for s in parsed if "auth_login" in s.situation]
        assert (
            stuck_matches
        ), f"heuristic should have detected the auth_login loop; got {[s.situation for s in parsed]}"
        loop_sug = stuck_matches[0]
        assert loop_sug.urgency == "medium"
        assert 0.0 <= loop_sug.urgency_score <= 1.0

    def test_auto_inject_only_fires_on_high_urgency(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        session = _make_looping_session()
        # Heuristic tops out at medium — auto-inject must be silent
        intervener = LLMIntervener(auto_inject=True)
        intervener.run(session)
        assert session.captured_interventions == []


class TestHumanCLIParticipantScripted:
    def test_scripted_inject_produces_intervention_of_correct_kind(self):
        session = _make_looping_session()
        human = HumanIntervener(
            non_interactive_script=[
                "",  # first prompt seam — no intervention
                "try /v2/auth instead",  # bare text → InjectMessage
                "/kill giving up on this trial",  # slash-command → Kill
                "",
                "",
                "",
            ]
        )
        log = human.run(session)

        submitted_kinds = [
            entry.intervention_kind for entry in log.entries if entry.intervention_kind
        ]
        assert "inject_message" in submitted_kinds
        assert "kill" in submitted_kinds
        # RecordedTrialSession rejects them, but records them for inspection
        assert len(session.captured_interventions) == 2


class TestSharedLogShapeAcrossParticipants:
    """The critical M2 acceptance criterion: same session-log shape regardless
    of participant type. Every entry has the same fields; only the values
    differ between LLM and human.
    """

    def test_field_set_is_identical(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        llm_log = LLMIntervener(auto_inject=False).run(_make_looping_session())
        human_log = HumanIntervener(non_interactive_script=[""] * 10).run(_make_looping_session())

        llm_fields = {frozenset(entry.model_dump(mode="json").keys()) for entry in llm_log.entries}
        human_fields = {
            frozenset(entry.model_dump(mode="json").keys()) for entry in human_log.entries
        }
        assert llm_fields == human_fields
        assert len(llm_log.entries) == len(human_log.entries)


class TestDemoDriverEndToEnd:
    def test_llm_driver_produces_session_log_yaml(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        traj = _write_looping_trajectory(tmp_path)

        from intervener.demo.attach_recorded import main

        rc = main(
            [
                "--trajectory",
                str(traj),
                "--as",
                "llm",
            ]
        )
        assert rc == 0

        out = traj.with_name("session_log__llm.yaml")
        assert out.exists()
        loaded = yaml.safe_load(out.read_text())
        assert loaded["trial_id"] == "TEST-01:0"
        assert loaded["participant_id"] == "llm_intervener"
        assert len(loaded["entries"]) > 0

    def test_human_driver_with_script_produces_session_log_yaml(self, tmp_path: Path):
        traj = _write_looping_trajectory(tmp_path)
        script = tmp_path / "human.script"
        script.write_text("try /v2/auth\n\n\n")

        from intervener.demo.attach_recorded import main

        rc = main(
            [
                "--trajectory",
                str(traj),
                "--as",
                "human",
                "--script",
                str(script),
            ]
        )
        assert rc == 0

        out = traj.with_name("session_log__human.yaml")
        assert out.exists()
        loaded = yaml.safe_load(out.read_text())
        assert loaded["participant_id"] == "human_intervener"


def _write_looping_trajectory(tmp_path: Path) -> Path:
    """Write a synthetic trajectory.yaml with three consecutive auth_login calls."""
    traj = {
        "task_id": "TEST-01",
        "trial_index": 0,
        "status": "failed",
        "termination_reason": None,
        "messages": [
            {"role": "user", "content": "Log in and fetch me the org list."},
            {
                "role": "assistant",
                "content": "Let me try.",
                "tool_calls": [{"id": "c1", "name": "auth_login", "arguments": "{}"}],
            },
            {
                "role": "tool",
                "tool_call_id": "c1",
                "name": "auth_login",
                "content": "401 Unauthorized",
            },
            {
                "role": "assistant",
                "content": "Retrying.",
                "tool_calls": [{"id": "c2", "name": "auth_login", "arguments": "{}"}],
            },
            {
                "role": "tool",
                "tool_call_id": "c2",
                "name": "auth_login",
                "content": "401 Unauthorized",
            },
            {
                "role": "assistant",
                "content": "Once more.",
                "tool_calls": [{"id": "c3", "name": "auth_login", "arguments": "{}"}],
            },
        ],
    }
    path = tmp_path / "trajectory.yaml"
    with path.open("w") as f:
        yaml.safe_dump(traj, f)
    return path


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
