"""Actor Protocol — behavioural contract for per-turn message producers.

Formalises the ``reply(context, *, observation) -> GenerationResult`` shape
that :class:`~tolokaforge.core.llm.client.UserSimulator` has satisfied
implicitly since it was written (``UserSimulator.reply`` at
``tolokaforge/core/llm/client.py:2137-2155``).

The Protocol follows the ADR-0026 *Service Readiness Contract* Pattern-A
shape and mirrors the :class:`~tolokaforge.core.grading.judge.Judge`
seam:

* ``@runtime_checkable`` so seam wiring can ``isinstance``-check.
* Minimal surface — one method carrying only per-invocation evidence.
* No construction-time deps on the Protocol itself; concrete impls own
  their ``llm_client``, budgets, tool schemas, personas, and so on.

The value objects on the contract are re-exported here so callers of
:class:`Actor` do not need to reach into ``tolokaforge.core.llm.*``.
:class:`GenerationResult` is bound lazily via :pep:`562` ``__getattr__``
because ``tolokaforge.core.llm.client`` imports :class:`Actor` for its
:class:`~tolokaforge.core.llm.client.UserSimulator` base — an eager
top-level import would loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from tolokaforge.core.models import Message
from tolokaforge.core.run_display_events import LLMCallObservation

if TYPE_CHECKING:
    from tolokaforge.core.llm.client import GenerationResult

__all__ = [
    "Actor",
    "GenerationResult",
    "LLMCallObservation",
    "Message",
]


@runtime_checkable
class Actor(Protocol):
    """Produce one reply given the conversation so far.

    ``context`` is the trial's message history in speaking order.
    ``observation`` bundles the live event sink plus the call-site
    identity (``trial_id`` + LLM role) so an LLM-backed actor can fire
    the LLM-call trio without knowing how the sink is routed; a
    scripted actor may ignore it.

    The returned :class:`GenerationResult` carries the reply body, any
    tool calls, a :class:`~tolokaforge.core.llm.usage.Usage` counter
    (zeroed for scripted actors), and the wall-clock latency.
    """

    def reply(
        self,
        context: list[Message],
        *,
        observation: LLMCallObservation | None = None,
    ) -> GenerationResult: ...


def __getattr__(name: str) -> Any:
    if name == "GenerationResult":
        from tolokaforge.core.llm.client import GenerationResult

        globals()["GenerationResult"] = GenerationResult
        return GenerationResult
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
