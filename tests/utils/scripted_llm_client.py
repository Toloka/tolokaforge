"""Deterministic scripted stand-in for :class:`tolokaforge.core.llm.client.LLMClient`.

The shared shape every canonical judge-loop test in tree drives the loop
under: a step-shape script list where each entry is either a plain
string (returned as assistant text with no tool calls) or a list of
``(tool_name, arguments_dict)`` tuples (returned as tool calls). The
client advances one entry per :meth:`generate` and returns the
``(exhausted)`` sentinel past the end so a runaway loop surfaces
distinctively rather than hanging on an empty queue.

The class satisfies the composed shape :class:`~tolokaforge.core.grading.judge_model_provider.JudgeModel`
demands — :meth:`generate` for the loop's LLM seam,
:meth:`classify_loop_error` for the loop's ``classify_error`` binding,
and :meth:`sanitize_tools_for_execution` for the loop's
``validation_schemas_by_tool`` build. A canonical suite pairs a fresh
:class:`ScriptedLLMClient` per leg with a scripted
``JudgeModelProvider`` that returns the client from ``.build(...)``.

**State is per instance.** ``_i`` advances on every :meth:`generate`,
so two legs of a parity test MUST NOT share one client — each side
constructs its own instance seeded from a fresh copy of the same
script list.
"""

from __future__ import annotations

from typing import Any

from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.llm.usage import Usage
from tolokaforge.core.models import ToolCall

__all__ = ["ScriptedLLMClient"]


class ScriptedLLMClient:
    """Scripted :class:`~tolokaforge.core.loop.LoopLLMClient` returning
    queued :class:`GenerationResult` values.

    Each script entry is either a plain ``str`` (assistant text, no tool
    calls) or a ``list`` of ``(tool_name, arguments_dict)`` tuples
    (emitted as one :class:`ToolCall` per tuple in the same order). A
    ``generate`` call past the end of the script returns
    ``GenerationResult(text="(exhausted)", tool_calls=[], usage=Usage())``
    so a runaway loop reads a distinctive sentinel instead of blocking
    on an empty queue.
    """

    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self._i = 0

    def generate(
        self,
        system,  # noqa: ARG002 — protocol arg, unused by the script
        messages,  # noqa: ARG002
        tools,  # noqa: ARG002
        tool_choice="auto",  # noqa: ARG002
        observation=None,  # noqa: ARG002
    ) -> GenerationResult:
        if self._i >= len(self._script):
            return GenerationResult(text="(exhausted)", tool_calls=[], usage=Usage())
        step = self._script[self._i]
        self._i += 1
        if isinstance(step, str):
            return GenerationResult(text=step, tool_calls=[], usage=Usage())
        tool_calls = [
            ToolCall(id=f"call_{self._i}_{j}", name=name, arguments=args)
            for j, (name, args) in enumerate(step)
        ]
        return GenerationResult(
            text="",
            tool_calls=tool_calls,
            usage=Usage(prompt_tokens=10, completion_tokens=5),
            cost_usd=0.001,
        )

    def classify_loop_error(self, exc: Exception):
        from tolokaforge.core.loop import classify_loop_error

        return classify_loop_error(exc, ())

    def sanitize_tools_for_execution(self, tools: list[dict]) -> dict[str, dict]:
        return {}
