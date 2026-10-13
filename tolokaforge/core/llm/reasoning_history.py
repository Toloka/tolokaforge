"""How much of the model's own prior reasoning goes back on the wire.

A route's :class:`~tolokaforge.core.llm.reasoning_codec.ReasoningCodec` decides
*what shape* replayed reasoning takes. This decides *how many* of the assistant
turns carrying it are replayed at all — the two are orthogonal, and only this
one costs input tokens on every later turn.

The policy is resolved once per request, over the whole message list, before any
provider serialisation, so a route's behaviour does not depend on which codec is
installed behind it.

A turn counts as carrying reasoning when the route's codec would put a payload
on the wire for it, not when a reader can read it: an opaque block the provider
replays is governed here exactly like readable thinking, and a block the codec
encodes to nothing is governed by neither.

Some routes do not get a choice: Anthropic requires thinking blocks to
round-trip intact alongside tool results. A codec whose route mandates replay
says so with :attr:`ReasoningCodec.forced_history`, and that overrides whatever a
preset asked for rather than letting a config produce 400s at run time.

A route that *generates* reasoning unconditionally is a separate matter and does
not constrain this policy. ``kimi-k2.7-code`` is such a route — thinking cannot
be switched off there — but omitting prior ``reasoning_content`` from the request
is documented as legal, so the saving is available even where the generation is
not optional.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, get_args

if TYPE_CHECKING:
    from tolokaforge.core.llm.capabilities import ModelCapabilities
    from tolokaforge.core.llm.reasoning_codec import ReasoningCodec
    from tolokaforge.core.models import Message

ReasoningHistory = Literal["none", "all", "last", "auto"]
"""``all`` replays every assistant turn's reasoning, ``none`` replays nothing,
``last`` replays only the most recent turn that carries any, and ``auto`` takes
the route's own default."""

REASONING_HISTORY_VALUES: tuple[str, ...] = get_args(ReasoningHistory)

DEFAULT_REASONING_HISTORY: ReasoningHistory = "auto"
"""What a preset that says nothing gets. ``auto`` resolves to ``all`` unless the
route's codec names something else, so an existing run is unchanged."""


def replayed_reasoning_payload(message: Message, codec: ReasoningCodec) -> dict[str, Any]:
    """The provider-shaped fields *message* contributes to a request, ``{}`` for none.

    The single definition of "this turn replays reasoning", read both by the
    policy below and by the serialiser that splices the payload onto the
    request, so a turn the policy counts is exactly a turn the wire carries.

    Reasoning a reader produced rather than the codec (``capture_only``) is
    never sent. Everything else is decided by the codec: readable text is not
    the test, because an opaque block carries a payload and a placeholder block
    carries none. A policy that counted a turn the codec encodes to nothing
    would call it "the last one carrying reasoning", strip the turn below it,
    and send nothing at all.
    """
    from tolokaforge.core.models import MessageRole

    if message.role is not MessageRole.ASSISTANT:
        return {}
    reasoning = message.reasoning
    if reasoning is None or reasoning.capture_only:
        return {}
    return codec.encode_for_replay(reasoning)


def resolve_reasoning_history(
    messages: list[Message], capabilities: ModelCapabilities
) -> list[Message]:
    """*messages* with reasoning dropped from the turns the policy excludes.

    Returns the input list unchanged whenever the policy is a no-op — the route
    mandates ``all``, the setting is ``all``, or no turn puts reasoning on the
    wire. A leg whose model emits no reasoning, and a route whose codec replays
    nothing, therefore allocate nothing.

    Only the copy bound for the wire is affected; the recorded messages the
    grader reads keep their reasoning either way.
    """
    setting = effective_reasoning_history(capabilities)
    if setting == "all":
        return messages

    codec = capabilities.reasoning_codec
    carriers = [i for i, m in enumerate(messages) if replayed_reasoning_payload(m, codec)]
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


def effective_reasoning_history(capabilities: ModelCapabilities) -> ReasoningHistory:
    """The policy actually applied, after the route has had its say.

    A route that mandates replay reports ``all`` here whatever the preset asked
    for, which is why the per-trial record reads this rather than
    :attr:`ModelCapabilities.reasoning_history`.
    """
    forced = getattr(capabilities.reasoning_codec, "forced_history", None)
    if forced is not None:
        return forced
    setting = capabilities.reasoning_history
    if setting == "auto":
        return getattr(capabilities.reasoning_codec, "auto_history", "all")
    return setting
