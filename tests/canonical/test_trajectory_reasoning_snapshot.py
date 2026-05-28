"""Canonical snapshot of ``trajectory.yaml`` with structured ``Message.reasoning``.

Guards the on-disk YAML shape of ``messages[*].reasoning`` after:

* Stage 0's type migration (``str`` → :class:`StructuredReasoning`).
* Stage 3's real codec-driven extraction (signatures preserved end-to-end).

Two invariants captured:

1. **Shape snapshot** — a golden ``trajectory.yaml`` file on disk records the
   exact YAML structure we emit, including base64-ish signature bytes.
   Accidental changes to serialisation will fail this test loudly.
2. **Round-trip** — ``yaml.safe_load`` + :meth:`Trajectory.model_validate`
   restores the original :class:`StructuredReasoning` (block types, text,
   signatures, and ``encrypted_data`` all intact).

Update the golden with::

    uv run pytest tests/canonical/test_trajectory_reasoning_snapshot.py --update-canon
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from tolokaforge.core.llm.reasoning import ReasoningBlock, StructuredReasoning
from tolokaforge.core.models import (
    Message,
    MessageRole,
    Trajectory,
    TrialStatus,
)
from tolokaforge.core.output_writer import OutputWriter

pytestmark = pytest.mark.canonical

_SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "trajectory_reasoning"
_SNAPSHOT_FILE = _SNAPSHOT_DIR / "trajectory.yaml"

# Stable signature bytes — base64-ish, include '+' / '/' / '=' to verify YAML
# round-trips the full printable ASCII signature surface safely.
_SIGNATURE = "EqoBCkgIARABGAIiQAK+9/o3Zm5/+Qh=nD4="
_REDACTED_DATA = "EvwBCkgIARABGAIiQOpaqueBytes+/AB="


def _build_trajectory() -> Trajectory:
    """Build a minimal deterministic trajectory with structured reasoning."""
    reasoning = StructuredReasoning(
        blocks=(
            ReasoningBlock(
                type="thinking",
                text="Considering the best tool to call.",
                signature=_SIGNATURE,
            ),
            ReasoningBlock(
                type="redacted_thinking",
                text="",
                encrypted_data=_REDACTED_DATA,
            ),
        ),
        summary="Short summary of the model's private rationale.",
        budget_used=512,
    )

    ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    return Trajectory(
        task_id="canon-reasoning-001",
        trial_index=0,
        system_prompt="You are a helpful assistant.",
        start_ts=ts,
        end_ts=ts,
        status=TrialStatus.COMPLETED,
        messages=[
            Message(role=MessageRole.USER, content="Hello", ts=ts),
            Message(
                role=MessageRole.ASSISTANT,
                content="I'll help with that.",
                reasoning=reasoning,
                ts=ts,
            ),
        ],
    )


def _assert_yaml_snapshot(actual_path: Path, request: pytest.FixtureRequest) -> str:
    """Compare ``actual_path`` content with ``_SNAPSHOT_FILE``, or refresh it."""
    actual_text = actual_path.read_text()
    update = request.config.getoption("--update-canon")
    if update:
        _SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SNAPSHOT_FILE.write_text(actual_text)
        return actual_text
    if not _SNAPSHOT_FILE.exists():
        raise AssertionError(f"Missing golden snapshot: {_SNAPSHOT_FILE}. Run with --update-canon.")
    expected = _SNAPSHOT_FILE.read_text()
    assert actual_text == expected, (
        "trajectory.yaml shape drifted. Diff:\n"
        f"--- golden ({_SNAPSHOT_FILE})\n+++ actual\n"
        f"{actual_text[:2000]}"
    )
    return actual_text


def test_trajectory_yaml_preserves_structured_reasoning(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """Golden-snapshot the persisted ``trajectory.yaml`` including signatures."""
    trajectory = _build_trajectory()
    writer = OutputWriter(tmp_path)
    writer.write_trajectory(trajectory)

    written = tmp_path / "trajectory.yaml"
    assert written.exists()

    snapshot_text = _assert_yaml_snapshot(written, request)

    # Structural assertions — guard against silent schema drift independent
    # of the byte-exact snapshot comparison.
    assert "reasoning:" in snapshot_text
    assert "blocks:" in snapshot_text
    assert "type: thinking" in snapshot_text
    assert "type: redacted_thinking" in snapshot_text
    assert _SIGNATURE in snapshot_text, "signature bytes must round-trip through YAML verbatim"
    assert _REDACTED_DATA in snapshot_text


def test_trajectory_yaml_round_trips_through_model_validate(tmp_path: Path) -> None:
    """Reload persisted YAML and restore a byte-equal :class:`StructuredReasoning`."""
    original = _build_trajectory()
    writer = OutputWriter(tmp_path)
    writer.write_trajectory(original)

    raw = yaml.safe_load((tmp_path / "trajectory.yaml").read_text())
    # Drop writer-only fields (isoformat timestamps) that Trajectory parses
    # back with identical semantics but different repr.
    reloaded = Trajectory.model_validate(raw)

    # Assistant message is index 1.
    asst_original = original.messages[1]
    asst_reloaded = reloaded.messages[1]
    assert asst_reloaded.reasoning is not None
    assert asst_reloaded.reasoning == asst_original.reasoning

    # Signature-bearing block survives verbatim.
    first = asst_reloaded.reasoning.blocks[0]
    assert first.type == "thinking"
    assert first.signature == _SIGNATURE

    redacted = asst_reloaded.reasoning.blocks[1]
    assert redacted.type == "redacted_thinking"
    assert redacted.encrypted_data == _REDACTED_DATA
    assert redacted.text == ""
