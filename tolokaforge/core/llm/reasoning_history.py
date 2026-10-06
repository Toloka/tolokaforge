"""How much of the model's own prior reasoning goes back on the wire.

A route's :class:`~tolokaforge.core.llm.reasoning_codec.ReasoningCodec` decides
*what shape* replayed reasoning takes. This decides *how many* of the assistant
turns carrying it are replayed at all — the two are orthogonal, and only this
one costs input tokens on every later turn.

The policy is resolved once per request, over the whole message list, before any
provider serialisation, so a route's behaviour does not depend on which codec is
installed behind it.

Some routes do not get a choice. Moonshot documents preserved thinking as
mandatory on ``kimi-k2.7-code``; Anthropic requires thinking blocks to round-trip
intact alongside tool results. A codec whose route mandates replay says so with
:attr:`ReasoningCodec.forced_history`, and that overrides whatever a preset asked
for rather than letting a config produce 400s at run time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, get_args

if TYPE_CHECKING:
    from tolokaforge.core.llm.capabilities import ModelCapabilities
    from tolokaforge.core.models import Message

ReasoningHistory = Literal["none", "all", "last", "auto"]
"""``all`` replays every assistant turn's reasoning, ``none`` replays nothing,
``last`` replays only the most recent turn that carries any, and ``auto`` takes
the route's own default."""

REASONING_HISTORY_VALUES: tuple[str, ...] = get_args(ReasoningHistory)

DEFAULT_REASONING_HISTORY: ReasoningHistory = "auto"
"""What a preset that says nothing gets. ``auto`` resolves to ``all`` unless the
route's codec names something else, so an existing run is unchanged."""


def _carries_replayable_reasoning(message: Message) -> bool:
    """True when this message's reasoning would reach the wire.

    Mirrors the two conditions the replay splice already applies: reasoning a
    reader produced rather than the codec (``capture_only``) is never sent, and
    reasoning with no text encodes to nothing. A policy that ignored either
    would count a message as "the last one carrying reasoning", strip the turn
    below it, and send nothing at all.
    """
    from tolokaforge.core.models import MessageRole

    if message.role is not MessageRole.ASSISTANT:
        return False
    reasoning = message.reasoning
    return reasoning is not None and not reasoning.capture_only and not reasoning.is_empty()


def resolve_reasoning_history(
    messages: list[Message], capabilities: ModelCapabilities
) -> list[Message]:
    """*messages* with reasoning dropped from the turns the policy excludes.

    Returns the input list unchanged whenever the policy is a no-op — the route
    mandates ``all``, nothing carries replayable reasoning, or the setting is
    ``all``. A leg whose model emits no reasoning therefore allocates nothing.

    Only the copy bound for the wire is affected; the recorded messages the
    grader reads keep their reasoning either way.
    """
    setting = _effective_setting(capabilities)
    if setting == "all":
        return messages

    carriers = [i for i, m in enumerate(messages) if _carries_replayable_reasoning(m)]
    if not carriers:
        return messages
    keep = {carriers[-1]} if setting == "last" else set()
    drop = [i for i in carriers if i not in keep]
    if not drop:
        return messages

    resolved = list(messages)
    for index in drop:
        resolved[index] = resolved[index].model_copy(update={"reasoning": None})
    return resolved


def _effective_setting(capabilities: ModelCapabilities) -> ReasoningHistory:
    """The policy actually applied, after the route has had its say."""
    forced = getattr(capabilities.reasoning_codec, "forced_history", None)
    if forced is not None:
        return forced
    setting = capabilities.reasoning_history
    if setting == "auto":
        return getattr(capabilities.reasoning_codec, "auto_history", "all")
    return setting
