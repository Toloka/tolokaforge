"""Context-window summarize + handoff seam.

A long-running trial's wire message history grows until it approaches the
provider's max input tokens. :class:`~tolokaforge.core.loop.ToolCallingLoop`
consults its :class:`SummarizePolicy` before every generation once the
capability is armed (``ModelCapabilities.max_context_tokens`` and
``context_watermark`` both set on the preset). When the previous
generation's ``prompt_tokens + context_watermark >= max_context_tokens``,
the loop asks the policy to produce a natural-language recap of the recorded
history and replaces the wire message list with ``[first_user_message,
Message(USER, content=recap)]``. The recorded ``Trajectory.messages`` list
keeps the full pre-summarize view so the grader's timeline builder is
unaffected.

The default implementation :class:`LLMSummarizer` reuses the trial's own
:class:`~tolokaforge.core.llm.client.LLMClient` — the same reasoning model
that produced the history summarizes it, which is the strongest available
signal at zero extra provider onboarding. The summarize ``generate`` call is
billed through the supplied :class:`~tolokaforge.core.loop.MetricsSink` so
its cost lands in the trial's :class:`~tolokaforge.core.models.Metrics`
alongside the agent's turns.

An empty recap raises :class:`SummarizerFailedError`. The loop maps that,
and every uncaught
:class:`~litellm.exceptions.ContextWindowExceededError` from the summarize
call itself, to
:attr:`~tolokaforge.core.models.TerminationReason.CONTEXT_WINDOW_EXCEEDED`.
Summarize is one attempt: the loop does not summarize the summary.
"""

from __future__ import annotations

from typing import Any, Protocol

from tolokaforge.core.llm.client import GenerationResult
from tolokaforge.core.models import Message, MessageRole

__all__ = [
    "LLMSummarizer",
    "NoSummarize",
    "SummarizePolicy",
    "SummarizerFailedError",
]


_SUMMARIZE_SYSTEM_PROMPT = (
    "You are a summarizer. Compress the conversation below into a concise "
    "natural-language recap so a fresh agent instance can continue from where "
    "the previous one left off. Preserve: the original task, key facts "
    "discovered, decisions made, current in-progress state, and open questions. "
    "Do NOT include chit-chat or reasoning. Output the recap as prose, not JSON."
)

_TOOL_RESULT_HEAD_CHARS = 2048


class SummarizerFailedError(RuntimeError):
    """The summarize policy could not produce a recap.

    Raised on an empty ``result.text`` from the summarize generation.
    The loop maps this to :attr:`TerminationReason.CONTEXT_WINDOW_EXCEEDED`.
    """


class _SummarizeClient(Protocol):
    """The generate seam :class:`LLMSummarizer` needs.

    Structurally satisfied by :class:`~tolokaforge.core.llm.client.LLMClient`.
    Kept narrow so tests can script the summarize call without stubbing the
    full LLM client surface.
    """

    def generate(
        self,
        system: str | None = None,
        messages: list[Message] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> GenerationResult: ...


class _SummarizeMetricsSink(Protocol):
    """The subset of :class:`~tolokaforge.core.loop.MetricsSink` the summarizer bills."""

    def record_generation(self, result: GenerationResult) -> None: ...


class SummarizePolicy(Protocol):
    """Produces a natural-language recap of a recorded message history.

    The loop passes the effective system prompt and the recorded messages at
    the summarize point (the same view the grader sees). The returned string
    lands on the wire as the second entry after ``messages[0]``.
    """

    def summarize(self, system_prompt: str, messages: list[Message]) -> str: ...


class NoSummarize:
    """No-op :class:`SummarizePolicy`.

    Returns an empty string, which the loop treats as summarize-failed. Used
    by presets that declare the capability but hold no summarizer, and as the
    engine-side default whenever the runner opts out.
    """

    def summarize(self, system_prompt: str, messages: list[Message]) -> str:
        return ""


class LLMSummarizer:
    """Summarizes a recorded history via the trial's own LLM client.

    The summarize ``generate`` is the largest single call an opted-in trial
    can make (it ships the full pre-summarize history), so ``metrics`` is
    required rather than optional: silently omitting the recording would
    under-report cost. ``max_output_tokens`` caps the recap length so the
    handoff message stays bounded even if the model would otherwise ramble.
    """

    def __init__(
        self,
        client: _SummarizeClient,
        metrics: _SummarizeMetricsSink,
        *,
        max_output_tokens: int = 4096,
    ) -> None:
        self._client = client
        self._metrics = metrics
        self._max_output_tokens = max_output_tokens

    def summarize(self, system_prompt: str, messages: list[Message]) -> str:
        rendered = _render_history(system_prompt, messages)
        recap_prompt = (
            "Produce the recap the summarize-system-prompt describes. The "
            "conversation to summarize follows.\n\n"
            f"{rendered}"
        )
        result = self._client.generate(
            system=_SUMMARIZE_SYSTEM_PROMPT,
            messages=[Message(role=MessageRole.USER, content=recap_prompt)],
            tools=[],
            tool_choice="none",
        )
        self._metrics.record_generation(result)
        text = result.text.strip()
        if not text:
            raise SummarizerFailedError(
                "summarize policy returned an empty recap; no handoff message to splice"
            )
        return text


def _render_history(system_prompt: str, messages: list[Message]) -> str:
    """Format a recorded history into role-prefixed lines for the summarizer.

    Tool calls flatten to ``tool_call(name, arguments_json)``, tool results
    to ``tool_result(name, text[:N])`` (``N = _TOOL_RESULT_HEAD_CHARS``).
    Reasoning blocks are elided — the summarizer sees behaviour, not
    internal thought.
    """
    import json

    lines: list[str] = [f"[system] {system_prompt}"]
    for msg in messages:
        role = msg.role.value
        if msg.role is MessageRole.ASSISTANT and msg.tool_calls:
            text = msg.content or ""
            lines.append(f"[{role}] {text}")
            for call in msg.tool_calls:
                args_json = json.dumps(call.arguments or {}, sort_keys=True, default=str)
                lines.append(f"[{role}] tool_call({call.name}, {args_json})")
        elif msg.role is MessageRole.TOOL:
            head = (msg.content or "")[:_TOOL_RESULT_HEAD_CHARS]
            name = msg.tool_call_id or "tool"
            lines.append(f"[{role}] tool_result({name}, {head})")
        else:
            lines.append(f"[{role}] {msg.content or ''}")
    return "\n".join(lines)
