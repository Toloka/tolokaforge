"""Recording :class:`~tolokaforge.core.grading.judge_model_provider.JudgeModel`
wrapper for ``--live-parity`` cassette-refresh writeback.

Wraps a real delegate client (or, for tests, a
:class:`~tests.utils.scripted_llm_client.ScriptedLLMClient`) and records
every :meth:`generate` call's turn in exactly
:class:`ScriptedLLMClient`'s script shape — a plain ``str`` for a
text-only turn, a ``list[(tool_name, arguments_dict)]`` for a
tool-calling turn — so the recorded script can seed a fresh
:class:`ScriptedLLMClient` byte-for-byte, or be serialised back into a
corpus fixture's YAML cassette.

A turn carrying both text AND tool calls is recorded as its tool-call
list only (text discarded) — the same limitation
:class:`ScriptedLLMClient` itself has, since today's three shipped judge
kinds never emit mixed turns. See the plan's "Risks / open questions"
for issue #1572.
"""

from __future__ import annotations

from typing import Any

from tolokaforge.core.grading.judge_model_provider import JudgeModel
from tolokaforge.core.llm.capabilities import ModelCapabilities
from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.loop import TerminationDecision
from tolokaforge.core.models import Message
from tolokaforge.core.run_display_events import LLMCallObservation

__all__ = ["RecordingLLMClient"]


def _capture_step(result: GenerationResult) -> str | list[tuple[str, dict[str, Any]]]:
    """Re-encode one :class:`GenerationResult` into a script-step."""
    if not result.tool_calls:
        return result.text
    return [(tc.name, dict(tc.arguments)) for tc in result.tool_calls]


class RecordingLLMClient:
    """:class:`JudgeModel` that delegates every call to *delegate* and
    records the turn shape, exposed via :attr:`recorded_script`."""

    def __init__(self, delegate: JudgeModel) -> None:
        self._delegate = delegate
        self._recorded: list[str | list[tuple[str, dict[str, Any]]]] = []

    @property
    def capabilities(self) -> ModelCapabilities:
        return self._delegate.capabilities

    def generate(
        self,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]],
        tool_choice: str = "auto",
        observation: LLMCallObservation | None = None,
    ) -> GenerationResult:
        result = self._delegate.generate(
            system, messages, tools, tool_choice=tool_choice, observation=observation
        )
        self._recorded.append(_capture_step(result))
        return result

    def classify_loop_error(self, exc: Exception) -> TerminationDecision:
        return self._delegate.classify_loop_error(exc)

    def sanitize_tools_for_execution(
        self, tools: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        return self._delegate.sanitize_tools_for_execution(tools)

    @property
    def recorded_script(self) -> list[str | list[tuple[str, dict[str, Any]]]]:
        return list(self._recorded)
