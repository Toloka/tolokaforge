"""Unit lock for :class:`RecordingLLMClient`'s script-capture fidelity.

Keyless and tier ``unit``: this is what CI's coverage step
(``pytest -m "unit or canonical"``) actually enforces for the
``--live-parity`` writeback path, since the live-key-gated end-to-end
test never runs in CI (see ``tests/canonical/test_judge_kind_parity.py``
module docstring).
"""

import pytest

from tests.utils.recording_llm_client import RecordingLLMClient
from tests.utils.scripted_llm_client import ScriptedLLMClient

pytestmark = pytest.mark.unit


def test_recorded_script_round_trips_text_and_tool_call_turns() -> None:
    script = [
        "first turn, plain text",
        [("submit_report", {"criteria": ["a", "b"]})],
        [("critique", {"verdict_draft": "met"}), ("submit_report", {"criteria": ["c"]})],
        "closing text",
    ]
    recorder = RecordingLLMClient(ScriptedLLMClient(list(script)))

    for _ in script:
        recorder.generate(system="sys", messages=[], tools=[])

    assert recorder.recorded_script == script


def test_delegates_capabilities_and_error_classification() -> None:
    delegate = ScriptedLLMClient(["hello"])
    recorder = RecordingLLMClient(delegate)

    assert recorder.capabilities is delegate.capabilities
    assert recorder.sanitize_tools_for_execution([]) == delegate.sanitize_tools_for_execution([])

    exc = RuntimeError("boom")
    assert recorder.classify_loop_error(exc) == delegate.classify_loop_error(exc)
